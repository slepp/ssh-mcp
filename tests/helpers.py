from __future__ import annotations

import textwrap
from pathlib import Path


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(0o755)
    return path


def create_fake_ssh(directory: Path) -> Path:
    fake_ssh = directory / "ssh"
    return _write_executable(
        fake_ssh,
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys

        VALUE_OPTIONS = {"-p", "-i", "-o", "-F"}

        def parse_arguments(argv):
            index = 0
            target = None
            command_parts = []
            while index < len(argv):
                arg = argv[index]
                if target is None and arg in VALUE_OPTIONS:
                    index += 2
                    continue
                if target is None and arg in {"-tt", "-T"}:
                    index += 1
                    continue
                if target is None and arg.startswith("-"):
                    if arg[:2] in VALUE_OPTIONS and len(arg) > 2:
                        index += 1
                        continue
                    index += 1
                    continue
                target = arg
                command_parts = argv[index + 1 :]
                break
            return target, command_parts

        target, command_parts = parse_arguments(sys.argv[1:])
        if target is None:
            print("fake ssh: missing target", file=sys.stderr)
            raise SystemExit(255)

        log_path = os.environ.get("FAKE_SSH_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "argv": sys.argv[1:],
                            "target": target,
                            "command": command_parts[0] if command_parts else None,
                        }
                    )
                )
                handle.write("\\n")

        environment = dict(os.environ)
        environment.setdefault("PS1", "")
        environment["ENV"] = ""
        environment.pop("BASH_ENV", None)
        environment.pop("PROMPT_COMMAND", None)

        if command_parts:
            os.execve("/bin/sh", ["/bin/sh", "-c", command_parts[0]], environment)
        os.execve("/bin/sh", ["/bin/sh", "-i"], environment)
        """,
    )


def create_fake_tmux(directory: Path) -> Path:
    fake_tmux = directory / "tmux"
    return _write_executable(
        fake_tmux,
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys

        log_path = os.environ.get("FAKE_TMUX_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"argv": sys.argv[1:]}))
                handle.write("\\n")

        raise SystemExit(0)
        """,
    )


def create_fake_scp(directory: Path) -> Path:
    fake_scp = directory / "scp"
    return _write_executable(
        fake_scp,
        """\
        #!/usr/bin/env python3
        import json
        import os
        import pathlib
        import shlex
        import shutil
        import sys

        VALUE_OPTIONS = {"-P", "-i", "-o", "-F"}

        def parse_remote(spec):
            if ":" not in spec:
                return None
            target, path = spec.split(":", 1)
            if not target or "/" in target:
                return None
            parts = shlex.split(path)
            resolved_path = parts[0] if parts else path
            return target, resolved_path

        def resolve_path(spec):
            remote = parse_remote(spec)
            if remote is None:
                return pathlib.Path(spec), False
            _, path = remote
            return pathlib.Path(path), True

        def copy_one(source, destination, recursive):
            if source.is_dir():
                if not recursive:
                    print(f"fake scp: omitting directory {source}", file=sys.stderr)
                    raise SystemExit(1)
                if destination.exists() and destination.is_dir():
                    target_path = destination / source.name
                else:
                    target_path = destination
                shutil.copytree(source, target_path, dirs_exist_ok=True, copy_function=shutil.copy2)
                return
            if destination.exists() and destination.is_dir():
                target_path = destination / source.name
            else:
                target_path = destination
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_path)

        argv = sys.argv[1:]
        index = 0
        recursive = False
        while index < len(argv):
            arg = argv[index]
            if arg == "-r":
                recursive = True
                index += 1
                continue
            if arg == "-p":
                index += 1
                continue
            if arg in VALUE_OPTIONS:
                index += 2
                continue
            if arg.startswith("-") and any(arg.startswith(prefix) for prefix in VALUE_OPTIONS):
                index += 1
                continue
            break
        operands = argv[index:]
        if len(operands) < 2:
            print("fake scp: expected source(s) and destination", file=sys.stderr)
            raise SystemExit(1)

        log_path = os.environ.get("FAKE_SCP_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"argv": sys.argv[1:]}))
                handle.write("\\n")

        sources = operands[:-1]
        destination, _ = resolve_path(operands[-1])
        if len(sources) > 1:
            destination.mkdir(parents=True, exist_ok=True)
        for source_spec in sources:
            source, _ = resolve_path(source_spec)
            copy_one(source, destination, recursive)
        raise SystemExit(0)
        """,
    )


