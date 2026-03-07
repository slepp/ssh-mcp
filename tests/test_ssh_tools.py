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
        self.assertFalse(result["tty"])
        self.assertIn("-p", result["ssh_argv"])
        self.assertIn("-v", result["ssh_argv"])
        self.assertIn("StrictHostKeyChecking=no", result["ssh_command"])

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
        self.assertIn("tty-mode", result["output"])
        self.assertTrue(result["tty"])

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
        self.assertIn("-r", uploaded["scp_argv"])
        self.assertIn("-p", uploaded["scp_argv"])
        self.assertIn("-P", uploaded["scp_argv"])
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
        self.assertEqual(uploaded["sources"], [str(upload_source)])

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
        self.assertIn("-a", synced["rsync_argv"])
        self.assertIn("-z", synced["rsync_argv"])
        self.assertIn("--delete", synced["rsync_argv"])
        self.assertIn("--itemize-changes", synced["rsync_argv"])
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
        self.assertEqual(synced["source"], f"{source_root}{os.sep}")

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
        self.assertTrue(Path(started["transcript_path"]).exists())
        self.assertEqual(started["observer"]["mode"], "transcript")
        self.assertIn("observe.py", started["observer_command"])

        wrote = self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": "printf '%s:%s\\n' \"$PWD\" \"$HELLO\"\n",
                "wait_seconds": 0.5,
                "max_output_chars": 4096,
            }
        )
        self.assertIn(f"{workspace}:world", wrote["output"])
        transcript_text = Path(started["transcript_path"]).read_text(encoding="utf-8")
        self.assertIn(f"{workspace}:world", transcript_text)

        listing = self.service.ssh_list_sessions({})
        self.assertEqual(listing["count"], 1)
        self.assertTrue(listing["sessions"][0]["running"])
        self.assertEqual(listing["sessions"][0]["transcript_path"], started["transcript_path"])

        exited = self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": "exit 4\n",
                "wait_seconds": 0.5,
                "max_output_chars": 4096,
            }
        )
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
        exited = self.service.ssh_write_session(
            {"session_id": session_id, "input": "exit 0\n", "wait_seconds": 0.5, "max_output_chars": 4096}
        )
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

    def test_output_dropped_chars_is_zero_when_buffer_not_exceeded(self) -> None:
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])
        result = session.read(wait_seconds=0.1, max_output_chars=4096)
        self.assertEqual(result["output_dropped_chars"], 0)

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

    def test_snapshot_exposes_output_dropped_chars(self) -> None:
        """_snapshot_locked must include 'output_dropped_chars' so clients
        can detect that the in-memory ring was trimmed."""
        started = self.service.ssh_start_session({"target": "example", "observer_mode": "transcript"})
        session = self.service._sessions.get(started["session_id"])

        cap = DEFAULT_UNREAD_BUFFER_CAP
        with session._condition:
            session._append_unread_locked("X" * (cap + 500))

        result = session.read(wait_seconds=0.0, max_output_chars=cap * 2)
        self.assertEqual(result["output_dropped_chars"], 500)
        # The buffer itself must never have grown beyond the cap.
        self.assertLessEqual(len(result["output"]), cap)

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
        transcript_path = Path(started["transcript_path"])

        # Write a payload that exceeds the in-memory cap.
        cap = DEFAULT_UNREAD_BUFFER_CAP
        large_payload = "Z" * (cap + 1024)
        # Use printf to emit the data (fake-ssh executes it locally).
        self.service.ssh_write_session(
            {
                "session_id": session_id,
                "input": f"printf '%s' '{large_payload}'\n",
                "wait_seconds": 1.0,
                "max_output_chars": 4096,
            }
        )
        self.service.ssh_stop_session({"session_id": session_id, "wait_seconds": 1.0})

        transcript_text = transcript_path.read_text(encoding="utf-8")
        self.assertIn("Z" * 1024, transcript_text)

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
