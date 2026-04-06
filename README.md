# ssh-mcp

An MCP server that gives AI agents SSH access to remote machines through your local OpenSSH client. It wraps `ssh`, `scp`, and `rsync` so agents can run remote commands, transfer files, maintain persistent shell sessions, and set up port forwards — all using your existing SSH config, keys, and credentials.

## Why ssh-mcp?

- **Uses your local SSH** — host aliases, `~/.ssh/config`, `ProxyJump`, agent forwarding, and existing credentials all work naturally. No SSH libraries or key management.
- **Persistent sessions** — agents can keep a shell open across multiple tool calls, just like a human would. Sessions survive context window resets when you give them a `session_name`.
- **Observable** — every session records a transcript and optionally launches a detached tmux viewer so you can watch what the agent is doing in real time.
- **Permission-gatable** — port forwarding is a separate tool from command execution, so MCP clients can allow SSH access without allowing port forwards.
- **Pure Python** — no third-party runtime dependencies. Runs anywhere Python 3.10+ and OpenSSH are available.

## Requirements

- Python 3.10+
- `ssh` and `scp` on PATH (or set `SSH_MCP_SSH_BIN` / `SSH_MCP_SCP_BIN`)
- `rsync` on PATH (or set `SSH_MCP_RSYNC_BIN`) — only needed for `ssh_sync`
- `tmux` — optional, for live session observation

## Installation

### With uv (recommended)

```bash
uvx --from slepp-ssh-mcp ssh-mcp
```

Or install persistently:

```bash
uv tool install slepp-ssh-mcp
```

### With pip

```bash
pip install slepp-ssh-mcp
```

## Setup

### Claude Code

```bash
claude mcp add --transport stdio --scope user ssh-mcp -- uvx --from slepp-ssh-mcp ssh-mcp
```

Or commit a `.mcp.json` to share with your team:

```json
{
  "mcpServers": {
    "ssh-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "slepp-ssh-mcp", "ssh-mcp"]
    }
  }
}
```

### Codex CLI

```bash
codex mcp add ssh-mcp -- uvx --from slepp-ssh-mcp ssh-mcp
```

### GitHub Copilot

Add to `~/.copilot/mcp-config.json` (or `.vscode/mcp.json` per-project):

```json
{
  "mcpServers": {
    "ssh-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "slepp-ssh-mcp", "ssh-mcp"]
    }
  }
}
```

### Generic MCP client

Any stdio MCP client works. Point it at `uvx --from slepp-ssh-mcp ssh-mcp` or at a virtualenv's `ssh-mcp` entrypoint.

## How it works

ssh-mcp runs as a stdio process that your MCP client spawns. It receives JSON-RPC tool calls and translates them into local `ssh`/`scp`/`rsync` commands. Because it uses your local SSH binary, everything in your `~/.ssh/config` works — jump hosts, custom ports, key selection, `SSH_AUTH_SOCK`, proxy commands.

There are three modes of operation:

### One-off commands (`ssh_exec`)

Run a command, get stdout/stderr/exit code back. Works like `ssh host 'command'`.

```json
{
  "target": "prod-web01",
  "command": "systemctl status nginx",
  "timeout": 10
}
```

Use `cwd` to set the working directory, `env` to export variables, and `tty: true` for commands that need a terminal (like `sudo` with a password prompt). Note that `tty` merges stdout and stderr.

### Interactive sessions (`ssh_ensure_session` + `ssh_write_session` + `ssh_read_session`)

For multi-step work, open a persistent shell. The agent writes commands and reads output just like typing in a terminal.

**Start or reuse a session:**

```json
{
  "target": "prod-web01",
  "session_name": "deploy-api"
}
```

Always use a descriptive `session_name`. It serves three purposes:
1. The agent can find the same session across multiple tool calls
2. The tmux observer window gets a human-readable name (e.g., `ssh-mcp-prod-web01-deploy-api`)
3. A different agent or conversation can recover the session by name

**Write a command:**

```json
{
  "session_id": "a1b2c3d4e5f6",
  "input": "cd /app && git pull\n",
  "wait_seconds": 5
}
```

Always include `\n` to press Enter. Use `\u0003` for Ctrl-C, `\u0004` for Ctrl-D. Set `wait_seconds` high enough for the command to produce output (default: 1 second).

**Read more output:**

```json
{
  "session_id": "a1b2c3d4e5f6",
  "wait_seconds": 10
}
```

Check `pending_output_chars` in the response — if non-zero, call again to drain the buffer.

**Session lifecycle:**
- `ssh_ensure_session` is idempotent — call it at the start of each step
- Sessions auto-detect dead connections via SSH keepalive (~90 seconds)
- Response includes `uptime_seconds`, `idle_seconds`, and `exit_reason` for health monitoring
- Use `auto_close: true` for one-shot commands that should clean up when done
- Exited sessions are pruned from memory after 1 hour (5 minutes for `auto_close`)
- `cwd`, `env`, and `shell` only apply when creating a new session — they are ignored when reusing an existing one

### File transfer (`ssh_scp`, `ssh_sync`)

Copy files between local and remote machines.

**scp** — simple file/directory copy:

```json
{
  "target": "prod-web01",
  "direction": "upload",
  "sources": ["/local/path/app.tar.gz"],
  "destination": "/tmp/"
}
```

For `upload`, `sources` are local paths and `destination` is remote. For `download`, it's reversed. The `target` parameter specifies the host — don't include the host in `sources` or `destination`.

