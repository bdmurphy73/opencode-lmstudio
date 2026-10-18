#!/usr/bin/env python3
"""opencode-lmstudio: add LM Studio models to your OpenCode config.

Single file. Python 3 standard library only. No pip installs, no jq, no setup.

What it does
------------
1. Detects this machine's LAN IP address.
2. Finds LM Studio servers (localhost and the local IP automatically; any others
   you type in). Every server gets a name.
3. Reads all models the server advertises (rich metadata from LM Studio's
   /api/v0/models, falling back to /v1/models).
4. Merges a provider + models into the global OpenCode config
   (~/.config/opencode/opencode.json), skipping duplicates.
5. With --prune, also removes configured models that the server no longer
   advertises (refuses to prune if the server reports 0 models).
6. Backs up the config and writes it atomically.

Usage
-----
    python3 opencode-lmstudio.py
    python3 opencode-lmstudio.py --server 192.168.123.16=BigBox --yes
    python3 opencode-lmstudio.py --server 10.0.0.5            # prompts for a name
    python3 opencode-lmstudio.py --server 10.0.0.5 --port 1234 --dry-run
    python3 opencode-lmstudio.py --server 10.0.0.5 --list     # just show models
    python3 opencode-lmstudio.py --server 10.0.0.5 --prune    # drop stale models
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib import error, request

DEFAULT_PORT = 1234
DEFAULT_CONFIG = Path("~/.config/opencode/opencode.json").expanduser()
SCHEMA = "https://opencode.ai/config.json"
PACKAGE = "@opencode/ai/providers/openai-compatible"


# --------------------------------------------------------------------------- net

def local_ip() -> str:
    """This machine's LAN IP. Sends no packets (UDP connect only sets the route)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def _get_json(url: str, timeout: float):
    req = request.Request(url, headers={"Accept": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_server(host: str, port: int, timeout: float = 6.0):
    """Return (baseURL, [model_id, ...], {model_id: metadata}).

    Raises OSError/error.URLError if there is no OpenAI-compatible server.
    """
    base = f"http://{host}:{port}/v1"
    rich: dict = {}
    try:
        data = _get_json(f"http://{host}:{port}/api/v0/models", timeout)
        for m in data.get("data", []):
            if m.get("id"):
                rich[m["id"]] = m
    except Exception:
        rich = {}  # not LM Studio, or the endpoint is disabled; /v1 still works

    data = _get_json(f"{base}/models", timeout)  # this one must succeed
    ids = [m["id"] for m in data.get("data", []) if m.get("id")]
    return base, ids, rich


# --------------------------------------------------------------------- formatting

def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower())
    return s.strip("-.") or "server"


def pretty_name(model_id: str) -> str:
    leaf = model_id.split("/")[-1]
    # Split on dashes/underscores/space but keep dots so version numbers survive
    # ("qwen3.5-9b" -> "Qwen3.5 9B", not "Qwen3 5 9B").
    tokens = [t for t in re.split(r"[-_\s]+", leaf) if t]
    out = []
    for t in tokens:
        if t.isdigit():
            out.append(t)
        elif re.fullmatch(r"\d+(?:\.\d+)?[bB]", t) or t.upper() in {
                "GPT", "R1", "LLM", "VLM", "MTP", "A1B", "MOE", "GGUF"}:
            out.append(t.upper())          # "8b" -> "8B", "2.6b" -> "2.6B"
        else:
            out.append(t[:1].upper() + t[1:])
    return " ".join(out) or model_id


def model_key(model_id: str, used: set) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", model_id.split("/")[-1]).strip("-.")
    key = key or "model"
    candidate, n = key, 2
    while candidate.lower() in used:
        candidate, n = f"{key}-{n}", n + 1
    used.add(candidate.lower())
    return candidate


def build_model(model_id: str, meta: dict) -> dict:
    mtype = meta.get("type", "")
    is_embed = mtype == "embeddings" or "embed" in model_id.lower()
    entry: dict = {"modelID": model_id, "name": pretty_name(model_id)}
    if meta.get("arch"):
        entry["family"] = meta["arch"]
    if is_embed:
        entry["disabled"] = True
    tools = "tool_use" in (meta.get("capabilities") or [])
    inputs = ["text", "image"] if mtype == "vlm" else ["text"]
    entry["capabilities"] = {"tools": tools, "input": inputs, "output": ["text"]}
    ctx = meta.get("max_context_length")
    if isinstance(ctx, int) and ctx > 0:
        entry["limit"] = {"context": ctx}
    return entry


# ------------------------------------------------------------------------ config

def load_config(path: Path) -> dict:
    if not path.exists():
        return {"$schema": SCHEMA, "providers": {}}
    text = path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not plain JSON ({e}).\n"
                 f"       This tool edits JSON only; convert JSONC comments first.")
    cfg.setdefault("$schema", SCHEMA)
    cfg.setdefault("providers", {})
    return cfg


