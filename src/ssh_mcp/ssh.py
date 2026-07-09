from __future__ import annotations

import codecs
import errno
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_OUTPUT_CHARS = 65_536
# Hard cap on in-memory unread-output buffer per session.  The transcript on
# disk always receives the full stream; only the in-memory ring is bounded.
DEFAULT_UNREAD_BUFFER_CAP = 1_048_576  # 1 MiB
DEFAULT_SESSION_START_WAIT_SECONDS = 1.0
DEFAULT_SESSION_READ_WAIT_SECONDS = 1.0
DEFAULT_SESSION_WRITE_WAIT_SECONDS = 1.0
DEFAULT_SESSION_STOP_WAIT_SECONDS = 2.0
DEFAULT_VIEW_MAX_BYTES = 20_480  # 20 KiB, matches the local file-viewing tool's cutoff.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_STRICT_HOST_KEY_CHECKING = {"yes", "no", "ask", "accept-new", "off"}
_ALLOWED_OBSERVER_MODES = {"transcript", "tmux"}
_ALLOWED_GREP_OUTPUT_MODES = {"content", "files_with_matches", "count"}
_EXIT_SIGNAL_NAMES = {9: "killed", 13: "broken-pipe", 15: "terminated"}

# Reserved exit codes used by the small POSIX shell scripts the remote file
# tools (ssh_view/ssh_create/ssh_edit/ssh_grep/ssh_glob) run over SSH.  Chosen
# to avoid colliding with common shell/utility exit statuses (0-2, 126, 127, 130).
_EXIT_NOT_FOUND = 21
_EXIT_NOT_REGULAR_FILE = 22
_EXIT_IS_DIRECTORY = 23
_EXIT_ALREADY_EXISTS = 24
_EXIT_NO_PARENT_DIR = 25
_EXIT_NOT_DIRECTORY = 26


class SshMcpError(Exception):
    """Base exception for SSH MCP failures."""


class ValidationError(SshMcpError):
    """Raised when tool arguments are invalid."""


class SessionNotFoundError(SshMcpError):
    """Raised when a session id is unknown."""


class ForwardNotFoundError(SshMcpError):
    """Raised when a forward id is unknown."""


