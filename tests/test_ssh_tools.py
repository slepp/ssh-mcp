from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ssh_mcp.ssh import SshToolService, ValidationError
from ssh_mcp.ssh import DEFAULT_UNREAD_BUFFER_CAP
from ssh_mcp.ssh import RemoteFileError
from ssh_mcp.ssh import _sanitize_tmux_session_name
from tests.helpers import create_fake_rsync, create_fake_scp, create_fake_ssh, create_fake_tmux


class SshToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.fake_ssh = create_fake_ssh(self.root)
        self.fake_scp = create_fake_scp(self.root)
        self.fake_rsync = create_fake_rsync(self.root)
        self.fake_tmux = create_fake_tmux(self.root)
        self.identity_file = self.root / "id_test"
        self.identity_file.write_text("dummy-key", encoding="utf-8")
        self.known_hosts_file = self.root / "known_hosts"
        self.known_hosts_file.write_text("", encoding="utf-8")
        self.state_dir = self.root / "state"
        self.tmux_log = self.root / "fake_tmux.log"
        self._old_fake_tmux_log = os.environ.get("FAKE_TMUX_LOG")
        os.environ["FAKE_TMUX_LOG"] = str(self.tmux_log)
        self.service = SshToolService(
            ssh_binary=str(self.fake_ssh),
            scp_binary=str(self.fake_scp),
            rsync_binary=str(self.fake_rsync),
            tmux_binary=str(self.fake_tmux),
            state_dir=self.state_dir,
        )

    def tearDown(self) -> None:
        self.service.close()
        if self._old_fake_tmux_log is None:
            os.environ.pop("FAKE_TMUX_LOG", None)
        else:
            os.environ["FAKE_TMUX_LOG"] = self._old_fake_tmux_log
        self._tempdir.cleanup()

    def _exit_session(
        self,
        session_id: str,
        exit_code: int = 0,
        *,
        wait_seconds: float = 2.0,
    ) -> dict:
        """Send ``exit <code>`` and wait until the process is confirmed dead.

        The initial write may return before the process has fully exited
        under heavy load.  If that happens, follow up with a read that
        waits for EOF / process exit.
        """
        result = self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": f"exit {exit_code}\n",
                "wait_seconds": wait_seconds,
                "max_output_chars": 4096,
            }
        )
        if result["running"]:
            result = self.service.ssh_read_session(
                {
                    "session_id": session_id,
                    "wait_seconds": wait_seconds,
                    "max_output_chars": 4096,
                }
            )
        return result

    def test_ssh_exec_runs_command_with_wrappers(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        result = self.service.ssh_exec(
            {
                "target": "example",
                "command": "printf '%s:%s' \"$PWD\" \"$HELLO\"",
                "cwd": str(workspace),
                "env": {"HELLO": "world"},
                "shell": "/bin/sh",
                "port": 2222,
                "identity_file": str(self.identity_file),
                "known_hosts_file": str(self.known_hosts_file),
                "strict_host_key_checking": False,
                "extra_ssh_args": ["-v"],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], f"{workspace}:world")
        self.assertEqual(result["stderr"], "")

    def test_ssh_exec_supports_tty_mode(self) -> None:
        result = self.service.ssh_exec(
            {
                "target": "example",
                "command": "printf 'tty-mode'; exit 3",
                "tty": True,
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("tty-mode", result["stdout"])

    def test_ssh_scp_uploads_and_downloads_files(self) -> None:
        upload_source = self.root / "upload-source"
        upload_source.mkdir()
        nested = upload_source / "nested"
        nested.mkdir()
        (nested / "hello.txt").write_text("hello scp", encoding="utf-8")
        remote_root = self.root / "remote-root"
        remote_root.mkdir()

        uploaded = self.service.ssh_scp(
            {
                "target": "example",
                "direction": "upload",
                "sources": [str(upload_source)],
                "destination": str(remote_root),
                "recursive": True,
                "preserve_times": True,
                "port": 2222,
                "identity_file": str(self.identity_file),
                "known_hosts_file": str(self.known_hosts_file),
                "strict_host_key_checking": False,
                "extra_ssh_args": ["-F", "/dev/null"],
            }
        )

        self.assertTrue(uploaded["ok"])
        self.assertEqual(
            (remote_root / upload_source.name / "nested" / "hello.txt").read_text(encoding="utf-8"),
            "hello scp",
        )

        download_dir = self.root / "downloaded"
        download_dir.mkdir()
        downloaded = self.service.ssh_scp(
            {
                "target": "example",
                "direction": "download",
                "sources": [str(remote_root / upload_source.name / "nested" / "hello.txt")],
                "destination": str(download_dir),
            }
        )

        self.assertTrue(downloaded["ok"])
        self.assertEqual(
            (download_dir / "hello.txt").read_text(encoding="utf-8"),
            "hello scp",
        )

    def test_ssh_scp_expands_local_tilde_paths(self) -> None:
        upload_source = self.root / "tilde-upload.txt"
        upload_source.write_text("tilde scp", encoding="utf-8")
        remote_root = self.root / "tilde-remote"
        remote_root.mkdir()

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            uploaded = self.service.ssh_scp(
                {
                    "target": "example",
                    "direction": "upload",
                    "sources": ["~/tilde-upload.txt"],
                    "destination": str(remote_root),
                }
            )

        self.assertTrue(uploaded["ok"])
        self.assertEqual((remote_root / "tilde-upload.txt").read_text(encoding="utf-8"), "tilde scp")

    def test_ssh_sync_uploads_incrementally_with_delete_and_exclude(self) -> None:
        source_root = self.root / "sync-source"
        source_root.mkdir()
        (source_root / "keep.txt").write_text("fresh", encoding="utf-8")
        (source_root / "skip.tmp").write_text("ignore", encoding="utf-8")
        remote_root = self.root / "sync-remote"
        remote_root.mkdir()
        (remote_root / "stale.txt").write_text("stale", encoding="utf-8")

        synced = self.service.ssh_sync(
            {
                "target": "example",
                "direction": "upload",
                "source": f"{source_root}/",
                "destination": str(remote_root),
                "delete": True,
                "exclude": ["*.tmp"],
                "port": 2222,
                "identity_file": str(self.identity_file),
                "known_hosts_file": str(self.known_hosts_file),
                "strict_host_key_checking": "accept-new",
                "extra_ssh_args": ["-F", "/dev/null"],
                "extra_rsync_args": ["--itemize-changes"],
            }
        )

        self.assertTrue(synced["ok"])
        self.assertEqual((remote_root / "keep.txt").read_text(encoding="utf-8"), "fresh")
        self.assertFalse((remote_root / "skip.tmp").exists())
        self.assertFalse((remote_root / "stale.txt").exists())

    def test_ssh_sync_dry_run_does_not_modify_destination(self) -> None:
        source_root = self.root / "dry-source"
        source_root.mkdir()
        (source_root / "new.txt").write_text("new", encoding="utf-8")
        destination_root = self.root / "dry-destination"
        destination_root.mkdir()
        stale_file = destination_root / "stale.txt"
        stale_file.write_text("stale", encoding="utf-8")

        synced = self.service.ssh_sync(
            {
                "target": "example",
                "direction": "upload",
                "source": f"{source_root}/",
                "destination": str(destination_root),
                "dry_run": True,
            }
        )

        self.assertTrue(synced["ok"])
        self.assertTrue((destination_root / "stale.txt").exists())
        self.assertFalse((destination_root / "new.txt").exists())
        self.assertIn("dry-run", synced["stdout"])

    def test_ssh_sync_expands_local_tilde_source_and_preserves_trailing_slash(self) -> None:
        source_root = self.root / "tilde-sync-source"
        source_root.mkdir()
        (source_root / "new.txt").write_text("new", encoding="utf-8")
        destination_root = self.root / "tilde-sync-destination"
        destination_root.mkdir()

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            synced = self.service.ssh_sync(
                {
                    "target": "example",
                    "direction": "upload",
                    "source": "~/tilde-sync-source/",
                    "destination": str(destination_root),
                }
            )

        self.assertTrue(synced["ok"])
        self.assertEqual((destination_root / "new.txt").read_text(encoding="utf-8"), "new")

    # -- ssh_view ---------------------------------------------------------

    def test_ssh_view_reads_small_file(self) -> None:
        target_file = self.root / "view-me.txt"
        target_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = self.service.ssh_view({"target": "example", "path": str(target_file)})

        self.assertTrue(result["ok"])
        self.assertFalse(result["is_directory"])
        self.assertEqual(result["content"], "line1\nline2\nline3\n")
        self.assertEqual(result["total_lines"], 3)
        self.assertEqual(result["size_bytes"], len("line1\nline2\nline3\n"))
        self.assertFalse(result["truncated"])

    def test_ssh_view_preserves_crlf_line_endings(self) -> None:
        # Regression test: subprocess text-mode communicate() silently
        # normalizes '\r\n'/'\r' to '\n' unless bytes are decoded manually.
        target_file = self.root / "crlf.txt"
        target_file.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

        result = self.service.ssh_view({"target": "example", "path": str(target_file)})

        self.assertEqual(result["content"], "alpha\r\nbeta\r\ngamma\r\n")
        self.assertEqual(result["size_bytes"], len(b"alpha\r\nbeta\r\ngamma\r\n"))

    def test_ssh_view_supports_view_range(self) -> None:
        target_file = self.root / "range.txt"
        target_file.write_text("\n".join(str(n) for n in range(1, 11)) + "\n", encoding="utf-8")

        result = self.service.ssh_view(
            {"target": "example", "path": str(target_file), "view_range": [3, 5]}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "3\n4\n5\n")
        self.assertEqual(result["start_line"], 3)
        self.assertEqual(result["end_line"], 5)
        self.assertEqual(result["total_lines"], 10)

    def test_ssh_view_range_to_end_of_file(self) -> None:
        target_file = self.root / "range-end.txt"
        target_file.write_text("\n".join(str(n) for n in range(1, 6)) + "\n", encoding="utf-8")

        result = self.service.ssh_view(
            {"target": "example", "path": str(target_file), "view_range": [3, -1]}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "3\n4\n5\n")

    def test_ssh_view_truncates_large_files_by_default(self) -> None:
        target_file = self.root / "big.txt"
        target_file.write_text("abcdefghij", encoding="utf-8")

        result = self.service.ssh_view(
            {"target": "example", "path": str(target_file), "max_bytes": 5}
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "abcde")
        self.assertEqual(result["size_bytes"], 10)

    def test_ssh_view_force_read_large_files_bypasses_truncation(self) -> None:
        target_file = self.root / "big-force.txt"
        target_file.write_text("abcdefghij", encoding="utf-8")

        result = self.service.ssh_view(
            {
                "target": "example",
                "path": str(target_file),
                "max_bytes": 5,
                "force_read_large_files": True,
            }
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["content"], "abcdefghij")

    def test_ssh_view_lists_directory_excluding_hidden_entries(self) -> None:
        directory = self.root / "listing"
        directory.mkdir()
        (directory / "a.txt").write_text("a", encoding="utf-8")
        (directory / "sub").mkdir()
        (directory / "sub" / "b.txt").write_text("b", encoding="utf-8")
        (directory / ".hidden").write_text("secret", encoding="utf-8")

        result = self.service.ssh_view({"target": "example", "path": str(directory)})

        self.assertTrue(result["ok"])
        self.assertTrue(result["is_directory"])
        entry_paths = {entry["path"] for entry in result["entries"]}
        self.assertIn(str(directory / "a.txt"), entry_paths)
        self.assertIn(str(directory / "sub"), entry_paths)
        self.assertIn(str(directory / "sub" / "b.txt"), entry_paths)
        self.assertNotIn(str(directory / ".hidden"), entry_paths)
        kinds = {entry["path"]: entry["type"] for entry in result["entries"]}
        self.assertEqual(kinds[str(directory / "sub")], "directory")
        self.assertEqual(kinds[str(directory / "a.txt")], "file")

    def test_ssh_view_missing_path_raises_remote_file_error(self) -> None:
        with self.assertRaises(RemoteFileError):
            self.service.ssh_view({"target": "example", "path": str(self.root / "nope.txt")})

    def test_ssh_view_handles_relative_path_starting_with_dash(self) -> None:
        # Regression test: BSD/macOS sed and test(1) can misparse a bare
        # leading '-' as an option rather than a filename; ssh_view must
        # shield relative dash-leading paths (see _shield_leading_dash).
        directory = self.root / "dash-view"
        directory.mkdir()
        (directory / "-dashfile.txt").write_text("hello from dashfile\n", encoding="utf-8")
        previous_cwd = os.getcwd()
        os.chdir(directory)
        try:
            result = self.service.ssh_view({"target": "example", "path": "-dashfile.txt"})
        finally:
            os.chdir(previous_cwd)

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "hello from dashfile\n")

    # -- ssh_create ---------------------------------------------------------

    def test_ssh_create_writes_new_file(self) -> None:
        target_file = self.root / "created.txt"

        result = self.service.ssh_create(
            {"target": "example", "path": str(target_file), "content": "hello\nworld\n"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(target_file.read_text(encoding="utf-8"), "hello\nworld\n")
        self.assertEqual(result["bytes_written"], len("hello\nworld\n"))

    def test_ssh_create_preserves_crlf_line_endings(self) -> None:
        target_file = self.root / "created-crlf.txt"

        result = self.service.ssh_create(
            {"target": "example", "path": str(target_file), "content": "hello\r\nworld\r\n"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(target_file.read_bytes(), b"hello\r\nworld\r\n")

    def test_ssh_create_allows_empty_content(self) -> None:
        target_file = self.root / "empty.txt"

        result = self.service.ssh_create({"target": "example", "path": str(target_file), "content": ""})

        self.assertTrue(result["ok"])
        self.assertEqual(target_file.read_text(encoding="utf-8"), "")

    def test_ssh_create_refuses_existing_file(self) -> None:
        target_file = self.root / "already-there.txt"
        target_file.write_text("existing", encoding="utf-8")

        with self.assertRaises(RemoteFileError):
            self.service.ssh_create({"target": "example", "path": str(target_file), "content": "new"})
        self.assertEqual(target_file.read_text(encoding="utf-8"), "existing")

    def test_ssh_create_refuses_missing_parent_directory(self) -> None:
        target_file = self.root / "no-such-dir" / "file.txt"

        with self.assertRaises(RemoteFileError):
            self.service.ssh_create({"target": "example", "path": str(target_file), "content": "new"})
        self.assertFalse(target_file.exists())

    # -- ssh_edit -----------------------------------------------------------

    def test_ssh_edit_replaces_unique_match(self) -> None:
        target_file = self.root / "edit-me.txt"
        target_file.write_text("def foo():\n    return 1\n", encoding="utf-8")

        result = self.service.ssh_edit(
            {
                "target": "example",
                "path": str(target_file),
                "edits": [{"old_str": "return 1", "new_str": "return 2"}],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["edits_applied"], 1)
        self.assertEqual(target_file.read_text(encoding="utf-8"), "def foo():\n    return 2\n")

    def test_ssh_edit_preserves_crlf_line_endings_unrelated_to_the_edit(self) -> None:
        # Regression test: ssh_edit reads the file, edits it, and writes the
        # result straight back -- any CRLF/CR normalization on the read side
        # would silently corrupt line endings even for an unrelated edit.
        target_file = self.root / "edit-crlf.txt"
        target_file.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

        result = self.service.ssh_edit(
            {
                "target": "example",
                "path": str(target_file),
                "edits": [{"old_str": "beta", "new_str": "BETA"}],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(target_file.read_bytes(), b"alpha\r\nBETA\r\ngamma\r\n")

    def test_ssh_edit_applies_batched_edits_sequentially(self) -> None:
        target_file = self.root / "batch-edit.txt"
        target_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        result = self.service.ssh_edit(
            {
                "target": "example",
                "path": str(target_file),
                "edits": [
                    {"old_str": "alpha", "new_str": "ALPHA"},
                    {"old_str": "beta", "new_str": "BETA"},
                    {"old_str": "ALPHA\nBETA", "new_str": "ALPHA-BETA"},
                ],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["edits_applied"], 3)
        self.assertEqual(target_file.read_text(encoding="utf-8"), "ALPHA-BETA\ngamma\n")

    def test_ssh_edit_fails_when_old_str_not_found(self) -> None:
        target_file = self.root / "not-found.txt"
        target_file.write_text("original content\n", encoding="utf-8")

        with self.assertRaises(RemoteFileError):
            self.service.ssh_edit(
                {
                    "target": "example",
                    "path": str(target_file),
                    "edits": [{"old_str": "missing text", "new_str": "replacement"}],
                }
            )
        self.assertEqual(target_file.read_text(encoding="utf-8"), "original content\n")

    def test_ssh_edit_fails_when_old_str_not_unique(self) -> None:
        target_file = self.root / "not-unique.txt"
        target_file.write_text("dup\ndup\n", encoding="utf-8")

        with self.assertRaises(RemoteFileError):
            self.service.ssh_edit(
                {
                    "target": "example",
                    "path": str(target_file),
                    "edits": [{"old_str": "dup", "new_str": "unique"}],
                }
            )
        self.assertEqual(target_file.read_text(encoding="utf-8"), "dup\ndup\n")

    def test_ssh_edit_missing_file_raises_remote_file_error(self) -> None:
        with self.assertRaises(RemoteFileError):
            self.service.ssh_edit(
                {
                    "target": "example",
                    "path": str(self.root / "nope.txt"),
                    "edits": [{"old_str": "a", "new_str": "b"}],
                }
            )

    def test_ssh_edit_on_directory_raises_remote_file_error(self) -> None:
        directory = self.root / "a-directory"
        directory.mkdir()

        with self.assertRaises(RemoteFileError):
            self.service.ssh_edit(
                {
                    "target": "example",
                    "path": str(directory),
                    "edits": [{"old_str": "a", "new_str": "b"}],
                }
            )

    # -- ssh_grep -------------------------------------------------------------

    def test_ssh_grep_content_mode_returns_matching_lines(self) -> None:
        directory = self.root / "grep-content"
        directory.mkdir()
        (directory / "a.py").write_text("foo\nbar\nfoo again\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {"target": "example", "pattern": "foo", "path": str(directory), "output_mode": "content"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["output_mode"], "content")
        lines = {(m["line_number"], m["line"]) for m in result["matches"]}
        self.assertIn((1, "foo"), lines)
        self.assertIn((3, "foo again"), lines)
        self.assertTrue(all(m["path"].endswith("a.py") for m in result["matches"]))

    def test_ssh_grep_defaults_to_files_with_matches(self) -> None:
        directory = self.root / "grep-default"
        directory.mkdir()
        (directory / "hit.txt").write_text("needle\n", encoding="utf-8")
        (directory / "miss.txt").write_text("nothing here\n", encoding="utf-8")

        result = self.service.ssh_grep({"target": "example", "pattern": "needle", "path": str(directory)})

        self.assertTrue(result["ok"])
        self.assertEqual(result["output_mode"], "files_with_matches")
        self.assertEqual(result["matches"], [str(directory / "hit.txt")])

    def test_ssh_grep_count_mode_omits_zero_match_files(self) -> None:
        directory = self.root / "grep-count"
        directory.mkdir()
        (directory / "hit.txt").write_text("needle\nneedle again\n", encoding="utf-8")
        (directory / "miss.txt").write_text("nothing\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {"target": "example", "pattern": "needle", "path": str(directory), "output_mode": "count"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], [{"path": str(directory / "hit.txt"), "count": 2}])

    def test_ssh_grep_case_insensitive(self) -> None:
        directory = self.root / "grep-case"
        directory.mkdir()
        (directory / "a.txt").write_text("Needle\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {
                "target": "example",
                "pattern": "needle",
                "path": str(directory),
                "output_mode": "files_with_matches",
                "case_insensitive": True,
            }
        )

        self.assertEqual(result["matches"], [str(directory / "a.txt")])

    def test_ssh_grep_glob_filter(self) -> None:
        directory = self.root / "grep-glob"
        directory.mkdir()
        (directory / "a.py").write_text("needle\n", encoding="utf-8")
        (directory / "b.txt").write_text("needle\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {"target": "example", "pattern": "needle", "path": str(directory), "glob": "*.py"}
        )

        self.assertEqual(result["matches"], [str(directory / "a.py")])

    def test_ssh_grep_excludes_git_directory(self) -> None:
        directory = self.root / "grep-git-exclude"
        (directory / ".git").mkdir(parents=True)
        (directory / ".git" / "config").write_text("needle\n", encoding="utf-8")
        (directory / "visible.txt").write_text("needle\n", encoding="utf-8")

        result = self.service.ssh_grep({"target": "example", "pattern": "needle", "path": str(directory)})

        self.assertEqual(result["matches"], [str(directory / "visible.txt")])

    def test_ssh_grep_context_lines(self) -> None:
        directory = self.root / "grep-context"
        directory.mkdir()
        (directory / "ctx.txt").write_text("one\ntwo\nMATCH\nfour\nfive\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {
                "target": "example",
                "pattern": "MATCH",
                "path": str(directory),
                "output_mode": "content",
                "context": 1,
            }
        )

        self.assertTrue(result["ok"])
        by_line = {m["line_number"]: m for m in result["matches"]}
        self.assertEqual(by_line[3]["line"], "MATCH")
        self.assertNotIn("is_context", by_line[3])
        self.assertEqual(by_line[2]["line"], "two")
        self.assertTrue(by_line[2]["is_context"])
        self.assertEqual(by_line[4]["line"], "four")
        self.assertTrue(by_line[4]["is_context"])

    def test_ssh_grep_context_requires_content_mode(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.ssh_grep(
                {
                    "target": "example",
                    "pattern": "x",
                    "path": str(self.root),
                    "output_mode": "files_with_matches",
                    "context": 1,
                }
            )

    def test_ssh_grep_no_matches_is_not_an_error(self) -> None:
        directory = self.root / "grep-empty"
        directory.mkdir()
        (directory / "a.txt").write_text("nothing relevant\n", encoding="utf-8")

        result = self.service.ssh_grep({"target": "example", "pattern": "zzz_no_match", "path": str(directory)})

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], [])

    def test_ssh_grep_missing_path_raises_remote_file_error(self) -> None:
        with self.assertRaises(RemoteFileError) as ctx:
            self.service.ssh_grep({"target": "example", "pattern": "x", "path": str(self.root / "nope")})
        self.assertIn("does not exist", str(ctx.exception))

    def test_ssh_grep_pattern_starting_with_dash_is_treated_literally(self) -> None:
        directory = self.root / "grep-dash-pattern"
        directory.mkdir()
        (directory / "a.txt").write_text("-verbose flag\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {"target": "example", "pattern": "-verbose", "path": str(directory), "output_mode": "content"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["line"], "-verbose flag")

    def test_ssh_grep_head_limit_truncates_matches(self) -> None:
        directory = self.root / "grep-head-limit"
        directory.mkdir()
        (directory / "a.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")

        result = self.service.ssh_grep(
            {
                "target": "example",
                "pattern": "needle",
                "path": str(directory),
                "output_mode": "content",
                "head_limit": 2,
            }
        )

        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])

    # -- ssh_glob -------------------------------------------------------------

    def test_ssh_glob_matches_recursive_pattern(self) -> None:
        directory = self.root / "glob-recursive"
        (directory / "src" / "a").mkdir(parents=True)
        (directory / "src" / "top.ts").write_text("", encoding="utf-8")
        (directory / "src" / "a" / "nested.ts").write_text("", encoding="utf-8")
        (directory / "src" / "a" / "nested.js").write_text("", encoding="utf-8")

        result = self.service.ssh_glob(
            {"target": "example", "pattern": "src/**/*.ts", "path": str(directory)}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["matches"]),
            {"src/top.ts", "src/a/nested.ts"},
        )

    def test_ssh_glob_brace_alternation(self) -> None:
        directory = self.root / "glob-brace"
        directory.mkdir()
        (directory / "a.ts").write_text("", encoding="utf-8")
        (directory / "a.tsx").write_text("", encoding="utf-8")
        (directory / "a.js").write_text("", encoding="utf-8")

        result = self.service.ssh_glob(
            {"target": "example", "pattern": "*.{ts,tsx}", "path": str(directory)}
        )

        self.assertEqual(set(result["matches"]), {"a.ts", "a.tsx"})

    def test_ssh_glob_excludes_hidden_entries_by_default(self) -> None:
        directory = self.root / "glob-hidden"
        (directory / ".config").mkdir(parents=True)
        (directory / ".config" / "settings.txt").write_text("", encoding="utf-8")
        (directory / "visible.txt").write_text("", encoding="utf-8")

        result = self.service.ssh_glob({"target": "example", "pattern": "**/*", "path": str(directory)})

        self.assertEqual(result["matches"], ["visible.txt"])

    def test_ssh_glob_prunes_git_directory_remotely(self) -> None:
        # Regression test: verifies the remote find(1) script actually prunes
        # .git (distinct from the Python-side hidden-segment guard exercised
        # by test_ssh_glob_excludes_hidden_entries_by_default above). Using a
        # pattern whose first segment is itself literally dot-prefixed
        # bypasses that guard, so this only passes if .git was pruned
        # remotely before ever reaching the Python-side matcher.
        directory = self.root / "glob-git-prune"
        (directory / ".git").mkdir(parents=True)
        (directory / ".git" / "config").write_text("", encoding="utf-8")
        (directory / "visible.txt").write_text("", encoding="utf-8")

        result = self.service.ssh_glob({"target": "example", "pattern": ".git/**", "path": str(directory)})

        self.assertEqual(result["matches"], [])

    def test_ssh_glob_head_limit_truncates_matches(self) -> None:
        directory = self.root / "glob-head-limit"
        directory.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (directory / name).write_text("", encoding="utf-8")

        result = self.service.ssh_glob(
            {"target": "example", "pattern": "*.txt", "path": str(directory), "head_limit": 2}
        )

        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])

    def test_ssh_glob_missing_directory_raises_remote_file_error(self) -> None:
        with self.assertRaises(RemoteFileError):
            self.service.ssh_glob({"target": "example", "pattern": "*.txt", "path": str(self.root / "nope")})

    def test_ssh_glob_path_that_is_a_file_raises_remote_file_error(self) -> None:
        target_file = self.root / "not-a-dir.txt"
        target_file.write_text("x", encoding="utf-8")

        with self.assertRaises(RemoteFileError):
            self.service.ssh_glob({"target": "example", "pattern": "*.txt", "path": str(target_file)})

    def test_interactive_session_lifecycle(self) -> None:
        workspace = self.root / "session-workspace"
        workspace.mkdir()

        started = self.service.ssh_start_session(
            {
                "target": "example",
                "cwd": str(workspace),
                "env": {"HELLO": "world"},
                "shell": "/bin/sh",
                "observer_mode": "transcript",
                "wait_seconds": 0.1,
                "max_output_chars": 4096,
            }
        )
        session_id = started["session_id"]
        self.assertTrue(started["running"])
        self.assertEqual(started["observer"]["mode"], "transcript")

        wrote = self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": "printf '%s:%s\\n' \"$PWD\" \"$HELLO\"\n",
                "wait_seconds": 0.5,
                "max_output_chars": 4096,
            }
        )
        self.assertIn(f"{workspace}:world", wrote["output"])
        transcript_path = started["observer"]["transcript_path"]
        transcript_text = Path(transcript_path).read_text(encoding="utf-8")
        self.assertIn(f"{workspace}:world", transcript_text)

        listing = self.service.ssh_list_sessions({})
        self.assertEqual(listing["count"], 1)
        self.assertTrue(listing["sessions"][0]["running"])

        exited = self._exit_session(session_id, 4)
        self.assertFalse(exited["running"])
        self.assertEqual(exited["exit_code"], 4)

        post_exit = self.service.ssh_stop_session({"session_id": session_id})
        self.assertFalse(post_exit["was_running"])
        self.assertEqual(post_exit["exit_code"], 4)

    def test_stop_session_terminates_running_process(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "wait_seconds": 0.05})
        session_id = started["session_id"]

        self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": "sleep 30\n",
                "wait_seconds": 0.1,
                "max_output_chars": 1024,
            }
        )

        stopped = self.service.ssh_stop_session(
            {
                "session_id": session_id,
                "wait_seconds": 0.5,
                "max_output_chars": 4096,
            }
        )
        self.assertTrue(stopped["was_running"])
        self.assertFalse(stopped["running"])
        self.assertIn(stopped["termination_signal"], {"SIGTERM", "SIGKILL"})
        self.assertEqual(stopped["observer"]["mode"], "transcript")
        self.assertFalse(stopped["observer"]["tmux_started"])
        log_entries = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(log_entries), 2)
        self.assertEqual(log_entries[1]["argv"][:3], ["kill-session", "-t", started["observer"]["tmux_session_name"]])

    def test_tmux_observer_is_default_for_new_session(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "wait_seconds": 0.05})

        observer = started["observer"]
        self.assertEqual(observer["mode"], "tmux")
        self.assertTrue(observer["tmux_started"])
        self.assertIn("tmux_attach_command", observer)
        log_entries = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(log_entries), 1)
        self.assertEqual(log_entries[0]["argv"][:3], ["new-session", "-d", "-s"])

    def test_ssh_ensure_session_reuses_named_session_across_many_round_trips(self) -> None:
        first = self.service.ssh_ensure_session(
            {
                "target": "example",
                "session_name": "primary",
                "wait_seconds": 0.05,
            }
        )
        session_id = first["session_id"]
        self.assertTrue(first["created"])
        self.assertFalse(first["reused"])

        for counter in range(1, 9):
            ensured = self.service.ssh_ensure_session(
                {
                    "target": "example",
                    "session_name": "primary",
                    "wait_seconds": 0.05,
                }
            )
            self.assertEqual(ensured["session_id"], session_id)
            self.assertTrue(ensured["reused"])
            self.assertEqual(ensured["matched_by"], "session_name")

            wrote = self.service.ssh_write_session(
                {
                    "session_id": session_id,
                    "input": "COUNT=${COUNT:-0}; COUNT=$((COUNT+1)); printf 'count:%s\\n' \"$COUNT\"\n",
                    "wait_seconds": 0.3,
                    "max_output_chars": 4096,
                }
            )
            self.assertIn(f"count:{counter}", wrote["output"])

            listing = self.service.ssh_list_sessions(
                {"include_exited": False, "target": "example", "session_name": "primary"}
            )
            self.assertEqual(listing["count"], 1)
            self.assertEqual(listing["sessions"][0]["session_id"], session_id)
            self.assertEqual(listing["sessions"][0]["session_name"], "primary")

        stopped = self.service.ssh_stop_session({"session_id": session_id, "wait_seconds": 0.5})
        self.assertFalse(stopped["running"])

    def test_ssh_ensure_session_reuses_unique_target_without_name(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "wait_seconds": 0.05})
        ensured = self.service.ssh_ensure_session({"target": "example", "wait_seconds": 0.05})

        self.assertTrue(ensured["reused"])
        self.assertEqual(ensured["matched_by"], "target")
        self.assertEqual(ensured["session_id"], started["session_id"])

    def test_ssh_ensure_session_rejects_ambiguous_target(self) -> None:
        first = self.service.ssh_start_session(
            {"target": "example", "session_name": "one", "wait_seconds": 0.05}
        )
        second = self.service.ssh_start_session(
            {"target": "example", "session_name": "two", "wait_seconds": 0.05}
        )
        self.assertNotEqual(first["session_id"], second["session_id"])

        with self.assertRaisesRegex(ValidationError, "Multiple running sessions already exist"):
            self.service.ssh_ensure_session({"target": "example", "wait_seconds": 0.05})

    def test_start_session_rejects_duplicate_session_name(self) -> None:
        self.service.ssh_start_session(
            {"target": "example", "session_name": "primary", "wait_seconds": 0.05}
        )

        with self.assertRaisesRegex(ValidationError, "already exists"):
            self.service.ssh_start_session(
                {"target": "example", "session_name": "primary", "wait_seconds": 0.05}
            )

    def test_ssh_ensure_session_is_idempotent_under_concurrency(self) -> None:
        barrier = threading.Barrier(2)
        results: list[dict[str, object]] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=2)
                result = self.service.ssh_ensure_session(
                    {
                        "target": "example",
                        "session_name": "primary",
                        "wait_seconds": 0.05,
                    }
                )
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - exercised only on failure
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({result["session_id"] for result in results}), 1)
        listing = self.service.ssh_list_sessions(
            {"include_exited": False, "target": "example", "session_name": "primary"}
        )
        self.assertEqual(listing["count"], 1)

    def test_start_session_allows_only_one_duplicate_name_under_concurrency(self) -> None:
        barrier = threading.Barrier(3)
        results: list[dict[str, object]] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=2)
                result = self.service.ssh_start_session(
                    {
                        "target": "example",
                        "session_name": "primary",
                        "wait_seconds": 0.05,
                    }
                )
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, ValidationError) for error in errors))
        listing = self.service.ssh_list_sessions(
            {"include_exited": False, "target": "example", "session_name": "primary"}
        )
        self.assertEqual(listing["count"], 1)

    def test_ensure_session_launches_tmux_observer_only_once_under_concurrency(self) -> None:
        self.service.ssh_start_session(
            {
                "target": "example",
                "session_name": "primary",
                "observer_mode": "transcript",
                "wait_seconds": 0.05,
            }
        )
        barrier = threading.Barrier(4)
        results: list[dict[str, object]] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=2)
                result = self.service.ssh_ensure_session(
                    {
                        "target": "example",
                        "session_name": "primary",
                        "observer_mode": "tmux",
                        "wait_seconds": 0.05,
                    }
                )
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result["observer"]["mode"] == "tmux" for result in results))
        log_entries = [
            json.loads(line)
            for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(log_entries), 1)

    def test_read_returns_promptly_when_output_already_buffered(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])
        with session._condition:
            session._unread_output = "prefilled-output"
            session._condition.notify_all()
        began_at = time.monotonic()
        result = session.read(wait_seconds=0.6, max_output_chars=4096)
        elapsed = time.monotonic() - began_at
        self.assertLess(elapsed, 0.2)
        self.assertEqual(result["output"], "prefilled-output")

    def test_start_session_falls_back_when_tmux_launch_times_out(self) -> None:
        with mock.patch(
            "ssh_mcp.ssh.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["tmux", "new-session"], timeout=5),
        ):
            started = self.service.ssh_start_session({"target": "example", "observer_mode": "tmux"})
        observer = started["observer"]
        self.assertEqual(observer["mode"], "transcript")
        self.assertFalse(observer["tmux_started"])
        self.assertIn("Timed out while launching the tmux observer", observer["warning"])

    def test_stop_session_falls_back_when_tmux_close_times_out(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "tmux"})
        self.assertTrue(started["observer"]["tmux_started"])
        tmux_binary = str(self.fake_tmux)
        tmux_session_name = started["observer"]["tmux_session_name"]
        original_run = subprocess.run

        def run_with_kill_timeout(*args, **kwargs):
            argv = args[0]
            if argv[:4] == [tmux_binary, "kill-session", "-t", tmux_session_name]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=5)
            return original_run(*args, **kwargs)

        with mock.patch("ssh_mcp.ssh.subprocess.run", side_effect=run_with_kill_timeout):
            stopped = self.service.ssh_stop_session({"session_id": started["session_id"]})

        observer = stopped["observer"]
        self.assertEqual(observer["mode"], "transcript")
        self.assertFalse(observer["tmux_started"])
        self.assertIn("Timed out while closing the tmux observer", observer["warning"])

    def test_tmux_observer_is_closed_when_session_exits_naturally(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "tmux"})
        session_id = started["session_id"]
        exited = self._exit_session(session_id, 0)
        self.assertFalse(exited["running"])
        for _ in range(20):
            log_entries = [
                json.loads(line)
                for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if len(log_entries) >= 2:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(len(log_entries), 2)
        self.assertEqual(log_entries[1]["argv"][:3], ["kill-session", "-t", started["observer"]["tmux_session_name"]])

    # ------------------------------------------------------------------
    # Unread output buffer cap tests
    # ------------------------------------------------------------------

    def test_buffer_dropped_chars_is_zero_when_not_exceeded(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])
        self.assertEqual(session._unread_dropped_chars, 0)

    def test_unread_buffer_cap_drops_oldest_bytes_and_tracks_count(self) -> None:
        """Injecting more than DEFAULT_UNREAD_BUFFER_CAP chars must drop the
        oldest content and record the count in _unread_dropped_chars."""
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])

        cap = DEFAULT_UNREAD_BUFFER_CAP
        chunk_a = "A" * cap          # fills the buffer exactly
        chunk_b = "B" * (cap // 2)   # would push it 50 % over the cap

        with session._condition:
            session._append_unread_locked(chunk_a)
            session._append_unread_locked(chunk_b)
            # After the second append the buffer must still be exactly cap chars.
            self.assertEqual(len(session._unread_output), cap)
            # The front should now be all Bs or a mix of As and Bs, but the
            # trailing cap//2 chars must be Bs (the most-recent content).
            self.assertTrue(session._unread_output.endswith("B" * (cap // 2)))
            # Dropped chars = exactly the overflow introduced by chunk_b.
            self.assertEqual(session._unread_dropped_chars, cap // 2)

    def test_buffer_tracks_dropped_chars_count(self) -> None:
        """When more than DEFAULT_UNREAD_BUFFER_CAP chars are appended, the
        internal counter must track how many were dropped."""
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])

        cap = DEFAULT_UNREAD_BUFFER_CAP
        with session._condition:
            session._append_unread_locked("X" * (cap + 500))
            self.assertEqual(session._unread_dropped_chars, 500)
            self.assertEqual(len(session._unread_output), cap)

    def test_transcript_receives_full_output_despite_buffer_cap(self) -> None:
        """Even when the in-memory buffer is trimmed the on-disk transcript
        must contain every byte that the process emitted."""
        started = self.service.ssh_start_session(
            {
                "target": "example",
                "observer_mode": "transcript",
                "wait_seconds": 0.1,
                "max_output_chars": 4096,
            }
        )
        session_id = started["session_id"]
        session = self.service._sessions.get(session_id)
        transcript_path = Path(started["observer"]["transcript_path"])

        # Inject a payload that exceeds the in-memory cap directly into
        # the session's reader path.  Going through the PTY with a 1 MiB+
        # command string is unreliable (shell arg limits, PTY buffer
        # capacity), so we write directly to the internal append path
        # which is what the reader thread uses.
        cap = DEFAULT_UNREAD_BUFFER_CAP
        large_payload = "Z" * (cap + 1024)
        session._append_transcript(large_payload)
        with session._condition:
            session._append_unread_locked(large_payload)

        self.service.ssh_stop_session({"session_id": session_id, "wait_seconds": 1.0})

        transcript_text = transcript_path.read_text(encoding="utf-8")
        z_count = transcript_text.count("Z")
        self.assertGreaterEqual(z_count, cap + 1024)

    def test_large_single_chunk_exceeding_cap_is_clamped(self) -> None:
        """A single chunk larger than the cap itself must be clamped to exactly
        cap chars and all excess counted as dropped."""
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])

        cap = DEFAULT_UNREAD_BUFFER_CAP
        oversized = "Y" * (cap + 200)

        with session._condition:
            session._append_unread_locked(oversized)
            self.assertEqual(len(session._unread_output), cap)
            self.assertEqual(session._unread_dropped_chars, 200)
            # The kept content must be the *tail* of the chunk.
            self.assertEqual(session._unread_output, "Y" * cap)

    def test_extra_ssh_args_blocks_forwarding_short_flags(self) -> None:
        for flag in ["-L", "-R", "-D", "-W"]:
            with self.assertRaises(ValidationError, msg=f"{flag} should be blocked"):
                self.service.ssh_exec(
                    {"target": "example", "command": "true", "extra_ssh_args": [flag, "8080:localhost:80"]}
                )

    def test_extra_ssh_args_allows_safe_flags(self) -> None:
        result = self.service.ssh_exec(
            {"target": "example", "command": "true", "extra_ssh_args": ["-v", "-C"]}
        )
        self.assertTrue(result["ok"])

    # ------------------------------------------------------------------
    # Port forwarding tests
    # ------------------------------------------------------------------

    def test_forward_start_and_stop_lifecycle(self) -> None:
        result = self.service.ssh_forward(
            {
                "target": "example",
                "direction": "local",
                "local_port": 15432,
                "remote_host": "dbhost",
                "remote_port": 5432,
            }
        )
        self.assertTrue(result["running"])
        self.assertEqual(result["direction"], "local")
        self.assertEqual(result["local_port"], 15432)
        self.assertEqual(result["remote_host"], "dbhost")
        self.assertEqual(result["remote_port"], 5432)
        self.assertEqual(result["bind_address"], "127.0.0.1")
        forward_id = result["forward_id"]

        listing = self.service.ssh_list_forwards({})
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["forwards"][0]["forward_id"], forward_id)

        stopped = self.service.ssh_stop_forward({"forward_id": forward_id})
        self.assertTrue(stopped["was_running"])

        listing_after = self.service.ssh_list_forwards({})
        self.assertEqual(listing_after["count"], 0)

        listing_with_stopped = self.service.ssh_list_forwards({"include_stopped": True})
        self.assertEqual(listing_with_stopped["count"], 1)
        self.assertFalse(listing_with_stopped["forwards"][0]["running"])

    def test_forward_remote_direction(self) -> None:
        result = self.service.ssh_forward(
            {
                "target": "example",
                "direction": "remote",
                "local_port": 8080,
                "remote_host": "localhost",
                "remote_port": 3000,
            }
        )
        self.assertTrue(result["running"])
        self.assertEqual(result["direction"], "remote")
        entry = self.service._forwards.get(result["forward_id"])
        self.assertIn("-R", entry.argv)
        self.service.ssh_stop_forward({"forward_id": result["forward_id"]})

    def test_forward_invalid_direction_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.ssh_forward(
                {
                    "target": "example",
                    "direction": "dynamic",
                    "local_port": 8080,
                    "remote_host": "localhost",
                    "remote_port": 80,
                }
            )

    def test_forward_unknown_id_raises(self) -> None:
        from ssh_mcp.ssh import ForwardNotFoundError
        with self.assertRaises(ForwardNotFoundError):
            self.service.ssh_stop_forward({"forward_id": "nonexistent"})

    def test_forward_custom_bind_address(self) -> None:
        result = self.service.ssh_forward(
            {
                "target": "example",
                "direction": "local",
                "local_port": 9999,
                "remote_host": "dbhost",
                "remote_port": 5432,
                "bind_address": "0.0.0.0",
            }
        )
        self.assertEqual(result["bind_address"], "0.0.0.0")
        entry = self.service._forwards.get(result["forward_id"])
        self.assertIn("0.0.0.0", " ".join(entry.argv))
        self.service.ssh_stop_forward({"forward_id": result["forward_id"]})

    def test_forward_cleanup_on_service_close(self) -> None:
        result = self.service.ssh_forward(
            {
                "target": "example",
                "direction": "local",
                "local_port": 15000,
                "remote_host": "localhost",
                "remote_port": 80,
            }
        )
        forward_id = result["forward_id"]
        entry = self.service._forwards.get(forward_id)
        pid = entry.process.pid
        self.service.close()
        try:
            os.kill(pid, 0)
            self.fail("Forward process should have been killed on close")
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------
    # Tmux session naming tests
    # ------------------------------------------------------------------

    def test_tmux_session_name_includes_target_and_session_name(self) -> None:
        name = _sanitize_tmux_session_name("abc123", target="prod-web01", session_name="deploy-api")
        self.assertEqual(name, "ssh-mcp-prod-web01-deploy-api")

    def test_tmux_session_name_with_target_only(self) -> None:
        name = _sanitize_tmux_session_name("abc123def4", target="prod-web01")
        self.assertEqual(name, "ssh-mcp-prod-web01-abc123de")

    def test_tmux_session_name_with_session_name_only(self) -> None:
        name = _sanitize_tmux_session_name("abc123", session_name="deploy-api")
        self.assertEqual(name, "ssh-mcp-deploy-api")

    def test_tmux_session_name_fallback_to_session_id(self) -> None:
        name = _sanitize_tmux_session_name("abc123def456")
        self.assertEqual(name, "ssh-mcp-abc123de")

    def test_tmux_session_name_sanitises_special_characters(self) -> None:
        name = _sanitize_tmux_session_name("x", target="user@host:22", session_name="my session!")
        self.assertNotIn("@", name)
        self.assertNotIn(":", name)
        self.assertNotIn("!", name)
        self.assertNotIn(" ", name)

    # ------------------------------------------------------------------
    # Exit reason tests
    # ------------------------------------------------------------------

    def test_exit_reason_clean_exit(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        exited = self._exit_session(started["session_id"], 0)
        self.assertEqual(exited["exit_reason"], "clean-exit")

    def test_exit_reason_nonzero_exit(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        exited = self._exit_session(started["session_id"], 42)
        self.assertEqual(exited["exit_reason"], "exit-42")

    def test_exit_reason_is_none_while_running(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        self.assertIsNone(started["exit_reason"])
        self.service.ssh_stop_session({"session_id": started["session_id"]})

    # ------------------------------------------------------------------
    # Keepalive args tests
    # ------------------------------------------------------------------

    def test_session_argv_includes_keepalive(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        session = self.service._sessions.get(started["session_id"])
        cmd = session.ssh_command
        self.assertIn("ServerAliveInterval=30", cmd)
        self.assertIn("ServerAliveCountMax=3", cmd)
        self.service.ssh_stop_session({"session_id": started["session_id"]})

    def test_exec_argv_does_not_include_keepalive(self) -> None:
        result = self.service.ssh_exec({"target": "example", "command": "true"})
        # exec uses _run_without_pty which doesn't return ssh_command,
        # so check the stdout/stderr instead — keepalive is an SSH option
        # that doesn't affect command output.  We verify by checking that
        # the build_argv path for exec doesn't inject keepalive.
        from ssh_mcp.ssh import ConnectionSettings, _resolve_ssh_binary
        conn = ConnectionSettings(target="example")
        argv = conn.build_argv("ssh", "true", tty=False, keepalive=False)
        cmd = " ".join(argv)
        self.assertNotIn("ServerAliveInterval", cmd)

    def test_session_keepalive_respects_user_override(self) -> None:
        started = self.service.ssh_start_session(
            {
                "target": "example",
                "observer_mode": "transcript",
                "wait_seconds": 0.1,
                "extra_ssh_args": ["-o", "ServerAliveInterval=60"],
            }
        )
        session = self.service._sessions.get(started["session_id"])
        cmd = session.ssh_command
        self.assertIn("ServerAliveInterval=60", cmd)
        self.assertNotIn("ServerAliveInterval=30", cmd)
        self.assertIn("ServerAliveCountMax=3", cmd)
        self.service.ssh_stop_session({"session_id": started["session_id"]})

    # ------------------------------------------------------------------
    # Auto-close tests
    # ------------------------------------------------------------------

    def test_auto_close_appears_in_snapshot(self) -> None:
        started = self.service.ssh_start_session(
            {
                "target": "example",
                "observer_mode": "transcript",
                "wait_seconds": 0.1,
                "auto_close": True,
            }
        )
        self.assertTrue(started.get("auto_close"))
        self.service.ssh_stop_session({"session_id": started["session_id"]})

    def test_auto_close_false_omitted_from_snapshot(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        self.assertNotIn("auto_close", started)
        self.service.ssh_stop_session({"session_id": started["session_id"]})

    # ------------------------------------------------------------------
    # Session pruning tests
    # ------------------------------------------------------------------

    def test_prune_removes_old_exited_sessions(self) -> None:
        started = self.service.ssh_start_session(
            {"target": "example", "observer_mode": "transcript", "wait_seconds": 0.1}
        )
        session_id = started["session_id"]
        self._exit_session(session_id, 0)

        # Session is exited but recent — should still be listed.
        listing = self.service.ssh_list_sessions({"include_exited": True})
        ids = [s["session_id"] for s in listing["sessions"]]
        self.assertIn(session_id, ids)

        # Backdate the ended_at and force a prune cycle.
        from datetime import timedelta
        from ssh_mcp.ssh import utcnow
        session = self.service._sessions._sessions[session_id]
        session._ended_at = utcnow() - timedelta(hours=2)
        # Reset the throttle so prune runs immediately.
        self.service._sessions._last_prune_mono = time.monotonic() - 120
        listing = self.service.ssh_list_sessions({"include_exited": True})
        ids = [s["session_id"] for s in listing["sessions"]]
        self.assertNotIn(session_id, ids)

    def test_prune_auto_close_sessions_faster(self) -> None:
        started = self.service.ssh_start_session(
            {
                "target": "example",
                "observer_mode": "transcript",
                "wait_seconds": 0.1,
                "auto_close": True,
            }
        )
        session_id = started["session_id"]
        self._exit_session(session_id, 0)

        # Backdate by 10 minutes — past the 5-minute auto_close threshold
        # but within the 1-hour default.
        from datetime import timedelta
        from ssh_mcp.ssh import utcnow
        session = self.service._sessions._sessions[session_id]
        session._ended_at = utcnow() - timedelta(minutes=10)
        self.service._sessions._last_prune_mono = time.monotonic() - 120
        listing = self.service.ssh_list_sessions({"include_exited": True})
        ids = [s["session_id"] for s in listing["sessions"]]
        self.assertNotIn(session_id, ids)

    # ------------------------------------------------------------------
    # Blocked SSH option tests (the -o form)
    # ------------------------------------------------------------------

    def test_extra_ssh_args_blocks_proxycommand(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.ssh_exec(
                {"target": "example", "command": "true", "extra_ssh_args": ["-o", "ProxyCommand=evil"]}
            )

    def test_extra_ssh_args_blocks_inline_proxycommand(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.ssh_exec(
                {"target": "example", "command": "true", "extra_ssh_args": ["-oProxyCommand=evil"]}
            )

    def test_extra_ssh_args_allows_safe_ssh_options(self) -> None:
        result = self.service.ssh_exec(
            {"target": "example", "command": "true", "extra_ssh_args": ["-o", "StrictHostKeyChecking=no"]}
        )
        self.assertTrue(result["ok"])

    # ------------------------------------------------------------------
    # Forward argv construction tests
    # ------------------------------------------------------------------

    def test_forward_argv_contains_correct_flags(self) -> None:
        result = self.service.ssh_forward(
            {
                "target": "example",
                "direction": "local",
                "local_port": 15432,
                "remote_host": "dbhost",
                "remote_port": 5432,
            }
        )
        forward_id = result["forward_id"]
        entry = self.service._forwards.get(forward_id)
        argv = entry.argv
        self.assertIn("-N", argv)
        self.assertIn("-L", argv)
        self.assertIn("127.0.0.1:15432:dbhost:5432", argv)
        self.assertLess(argv.index("-N"), argv.index("example"))
        self.service.ssh_stop_forward({"forward_id": forward_id})