def write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  backup: {backup}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def provider_for_baseurl(cfg: dict, base: str) -> str | None:
    for key, prov in cfg.get("providers", {}).items():
        if isinstance(prov, dict):
            if prov.get("settings", {}).get("baseURL", "").rstrip("/") == base.rstrip("/"):
                return key
    return None


def merge_provider(cfg: dict, name: str, base: str, ids: list, rich: dict,
                   prune: bool = False) -> dict:
    """Merge one server into cfg. Returns a summary dict."""
    providers = cfg["providers"]
    reused = provider_for_baseurl(cfg, base)

    if reused:
        key = reused
        providers[key]["name"] = name  # honour the name the user gave
    else:
        key = slugify(name)
        n = 2
        while key in providers:
            key, n = f"{slugify(name)}-{n}", n + 1
        providers[key] = {"name": name, "package": PACKAGE,
                          "settings": {"baseURL": base}, "models": {}}

    prov = providers[key]
    prov.setdefault("package", PACKAGE)
    prov.setdefault("settings", {})["baseURL"] = base
    models = prov.setdefault("models", {})

    existing_by_id = {v.get("modelID"): k
                      for k, v in models.items() if isinstance(v, dict)}
    used_keys = {k.lower() for k in models}

    added, skipped, removed = [], [], []
    seen = set()
    for mid in ids:
        if mid in seen:            # duplicate within the same server
            continue
        seen.add(mid)
        if mid in existing_by_id:  # already in the config
            skipped.append(mid)
            continue
        models[model_key(mid, used_keys)] = build_model(mid, rich.get(mid, {}))
        added.append(mid)

    if prune:
        if not ids:
            # A reachable server that reports nothing is almost certainly a
            # transient/partial response; never wipe the config on that.
            print("  ! server reported 0 models; refusing to prune")
        else:
            live = set(ids)
            for k in [k for k, v in models.items()
                      if isinstance(v, dict) and v.get("modelID") not in live]:
                removed.append(models[k].get("modelID", k))
                del models[k]

    return {"key": key, "name": name, "base": base, "reused": bool(reused),
            "added": added, "skipped": skipped, "removed": removed,
            "total": len(ids)}


# --------------------------------------------------------------------------- cli

