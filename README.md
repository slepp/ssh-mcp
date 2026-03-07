# ssh-mcp

`ssh-mcp` is a stdio MCP server that wraps the local OpenSSH client and exposes remote execution plus interactive SSH sessions that feel close to a Bash tool.

## Features

- Pure-stdlib Python runtime; no third-party runtime dependencies.
- Uses the local `ssh` binary, so host aliases, SSH config, `ProxyJump`, SSH agent forwarding, and existing credentials work naturally.
- Newline-delimited JSON-RPC 2.0 over stdio for MCP clients.
- One-off remote command execution with optional remote `cwd`, `env`, shell selection, timeout, TTY, and SSH connection flags.
- Persistent interactive SSH sessions backed by a local PTY.
- Stable session reuse with `ssh_ensure_session` so long-lived agent workflows can keep using the same shell instead of accidentally starting duplicates.
- Passive session observation via per-session transcripts and optional detached tmux viewers.
- Structured tool responses that are easy for an LLM to consume.

## Requirements

- Python 3.10+
- OpenSSH client available as `ssh` and `scp` on `PATH` (or via `SSH_MCP_SSH_BIN` / `SSH_MCP_SCP_BIN`)
- `rsync` available on `PATH` (or via `SSH_MCP_RSYNC_BIN`) when you want incremental sync support
- `tmux` is optional; it is only needed when you want the server to auto-launch a detached live observer.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Running the server

After installation, run either of these:

```bash
python -m ssh_mcp
```

or

```bash
ssh-mcp
```

The server speaks newline-delimited JSON-RPC on stdin/stdout, which matches current MCP stdio transport guidance.

## MCP stdio config example

Example configuration for an MCP client that launches the server from a virtualenv:

```json
{
  "mcpServers": {
    "ssh": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "ssh_mcp"]
    }
  }
}
```

If you want to point at a non-default SSH executable for testing, you can set:

```json
{
  "mcpServers": {
    "ssh": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "ssh_mcp"],
      "env": {
        "SSH_MCP_SSH_BIN": "/absolute/path/to/ssh"
      }
    }
  }
}
```

Additional optional environment variables:

- `SSH_MCP_TMUX_BIN`: override the `tmux` executable used for observer panes
- `SSH_MCP_SCP_BIN`: override the `scp` executable used by `ssh_scp`
- `SSH_MCP_RSYNC_BIN`: override the `rsync` executable used by `ssh_sync`
- `SSH_MCP_STATE_DIR`: override where session transcripts are stored. By default, the server uses `$XDG_STATE_HOME/ssh-mcp` when available, or `~/.local/state/ssh-mcp`.

## Tools

### `ssh_exec`

Run a one-off remote command.

Arguments:

- `target` *(required)*: SSH destination such as `prod`, `user@host`, or a host alias from `~/.ssh/config`
- `command` *(required)*: remote command string
- `cwd`: remote directory to `cd` into before running the command
- `env`: object of remote environment variables to export first
- `shell`: remote shell executable used to wrap the command when needed
- `timeout`: local timeout in seconds
- `tty`: request a TTY with `-tt`
- `port`
- `identity_file`
- `known_hosts_file`
- `strict_host_key_checking`
- `extra_ssh_args`

### `ssh_scp`

Copy files or directories between the local machine and one remote target via `scp`.

Arguments:

- `target` *(required)*
- `direction` *(required)*: `upload` or `download`
- `sources` *(required)*: one or more source paths
- `destination` *(required)*
- `recursive`
- `preserve_times`
- `timeout`
- `port`
- `identity_file`
- `known_hosts_file`
- `strict_host_key_checking`
- `extra_ssh_args`

### `ssh_sync`

Incrementally sync files or directories between the local machine and one remote target via `rsync` over SSH.

Arguments:

- `target` *(required)*
- `direction` *(required)*: `upload` or `download`
- `source` *(required)*
- `destination` *(required)*
- `delete`
- `compress` *(defaults to `true`)*
- `dry_run`
- `exclude`
- `timeout`
- `extra_rsync_args`
- `port`
- `identity_file`
- `known_hosts_file`
- `strict_host_key_checking`
- `extra_ssh_args`

### `ssh_start_session`

Start a persistent interactive SSH session attached to a local PTY and return a session id plus initial unread output.

Arguments:

- `target` *(required)*
- `session_name`: optional stable identifier for this session
- `cwd`
- `env`
- `shell`
- `observer_mode`: `tmux` (default, with transcript fallback if tmux is unavailable) or `transcript`
- `wait_seconds`
- `max_output_chars`
- `port`
- `identity_file`
- `known_hosts_file`
- `strict_host_key_checking`
- `extra_ssh_args`

If you are building a long-lived workflow, prefer `ssh_ensure_session` below.

### `ssh_ensure_session`

