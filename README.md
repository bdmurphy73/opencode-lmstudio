# opencode-lmstudio

A single-file, dependency-free helper that discovers LM Studio (and other
OpenAI-compatible) servers and writes their models into your OpenCode config.

- **Python 3.8+, standard library only** — no `pip install`, no `jq`, no setup.
- Detects this machine's LAN IP, probes `localhost` and the local IP, then
  prompts for any additional servers (each gets a name).
- Reads rich metadata from LM Studio's `/api/v0/models`, falling back to
  `/v1/models` for plain OpenAI-compatible servers.
- Merges providers/models into `~/.config/opencode/opencode.json`, skipping
  duplicates. `--prune` removes models the server no longer advertises.
- Backs up the config and writes atomically.

## Usage

```bash
# interactive: detects local IP, offers localhost + the local LM Studio,
# then prompts for other servers and names
python3 opencode-lmstudio.py

# non-interactive, name a server explicitly
python3 opencode-lmstudio.py --server 192.168.0.16=BigBox --yes

# preview changes without writing / just list models
python3 opencode-lmstudio.py --server 192.168.0.16=BigBox --dry-run
python3 opencode-lmstudio.py --server 192.168.0.16=BigBox --list

# drop models the server no longer advertises
python3 opencode-lmstudio.py --server 192.168.0.16=BigBox --prune --yes
```

## Options

| Flag | Description |
|---|---|
| `--server HOST[=NAME]` | Server IP/host, optionally `=Name` (repeatable) |
| `--port PORT` | Server port (default `1234`) |
| `--config PATH` | Config file (default `~/.config/opencode/opencode.json`) |
| `--timeout SECS` | Per-request timeout (default `6.0`) |
| `--list` | Only list models; do not write the config |
| `--prune` | Remove configured models the server no longer advertises |
| `--dry-run` | Show changes without writing |
| `--yes` | Assume yes / accept default names |

## How a server becomes a provider

Each server is written as an OpenCode provider using
`@opencode/ai/providers/openai-compatible` with `settings.baseURL` set to
`http://HOST:PORT/v1`. Model capability flags (`tools`, `input`, `output`) and
`limit.context` come from the server's metadata; vision models advertise image
input, and embedding models are added but marked `"disabled": true`.

Duplicate handling is by `baseURL` (an existing provider for the same server is
reused even if named differently) and by `modelID` (models already present are
kept, not re-added).

## Safety

- Backs up the previous config before every write (`opencode.json.bak-<time>`).
- Refuses to prune when a reachable server reports **0** models.
- Makes no changes (and writes nothing) when a run would add and remove nothing.

## Sharing on a LAN

```bash
# on the machine that has the script
python3 -m http.server 8000
# on another machine
curl -O http://<this-host-ip>:8000/opencode-lmstudio.py
```
