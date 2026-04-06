from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__
from .ssh import SessionNotFoundError, SshToolService, ValidationError

SERVER_NAME = "ssh-mcp"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_CAPABILITIES = {"tools": {"listChanged": False}}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_SERVER_NOT_INITIALIZED = -32002


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _tool_success(result: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    return {
        "content": [{"type": "text", "text": _pretty_json(payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str, *, error_type: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    structured: dict[str, Any] = {"ok": False, "error_type": error_type, "message": message}
    if details:
        structured["details"] = dict(details)
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": structured,
        "isError": True,
    }


def _jsonrpc_error(code: int, message: str, *, request_id: Any, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data:
        payload["error"]["data"] = dict(data)
    return payload


_EXTRA_SSH_ARGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Additional ssh(1) flags passed verbatim, e.g. [\"-J\", \"jumphost\"]. "
        "Prefer the dedicated port, identity_file, and strict_host_key_checking parameters."
    ),
}

_STRICT_HOST_KEY_CHECKING_SCHEMA: dict[str, Any] = {
    "description": "Boolean or one of yes, no, ask, accept-new, off.",
    "oneOf": [{"type": "boolean"}, {"type": "string"}],
}

_SESSION_NAME_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Stable name for this session, used for recovery and reuse across tool calls. "
        "Choose a short, descriptive kebab-case name reflecting the task, "
        "e.g. 'deploy-staging', 'tail-api-logs', 'debug-worker-3'. "
        "This name also appears in tmux session listings for human observers. "
        "Strongly recommended for any multi-step workflow."
    ),
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "ssh_exec",
        "description": (
            "Run a one-off remote SSH command and return stdout, stderr, and exit metadata. "
            "The command runs in a non-interactive context. Use 'cwd' and 'env' for remote "
            "directory and environment setup (requires a POSIX shell on the remote). "
            "For interactive or long-running commands, use ssh_ensure_session instead."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target", "command"],
            "properties": {
                "target": {"type": "string", "description": "OpenSSH target such as host, alias, or user@host."},
                "command": {"type": "string", "description": "Remote command string to execute."},
                "cwd": {"type": "string", "description": "Remote directory to cd into before running the command."},
                "env": {
                    "type": "object",
                    "description": "Remote environment variables exported before the command runs.",
                    "additionalProperties": {"type": "string"},
                },
                "shell": {
                    "type": "string",
                    "description": "Remote shell executable used to wrap the command when shell/cwd/env behavior is needed.",
                },
                "timeout": {"type": "number", "description": "Local timeout in seconds."},
                "tty": {
                    "type": "boolean",
                    "description": (
                        "Request a remote TTY (ssh -tt). Required for interactive commands "
                        "(sudo with password prompt, etc.). Avoid for scripted commands: "
                        "stdout and stderr are merged and ANSI escape codes will be present in output. "
                        "Default: false."
                    ),
                },
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "identity_file": {"type": "string"},
                "known_hosts_file": {"type": "string"},
                "strict_host_key_checking": _STRICT_HOST_KEY_CHECKING_SCHEMA,
                "extra_ssh_args": _EXTRA_SSH_ARGS_SCHEMA,
            },
        },
    },
    {
        "name": "ssh_scp",
        "description": "Copy files or directories between the local machine and one remote target via the local scp client.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target", "direction", "sources", "destination"],
            "properties": {
                "target": {"type": "string", "description": "OpenSSH target (host alias or user@host). Specifies the remote host — do not include the host in sources or destination."},
                "direction": {"type": "string", "enum": ["upload", "download"]},
                "sources": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "For upload: local file or directory paths. For download: remote paths (without host prefix — the host is set by 'target').",
                },
                "destination": {
                    "type": "string",
                    "description": "For upload: remote directory path. For download: local directory or file path.",
                },
                "recursive": {"type": "boolean", "description": "Copy directories recursively. Required when any source is a directory."},
                "preserve_times": {"type": "boolean", "description": "Preserve modification times and modes."},
                "timeout": {"type": "number", "description": "Local timeout in seconds."},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "identity_file": {"type": "string"},
                "known_hosts_file": {"type": "string"},
                "strict_host_key_checking": _STRICT_HOST_KEY_CHECKING_SCHEMA,
                "extra_ssh_args": _EXTRA_SSH_ARGS_SCHEMA,
            },
        },
    },
    {
        "name": "ssh_sync",
        "description": "Incrementally sync files or directories between the local machine and one remote target via local rsync over SSH.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target", "direction", "source", "destination"],
            "properties": {
                "target": {"type": "string", "description": "OpenSSH target (host alias or user@host). Specifies the remote host — do not include the host in source or destination."},
                "direction": {"type": "string", "enum": ["upload", "download"]},
                "source": {
                    "type": "string",
                    "description": "For upload: local path. For download: remote path (without host prefix).",
                },
                "destination": {
                    "type": "string",
                    "description": "For upload: remote path. For download: local path.",
                },
                "delete": {"type": "boolean", "description": "Delete files in the destination that are not in the source."},
                "compress": {"type": "boolean", "description": "Compress data during transfer. Default: true."},
                "dry_run": {"type": "boolean", "description": "Show what would be transferred without actually doing it."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to exclude from the sync."},
                "timeout": {"type": "number", "description": "Local timeout in seconds."},
                "extra_rsync_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional rsync flags passed verbatim.",
                },
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "identity_file": {"type": "string"},
                "known_hosts_file": {"type": "string"},
                "strict_host_key_checking": _STRICT_HOST_KEY_CHECKING_SCHEMA,
                "extra_ssh_args": _EXTRA_SSH_ARGS_SCHEMA,
            },
        },
    },
    {
        "name": "ssh_start_session",
        "description": (
            "Start a new persistent interactive SSH session backed by a local PTY. "
            "Returns the session id, initial output, and observer metadata. "
            "If 'truncated' is true or 'pending_output_chars' > 0 in the response, "
            "call ssh_read_session to retrieve the remaining buffered output. "
            "For most agent workflows, prefer ssh_ensure_session instead — it reuses "
            "existing sessions and avoids accidental duplicates."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target"],
            "properties": {
                "target": {"type": "string", "description": "OpenSSH target such as host, alias, or user@host."},
                "session_name": _SESSION_NAME_SCHEMA,
                "cwd": {"type": "string", "description": "Remote directory to cd into at session start."},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Remote environment variables to export at session start."},
                "shell": {"type": "string", "description": "Remote shell executable to launch."},
                "observer_mode": {
                    "type": "string",
                    "enum": ["transcript", "tmux"],
                    "description": "Passive observer mode. Defaults to 'tmux' and falls back to transcript-only observation if tmux is unavailable. 'transcript' always records a transcript and returns a local follow command.",
                },
                "auto_close": {
                    "type": "boolean",
                    "description": (
                        "When true, the session is automatically cleaned up after the remote shell "
                        "or command exits. Use for one-shot long-running commands where you want "
                        "session-style output streaming but don't need the shell afterwards. "
                        "Default: false."
                    ),
                },
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Seconds to wait for initial output. Default: 1.0.",
                },
                "max_output_chars": {"type": "integer", "minimum": 1},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "identity_file": {"type": "string"},
                "known_hosts_file": {"type": "string"},
                "strict_host_key_checking": _STRICT_HOST_KEY_CHECKING_SCHEMA,
                "extra_ssh_args": _EXTRA_SSH_ARGS_SCHEMA,
            },
        },
    },
    {
        "name": "ssh_ensure_session",
        "description": (
            "Return an existing running SSH session or start a new one. "
            "This is the recommended tool for agent workflows — always provide a descriptive "
            "session_name so the session can be reliably found across tool calls and conversations. "
            "When a session is reused (reused=true in response), the cwd, env, and shell "
            "parameters are ignored — they only apply when creating a new session. "
            "Check 'created' vs 'reused' in the response to know which happened."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target"],
            "properties": {
                "target": {"type": "string", "description": "OpenSSH target such as host, alias, or user@host."},
                "session_name": _SESSION_NAME_SCHEMA,
                "cwd": {"type": "string", "description": "Remote directory to cd into (only when creating a new session)."},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Remote environment variables (only when creating a new session)."},
                "shell": {"type": "string", "description": "Remote shell executable (only when creating a new session)."},
                "observer_mode": {
                    "type": "string",
                    "enum": ["transcript", "tmux"],
                    "description": "Observer mode to ensure on the reused or newly created session. Defaults to 'tmux' and falls back to transcript-only observation if tmux is unavailable.",
                },
                "auto_close": {
                    "type": "boolean",
                    "description": (
                        "When true, the session is automatically cleaned up after the remote shell "
                        "or command exits. Only applies when creating a new session. Default: false."
                    ),
                },
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Seconds to wait for initial output. Default: 1.0.",
                },
                "max_output_chars": {"type": "integer", "minimum": 1},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "identity_file": {"type": "string"},
                "known_hosts_file": {"type": "string"},
                "strict_host_key_checking": _STRICT_HOST_KEY_CHECKING_SCHEMA,
                "extra_ssh_args": _EXTRA_SSH_ARGS_SCHEMA,
            },
        },
    },
    {
        "name": "ssh_read_session",
        "description": (
            "Read unread output from a tracked SSH session. "
            "Set wait_seconds (e.g. 1-10) to block until new output arrives; "
            "0 returns immediately with whatever is buffered right now. Default: 1.0. "
            "If 'truncated' is true or 'pending_output_chars' > 0, call again to drain remaining output. "
            "Check 'running' to know whether the session is still alive."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string"},
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Seconds to wait for new output. Default: 1.0. Set higher (5-10) for slow commands. Set 0 for instant polling.",
                },
                "max_output_chars": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "ssh_write_session",
        "description": (
            "Write text or a control sequence to a tracked SSH session PTY. "
            "Always terminate commands with a trailing newline (\\n) to submit them. "
            "Control characters: \\u0003 for Ctrl-C, \\u0004 for Ctrl-D, \\u001a for Ctrl-Z. "
            "Use wait_seconds (e.g. 1-5) to receive the command response in the same call. "
            "Check 'pending_output_chars' in the response; if non-zero, call ssh_read_session to drain."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "input"],
            "properties": {
                "session_id": {"type": "string"},
                "input": {
                    "type": "string",
                    "description": (
                        "Raw text to write to the session PTY. Include '\\n' to press Enter. "
                        "Control characters work: '\\u0003' for Ctrl-C, '\\u0004' for Ctrl-D. "
                        "Text is written exactly as provided."
                    ),
                },
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Seconds to wait for output after writing. Default: 1.0.",
                },
                "max_output_chars": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "ssh_stop_session",
        "description": (
            "Terminate a tracked SSH session, close any attached tmux observer, and return "
            "final unread output plus exit metadata. The transcript file is preserved on disk."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string"},
                "force": {"type": "boolean", "description": "Send SIGKILL instead of SIGTERM. Default: false."},
                "wait_seconds": {"type": "number", "minimum": 0, "description": "Seconds to wait for process to exit. Default: 2.0."},
                "max_output_chars": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "ssh_list_sessions",
        "description": (
            "List tracked SSH sessions with their session_id, session_name, target, state, "
            "uptime, and observer details (including tmux_session_name for cross-referencing "
            "with tmux ls). Use session_name filter to find a specific named session."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "include_exited": {"type": "boolean", "description": "Include sessions that have exited. Default: true."},
                "target": {"type": "string", "description": "Filter by SSH target."},
                "session_name": {"type": "string", "description": "Filter by session name."},
            },
        },
    },
]