**rsync** — incremental sync with `--delete` and `--exclude`:

```json
{
  "target": "prod-web01",
  "direction": "upload",
  "source": "./dist/",
  "destination": "/var/www/app/",
  "delete": true,
  "exclude": ["*.log", ".git"]
}
```

### Port forwarding (`ssh_forward`)

Create local or remote port forwards. This is a separate tool from `ssh_exec` so MCP clients can grant SSH access without granting port forwarding.

**Local forward** — make a remote service reachable locally:

```json
{
  "target": "prod-web01",
  "direction": "local",
  "local_port": 15432,
  "remote_host": "prod-db.internal",
  "remote_port": 5432
}
```

This binds `localhost:15432` and tunnels it to `prod-db.internal:5432` through `prod-web01`.

**Remote forward** — expose a local service on the remote host:

```json
{
  "target": "prod-web01",
  "direction": "remote",
  "local_port": 3000,
  "remote_host": "localhost",
  "remote_port": 8080
}
```

Forwards bind to `127.0.0.1` by default. Set `bind_address: "0.0.0.0"` to expose on all interfaces (use with caution).

Use `ssh_list_forwards` to see active forwards and `ssh_stop_forward` to tear them down.

## Watching sessions

Every interactive session records a transcript to `~/.local/state/ssh-mcp/<session_id>/transcript.log`.

By default, sessions also launch a detached tmux window so you can watch in real time. The tmux session name includes the target and session name for easy identification:

```bash
# List ssh-mcp tmux sessions
tmux ls | grep ssh-mcp

# Attach to watch
tmux attach -t ssh-mcp-prod-web01-deploy-api
```

If you prefer not to use tmux, set `observer_mode: "transcript"` and tail the transcript file directly — the response includes an `observer_command` you can copy-paste.

The tmux observer is tied to the session lifecycle: stopping a session closes its tmux window. The MCP server also cleans up tmux on shutdown.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SSH_MCP_SSH_BIN` | `ssh` | Path to the SSH client |
| `SSH_MCP_SCP_BIN` | `scp` | Path to the SCP client |
| `SSH_MCP_RSYNC_BIN` | `rsync` | Path to rsync |
| `SSH_MCP_TMUX_BIN` | `tmux` | Path to tmux |
| `SSH_MCP_STATE_DIR` | `~/.local/state/ssh-mcp` | Where transcripts are stored |

## Security

ssh-mcp is designed for single-developer use on your own machine. It runs SSH commands as your user with your credentials.

**What's protected:**
- Port forwarding flags (`-L`, `-R`, `-D`, `-W`) and dangerous SSH options (`ProxyCommand`, `LocalCommand`, `LocalForward`, `RemoteForward`, `DynamicForward`) are blocked in `extra_ssh_args`. The only way to create forwards is through the explicit `ssh_forward` tool, which MCP clients can permission-gate.
- Transcript files are created with mode `0600` and the state directory with `0700`.
- All command arguments use `shlex.quote()` to prevent shell injection. Subprocess calls use list arguments, never `shell=True`.
- Environment variable names are validated against `^[A-Za-z_][A-Za-z0-9_]*$`.

**What's not protected:**
- An agent with `ssh_exec` access can run arbitrary commands on any host your SSH config can reach. The security boundary is SSH itself (keys, known_hosts).
- Transcript files persist on disk after sessions end and may contain secrets (passwords typed at sudo prompts, API keys in output). Clean up `SSH_MCP_STATE_DIR` when you no longer need them.
- The `shell` parameter lets agents choose any remote executable. This is by design — the tool is for remote execution.

## Known limitations

- **POSIX-only remotes** — `cwd`, `env`, and `shell` wrapping assumes a POSIX shell on the remote side. Windows SSH targets need commands written for their shell.
- **PTY output** — interactive sessions use a PTY, so output includes terminal formatting (ANSI escape codes, command echo, line wrapping). This is intentional — it matches what a human would see.
- **No multiplexing** — each `ssh_exec` call opens a new SSH connection. If your agent runs many rapid commands to the same host, consider using a session instead, or configure `ControlMaster` in your `~/.ssh/config`.
- **Transcript growth** — transcripts grow without bound for long-running sessions. The response includes `transcript_size_bytes` so you can monitor this. Restart the session if it gets too large.
- **Forward connections are standalone** — each `ssh_forward` opens its own SSH connection. Forwards are not tied to sessions.

## Tools reference

| Tool | Description |
|------|-------------|
| `ssh_exec` | Run a one-off remote command |
| `ssh_scp` | Copy files via scp |
| `ssh_sync` | Incremental sync via rsync |
| `ssh_start_session` | Start a new interactive session |
| `ssh_ensure_session` | Reuse or start an interactive session (recommended) |
| `ssh_read_session` | Read output from a session |
| `ssh_write_session` | Write input to a session |
| `ssh_stop_session` | Stop a session |
| `ssh_list_sessions` | List tracked sessions |
| `ssh_forward` | Start a port forward |
| `ssh_list_forwards` | List tracked forwards |
| `ssh_stop_forward` | Stop a port forward |

All session and forward tools accept standard SSH connection parameters: `port`, `identity_file`, `known_hosts_file`, `strict_host_key_checking`, and `extra_ssh_args`.

## Development

```bash
python3 -m pip install build
python3 -m unittest discover -s tests -v
python3 -m compileall src
python3 -m build
```

## License

MIT. See `LICENSE`.