class RemoteFileError(SshMcpError):
    """Raised when a remote file/directory operation cannot proceed as requested."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_arguments(arguments: Any) -> Mapping[str, Any]:
    if not isinstance(arguments, Mapping):
        raise ValidationError("Tool arguments must be a JSON object.")
    return arguments


def _string_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    required: bool = False,
    strip: bool = True,
) -> str | None:
    value = arguments.get(name)
    if value is None:
        if required:
            raise ValidationError(f"'{name}' is required.")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"'{name}' must be a string.")
    if "\x00" in value:
        raise ValidationError(f"'{name}' cannot contain NUL bytes.")
    normalized = value.strip() if strip else value
    if not normalized.strip():
        raise ValidationError(f"'{name}' must be a non-empty string.")
    return normalized


def _bool_argument(arguments: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        mapping = {
            "true": True,
            "false": False,
            "yes": True,
            "no": False,
            "on": True,
            "off": False,
        }
        if normalized in mapping:
            return mapping[normalized]
    raise ValidationError(f"'{name}' must be a boolean.")


def _float_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: float | None = None,
    minimum: float = 0.0,
    allow_zero: bool = True,
) -> float | None:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError(f"'{name}' must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"'{name}' must be a number.") from exc
    if number < minimum or (not allow_zero and number == 0):
        comparator = ">=" if allow_zero else ">"
        raise ValidationError(f"'{name}' must be {comparator} {minimum}.")
    return number


def _int_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: int | None = None,
    minimum: int = 0,
    allow_zero: bool = True,
) -> int | None:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError(f"'{name}' must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"'{name}' must be an integer.") from exc
    if number < minimum or (not allow_zero and number == 0):
        comparator = ">=" if allow_zero else ">"
        raise ValidationError(f"'{name}' must be {comparator} {minimum}.")
    return number


def _path_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    must_exist: bool = False,
) -> str | None:
    value = _string_argument(arguments, name)
    if value is None:
        return None
    path = Path(value).expanduser()
    if must_exist and not path.exists():
        raise ValidationError(f"'{name}' does not exist: {path}")
    if not must_exist and not path.parent.exists():
        raise ValidationError(f"Parent directory for '{name}' does not exist: {path.parent}")
    return str(path)


def _string_list_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    required: bool = False,
    strip: bool = False,
    minimum_length: int = 0,
) -> list[str]:
    value = arguments.get(name)
    if value is None:
        if required:
            raise ValidationError(f"'{name}' is required.")
        return []
    if not isinstance(value, list):
        raise ValidationError(f"'{name}' must be an array of strings.")
    if len(value) < minimum_length:
        comparator = "at least" if minimum_length > 0 else "exactly"
        raise ValidationError(f"'{name}' must contain {comparator} {minimum_length} item(s).")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError(f"'{name}[{index}]' must be a string.")
        if "\x00" in item:
            raise ValidationError(f"'{name}[{index}]' cannot contain NUL bytes.")
        candidate = item.strip() if strip else item
        if not candidate.strip():
            raise ValidationError(f"'{name}[{index}]' must be a non-empty string.")
        normalized.append(candidate)
    return normalized


def _env_argument(arguments: Mapping[str, Any], name: str = "env") -> dict[str, str]:
    value = arguments.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError(f"'{name}' must be an object mapping strings to strings.")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not _ENV_NAME_RE.fullmatch(raw_key):
            raise ValidationError(
                f"Environment variable names in '{name}' must match {_ENV_NAME_RE.pattern}."
            )
        if not isinstance(raw_value, str):
            raise ValidationError(f"Environment variable '{raw_key}' must have a string value.")
        if "\x00" in raw_value:
            raise ValidationError(f"Environment variable '{raw_key}' cannot contain NUL bytes.")
        normalized[raw_key] = raw_value
    return normalized


# SSH options that execute local commands or open local resources.  These are
# blocked in extra_ssh_args to prevent prompt-injection attacks from running
# arbitrary commands on the *local* machine (as opposed to remote execution,
# which is the tool's intended purpose).  The blocklist targets options whose
# values are executed by SSH via /bin/sh -c or that open local network ports.
_BLOCKED_SSH_OPTIONS = {
    "proxycommand",
    "localcommand",
    "permitlocalcommand",
    "localforward",
    "remoteforward",
    "dynamicforward",
}

# rsync flags that execute local commands or override the transport.
_BLOCKED_RSYNC_FLAGS = {"--rsh", "-e", "--rsync-path"}

_BLOCKED_SSH_SHORT_FLAGS = {"-L", "-R", "-D", "-W"}


def _check_blocked_ssh_options(args: list[str], name: str) -> None:
    """Reject SSH -o options that would execute local commands."""
    for i, arg in enumerate(args):
        if arg in _BLOCKED_SSH_SHORT_FLAGS:
            raise ValidationError(
                f"'{name}' contains blocked SSH flag '{arg}'. "
                "Use the ssh_forward tool for port forwarding."
            )
        # Extract the option name from "-o Key=val" or "-oKey=val" forms.
        raw_option: str | None = None
        if arg == "-o" and i + 1 < len(args):
            raw_option = args[i + 1]
        elif arg.startswith("-o") and len(arg) > 2:
            raw_option = arg[2:]
        if raw_option is not None:
            option_key = raw_option.split("=", 1)[0].strip()
            if option_key.lower() in _BLOCKED_SSH_OPTIONS:
                raise ValidationError(
                    f"'{name}' contains blocked SSH option '{option_key}'. "
                    "This option can execute local commands and is not permitted via extra_ssh_args."
                )


def _extra_ssh_args_argument(arguments: Mapping[str, Any], name: str = "extra_ssh_args") -> list[str]:
    args = _string_list_argument(arguments, name, strip=False)
    _check_blocked_ssh_options(args, name)
    return args


def _exclude_argument(arguments: Mapping[str, Any], name: str = "exclude") -> list[str]:
    return _string_list_argument(arguments, name, strip=False)


def _extra_rsync_args_argument(arguments: Mapping[str, Any], name: str = "extra_rsync_args") -> list[str]:
    args = _string_list_argument(arguments, name, strip=False)
    for arg in args:
        # Check both long form (--rsh) and short form (-e), and --rsh=...
        flag = arg.split("=", 1)[0].lower()
        if flag in _BLOCKED_RSYNC_FLAGS:
            raise ValidationError(
                f"'{name}' contains blocked rsync flag '{arg.split('=', 1)[0]}'. "
                "This flag can execute local commands and is not permitted via extra_rsync_args."
            )
    return args


def _strict_host_key_checking_argument(
    arguments: Mapping[str, Any],
    name: str = "strict_host_key_checking",
) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _ALLOWED_STRICT_HOST_KEY_CHECKING:
            return normalized
    allowed = ", ".join(sorted(_ALLOWED_STRICT_HOST_KEY_CHECKING))
    raise ValidationError(
        f"'{name}' must be a boolean or one of: {allowed}."
    )


def _observer_mode_argument(
    arguments: Mapping[str, Any],
    name: str = "observer_mode",
    *,
    default: str = "tmux",
) -> str:
    value = arguments.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        allowed = ", ".join(sorted(_ALLOWED_OBSERVER_MODES))
        raise ValidationError(f"'{name}' must be a string and one of: {allowed}.")
    normalized = value.strip().lower()
    if normalized not in _ALLOWED_OBSERVER_MODES:
        allowed = ", ".join(sorted(_ALLOWED_OBSERVER_MODES))
        raise ValidationError(f"'{name}' must be one of: {allowed}.")
    return normalized


def _build_export_commands(environment: Mapping[str, str]) -> list[str]:
    return [f"export {name}={shlex.quote(value)}" for name, value in environment.items()]


def build_exec_remote_command(
    command: str,
    *,
    cwd: str | None,
    environment: Mapping[str, str],
    shell: str | None,
) -> str:
    if shell is None and cwd is None and not environment:
        return command
    script_parts = _build_export_commands(environment)
    body = command
    if cwd is not None:
        body = f"cd {shlex.quote(cwd)} && {body}"
    script_parts.append(body)
    script = "; ".join(script_parts)
    wrapper_shell = shell or "sh"
    return f"exec {shlex.quote(wrapper_shell)} -c {shlex.quote(script)}"


def build_session_remote_command(
    *,
    cwd: str | None,
    environment: Mapping[str, str],
    shell: str | None,
) -> str | None:
    if shell is None and cwd is None and not environment:
        return None
    script_parts = _build_export_commands(environment)
    shell_exec = (
        f"exec {shlex.quote(shell)} -i"
        if shell is not None
        else 'exec "${SHELL:-/bin/sh}" -i'
    )
    if cwd is not None:
        shell_exec = f"cd {shlex.quote(cwd)} && {shell_exec}"
    script_parts.append(shell_exec)
    script = "; ".join(script_parts)
    return f"exec sh -c {shlex.quote(script)}"


def _split_return_code(return_code: int | None) -> tuple[int | None, int | None]:
    if return_code is None:
        return None, None
    if return_code >= 0:
        return return_code, None
    return None, -return_code


def _wait_for_exit(process: subprocess.Popen[Any], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return process.poll() is not None


def _signal_process_group(process: subprocess.Popen[Any], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    first_signal: signal.Signals = signal.SIGTERM,
    grace_period: float = 0.5,
) -> bool:
    if process.poll() is not None:
        return False
    _signal_process_group(process, first_signal)
    if _wait_for_exit(process, grace_period):
        return False
    _signal_process_group(process, signal.SIGKILL)
    _wait_for_exit(process, 1.0)
    return True


def _resolve_binary(configured_binary: str, *, label: str) -> str:
    candidate = configured_binary.strip()
    if not candidate:
        raise ValidationError(f"{label} name must be a non-empty string.")
    has_path_separator = os.sep in candidate or (os.altsep is not None and os.altsep in candidate)
    if has_path_separator or candidate.startswith("."):
        expanded = str(Path(candidate).expanduser())
        if not Path(expanded).exists():
            raise ValidationError(f"{label} not found: {expanded}")
        return expanded
    resolved = shutil.which(candidate)
    if resolved is None:
        raise ValidationError(f"{label} '{candidate}' was not found on PATH.")
    return resolved


def _resolve_ssh_binary(configured_binary: str) -> str:
    return _resolve_binary(configured_binary, label="SSH client")


def _resolve_scp_binary(configured_binary: str) -> str:
    return _resolve_binary(configured_binary, label="SCP client")


def _resolve_rsync_binary(configured_binary: str) -> str:
    return _resolve_binary(configured_binary, label="rsync client")


def _default_state_dir() -> Path:
    configured = os.environ.get("SSH_MCP_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "ssh-mcp"
    return Path.home() / ".local" / "state" / "ssh-mcp"


def _slug(value: str, *, max_len: int = 30) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:max_len]


def _sanitize_tmux_session_name(
    session_id: str,
    *,
    target: str | None = None,
    session_name: str | None = None,
) -> str:
    if session_name:
        label = _slug(session_name)
        if target:
            return f"ssh-mcp-{_slug(target)}-{label}"
        return f"ssh-mcp-{label}"
    if target:
        return f"ssh-mcp-{_slug(target)}-{session_id[:8]}"
    return f"ssh-mcp-{session_id[:8]}"


def _read_all_available(master_fd: int, decoder: codecs.IncrementalDecoder) -> str:
    chunks: list[str] = []
    while True:
        try:
            ready, _, _ = select.select([master_fd], [], [], 0)
        except OSError:
            break
        if not ready:
            break
        try:
            data = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno in {errno.EIO, errno.EBADF}:
                break
            raise
        if not data:
            break
        chunks.append(decoder.decode(data))
    return "".join(chunks)


def _run_without_pty(argv: list[str], *, timeout: float | None) -> dict[str, Any]:
    start = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    forced_kill = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        forced_kill = _terminate_process_group(process)
        stdout, stderr = process.communicate()
    duration_ms = int((time.monotonic() - start) * 1000)
    exit_code, term_signal = _split_return_code(process.returncode)
    return {
        "exit_code": exit_code,
        "signal": term_signal,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def _run_without_pty_binary_safe(
    argv: list[str], *, timeout: float | None, input_text: str | None = None
) -> dict[str, Any]:
    """Like ``_run_without_pty``, but preserves file content byte-for-byte.

    ``subprocess.Popen(..., text=True)`` enables Python's universal-newlines
    translation, which silently rewrites ``\\r\\n`` and lone ``\\r`` to
    ``\\n`` while reading -- harmless for displaying command output, but
    silently corrupts CRLF line endings when the remote file tools read a
    file's exact content and (for ssh_edit) write it straight back. This
    variant communicates in raw bytes and decodes manually, which performs no
    newline translation, so it's used for all ssh_view/ssh_create/ssh_edit/
    ssh_grep/ssh_glob remote script execution.
    """
    start = time.monotonic()
    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    forced_kill = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        forced_kill = _terminate_process_group(process)
        stdout_bytes, stderr_bytes = process.communicate()
    duration_ms = int((time.monotonic() - start) * 1000)
    exit_code, term_signal = _split_return_code(process.returncode)
    return {
        "exit_code": exit_code,
        "signal": term_signal,
        "stdout": (stdout_bytes or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_bytes or b"").decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def _run_with_pty(argv: list[str], *, timeout: float | None) -> dict[str, Any]:
    start = time.monotonic()
    master_fd, slave_fd = os.openpty()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    output_chunks: list[str] = []
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    timed_out = False
    forced_kill = False
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while True:
            if deadline is not None and process.poll() is None and time.monotonic() >= deadline:
                timed_out = True
                forced_kill = _terminate_process_group(process)
            wait_timeout = 0.1
            if deadline is not None and process.poll() is None:
                wait_timeout = max(0.0, min(0.1, deadline - time.monotonic()))
            try:
                ready, _, _ = select.select([master_fd], [], [], wait_timeout)
            except OSError:
                ready = []
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        data = b""
                    else:
                        raise
                if data:
                    output_chunks.append(decoder.decode(data))
            if process.poll() is not None:
                output_chunks.append(_read_all_available(master_fd, decoder))
                break
        output_chunks.append(decoder.decode(b"", final=True))
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    duration_ms = int((time.monotonic() - start) * 1000)
    output = "".join(part for part in output_chunks if part)
    exit_code, term_signal = _split_return_code(process.returncode)
    return {
        "exit_code": exit_code,
        "signal": term_signal,
        "stdout": output,
        "stderr": "",
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


@dataclass(slots=True)
class ObserverInfo:
    transcript_path: str
    command: str
    requested_mode: str = "transcript"
    mode: str = "transcript"
    tmux_binary: str | None = None
    tmux_session_name: str | None = None
    tmux_started: bool = False
    warning: str | None = None

    def tmux_launch_command(self) -> str | None:
        if self.tmux_binary is None or self.tmux_session_name is None:
            return None
        return shlex.join(
            [self.tmux_binary, "new-session", "-d", "-s", self.tmux_session_name, self.command]
        )

    def tmux_attach_command(self) -> str | None:
        if not self.tmux_started or self.tmux_binary is None or self.tmux_session_name is None:
            return None
        return shlex.join([self.tmux_binary, "attach", "-t", self.tmux_session_name])

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested_mode": self.requested_mode,
            "mode": self.mode,
            "command": self.command,
            "transcript_path": self.transcript_path,
            "tmux_started": self.tmux_started,
        }
        if self.tmux_session_name is not None:
            payload["tmux_session_name"] = self.tmux_session_name
        tmux_launch_command = self.tmux_launch_command()
        if tmux_launch_command is not None:
            payload["tmux_launch_command"] = tmux_launch_command
        tmux_attach_command = self.tmux_attach_command()
        if tmux_attach_command is not None:
            payload["tmux_attach_command"] = tmux_attach_command
        if self.warning is not None:
            payload["warning"] = self.warning
        return payload


@dataclass(slots=True)
class ConnectionSettings:
    target: str
    port: int | None = None
    identity_file: str | None = None
    known_hosts_file: str | None = None
    strict_host_key_checking: str | None = None
    extra_ssh_args: list[str] = field(default_factory=list)

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, Any]) -> "ConnectionSettings":
        target = _string_argument(arguments, "target", required=True)
        port = _int_argument(arguments, "port", minimum=1, allow_zero=False)
        if port is not None and port > 65535:
            raise ValidationError("'port' must be <= 65535.")
        return cls(
            target=target,
            port=port,
            identity_file=_path_argument(arguments, "identity_file", must_exist=True),
            known_hosts_file=_path_argument(arguments, "known_hosts_file", must_exist=False),
            strict_host_key_checking=_strict_host_key_checking_argument(arguments),
            extra_ssh_args=_extra_ssh_args_argument(arguments),
        )

    def build_transport_argv(
        self,
        program: str,
        *,
        port_flag: str,
        tty: bool | None = None,
    ) -> list[str]:
        argv = [program]
        if tty is not None:
            argv.append("-tt" if tty else "-T")
        if self.port is not None:
            argv.extend([port_flag, str(self.port)])
        if self.identity_file is not None:
            argv.extend(["-i", self.identity_file])
        if self.known_hosts_file is not None:
            argv.extend(["-o", f"UserKnownHostsFile={self.known_hosts_file}"])
        if self.strict_host_key_checking is not None:
            argv.extend(["-o", f"StrictHostKeyChecking={self.strict_host_key_checking}"])
        argv.extend(self.extra_ssh_args)
        return argv

    def build_argv(
        self,
        ssh_binary: str,
        remote_command: str | None,
        *,
        tty: bool,
        keepalive: bool = False,
    ) -> list[str]:
        argv = self.build_transport_argv(ssh_binary, port_flag="-p", tty=tty)
        if keepalive:
            # Inject keepalive unless the caller already set them via
            # extra_ssh_args — respect explicit user overrides.
            has_interval = any("ServerAliveInterval" in a for a in self.extra_ssh_args)
            has_count = any("ServerAliveCountMax" in a for a in self.extra_ssh_args)
            if not has_interval:
                argv.extend(["-o", "ServerAliveInterval=30"])
            if not has_count:
                argv.extend(["-o", "ServerAliveCountMax=3"])
        argv.append(self.target)
        if remote_command is not None:
            argv.append(remote_command)
        return argv


def _build_remote_path(target: str, path: str) -> str:
    return f"{target}:{shlex.quote(path)}"


def _expand_local_path(path: str) -> str:
    expanded = str(Path(path).expanduser())
    trailing_separators = [os.sep]
    if os.altsep is not None:
        trailing_separators.append(os.altsep)
    if path.endswith(tuple(trailing_separators)) and not expanded.endswith(os.sep):
        expanded += os.sep
    return expanded


def _normalize_local_sources(paths: list[str], *, recursive: bool) -> list[str]:
    normalized: list[str] = []
    for raw_path in paths:
        expanded = _expand_local_path(raw_path)
        path = Path(expanded)
        if not path.exists():
            raise ValidationError(f"Local source path does not exist: {path}")
        if path.is_dir() and not recursive:
            raise ValidationError(
                f"Local source path is a directory and requires 'recursive=true': {path}"
            )
        normalized.append(expanded)
    return normalized


def _normalize_local_destination(path: str, *, require_directory: bool = False) -> str:
    expanded = _expand_local_path(path)
    destination = Path(expanded)
    if require_directory:
        if not destination.exists() or not destination.is_dir():
            raise ValidationError(
                "Local destination must be an existing directory when copying multiple sources: "
                f"{destination}"
            )
        return expanded
    parent = destination if destination.exists() and destination.is_dir() else destination.parent
    if not parent.exists():
        raise ValidationError(f"Parent directory for local destination does not exist: {parent}")
    return expanded


# ---------------------------------------------------------------------------
# Remote file tools (ssh_view / ssh_create / ssh_edit / ssh_grep / ssh_glob).
#
# These mirror the read/edit/search tools agents already use locally (view,
# create, edit, grep, glob) but operate on files reached over SSH, so remote
# editing feels the same as local editing.
#
# All *data* (paths, patterns, file content) is passed to the remote shell as
# positional parameters ($1, $2, ...) or over stdin -- never interpolated
# into script text -- so script text is fixed and there is no remote-side
# quoting/injection risk.  The scripts use a private range of exit codes
# (_EXIT_*, see top of file) to report semantic outcomes (not found, already
# exists, is a directory, ...) distinctly from a wrapped command's own exit
# status.
# ---------------------------------------------------------------------------


def _shield_leading_dash(path: str) -> str:
    """Rewrite a relative path starting with '-' as './-...'.

    BSD tools (e.g. macOS's sed) don't reliably support '--' as an
    end-of-options marker -- it can be misparsed as a literal filename
    argument, corrupting exit codes -- so a leading '-' in a relative path is
    neutralized this way instead of relying on '--'.
    """
    if path.startswith("-") and not path.startswith("/"):
        return f"./{path}"
    return path


def _build_script_command(script: str, args: Sequence[str]) -> str:
    """Build a remote command string that runs ``script`` under ``sh -c``,
    passing ``args`` as positional parameters ($1, $2, ...) rather than
    interpolating them into the script text.
    """
    words = [shlex.quote(script), "sh", *(shlex.quote(arg) for arg in args)]
    return "exec sh -c " + " ".join(words)


def _remote_file_error_for_exit(
    exit_code: int | None, path: str, *, timed_out: bool, stderr: str
) -> RemoteFileError | None:
    """Translate a remote file-tool script's outcome into a RemoteFileError, if any."""
    if timed_out:
        return RemoteFileError(f"Remote operation on '{path}' timed out.")
    if exit_code == 0:
        return None
    if exit_code == _EXIT_NOT_FOUND:
        return RemoteFileError(f"Remote path does not exist: {path}")
    if exit_code == _EXIT_NOT_REGULAR_FILE:
        return RemoteFileError(f"Remote path exists but is not a regular file: {path}")
    if exit_code == _EXIT_IS_DIRECTORY:
        return RemoteFileError(f"Remote path is a directory, not a file: {path}")
    if exit_code == _EXIT_ALREADY_EXISTS:
        return RemoteFileError(f"Remote path already exists: {path}")
    if exit_code == _EXIT_NO_PARENT_DIR:
        return RemoteFileError(f"Parent directory does not exist remotely for: {path}")
    if exit_code == _EXIT_NOT_DIRECTORY:
        return RemoteFileError(f"Remote path exists but is not a directory: {path}")
    message = stderr.strip() or f"remote command exited with code {exit_code}."
    return RemoteFileError(f"Remote operation on '{path}' failed: {message}")


def _parse_view_range(value: Any) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValidationError("'view_range' must be a two-element array, e.g. [start, end].")
    start_raw, end_raw = value
    if isinstance(start_raw, bool) or isinstance(end_raw, bool):
        raise ValidationError("'view_range' elements must be integers.")
    try:
        start = int(start_raw)
        end = int(end_raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("'view_range' elements must be integers.") from exc
    if start < 1:
        raise ValidationError("'view_range[0]' (start line) must be >= 1.")
    if end != -1 and end < start:
        raise ValidationError("'view_range[1]' (end line) must be -1 (end of file) or >= start line.")
    return start, end


def _edits_argument(arguments: Mapping[str, Any]) -> list[tuple[str, str]]:
    value = arguments.get("edits")
    if not isinstance(value, list) or not value:
        raise ValidationError(
            "'edits' is required and must be a non-empty array of {old_str, new_str} objects."
        )
    parsed: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError(f"'edits[{index}]' must be an object with 'old_str' and 'new_str'.")
        old_str = item.get("old_str")
        new_str = item.get("new_str")
        if not isinstance(old_str, str) or not old_str:
            raise ValidationError(f"'edits[{index}].old_str' is required and must be a non-empty string.")
        if "\x00" in old_str:
            raise ValidationError(f"'edits[{index}].old_str' cannot contain NUL bytes.")
        if not isinstance(new_str, str):
            raise ValidationError(f"'edits[{index}].new_str' is required and must be a string.")
        if "\x00" in new_str:
            raise ValidationError(f"'edits[{index}].new_str' cannot contain NUL bytes.")
        parsed.append((old_str, new_str))
    return parsed


def _apply_edits(content: str, edits: list[tuple[str, str]]) -> str:
    """Apply ``edits`` in order, mirroring the local edit tool's semantics:
    each ``old_str`` must match exactly one location in the content *as it
    stands after prior edits in this call have been applied*.
    """
    updated = content
    for index, (old_str, new_str) in enumerate(edits):
        count = updated.count(old_str)
        if count == 0:
            raise RemoteFileError(
                f"'edits[{index}].old_str' was not found in the file "
                "(checked after applying any earlier edits in this call)."
            )
        if count > 1:
            raise RemoteFileError(
                f"'edits[{index}].old_str' matches {count} locations in the file; "
                "it must match exactly one. Include more surrounding context to make it unique."
            )
        updated = updated.replace(old_str, new_str, 1)
    return updated


_BRACE_GROUP_RE = re.compile(r"\{([^{}]*)\}")


def _expand_braces(pattern: str) -> list[str]:
    """Expand one level of ``{a,b,c}`` alternation (non-nested) into concrete patterns."""
    match = _BRACE_GROUP_RE.search(pattern)
    if match is None:
        return [pattern]
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    expanded: list[str] = []
    for option in match.group(1).split(","):
        expanded.extend(_expand_braces(prefix + option + suffix))
    return expanded


def _translate_glob_segment(segment: str) -> str:
    """Translate one path segment (no ``/``) of a glob pattern into a regex fragment.

    Supports ``*`` (any run of characters), ``?`` (single character), and
    ``[seq]``/``[!seq]``/``[^seq]`` character classes.
    """
    chars: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c == "*":
            chars.append("[^/]*")
            i += 1
        elif c == "?":
            chars.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and segment[j] in "!^":
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:
                # Unterminated '[' -- treat as a literal character.
                chars.append(re.escape("["))
                i += 1
            else:
                inner = segment[i + 1 : j]
                if inner.startswith("!") or inner.startswith("^"):
                    inner = "^" + inner[1:]
                inner = inner.replace("\\", "\\\\")
                chars.append(f"[{inner}]")
                i = j + 1
        else:
            chars.append(re.escape(c))
            i += 1
    return "".join(chars)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a shell-style glob pattern to a regex, for use with ``.fullmatch()``.

    Supports the primitives agents expect from the native glob tool: ``*``
    and ``?`` match within a single path segment, ``[seq]``/``[!seq]`` are
    character classes, ``{a,b,c}`` is (single-level) alternation, and ``**``
    matches zero or more whole path segments. A candidate path segment that
    starts with ``.`` is only matched by a pattern segment that is itself
    literally dot-prefixed (classic Unix "hidden file" glob behavior).
    """
    non_hidden_segment = "[^/.][^/]*"
    alternatives: list[str] = []
    for variant in _expand_braces(pattern):
        segments = variant.split("/")
        fragments: list[str | None] = []
        for segment in segments:
            if segment == "**":
                fragments.append(None)
            else:
                fragment = _translate_glob_segment(segment)
                if not segment.startswith("."):
                    fragment = r"(?!\.)" + fragment
                fragments.append(fragment)
        pieces: list[str] = []
        last_index = len(fragments) - 1
        for index, fragment in enumerate(fragments):
            previous_is_double_star = index > 0 and fragments[index - 1] is None
            needs_slash_join = index > 0 and not previous_is_double_star
            if fragment is None:
                if needs_slash_join:
                    pieces.append("/")
                if index == last_index:
                    pieces.append(f"(?:{non_hidden_segment}(?:/{non_hidden_segment})*)?")
                else:
                    pieces.append(f"(?:{non_hidden_segment}/)*")
            else:
                if needs_slash_join:
                    pieces.append("/")
                pieces.append(fragment)
        alternatives.append("".join(pieces))
    combined = "|".join(f"(?:{alt})" for alt in alternatives)
    return re.compile(f"(?:{combined})", re.DOTALL)


# Matches both grep match lines ("path:line:text") and context lines
# ("path-line-text"); the backreference requires the same separator on
# both sides, since a path may itself legitimately contain '-' or ':'.
_GREP_MATCH_LINE_RE = re.compile(r"\A(?P<path>.*?)(?P<sep>[:-])(?P<line>\d+)(?P=sep)(?P<text>.*)\Z", re.DOTALL)

# $1=path $2=mode('range'|'full'|<auto>) $3=start $4=end(or -1) $5=max_bytes(auto mode)
_VIEW_SCRIPT = r"""
P=$1
MODE=$2
if [ ! -e "$P" ]; then exit 21; fi
if [ -d "$P" ]; then
  printf 'DIR\0'
  find "$P" -mindepth 1 -maxdepth 2 -name ".*" -prune -o -print 2>/dev/null | sort | while IFS= read -r entry; do
    if [ -d "$entry" ]; then
      printf 'd\t%s\n' "$entry"
    else
      printf 'f\t%s\n' "$entry"
    fi
  done
  exit 0
fi
if [ ! -f "$P" ]; then exit 22; fi
SIZE=$(wc -c < "$P" | tr -d '[:space:]')
LINES=$(wc -l < "$P" | tr -d '[:space:]')
printf 'FILE\t%s\t%s\0' "$SIZE" "$LINES"
case "$MODE" in
  range)
    START=$3
    END=$4
    if [ "$END" = "-1" ]; then
      sed -n "${START},\$p" "$P"
    else
      sed -n "${START},${END}p" "$P"
    fi
    ;;
  full)
    cat "$P"
    ;;
  *)
    MAXB=$5
    if [ "$SIZE" -gt "$MAXB" ]; then
      head -c "$MAXB" "$P"
    else
      cat "$P"
    fi
    ;;