class McpServer:
    def __init__(
        self,
        *,
        stdin: Any | None = None,
        stdout: Any | None = None,
        tool_service: SshToolService | None = None,
    ) -> None:
        self._stdin = stdin or sys.stdin.buffer
        self._stdout = stdout or sys.stdout.buffer
        self._tool_service = tool_service or SshToolService()
        self._initialized = False
        self._tool_handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "ssh_exec": self._tool_service.ssh_exec,
            "ssh_scp": self._tool_service.ssh_scp,
            "ssh_sync": self._tool_service.ssh_sync,
            "ssh_start_session": self._tool_service.ssh_start_session,
            "ssh_ensure_session": self._tool_service.ssh_ensure_session,
            "ssh_read_session": self._tool_service.ssh_read_session,
            "ssh_write_session": self._tool_service.ssh_write_session,
            "ssh_stop_session": self._tool_service.ssh_stop_session,
            "ssh_list_sessions": self._tool_service.ssh_list_sessions,
        }

    def serve(self) -> None:
        try:
            while True:
                line = self._stdin.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._write_message(
                        _jsonrpc_error(
                            JSONRPC_PARSE_ERROR,
                            f"Invalid JSON: {exc.msg}",
                            request_id=None,
                        )
                    )
                    continue
                response = self._handle_message(message)
                if response is not None:
                    self._write_message(response)
        finally:
            self._tool_service.close()

    def _handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _jsonrpc_error(
                JSONRPC_INVALID_REQUEST,
                "JSON-RPC messages must be objects.",
                request_id=None,
            )
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0":
            return _jsonrpc_error(
                JSONRPC_INVALID_REQUEST,
                "Only JSON-RPC 2.0 is supported.",
                request_id=request_id,
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(
                JSONRPC_INVALID_REQUEST,
                "Request method must be a non-empty string.",
                request_id=request_id,
            )
        params = message.get("params", {})
        is_notification = "id" not in message
        try:
            result = self._dispatch(method, params)
        except JsonRpcRequestError as exc:
            if is_notification:
                return None
            return _jsonrpc_error(exc.code, exc.message, request_id=request_id, data=exc.data)
        except Exception as exc:  # pragma: no cover - defensive fallback
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            if is_notification:
                return None
            return _jsonrpc_error(
                JSONRPC_INTERNAL_ERROR,
                f"Internal server error: {exc}",
                request_id=request_id,
            )
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: Any) -> dict[str, Any]:
        if method == "initialize":
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise JsonRpcRequestError(
                    JSONRPC_INVALID_PARAMS,
                    "'initialize' params must be an object.",
                )
            protocol_version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            if not isinstance(protocol_version, str) or not protocol_version:
                raise JsonRpcRequestError(
                    JSONRPC_INVALID_PARAMS,
                    "'protocolVersion' must be a non-empty string when provided.",
                )
            negotiated_version = (
                protocol_version
                if protocol_version in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            self._initialized = True
            return {
                "protocolVersion": negotiated_version,
                "capabilities": SERVER_CAPABILITIES,
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method == "notifications/initialized":
            return {}
        if method == "ping":
            return {}
        if not self._initialized:
            raise JsonRpcRequestError(
                JSONRPC_SERVER_NOT_INITIALIZED,
                "Server has not been initialized yet. Send 'initialize' first.",
            )
        if method == "tools/list":
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise JsonRpcRequestError(
                    JSONRPC_INVALID_PARAMS,
                    "'tools/list' params must be an object when provided.",
                )
            cursor = params.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                raise JsonRpcRequestError(
                    JSONRPC_INVALID_PARAMS,
                    "'tools/list.cursor' must be a string when provided.",
                )
            return {"tools": TOOL_DEFINITIONS}
        if method == "tools/call":
            return self._handle_tool_call(params)
        raise JsonRpcRequestError(
            JSONRPC_METHOD_NOT_FOUND,
            f"Unknown method '{method}'.",
        )

    def _handle_tool_call(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise JsonRpcRequestError(
                JSONRPC_INVALID_PARAMS,
                "'tools/call' params must be an object.",
            )
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcRequestError(
                JSONRPC_INVALID_PARAMS,
                "'tools/call' requires a non-empty string 'name'.",
            )
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcRequestError(
                JSONRPC_INVALID_PARAMS,
                "'tools/call.arguments' must be an object when provided.",
            )
        handler = self._tool_handlers.get(name)
        if handler is None:
            return _tool_error(f"Unknown tool '{name}'.", error_type="unknown_tool")
        try:
            return _tool_success(handler(arguments))
        except ValidationError as exc:
            return _tool_error(str(exc), error_type="validation_error")
        except SessionNotFoundError as exc:
            return _tool_error(str(exc), error_type="session_not_found")
        except Exception as exc:  # pragma: no cover - defensive fallback
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            return _tool_error(f"Internal tool error: {exc}", error_type="internal_error")

    def _write_message(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._stdout.write(encoded + b"\n")
        self._stdout.flush()


class JsonRpcRequestError(Exception):
    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
