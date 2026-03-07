from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import create_fake_rsync, create_fake_scp, create_fake_ssh, create_fake_tmux


class StdioServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.fake_ssh = create_fake_ssh(self.root)
        self.fake_scp = create_fake_scp(self.root)
        self.fake_rsync = create_fake_rsync(self.root)
        self.fake_tmux = create_fake_tmux(self.root)
        self.tmux_log = self.root / "fake_tmux.log"
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["SSH_MCP_SSH_BIN"] = str(self.fake_ssh)
        env["SSH_MCP_SCP_BIN"] = str(self.fake_scp)
        env["SSH_MCP_RSYNC_BIN"] = str(self.fake_rsync)
        env["SSH_MCP_TMUX_BIN"] = str(self.fake_tmux)
        env["SSH_MCP_STATE_DIR"] = str(self.root / "state")
        env["FAKE_TMUX_LOG"] = str(self.tmux_log)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ssh_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=repo_root,
            env=env,
        )

    def tearDown(self) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=2)
        if self.process.stdout and not self.process.stdout.closed:
            self.process.stdout.close()
        if self.process.stderr and not self.process.stderr.closed:
            self.process.stderr.close()
        self._tempdir.cleanup()

    def _rpc(self, message: dict[str, object]) -> dict[str, object]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        self.assertTrue(line, "expected a JSON-RPC response line")
        return json.loads(line)

    def test_stdio_jsonrpc_round_trip(self) -> None:
        initialize = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(initialize["result"]["serverInfo"]["name"], "ssh-mcp")

        tool_list = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"cursor": "ignored"},
            }
        )
        names = [tool["name"] for tool in tool_list["result"]["tools"]]
        self.assertIn("ssh_exec", names)
        self.assertIn("ssh_scp", names)
        self.assertIn("ssh_sync", names)
        self.assertIn("ssh_start_session", names)
        self.assertIn("ssh_ensure_session", names)

        exec_call = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ssh_exec",
                    "arguments": {"target": "example", "command": "printf 'hello'"},
                },
            }
        )
        structured = exec_call["result"]["structuredContent"]
        self.assertTrue(structured["ok"])
        self.assertEqual(structured["stdout"], "hello")

        local_upload = self.root / "stdio-upload.txt"
        local_upload.write_text("stdio scp", encoding="utf-8")
        remote_dir = self.root / "stdio-remote"
        remote_dir.mkdir()
        scp_call = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {
                    "name": "ssh_scp",
                    "arguments": {
                        "target": "example",
                        "direction": "upload",
                        "sources": [str(local_upload)],
                        "destination": str(remote_dir),
                    },
                },
            }
        )
        scp_structured = scp_call["result"]["structuredContent"]
        self.assertTrue(scp_structured["ok"])
        self.assertEqual((remote_dir / "stdio-upload.txt").read_text(encoding="utf-8"), "stdio scp")

        sync_source = self.root / "stdio-sync-source"
        sync_source.mkdir()
        (sync_source / "keep.txt").write_text("stdio sync", encoding="utf-8")
        sync_dest = self.root / "stdio-sync-dest"
        sync_dest.mkdir()
        sync_call = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {
                    "name": "ssh_sync",
                    "arguments": {
                        "target": "example",
                        "direction": "upload",
                        "source": f"{sync_source}/",
                        "destination": str(sync_dest),
                    },
                },
            }
        )
        sync_structured = sync_call["result"]["structuredContent"]
        self.assertTrue(sync_structured["ok"])
        self.assertEqual((sync_dest / "keep.txt").read_text(encoding="utf-8"), "stdio sync")

        session_call = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "ssh_start_session",
                    "arguments": {"target": "example", "wait_seconds": 0.05},
                },
            }
        )
        session = session_call["result"]["structuredContent"]
        self.assertIn("transcript_path", session)
        self.assertEqual(session["observer"]["mode"], "tmux")
        self.assertTrue(session["observer"]["tmux_started"])
        self.assertTrue(Path(session["transcript_path"]).exists())
        log_entries = [json.loads(line) for line in self.tmux_log.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(log_entries), 1)

        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "ssh_stop_session",
                    "arguments": {"session_id": session["session_id"]},
                },
            }
        )

        invalid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "ssh_exec", "arguments": {"target": "", "command": "true"}},
            }
        )
        self.assertTrue(invalid["result"]["isError"])
        self.assertIn("target", invalid["result"]["structuredContent"]["message"])

    def test_stdio_ensure_session_reuses_existing_session(self) -> None:
        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )

        first = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "ssh_ensure_session",
                    "arguments": {
                        "target": "example",
                        "session_name": "primary",
                        "wait_seconds": 0.05,
                    },
                },
            }
        )["result"]["structuredContent"]
        second = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ssh_ensure_session",
                    "arguments": {
                        "target": "example",
                        "session_name": "primary",
                        "wait_seconds": 0.05,
                    },
                },
            }
        )["result"]["structuredContent"]

        self.assertTrue(first["created"])
        self.assertFalse(first["reused"])
        self.assertFalse(second["created"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["matched_by"], "session_name")
        self.assertEqual(first["session_id"], second["session_id"])

        listing = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "ssh_list_sessions",
                    "arguments": {
                        "include_exited": False,
                        "target": "example",
                        "session_name": "primary",
                    },
                },
            }
        )["result"]["structuredContent"]
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["sessions"][0]["session_name"], "primary")

        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "ssh_stop_session",
                    "arguments": {"session_id": first["session_id"]},
                },
            }
        )

    def test_stdio_server_shutdown_closes_tmux_observer(self) -> None:
        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "ssh_start_session",
                    "arguments": {"target": "example", "wait_seconds": 0.05},
                },
            }
        )

        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=2)

        log_entries = [json.loads(line) for line in self.tmux_log.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(log_entries), 2)
        self.assertEqual(log_entries[0]["argv"][:3], ["new-session", "-d", "-s"])
        self.assertEqual(log_entries[1]["argv"][:2], ["kill-session", "-t"])
