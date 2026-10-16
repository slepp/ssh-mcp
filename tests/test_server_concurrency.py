from __future__ import annotations

import json
import queue
import threading
import unittest

from ssh_mcp.server import McpServer


class Input:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get()

    def send(self, method, request_id=None, params=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        self.lines.put(json.dumps(message).encode() + b"\n")


class Output:
    def __init__(self):
        self.responses = queue.Queue()

    def write(self, data):
        self.responses.put(json.loads(data))

    def flush(self):
        pass


class Service:
    def __init__(self):
        self.started = queue.Queue()
        self.release = threading.Event()
        self.closed = threading.Event()

    def __getattr__(self, name):
        return self.call

    def call(self, arguments):
        self.started.put(arguments["label"])
        if arguments.get("block"):
            if not self.release.wait(5):
                raise RuntimeError("test worker was not released")
        return {"ok": True, "label": arguments["label"]}

    def close(self):
        self.closed.set()


class ConcurrentServerTests(unittest.TestCase):
    def setUp(self):
        self.stdin = Input()
        self.stdout = Output()
        self.service = Service()
        self.server = McpServer(stdin=self.stdin, stdout=self.stdout,
                                tool_service=self.service, max_workers=2, max_pending=1)
        self.thread = threading.Thread(target=self.server.serve)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.stdin.send("initialize", 1)
        self.assertEqual(self.response()["id"], 1)

    def cleanup(self):
        self.service.release.set()
        self.stdin.lines.put(b"")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def response(self):
        return self.stdout.responses.get(timeout=3)

    def call(self, request_id, *, block=False, tool="ssh_exec"):
        self.stdin.send("tools/call", request_id,
                        {"name": tool, "arguments": {"label": request_id, "block": block}})

    def test_delayed_exec_does_not_delay_ping_or_independent_ssh(self):
        self.call("slow", block=True)
        self.assertEqual(self.service.started.get(timeout=3), "slow")
        self.stdin.send("ping", "ping")
        self.assertEqual(self.response(), {"jsonrpc": "2.0", "id": "ping", "result": {}})
        self.call(2, tool="ssh_view")
        self.assertEqual(self.service.started.get(timeout=3), 2)
        self.assertEqual(self.response()["id"], 2)
        self.service.release.set()
        self.assertEqual(self.response()["id"], "slow")

    def test_saturation_and_queued_cancellation(self):
        for rid in (2, 3):
            self.call(rid, block=True)
            self.assertEqual(self.service.started.get(timeout=3), rid)
        self.call(4)
        self.call(5)
        self.assertEqual(self.response()["error"]["code"], -32000)
        self.stdin.send("notifications/cancelled", params={"requestId": 4})
        self.stdin.send("ping", 6)
        self.assertEqual(self.response()["id"], 6)
        self.service.release.set()
        replies = [self.response() for _ in range(2)]
        self.assertEqual({r["id"] for r in replies}, {2, 3})
        self.call(7)
        self.assertEqual(self.response()["id"], 7)
        self.assertEqual(self.service.started.get(timeout=3), 7)
        self.assertTrue(self.service.started.empty())

    def test_running_cancellation_keeps_capacity_until_completion(self):
        for rid in (2, 3):
            self.call(rid, block=True)
            self.service.started.get(timeout=3)
        self.stdin.send("notifications/cancelled", params={"requestId": 2})
        self.call(4)
        self.call(5)
        self.assertEqual(self.response()["id"], 5)
        self.service.release.set()
        replies = [self.response() for _ in range(2)]
        self.assertEqual({r["id"] for r in replies}, {3, 4})
        self.stdin.lines.put(b"")
        self.thread.join(3)
        self.assertTrue(self.stdout.responses.empty())

    def test_eof_drains_accepted_calls_before_close(self):
        for rid in (2, 3):
            self.call(rid, block=True)
            self.service.started.get(timeout=3)
        self.call(4)
        self.stdin.lines.put(b"")
        self.assertFalse(self.service.closed.is_set())
        self.service.release.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.service.closed.is_set())
        self.assertEqual({self.response()["id"] for _ in range(3)}, {2, 3, 4})

    def test_repeated_cancellation_does_not_bypass_capacity(self):
        for rid in (2, 3):
            self.call(rid, block=True)
            self.service.started.get(timeout=3)
        self.call(4)
        self.stdin.send("notifications/cancelled", params={"requestId": 4})
        for rid in range(5, 25):
            self.call(rid)
            self.assertEqual(self.response()["error"]["code"], -32000)
            self.stdin.send("notifications/cancelled", params={"requestId": rid})
        self.stdin.send("ping", 25)
        self.assertEqual(self.response()["id"], 25)
        self.assertEqual(len(self.server._requests), 3)
        self.service.release.set()
        self.assertEqual({self.response()["id"] for _ in range(2)}, {2, 3})

    def test_duplicate_and_invalid_ids_do_not_start_extra_work(self):
        self.call(2, block=True)
        self.service.started.get(timeout=3)
        self.call(2)
        self.assertEqual(self.response()["error"]["code"], -32600)
        self.call([])
        self.assertEqual(self.response()["error"]["code"], -32600)
        self.assertTrue(self.service.started.empty())

    def test_output_failure_during_eof_drain_is_propagated(self):
        class FailingOutput(Output):
            def write(self, data):
                raise BrokenPipeError("closed transport")

        stdin, service = Input(), Service()
        server = McpServer(stdin=stdin, stdout=FailingOutput(), tool_service=service)
        server._initialized = True
        errors = []
        def serve():
            try:
                server.serve()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            stdin.send("tools/call", 2, {"name": "ssh_exec",
                       "arguments": {"label": 2, "block": True}})
            service.started.get(timeout=3)
            stdin.lines.put(b"")
        finally:
            service.release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(service.closed.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BrokenPipeError)

    def test_errors_and_notifications_keep_reader_responsive(self):
        self.stdin.lines.put(b"invalid\n")
        self.assertEqual(self.response()["error"]["code"], -32700)
        self.stdin.send("tools/call", 2, {"name": "ssh_exec", "arguments": []})
        self.assertEqual(self.response()["error"]["code"], -32602)
        self.stdin.send("notifications/cancelled", params={"requestId": []})
        self.stdin.send("ping", 3)
        self.assertEqual(self.response()["id"], 3)


if __name__ == "__main__":
    unittest.main()