Reuse an existing running SSH session or start one if needed.

This is the safest tool for multi-step agent workflows because it lets the client recover the same shell even if it forgot the last `session_id`.

Matching rules:

- If `session_name` is provided, the server reuses the running session with the same `target` + `session_name`
- Otherwise, it reuses a session only when there is exactly one running session for that `target`
- If the target is ambiguous, the tool returns an error instead of silently picking the wrong session

Arguments:

- `target` *(required)*
- `session_name`: recommended stable identifier such as `deploy-shell` or `main`
- `cwd`
- `env`
- `shell`
- `observer_mode`
- `wait_seconds`
- `max_output_chars`
- `port`
- `identity_file`
- `known_hosts_file`
- `strict_host_key_checking`
- `extra_ssh_args`

### `ssh_read_session`

Read unread output from a tracked session.

Arguments:

- `session_id` *(required)*
- `wait_seconds`
- `max_output_chars`

### `ssh_write_session`

Write raw input to a session PTY and optionally wait for more output.

Arguments:

- `session_id` *(required)*
- `input` *(required, may include control characters like `\u0003` or trailing newlines)*
- `wait_seconds`
- `max_output_chars`

### `ssh_stop_session`

Terminate a session, close any detached tmux observer for it, and return final metadata plus remaining unread output.

Arguments:

- `session_id` *(required)*
- `force`
- `wait_seconds`
- `max_output_chars`

### `ssh_list_sessions`

List tracked running and exited sessions.

Arguments:

- `include_exited` (defaults to `true`)
- `target`: optional filter
- `session_name`: optional filter

## Watching a session as an observer

Every interactive session now records a transcript file and returns:

- `transcript_path`: local file path for the session transcript
- `observer_command`: a local command that tails the transcript with the bundled observer helper
- `observer`: a structured object containing transcript and optional tmux metadata

By default, interactive sessions try to launch a detached tmux observer and fall back to transcript-only observation if tmux is unavailable.

If you start a session with `observer_mode: "tmux"` or leave the default in place and tmux is available, the server also launches a detached tmux session and returns:

- `observer.tmux_session_name`
- `observer.tmux_attach_command`

If you prefer not to launch tmux, set `observer_mode: "transcript"` explicitly.

### Example observer workflow

1. Start a session with:

   ```json
   {
      "target": "prod-shell",
      "session_name": "main",
      "observer_mode": "tmux"
    }
    ```

2. Copy the returned `observer.tmux_attach_command` into a local terminal.
3. Watch the session in tmux while the MCP client continues to control the SSH PTY.
4. If you do not want tmux, run the returned `observer_command` instead for a plain transcript follower.

Observer behavior and cleanup notes:

- Each tracked session gets exactly one transcript file. `ssh_ensure_session` reuses the existing session and does not launch duplicate tmux observers for it.
- Detached tmux observers are tied to the tracked session lifecycle: `ssh_stop_session` closes them, and the MCP server also closes them automatically when the stdio client disconnects and the server shuts down.
- The server also force-stops tracked SSH sessions during shutdown, but transcript files are intentionally left on disk for later inspection until you delete them from the state directory yourself.
- If tmux is unavailable or a tmux launch/cleanup step fails, the tool still returns transcript-based observer details plus a warning.

## Recommended agent pattern

For sustained work on one host, use a stable `session_name` and call `ssh_ensure_session` at the start of each new step:

```json
{
  "target": "prod-shell",
  "session_name": "main",
  "observer_mode": "tmux"
}
```

That gives the model an idempotent "get me the working shell" operation, which is much safer than calling `ssh_start_session` repeatedly.

## Notes

- The server inherits your local environment when launching `ssh`, so SSH config, `SSH_AUTH_SOCK`, and related OpenSSH behavior are preserved.
- `cwd`, `env`, and `shell` wrappers assume a POSIX-like shell exists on the remote side.
- Interactive sessions intentionally use a PTY, so command echo and terminal formatting may appear in output just like a terminal session.
- Session transcripts may contain sensitive command output, so choose `SSH_MCP_STATE_DIR` appropriately and clean up persisted transcript files when you no longer need them.

## License

This project is licensed under the MIT License. See `LICENSE`.

## Development and CI

The public CI workflow runs these checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall src
python3 -m build
```

To mirror CI locally, install the build frontend once and run:

```bash
python3 -m pip install build
python3 -m unittest discover -s tests -v
python3 -m compileall src
python3 -m build
```

Optional manual smoke checks:

- Start the MCP server from your MCP client or with `python -m ssh_mcp`
- Open a session with `ssh_ensure_session` and `observer_mode: "tmux"`
- Attach using `observer.tmux_attach_command` or follow the transcript with `observer_command`
- Reuse the same `target` + `session_name` a few times and confirm the same shell state is preserved