def create_fake_rsync(directory: Path) -> Path:
    fake_rsync = directory / "rsync"
    return _write_executable(
        fake_rsync,
        """\
        #!/usr/bin/env python3
        import fnmatch
        import json
        import os
        import pathlib
        import shlex
        import shutil
        import sys

        def parse_remote(spec):
            if ":" not in spec:
                return None
            target, path = spec.split(":", 1)
            if not target or "/" in target:
                return None
            parts = shlex.split(path)
            resolved_path = parts[0] if parts else path
            return target, resolved_path

        def resolve_path(spec):
            remote = parse_remote(spec)
            if remote is None:
                return pathlib.Path(spec), False
            _, path = remote
            return pathlib.Path(path), True

        def is_excluded(relative_path, patterns):
            return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)

        argv = sys.argv[1:]
        delete = False
        dry_run = False
        excludes = []
        index = 0
        while index < len(argv):
            arg = argv[index]
            if arg in {"-a", "-z"}:
                index += 1
                continue
            if arg == "--delete":
                delete = True
                index += 1
                continue
            if arg == "--dry-run":
                dry_run = True
                index += 1
                continue
            if arg == "--exclude":
                excludes.append(argv[index + 1])
                index += 2
                continue
            if arg.startswith("--exclude="):
                excludes.append(arg.split("=", 1)[1])
                index += 1
                continue
            if arg == "-e":
                index += 2
                continue
            if arg.startswith("-"):
                index += 1
                continue
            break

        operands = argv[index:]
        if len(operands) != 2:
            print("fake rsync: expected source and destination", file=sys.stderr)
            raise SystemExit(1)

        log_path = os.environ.get("FAKE_RSYNC_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"argv": sys.argv[1:]}))
                handle.write("\\n")

        raw_source, raw_destination = operands
        source, _ = resolve_path(raw_source)
        destination, _ = resolve_path(raw_destination)
        source_had_trailing_slash = raw_source.endswith("/")

        if dry_run:
            print("dry-run")
            raise SystemExit(0)

        if source.is_dir():
            if source_had_trailing_slash:
                source_root = source
                destination_root = destination
            else:
                source_root = source.parent
                destination_root = destination
            destination_root.mkdir(parents=True, exist_ok=True)
            copied = set()
            for current_root, dirnames, filenames in os.walk(source):
                current_path = pathlib.Path(current_root)
                relative_root = current_path.relative_to(source_root)
                if str(relative_root) != "." and is_excluded(str(relative_root), excludes):
                    dirnames[:] = []
                    continue
                filtered_dirnames = []
                for dirname in dirnames:
                    relative_dir = current_path.joinpath(dirname).relative_to(source_root)
                    if not is_excluded(str(relative_dir), excludes):
                        filtered_dirnames.append(dirname)
                dirnames[:] = filtered_dirnames
                for filename in filenames:
                    source_file = current_path / filename
                    relative_file = source_file.relative_to(source_root)
                    if is_excluded(str(relative_file), excludes):
                        continue
                    destination_file = destination_root / relative_file
                    destination_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, destination_file)
                    copied.add(str(relative_file))
                    print(relative_file)
            if delete and destination_root.exists():
                for current_root, dirnames, filenames in os.walk(destination_root, topdown=False):
                    current_path = pathlib.Path(current_root)
                    for filename in filenames:
                        destination_file = current_path / filename
                        relative_file = destination_file.relative_to(destination_root)
                        if str(relative_file) not in copied and not is_excluded(str(relative_file), excludes):
                            destination_file.unlink()
                    for dirname in dirnames:
                        destination_dir = current_path / dirname
                        relative_dir = destination_dir.relative_to(destination_root)
                        if is_excluded(str(relative_dir), excludes):
                            continue
                        if destination_dir.exists() and not any(destination_dir.iterdir()):
                            destination_dir.rmdir()
        else:
            if destination.exists() and destination.is_dir():
                destination_file = destination / source.name
            else:
                destination_file = destination
                destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination_file)
            print(destination_file.name)
        raise SystemExit(0)
        """,
    )