def prompt(msg: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def confirm(msg: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    ans = prompt(f"{msg} [{d}]").lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def parse_server_arg(spec: str):
    if "=" in spec:
        host, name = spec.split("=", 1)
        return host.strip(), name.strip()
    return spec.strip(), None


def collect_servers(args) -> list:
    """Return a list of {'host','name'} for the servers to add."""
    servers: list = []
    seen_hosts: set = set()

    if args.server:
        for spec in args.server:
            host, name = parse_server_arg(spec)
            seen_hosts.add(host)
            servers.append({"host": host, "name": name})
        return servers

    # Interactive: this machine's IP + localhost, then ask for any others.
    ip = local_ip()
    print(f"Local machine IP: {ip}\n")
    for host, default_name in (("127.0.0.1", "Local LM Studio"),
                               (ip, f"LM Studio {ip}")):
        if host in seen_hosts:
            continue
        try:
            base, ids, _ = fetch_server(host, args.port, args.timeout)
        except Exception:
            continue
        seen_hosts.add(host)
        if confirm(f"Found LM Studio at {host}:{args.port} "
                   f"({len(ids)} models) — add it?"):
            servers.append({"host": host, "name": prompt("  Name", default_name)})

    while True:
        host = prompt("\nLM Studio server IP/host (blank to finish)")
        if not host:
            break
        if host in seen_hosts:
            print("  already added, skipping")
            continue
        seen_hosts.add(host)
        servers.append({"host": host,
                        "name": prompt("  Name", f"LM Studio {host}")})
    return servers


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="opencode-lmstudio",
        description="Add LM Studio models to the OpenCode config (stdlib only).")
    p.add_argument("--server", action="append", metavar="HOST[=NAME]",
                   help="server IP/host, optionally =Name (repeatable)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--timeout", type=float, default=6.0)
    p.add_argument("--list", action="store_true",
                   help="only list models; do not write config")
    p.add_argument("--prune", action="store_true",
                   help="remove configured models the server no longer advertises")
    p.add_argument("--dry-run", action="store_true",
                   help="show changes without writing")
    p.add_argument("--yes", action="store_true",
                   help="assume yes / accept default names")
    args = p.parse_args(argv)

    if args.list and not args.server:
        args.server = ["127.0.0.1"]

    servers = collect_servers(args)
    if not servers:
        print("No servers selected. Nothing to do.")
        return 0

    # Resolve names (prompt if missing and interactive).
    for s in servers:
        if not s["name"]:
            s["name"] = ("LM Studio " + s["host"]) if args.yes else \
                prompt(f"Name for {s['host']}", f"LM Studio {s['host']}")

    cfg = load_config(args.config)
    original = json.loads(json.dumps(cfg))  # pristine copy, to detect a no-op run
    summaries = []

    for s in servers:
        host, name = s["host"], s["name"]
        print(f"\n{name}  ({host}:{args.port})")
        try:
            base, ids, rich = fetch_server(host, args.port, args.timeout)
        except (error.URLError, OSError, ValueError) as e:
            print(f"  ! could not reach an OpenAI-compatible server: {e}")
            continue
        print(f"  baseURL : {base}")
        print(f"  models  : {len(ids)}")
        for mid in ids:
            tag = ""
            meta = rich.get(mid, {})
            if meta.get("type") == "embeddings" or "embed" in mid.lower():
                tag = "  (embeddings, will be disabled)"
            elif meta.get("type") == "vlm":
                tag = "  (vision)"
            print(f"    - {mid}{tag}")
        if args.list:
            continue
        summaries.append(merge_provider(cfg, name, base, ids, rich,
                                        prune=args.prune))

    if args.list:
        return 0
    if not summaries:
        print("\nNo servers could be reached; config unchanged.")
        return 1

    print("\nChanges:")
    for r in summaries:
        action = "merged into" if r["reused"] else "new provider"
        parts = [f"+{len(r['added'])} added", f"{len(r['skipped'])} kept"]
        if r["removed"]:
            parts.append(f"-{len(r['removed'])} pruned")
        print(f"  {r['key']} ({r['name']}): {action} {r['base']}  "
              f"[{', '.join(parts)}]")
        for mid in r["added"]:
            print(f"      + {mid}")
        for mid in r["skipped"]:
            print(f"      = {mid} (already present)")
        for mid in r["removed"]:
            print(f"      - {mid} (no longer on server)")

    if args.dry_run:
        print(f"\n(dry-run) config not written: {args.config}")
        return 0

    if cfg == original:
        print("\nAlready up to date; config unchanged.")
        return 0

    if not args.yes and not confirm(f"\nWrite {args.config}?", True):
        print("Aborted; config unchanged.")
        return 1

    write_config(args.config, cfg)
    print(f"\nUpdated {args.config}")
    print("Restart OpenCode (or reconnect) and pick a model under /models.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; config unchanged.")
        raise SystemExit(130)