esac
"""

# $1=path.  Content is piped over stdin.  Refuses to clobber an existing path
# or create inside a missing parent directory, mirroring the local create tool.
_CREATE_SCRIPT = r"""
P=$1
if [ -e "$P" ]; then exit 24; fi
D=$(dirname "$P")
if [ ! -d "$D" ]; then exit 25; fi
cat > "$P"
"""

# $1=path $2=mode('read'|'write').  Used by ssh_edit for both the initial
# fetch and the write-back; requires the path to already exist as a regular
# file in both directions.
_EXISTING_FILE_SCRIPT = r"""
P=$1
MODE=$2
if [ ! -e "$P" ]; then exit 21; fi
if [ -d "$P" ]; then exit 23; fi
if [ ! -f "$P" ]; then exit 22; fi
if [ "$MODE" = "write" ]; then
  cat > "$P"
else
  cat "$P"
fi
"""

# args: [path <flags...> -- pattern].  Prefers GNU-compatible PCRE (-P) for
# closer parity with the regex flavor agents expect, falling back to POSIX
# ERE (-E) on remotes whose grep doesn't support -P (e.g. macOS/BSD grep).
_GREP_SCRIPT = r"""
P=$1
shift
if [ ! -e "$P" ]; then exit 21; fi
GREP=grep
if command -v ggrep >/dev/null 2>&1; then GREP=ggrep; fi
if printf 'x' | "$GREP" -P 'x' >/dev/null 2>&1; then FLAG=-P; else FLAG=-E; fi
exec "$GREP" -r -I -H "$FLAG" --exclude-dir=.git --exclude-dir=.hg --exclude-dir=.svn "$@" "$P"
"""

# $1=path (a directory).  Enumerates files for ssh_glob; pattern matching
# against the result happens locally via glob_to_regex().
_GLOB_SCRIPT = r"""
P=$1
if [ ! -e "$P" ]; then exit 21; fi
if [ ! -d "$P" ]; then exit 26; fi
cd "$P" || exit 26
find . -type d \( -name .git -o -name .hg -o -name .svn \) -prune -o -type f -print
"""


@dataclass
class ForwardEntry:
    forward_id: str
    target: str
    direction: str
    local_port: int
    remote_host: str
    remote_port: int
    bind_address: str
    process: subprocess.Popen[Any]
    argv: list[str]
    started_at: datetime

    def summary(self) -> dict[str, Any]:
        return_code = self.process.poll()
        exit_code, term_signal = _split_return_code(return_code)
        running = return_code is None
        now = utcnow()
        uptime_seconds = round(
            (now - self.started_at).total_seconds(), 1
        ) if running else None
        return {
            "forward_id": self.forward_id,
            "target": self.target,
            "direction": self.direction,
            "local_port": self.local_port,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "bind_address": self.bind_address,
            "running": running,
            "exit_code": exit_code,
            "signal": term_signal,
            "uptime_seconds": uptime_seconds,
        }


DEFAULT_FORWARD_STARTUP_WAIT = 0.5


class ForwardManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._forwards: dict[str, ForwardEntry] = {}

    def _allocate_forward_id(self) -> str:
        while True:
            forward_id = uuid.uuid4().hex[:12]
            if forward_id not in self._forwards:
                return forward_id

    def start(
        self,
        *,
        target: str,
        direction: str,
        local_port: int,
        remote_host: str,
        remote_port: int,
        bind_address: str,
        argv: list[str],
    ) -> ForwardEntry:
        with self._lock:
            forward_id = self._allocate_forward_id()
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            entry = ForwardEntry(
                forward_id=forward_id,
                target=target,
                direction=direction,
                local_port=local_port,
                remote_host=remote_host,
                remote_port=remote_port,
                bind_address=bind_address,
                process=process,
                argv=list(argv),
                started_at=utcnow(),
            )
            self._forwards[forward_id] = entry
        return entry

    def get(self, forward_id: str) -> ForwardEntry:
        if not isinstance(forward_id, str) or not forward_id.strip():
            raise ValidationError("'forward_id' must be a non-empty string.")
        with self._lock:
            entry = self._forwards.get(forward_id)
        if entry is None:
            raise ForwardNotFoundError(f"Unknown forward_id '{forward_id}'.")
        return entry

    def list_forwards(
        self,
        *,
        include_stopped: bool = False,
        target: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            entries = list(self._forwards.values())
        summaries = []
        for entry in entries:
            summary = entry.summary()
            if not include_stopped and not summary["running"]:
                continue
            if target is not None and summary["target"] != target:
                continue
            summaries.append(summary)
        return {"count": len(summaries), "forwards": summaries}

    def _close_pipes(self, entry: ForwardEntry) -> None:
        for pipe in (entry.process.stdout, entry.process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def stop(self, forward_id: str) -> dict[str, Any]:
        entry = self.get(forward_id)
        was_running = entry.process.poll() is None
        forced_kill = False
        termination_signal = None
        if was_running:
            termination_signal = "SIGTERM"
            forced_kill = _terminate_process_group(
                entry.process, grace_period=DEFAULT_SESSION_STOP_WAIT_SECONDS,
            )
            if forced_kill:
                termination_signal = "SIGKILL"
        self._close_pipes(entry)
        summary = entry.summary()
        summary["was_running"] = was_running
        summary["termination_signal"] = termination_signal
        summary["forced_kill"] = forced_kill
        return summary

    def close(self) -> None:
        with self._lock:
            entries = list(self._forwards.values())
        for entry in entries:
            try:
                if entry.process.poll() is None:
                    _terminate_process_group(entry.process, grace_period=0.5)
                self._close_pipes(entry)
            except Exception:
                continue


class SshSession:
    def __init__(
        self,
        *,
        session_id: str,
        session_name: str | None,
        target: str,
        argv: list[str],
        remote_command: str | None,
        process: subprocess.Popen[Any],
        master_fd: int,
        transcript_path: Path,
        observer_mode: str,
        tmux_binary: str | None,
        auto_close: bool = False,
    ) -> None:
        self.session_id = session_id
        self.session_name = session_name
        self.target = target
        self.argv = list(argv)
        self.ssh_command = shlex.join(argv)
        self.remote_command = remote_command
        self.process = process
        self._master_fd = master_fd
        self._master_closed = False
        self._auto_close = auto_close
        self._condition = threading.Condition()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._unread_output = ""
        self._unread_dropped_chars = 0
        self._total_output_chars = 0
        self._started_at = utcnow()
        self._ended_at: datetime | None = None
        self._last_output_mono: float | None = None
        self._eof = False
        self._transcript_path = transcript_path
        self._transcript_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._transcript_lock = threading.Lock()
        # Restrict transcript permissions — transcripts may contain secrets
        # (sudo passwords, API keys echoed during sessions).
        transcript_fd = os.open(
            str(self._transcript_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        self._transcript_file = os.fdopen(transcript_fd, "a", encoding="utf-8")
        self._configured_tmux_binary = tmux_binary
        self._observer_lock = threading.Lock()
        self._tmux_launch_lock = threading.Lock()
        self._observer_stopping = False
        observer_script = Path(__file__).with_name("observe.py").resolve()
        observer_command = shlex.join(
            [sys.executable or "python3", str(observer_script), str(self._transcript_path)]
        )
        self._observer = ObserverInfo(
            transcript_path=str(self._transcript_path),
            command=observer_command,
            requested_mode=observer_mode,
            mode="transcript",
            tmux_session_name=_sanitize_tmux_session_name(
                session_id, target=target, session_name=session_name,
            ),
        )
        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"ssh-session-{session_id}",
            daemon=True,
        )
        self._reader.start()

    def _update_process_status_locked(self) -> None:
        return_code = self.process.poll()
        if return_code is not None and self._ended_at is None:
            self._ended_at = utcnow()

    def _append_transcript(self, text: str) -> None:
        if not text:
            return
        with self._transcript_lock:
            self._transcript_file.write(text)
            self._transcript_file.flush()

    def _close_transcript(self) -> None:
        with self._transcript_lock:
            if self._transcript_file.closed:
                return
            self._transcript_file.close()

    def _start_tmux_observer(self) -> None:
        configured_tmux = self._configured_tmux_binary or os.environ.get("SSH_MCP_TMUX_BIN", "tmux")
        try:
            tmux_binary = _resolve_binary(configured_tmux, label="tmux client")
        except ValidationError as exc:
            with self._observer_lock:
                self._observer.warning = (
                    f"Requested tmux observer, but {exc}. The transcript observer is still available."
                )
            return
        with self._observer_lock:
            self._observer.tmux_binary = tmux_binary
            tmux_session_name = self._observer.tmux_session_name
        launch_argv = [
            tmux_binary,
            "new-session",
            "-d",
            "-s",
            tmux_session_name,
            self._observer.command,
        ]
        try:
            subprocess.run(
                launch_argv,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            suffix = f": {detail}" if detail else "."
            with self._observer_lock:
                self._observer.warning = (
                    "Failed to launch the tmux observer"
                    f"{suffix} The transcript observer is still available."
                )
            return
        except subprocess.TimeoutExpired:
            with self._observer_lock:
                self._observer.warning = (
                    "Timed out while launching the tmux observer. "
                    "The transcript observer is still available."
                )
            return
        except OSError as exc:
            with self._observer_lock:
                self._observer.warning = (
                    f"Failed to launch the tmux observer: {exc}. The transcript observer is still available."
                )
            return
        with self._observer_lock:
            self._observer.mode = "tmux"
            self._observer.tmux_started = True

    def ensure_observer_mode(self, observer_mode: str) -> None:
        if observer_mode != "tmux":
            return
        with self._observer_lock:
            if self._observer.tmux_started:
                return
        # Serialize launches: if another thread is launching, we block here
        # until it finishes, then the inner check sees tmux_started = True.
        with self._tmux_launch_lock:
            with self._observer_lock:
                if self._observer.tmux_started:
                    return
            self._start_tmux_observer()

    def _observer_snapshot(self) -> dict[str, Any]:
        with self._observer_lock:
            return self._observer.as_dict()

    def _reset_observer_to_transcript_locked(self) -> None:
        """Reset observer state to transcript-only. Must hold _observer_lock."""
        self._observer.mode = "transcript"
        self._observer.tmux_started = False
        self._observer.tmux_binary = None
        self._observer.tmux_session_name = None

    def _stop_tmux_observer(self) -> None:
        with self._observer_lock:
            if self._observer_stopping or not self._observer.tmux_started:
                return
            tmux_binary = self._observer.tmux_binary
            tmux_session_name = self._observer.tmux_session_name
            if tmux_binary is None or tmux_session_name is None:
                self._observer.mode = "transcript"
                self._observer.tmux_started = False
                self._observer.tmux_binary = None
                self._observer.tmux_session_name = None
                return
            self._observer_stopping = True
        try:
            completed = subprocess.run(
                [tmux_binary, "kill-session", "-t", tmux_session_name],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except OSError as exc:
            with self._observer_lock:
                self._observer.warning = (
                    f"Failed to close the tmux observer: {exc}. The transcript is still available at "
                    f"{self._transcript_path}."
                )
                self._reset_observer_to_transcript_locked()
                self._observer_stopping = False
            return
        except subprocess.TimeoutExpired:
            with self._observer_lock:
                self._observer.warning = (
                    "Timed out while closing the tmux observer. "
                    f"The transcript is still available at {self._transcript_path}."
                )
                self._reset_observer_to_transcript_locked()
                self._observer_stopping = False
            return
        stderr_text = (completed.stderr or "").strip()
        lowered_stderr = stderr_text.lower()
        session_missing = "can't find session" in lowered_stderr or "no server running" in lowered_stderr
        with self._observer_lock:
            self._observer_stopping = False
            if completed.returncode not in (0,) and not session_missing:
                suffix = f": {stderr_text}" if stderr_text else "."
                self._observer.warning = (
                    "Failed to close the tmux observer"
                    f"{suffix} The transcript is still available at {self._transcript_path}."
                )
                return
            self._reset_observer_to_transcript_locked()

    def _close_master_fd(self) -> None:
        with self._condition:
            if self._master_closed:
                return
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_closed = True

    def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    data = os.read(self._master_fd, 4096)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if not data:
                    break
                text = self._decoder.decode(data)
                if not text:
                    continue
                self._append_transcript(text)
                with self._condition:
                    self._append_unread_locked(text)
                    self._condition.notify_all()
        finally:
            final_text = self._decoder.decode(b"", final=True)
            self._append_transcript(final_text)
            with self._condition:
                if final_text:
                    self._append_unread_locked(final_text)
                self._eof = True
                self._update_process_status_locked()
                self._condition.notify_all()
            self._close_transcript()
            self._close_master_fd()
            self._stop_tmux_observer()

    def _append_unread_locked(self, text: str) -> None:
        """Append *text* to the in-memory unread buffer, enforcing the cap.

        Must be called with ``self._condition`` held.  Text is always counted
        toward ``_total_output_chars``; any excess beyond the cap is dropped
        from the *front* of the combined buffer so the most-recent output is
        preserved.  Dropped character counts are accumulated in
        ``_unread_dropped_chars`` so callers can surface them to clients.
        """
        self._total_output_chars += len(text)
        self._last_output_mono = time.monotonic()
        new_len = len(self._unread_output) + len(text)
        if new_len <= DEFAULT_UNREAD_BUFFER_CAP:
            self._unread_output += text
            return
        # Drop from the front to keep the buffer at the cap size.
        combined_overflow = new_len - DEFAULT_UNREAD_BUFFER_CAP
        drop_from_existing = min(combined_overflow, len(self._unread_output))
        drop_from_text = combined_overflow - drop_from_existing
        self._unread_dropped_chars += combined_overflow
        self._unread_output = self._unread_output[drop_from_existing:] + text[drop_from_text:]

    def _pop_output_locked(self, max_output_chars: int) -> tuple[str, bool]:
        if max_output_chars <= 0:
            return "", False
        if len(self._unread_output) <= max_output_chars:
            output = self._unread_output
            self._unread_output = ""
            return output, False
        output = self._unread_output[:max_output_chars]
        self._unread_output = self._unread_output[max_output_chars:]
        return output, True

    def _snapshot_locked(self, *, output: str, truncated: bool) -> dict[str, Any]:
        return_code = self.process.poll()
        exit_code, term_signal = _split_return_code(return_code)
        running = return_code is None
        now = utcnow()
        # Derive an agent-readable exit reason.
        exit_reason: str | None = None
        if not running:
            if term_signal is not None:
                exit_reason = _EXIT_SIGNAL_NAMES.get(term_signal, f"signal-{term_signal}")
            elif exit_code == 255:
                exit_reason = "ssh-connection-failed"
            elif exit_code == 0:
                exit_reason = "clean-exit"
            elif exit_code is not None:
                exit_reason = f"exit-{exit_code}"
        end = now if running else (self._ended_at or now)
        uptime_seconds = round((end - self._started_at).total_seconds(), 1)
        mono_now = time.monotonic()
        idle_seconds = (
            round(mono_now - self._last_output_mono, 1)
            if self._last_output_mono is not None
            else None
        )
        observer = self._observer_snapshot()
        result: dict[str, Any] = {
            "session_id": self.session_id,
            "session_name": self.session_name,
            "target": self.target,
            "running": running,
            "exit_code": exit_code,
            "signal": term_signal,
            "exit_reason": exit_reason,
            "output": output,
            "truncated": truncated,
            "pending_output_chars": len(self._unread_output),
            "uptime_seconds": uptime_seconds,
            "idle_seconds": idle_seconds,
            "observer": observer,
        }
        if observer.get("tmux_session_name"):
            result["tmux_session_name"] = observer["tmux_session_name"]
        if self._auto_close:
            result["auto_close"] = True
        return result

    def read(
        self,
        *,
        wait_seconds: float,
        max_output_chars: int,
        wait_for_new: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            self._update_process_status_locked()
            had_buffered_output = bool(self._unread_output) and not wait_for_new
            while True:
                self._update_process_status_locked()
                if had_buffered_output and self._unread_output:
                    break
                remaining = deadline - time.monotonic()
                # Break on timeout, EOF, or process exit (poll() may return
                # before the reader thread has set _eof).
                if remaining <= 0 or self._eof or self.process.poll() is not None:
                    break
                self._condition.wait(timeout=min(remaining, 0.1))
            output, truncated = self._pop_output_locked(max_output_chars)
            self._update_process_status_locked()
            return self._snapshot_locked(output=output, truncated=truncated)

    def write(self, *, input_text: str, wait_seconds: float, max_output_chars: int) -> dict[str, Any]:
        if not isinstance(input_text, str):
            raise ValidationError("'input' must be a string.")
        payload = input_text.encode("utf-8")
        # Validate session state under the lock, then snapshot the fd so we can
        # write *outside* the lock.  Holding _condition during os.write() would
        # deadlock with the reader thread when the PTY kernel buffer is full:
        # the reader needs the lock to drain output, but draining is what makes
        # space for the write to complete.
        with self._condition:
            self._update_process_status_locked()
            if self.process.poll() is not None:
                raise ValidationError(
                    f"Session '{self.session_id}' has already exited with return code {self.process.returncode}."
                )
            if self._master_closed:
                raise ValidationError(f"Session '{self.session_id}' is not writable anymore.")
            master_fd = self._master_fd
        bytes_written = 0
        try:
            buffer = memoryview(payload)
            while bytes_written < len(buffer):
                written = os.write(master_fd, buffer[bytes_written:])
                if written <= 0:
                    raise OSError(errno.EIO, "short write to PTY")
                bytes_written += written
        except OSError as exc:
            with self._condition:
                self._update_process_status_locked()
                closed = exc.errno in {errno.EBADF, errno.EIO} or self.process.poll() is not None or self._master_closed
            if closed:
                raise ValidationError(
                    f"Session '{self.session_id}' is no longer writable because it has already closed."
                ) from exc
            raise ValidationError(
                f"Failed to write to session '{self.session_id}': {exc.strerror or exc}"
            ) from exc
        result = self.read(
            wait_seconds=wait_seconds, max_output_chars=max_output_chars, wait_for_new=True,
        )
        result["bytes_written"] = bytes_written
        return result

    def stop(self, *, force: bool, wait_seconds: float, max_output_chars: int) -> dict[str, Any]:
        with self._condition:
            self._update_process_status_locked()
            was_running = self.process.poll() is None
        forced_kill = False
        termination_signal = None
        if was_running:
            termination_signal = "SIGKILL" if force else "SIGTERM"
            _signal_process_group(self.process, signal.SIGKILL if force else signal.SIGTERM)
            if not _wait_for_exit(self.process, wait_seconds):
                _signal_process_group(self.process, signal.SIGKILL)
                forced_kill = True
                _wait_for_exit(self.process, 1.0)
            with self._condition:
                self._update_process_status_locked()
                self._condition.notify_all()
        final_output = self.read(wait_seconds=min(wait_seconds, 0.25), max_output_chars=max_output_chars)
        if not final_output["running"]:
            self._reader.join(timeout=0.1)
        self._stop_tmux_observer()
        final_output["was_running"] = was_running
        final_output["termination_signal"] = termination_signal
        return final_output

    def summary(self) -> dict[str, Any]:
        with self._condition:
            self._update_process_status_locked()
            return self._snapshot_locked(output="", truncated=False)


class SessionManager:
    def __init__(self, *, state_dir: Path, tmux_binary: str | None = None) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SshSession] = {}
        self._state_dir = state_dir
        self._tmux_binary = tmux_binary
        self._last_prune_mono: float = 0.0

    def _prune_exited_locked(
        self,
        max_age_seconds: float = 3600,
        auto_close_age_seconds: float = 300,
    ) -> None:
        """Remove sessions that exited more than *max_age_seconds* ago.

        Sessions created with ``auto_close=True`` are pruned after the shorter
        *auto_close_age_seconds* (default 5 minutes).
        """
        mono = time.monotonic()
        if mono - self._last_prune_mono < 60:
            return
        self._last_prune_mono = mono
        now = utcnow()
        to_remove: list[str] = []
        for sid, session in self._sessions.items():
            if session._ended_at is not None:
                age = (now - session._ended_at).total_seconds()
                threshold = auto_close_age_seconds if session._auto_close else max_age_seconds
                if age > threshold:
                    to_remove.append(sid)
        for sid in to_remove:
            del self._sessions[sid]

    def _allocate_session_id(self) -> str:
        while True:
            session_id = uuid.uuid4().hex[:12]
            if session_id not in self._sessions:
                return session_id

    def _find_matching_running_sessions(
        self,
        *,
        target: str,
        session_name: str | None,
    ) -> list[SshSession]:
        sessions = list(self._sessions.values())
        matches: list[SshSession] = []
        for session in sessions:
            if session.process.poll() is not None or session.target != target:
                continue
            if session_name is not None and session.session_name != session_name:
                continue
            matches.append(session)
        return matches

    def start(
        self,
        *,
        session_name: str | None,
        target: str,
        argv: list[str],
        remote_command: str | None,
        observer_mode: str,
        auto_close: bool = False,
    ) -> SshSession:
        with self._lock:
            self._prune_exited_locked()
            if session_name is not None:
                duplicates = self._find_matching_running_sessions(target=target, session_name=session_name)
                if duplicates:
                    raise ValidationError(
                        f"A running session named '{session_name}' already exists for target '{target}'. "
                        "Use 'ssh_ensure_session' to reuse it or choose a different 'session_name'."
                    )
            session_id = self._allocate_session_id()
            session_dir = self._state_dir / session_id
            transcript_path = session_dir / "transcript.log"
            master_fd, slave_fd = os.openpty()
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                session = SshSession(
                    session_id=session_id,
                    session_name=session_name,
                    target=target,
                    argv=argv,
                    remote_command=remote_command,
                    process=process,
                    master_fd=master_fd,
                    transcript_path=transcript_path,
                    observer_mode=observer_mode,
                    tmux_binary=self._tmux_binary,
                    auto_close=auto_close,
                )
            except Exception:
                _terminate_process_group(process, grace_period=0.1)
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                raise
            self._sessions[session_id] = session
        session.ensure_observer_mode(observer_mode)
        return session

    def ensure(
        self,
        *,
        session_name: str | None,
        target: str,
        argv: list[str],
        remote_command: str | None,
        observer_mode: str,
        auto_close: bool = False,
    ) -> tuple[SshSession, bool, str | None]:
        with self._lock:
            self._prune_exited_locked()
            matches = self._find_matching_running_sessions(target=target, session_name=session_name)
            if len(matches) > 1:
                if session_name is None:
                    raise ValidationError(
                        f"Multiple running sessions already exist for target '{target}'. "
                        "Provide a stable 'session_name' or inspect them with 'ssh_list_sessions' first."
                    )
                raise ValidationError(
                    f"Multiple running sessions already exist for target '{target}' with session_name "
                    f"'{session_name}'. Stop duplicates or use 'ssh_list_sessions' to choose a specific session_id."
                )
            if matches:
                session = matches[0]
                reused = True
                matched_by = "session_name" if session_name is not None else "target"
            else:
                session = self.start(
                    session_name=session_name,
                    target=target,
                    argv=argv,
                    remote_command=remote_command,
                    observer_mode=observer_mode,
                    auto_close=auto_close,
                )
                reused = False
                matched_by = None
        if reused:
            session.ensure_observer_mode(observer_mode)
        return session, reused, matched_by

    def get(self, session_id: str) -> SshSession:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValidationError("'session_id' must be a non-empty string.")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Unknown session_id '{session_id}'.")
        return session

    def list_sessions(
        self,
        *,
        include_exited: bool,
        target: str | None = None,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune_exited_locked()
            sessions = list(self._sessions.values())
        summaries = []
        for session in sessions:
            summary = session.summary()
            if not include_exited and not summary["running"]:
                continue
            if target is not None and summary["target"] != target:
                continue
            if session_name is not None and summary["session_name"] != session_name:
                continue
            summaries.append(summary)
        return {"count": len(summaries), "sessions": summaries}

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                session.stop(force=True, wait_seconds=0.5, max_output_chars=1)
            except Exception:
                continue


class SshToolService:
    def __init__(
        self,
        *,
        ssh_binary: str | None = None,
        scp_binary: str | None = None,
        rsync_binary: str | None = None,
        tmux_binary: str | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self._configured_ssh_binary = ssh_binary or os.environ.get("SSH_MCP_SSH_BIN", "ssh")
        self._configured_scp_binary = scp_binary or os.environ.get("SSH_MCP_SCP_BIN", "scp")
        self._configured_rsync_binary = rsync_binary or os.environ.get("SSH_MCP_RSYNC_BIN", "rsync")
        self._configured_tmux_binary = tmux_binary or os.environ.get("SSH_MCP_TMUX_BIN", "tmux")
        self._state_dir = Path(state_dir).expanduser() if state_dir is not None else _default_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sessions = SessionManager(state_dir=self._state_dir, tmux_binary=self._configured_tmux_binary)
        self._forwards = ForwardManager()

    def close(self) -> None:
        self._forwards.close()
        self._sessions.close()

    def _run_remote_script(
        self,
        connection: ConnectionSettings,
        script: str,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        ssh_binary = _resolve_ssh_binary(self._configured_ssh_binary)
        remote_command = _build_script_command(script, args)
        argv = connection.build_argv(ssh_binary, remote_command, tty=False)
        return _run_without_pty_binary_safe(argv, timeout=timeout, input_text=input_text)

    def ssh_exec(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        command = _string_argument(validated, "command", required=True, strip=False)
        cwd = _string_argument(validated, "cwd")
        shell = _string_argument(validated, "shell")
        environment = _env_argument(validated)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)
        tty = _bool_argument(validated, "tty", default=False)
        ssh_binary = _resolve_ssh_binary(self._configured_ssh_binary)
        remote_command = build_exec_remote_command(
            command,
            cwd=cwd,
            environment=environment,
            shell=shell,
        )
        argv = connection.build_argv(ssh_binary, remote_command, tty=tty)
        result = _run_with_pty(argv, timeout=timeout) if tty else _run_without_pty(argv, timeout=timeout)
        result["ok"] = result["exit_code"] == 0 and not result["timed_out"]
        result["target"] = connection.target
        result["command"] = command
        return result

    def ssh_scp(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        direction = _string_argument(validated, "direction", required=True)
        if direction not in {"upload", "download"}:
            raise ValidationError("'direction' must be one of: upload, download.")
        sources = _string_list_argument(validated, "sources", required=True, strip=False, minimum_length=1)
        destination = _string_argument(validated, "destination", required=True, strip=False)
        recursive = _bool_argument(validated, "recursive", default=False)
        preserve_times = _bool_argument(validated, "preserve_times", default=False)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)
        scp_binary = _resolve_scp_binary(self._configured_scp_binary)
        argv = connection.build_transport_argv(scp_binary, port_flag="-P")
        if recursive:
            argv.append("-r")
        if preserve_times:
            argv.append("-p")
        if direction == "upload":
            normalized_sources = _normalize_local_sources(sources, recursive=recursive)
            normalized_destination = destination
            argv.extend(normalized_sources)
            argv.append(_build_remote_path(connection.target, normalized_destination))
        else:
            normalized_sources = sources
            normalized_destination = _normalize_local_destination(
                destination, require_directory=len(sources) > 1
            )
            argv.extend(_build_remote_path(connection.target, source) for source in normalized_sources)
            argv.append(normalized_destination)
        result = _run_without_pty(argv, timeout=timeout)
        result["ok"] = result["exit_code"] == 0 and not result["timed_out"]
        result["target"] = connection.target
        result["direction"] = direction
        return result

    def ssh_sync(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        direction = _string_argument(validated, "direction", required=True)
        if direction not in {"upload", "download"}:
            raise ValidationError("'direction' must be one of: upload, download.")
        source = _string_argument(validated, "source", required=True, strip=False)
        destination = _string_argument(validated, "destination", required=True, strip=False)
        delete = _bool_argument(validated, "delete", default=False)
        compress = _bool_argument(validated, "compress", default=True)
        dry_run = _bool_argument(validated, "dry_run", default=False)
        exclude = _exclude_argument(validated)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)
        extra_rsync_args = _extra_rsync_args_argument(validated)
        ssh_binary = _resolve_ssh_binary(self._configured_ssh_binary)
        rsync_binary = _resolve_rsync_binary(self._configured_rsync_binary)
        ssh_transport_argv = connection.build_transport_argv(ssh_binary, port_flag="-p")
        argv = [rsync_binary, "-a"]
        if compress:
            argv.append("-z")
        if delete:
            argv.append("--delete")
        if dry_run:
            argv.append("--dry-run")
        for pattern in exclude:
            argv.extend(["--exclude", pattern])
        argv.extend(extra_rsync_args)
        argv.extend(["-e", shlex.join(ssh_transport_argv)])
        if direction == "upload":
            normalized_source = _normalize_local_sources([source], recursive=True)[0]
            normalized_destination = destination
            argv.extend([normalized_source, _build_remote_path(connection.target, normalized_destination)])
        else:
            normalized_source = source
            normalized_destination = _normalize_local_destination(destination)
            argv.extend([_build_remote_path(connection.target, normalized_source), normalized_destination])
        result = _run_without_pty(argv, timeout=timeout)
        result["ok"] = result["exit_code"] == 0 and not result["timed_out"]
        result["target"] = connection.target
        result["direction"] = direction
        return result

    def ssh_view(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        path = _string_argument(validated, "path", required=True)
        force_full = _bool_argument(validated, "force_read_large_files", default=False)
        max_bytes = _int_argument(
            validated, "max_bytes", default=DEFAULT_VIEW_MAX_BYTES, minimum=1, allow_zero=False
        )
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)
        view_range = validated.get("view_range")

        start_line: int | None = None
        end_line: int | None = None
        shielded_path = _shield_leading_dash(path)
        if view_range is not None:
            start_line, end_line = _parse_view_range(view_range)
            mode = "range"
            script_args = [shielded_path, mode, str(start_line), str(end_line), ""]
        elif force_full:
            mode = "full"
            script_args = [shielded_path, mode, "", "", ""]
        else:
            mode = "auto"
            script_args = [shielded_path, mode, "", "", str(max_bytes)]

        result = self._run_remote_script(connection, _VIEW_SCRIPT, script_args, timeout=timeout)
        error = _remote_file_error_for_exit(
            result["exit_code"], path, timed_out=result["timed_out"], stderr=result["stderr"]
        )
        if error is not None:
            raise error

        stdout = result["stdout"]
        separator = stdout.find("\x00")
        header, payload = (stdout, "") if separator == -1 else (stdout[:separator], stdout[separator + 1 :])

        if header == "DIR":
            entries: list[dict[str, str]] = []
            for line in payload.split("\n"):
                if not line:
                    continue
                kind, _, entry_path = line.partition("\t")
                entries.append({"path": entry_path, "type": "directory" if kind == "d" else "file"})
            return {
                "ok": True,
                "target": connection.target,
                "path": path,
                "is_directory": True,
                "entries": entries,
            }

        header_parts = header.split("\t")
        if len(header_parts) != 3 or header_parts[0] != "FILE":
            raise RemoteFileError(f"Unexpected response reading remote file: {path}")
        size_bytes = int(header_parts[1])
        total_lines = int(header_parts[2])
        truncated = mode == "auto" and size_bytes > max_bytes
        response: dict[str, Any] = {
            "ok": True,
            "target": connection.target,
            "path": path,
            "is_directory": False,
            "content": payload,
            "size_bytes": size_bytes,
            "total_lines": total_lines,
            "truncated": truncated,
        }
        if start_line is not None:
            response["start_line"] = start_line
            response["end_line"] = end_line
        return response

    def ssh_create(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        path = _string_argument(validated, "path", required=True)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValidationError("'content' is required and must be a string.")
        if "\x00" in content:
            raise ValidationError("'content' cannot contain NUL bytes.")
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)

        result = self._run_remote_script(
            connection, _CREATE_SCRIPT, [_shield_leading_dash(path)], input_text=content, timeout=timeout
        )
        error = _remote_file_error_for_exit(
            result["exit_code"], path, timed_out=result["timed_out"], stderr=result["stderr"]
        )
        if error is not None:
            raise error
        return {
            "ok": True,
            "target": connection.target,
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
        }

    def ssh_edit(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        path = _string_argument(validated, "path", required=True)
        edits = _edits_argument(validated)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)
        shielded_path = _shield_leading_dash(path)

        read_result = self._run_remote_script(
            connection, _EXISTING_FILE_SCRIPT, [shielded_path, "read"], timeout=timeout
        )
        error = _remote_file_error_for_exit(
            read_result["exit_code"], path, timed_out=read_result["timed_out"], stderr=read_result["stderr"]
        )
        if error is not None:
            raise error

        updated_content = _apply_edits(read_result["stdout"], edits)

        write_result = self._run_remote_script(
            connection,
            _EXISTING_FILE_SCRIPT,
            [shielded_path, "write"],
            input_text=updated_content,
            timeout=timeout,
        )
        error = _remote_file_error_for_exit(
            write_result["exit_code"], path, timed_out=write_result["timed_out"], stderr=write_result["stderr"]
        )
        if error is not None:
            raise error

        return {
            "ok": True,
            "target": connection.target,
            "path": path,
            "edits_applied": len(edits),
            "bytes_written": len(updated_content.encode("utf-8")),
        }

    def ssh_grep(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        pattern = _string_argument(validated, "pattern", required=True, strip=False)
        path = _string_argument(validated, "path", strip=False) or "."
        glob_filter = _string_argument(validated, "glob", strip=False)
        case_insensitive = _bool_argument(validated, "case_insensitive", default=False)
        output_mode = _string_argument(validated, "output_mode") or "files_with_matches"
        if output_mode not in _ALLOWED_GREP_OUTPUT_MODES:
            allowed = ", ".join(sorted(_ALLOWED_GREP_OUTPUT_MODES))
            raise ValidationError(f"'output_mode' must be one of: {allowed}.")
        context = _int_argument(validated, "context", default=None, minimum=0)
        context_before = _int_argument(validated, "context_before", default=0, minimum=0)
        context_after = _int_argument(validated, "context_after", default=0, minimum=0)
        if context is not None:
            context_before = context_after = context
        if (context_before or context_after) and output_mode != "content":
            raise ValidationError(
                "'context'/'context_before'/'context_after' only apply when output_mode is 'content'."
            )
        head_limit = _int_argument(validated, "head_limit", default=None, minimum=1, allow_zero=False)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)

        mode_flags: list[str] = []
        if output_mode == "files_with_matches":
            mode_flags.append("-l")
        elif output_mode == "count":
            mode_flags.append("-c")
        else:
            mode_flags.append("-n")
            if context_before and context_before == context_after:
                mode_flags.extend(["-C", str(context_before)])
            else:
                if context_before:
                    mode_flags.extend(["-B", str(context_before)])
                if context_after:
                    mode_flags.extend(["-A", str(context_after)])
        case_flags = ["-i"] if case_insensitive else []
        include_flags: list[str] = []
        if glob_filter:
            for variant in _expand_braces(glob_filter):
                include_flags.extend(["--include", variant])

        script_args = [_shield_leading_dash(path), *mode_flags, *case_flags, *include_flags, "--", pattern]
        result = self._run_remote_script(connection, _GREP_SCRIPT, script_args, timeout=timeout)

        exit_code = result["exit_code"]
        if result["timed_out"]:
            raise RemoteFileError(f"Remote search under '{path}' timed out.")
        if exit_code == _EXIT_NOT_FOUND:
            raise RemoteFileError(f"Remote path does not exist: {path}")
        if exit_code is not None and exit_code > 1:
            message = result["stderr"].strip() or f"grep exited with code {exit_code}."
            raise RemoteFileError(f"Remote search under '{path}' failed: {message}")

        lines = result["stdout"].split("\n")
        if lines and lines[-1] == "":
            lines.pop()

        matches: list[Any] = []
        if output_mode == "files_with_matches":
            matches = list(lines)
        elif output_mode == "count":
            for line in lines:
                file_path, _, count_str = line.rpartition(":")
                if not file_path:
                    continue
                try:
                    count = int(count_str)
                except ValueError:
                    continue
                if count > 0:
                    matches.append({"path": file_path, "count": count})
        else:
            for line in lines:
                found = _GREP_MATCH_LINE_RE.match(line)
                if found is None:
                    # Group separator lines (e.g. '--' between context blocks).
                    continue
                entry: dict[str, Any] = {
                    "path": found.group("path"),
                    "line_number": int(found.group("line")),
                    "line": found.group("text"),
                }
                if found.group("sep") == "-":
                    entry["is_context"] = True
                matches.append(entry)

        truncated = False
        if head_limit is not None and len(matches) > head_limit:
            matches = matches[:head_limit]
            truncated = True

        return {
            "ok": True,
            "target": connection.target,
            "pattern": pattern,
            "path": path,
            "output_mode": output_mode,
            "matches": matches,
            "truncated": truncated,
        }

    def ssh_glob(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        pattern = _string_argument(validated, "pattern", required=True, strip=False)
        path = _string_argument(validated, "path", strip=False) or "."
        head_limit = _int_argument(validated, "head_limit", default=None, minimum=1, allow_zero=False)
        timeout = _float_argument(validated, "timeout", default=None, minimum=0.0, allow_zero=False)

        result = self._run_remote_script(
            connection, _GLOB_SCRIPT, [_shield_leading_dash(path)], timeout=timeout
        )
        error = _remote_file_error_for_exit(
            result["exit_code"], path, timed_out=result["timed_out"], stderr=result["stderr"]
        )
        if error is not None:
            raise error

        regex = glob_to_regex(pattern)
        candidates = (
            line[2:] if line.startswith("./") else line
            for line in result["stdout"].split("\n")
            if line
        )
        matched = sorted(candidate for candidate in candidates if regex.fullmatch(candidate))

        truncated = False
        if head_limit is not None and len(matched) > head_limit:
            matched = matched[:head_limit]
            truncated = True

        return {
            "ok": True,
            "target": connection.target,
            "path": path,
            "pattern": pattern,
            "matches": matched,
            "truncated": truncated,
        }

    def _parse_session_arguments(self, arguments: Mapping[str, Any]) -> tuple[
        Mapping[str, Any], ConnectionSettings, str | None, str, bool,
        float, int, list[str], str | None,
    ]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        session_name = _string_argument(validated, "session_name")
        cwd = _string_argument(validated, "cwd")
        shell = _string_argument(validated, "shell")
        environment = _env_argument(validated)
        observer_mode = _observer_mode_argument(validated)
        auto_close = _bool_argument(validated, "auto_close", default=False)
        wait_seconds = _float_argument(
            validated,
            "wait_seconds",
            default=DEFAULT_SESSION_START_WAIT_SECONDS,
            minimum=0.0,
        )
        max_output_chars = _int_argument(
            validated,
            "max_output_chars",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=1,
            allow_zero=False,
        )
        ssh_binary = _resolve_ssh_binary(self._configured_ssh_binary)
        remote_command = build_session_remote_command(cwd=cwd, environment=environment, shell=shell)
        argv = connection.build_argv(ssh_binary, remote_command, tty=True, keepalive=True)
        return (
            validated, connection, session_name, observer_mode, auto_close,
            wait_seconds, max_output_chars, argv, remote_command,
        )

    def ssh_start_session(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        (_, connection, session_name, observer_mode, auto_close,
         wait_seconds, max_output_chars, argv, remote_command) = self._parse_session_arguments(arguments)
        session = self._sessions.start(
            session_name=session_name,
            target=connection.target,
            argv=argv,
            remote_command=remote_command,
            observer_mode=observer_mode,
            auto_close=auto_close,
        )
        result = session.read(wait_seconds=wait_seconds, max_output_chars=max_output_chars)
        return result

    def ssh_ensure_session(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        (_, connection, session_name, observer_mode, auto_close,
         wait_seconds, max_output_chars, argv, remote_command) = self._parse_session_arguments(arguments)
        session, reused, matched_by = self._sessions.ensure(
            session_name=session_name,
            target=connection.target,
            argv=argv,
            remote_command=remote_command,
            observer_mode=observer_mode,
            auto_close=auto_close,
        )
        result = session.read(wait_seconds=wait_seconds, max_output_chars=max_output_chars)
        result["created"] = not reused
        result["reused"] = reused
        result["matched_by"] = matched_by
        return result

    def ssh_read_session(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        session_id = _string_argument(validated, "session_id", required=True)
        wait_seconds = _float_argument(
            validated, "wait_seconds", default=DEFAULT_SESSION_READ_WAIT_SECONDS, minimum=0.0,
        )
        max_output_chars = _int_argument(
            validated,
            "max_output_chars",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=1,
            allow_zero=False,
        )
        session = self._sessions.get(session_id)
        return session.read(wait_seconds=wait_seconds, max_output_chars=max_output_chars)

    def ssh_write_session(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        session_id = _string_argument(validated, "session_id", required=True)
        input_text = arguments.get("input")
        if not isinstance(input_text, str):
            raise ValidationError("'input' is required and must be a string.")
        wait_seconds = _float_argument(
            validated, "wait_seconds", default=DEFAULT_SESSION_WRITE_WAIT_SECONDS, minimum=0.0,
        )
        max_output_chars = _int_argument(
            validated,
            "max_output_chars",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=1,
            allow_zero=False,
        )
        session = self._sessions.get(session_id)
        return session.write(
            input_text=input_text,
            wait_seconds=wait_seconds,
            max_output_chars=max_output_chars,
        )

    def ssh_stop_session(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        session_id = _string_argument(validated, "session_id", required=True)
        force = _bool_argument(validated, "force", default=False)
        wait_seconds = _float_argument(
            validated,
            "wait_seconds",
            default=DEFAULT_SESSION_STOP_WAIT_SECONDS,
            minimum=0.0,
        )
        max_output_chars = _int_argument(
            validated,
            "max_output_chars",
            default=DEFAULT_MAX_OUTPUT_CHARS,
            minimum=1,
            allow_zero=False,
        )
        session = self._sessions.get(session_id)
        return session.stop(force=force, wait_seconds=wait_seconds, max_output_chars=max_output_chars)

    def ssh_list_sessions(self, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        validated = _validate_arguments(arguments or {})
        include_exited = _bool_argument(validated, "include_exited", default=True)
        target = _string_argument(validated, "target")
        session_name = _string_argument(validated, "session_name")
        return self._sessions.list_sessions(
            include_exited=include_exited,
            target=target,
            session_name=session_name,
        )

    def ssh_forward(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        connection = ConnectionSettings.from_arguments(validated)
        direction = _string_argument(validated, "direction", required=True)
        if direction not in {"local", "remote"}:
            raise ValidationError("'direction' must be one of: local, remote.")
        local_port = _int_argument(validated, "local_port", minimum=1, allow_zero=False)
        if local_port is None:
            raise ValidationError("'local_port' is required.")
        if local_port > 65535:
            raise ValidationError("'local_port' must be <= 65535.")
        remote_host = _string_argument(validated, "remote_host", required=True)
        remote_port = _int_argument(validated, "remote_port", minimum=1, allow_zero=False)
        if remote_port is None:
            raise ValidationError("'remote_port' is required.")
        if remote_port > 65535:
            raise ValidationError("'remote_port' must be <= 65535.")
        bind_address = _string_argument(validated, "bind_address") or "127.0.0.1"
        ssh_binary = _resolve_ssh_binary(self._configured_ssh_binary)
        argv = connection.build_argv(ssh_binary, None, tty=False, keepalive=True)
        target_index = argv.index(connection.target)
        forward_spec = f"{bind_address}:{local_port}:{remote_host}:{remote_port}"
        flag = "-L" if direction == "local" else "-R"
        argv[target_index:target_index] = ["-N", flag, forward_spec]
        entry = self._forwards.start(
            target=connection.target,
            direction=direction,
            local_port=local_port,
            remote_host=remote_host,
            remote_port=remote_port,
            bind_address=bind_address,
            argv=argv,
        )
        # Wait briefly to detect immediate SSH failures.
        time.sleep(DEFAULT_FORWARD_STARTUP_WAIT)
        result = entry.summary()
        if not result["running"]:
            try:
                _, stderr_bytes = entry.process.communicate(timeout=0.1)
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            except Exception:
                stderr = ""
            if stderr:
                result["stderr"] = stderr
        return result

    def ssh_list_forwards(self, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        validated = _validate_arguments(arguments or {})
        include_stopped = _bool_argument(validated, "include_stopped", default=False)
        target = _string_argument(validated, "target")
        return self._forwards.list_forwards(include_stopped=include_stopped, target=target)

    def ssh_stop_forward(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_arguments(arguments)
        forward_id = _string_argument(validated, "forward_id", required=True)
        return self._forwards.stop(forward_id)
