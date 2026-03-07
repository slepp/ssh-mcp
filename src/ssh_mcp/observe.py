from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import NoReturn


def follow_file(path: Path, *, poll_interval: float = 0.1) -> NoReturn:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = handle.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                continue
            time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Follow an ssh-mcp session transcript.")
    parser.add_argument("path", help="Transcript file path to follow.")
    args = parser.parse_args(argv)
    transcript_path = Path(args.path).expanduser()
    if not transcript_path.exists():
        parser.error(f"Transcript file does not exist: {transcript_path}")
    try:
        return follow_file(transcript_path)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
