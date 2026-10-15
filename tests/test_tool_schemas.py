"""Discovery, JSON Schema, and offline OpenAI registration regressions."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from openai import OpenAI

from ssh_mcp.server import McpServer
from ssh_mcp.ssh import ConnectionSettings, ValidationError


def discover_tools():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    output = io.BytesIO()
    McpServer(stdin=io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in requests)),
              stdout=output).serve()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    return replies[1]["result"]["tools"]


class ToolSchemaTests(unittest.TestCase):
    def test_all_discovered_schemas_use_supported_keywords(self):
        # Audit every schema node, including array items and dynamic env values.
        allowed = {"type", "description", "properties", "required", "additionalProperties",
                   "items", "enum", "anyOf", "minimum", "maximum", "minItems", "maxItems"}

        def audit(schema):
            self.assertLessEqual(set(schema), allowed)
            for child in schema.get("properties", {}).values():
                audit(child)
            if "items" in schema:
                audit(schema["items"])
            if isinstance(schema.get("additionalProperties"), dict):
                audit(schema["additionalProperties"])
            for child in schema.get("anyOf", []):
                audit(child)

        tools = discover_tools()
        self.assertEqual(len(tools), 17)
        self.assertEqual(len({t["name"] for t in tools}), len(tools))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                Draft202012Validator.check_schema(schema)
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertLessEqual(set(schema.get("required", [])), set(schema["properties"]))
                audit(schema)

    def test_host_key_schema_preserves_boolean_and_string_inputs(self):
        count = 0
        for tool in discover_tools():
            schema = tool["inputSchema"]["properties"].get("strict_host_key_checking")
            if schema is None:
                continue
            count += 1
            validator = Draft202012Validator(schema)
            for value in (True, False, "yes", "no", "ask", "accept-new", "off", " YES "):
                with self.subTest(tool=tool["name"], value=value):
                    validator.validate(value)
            for value in (None, 0, 1, [], {}):
                with self.subTest(tool=tool["name"], invalid=value):
                    with self.assertRaises(SchemaError):
                        validator.validate(value)
        self.assertEqual(count, 11)

    def test_host_key_transport_arguments_and_runtime_validation(self):
        for value, mode in [(True, "yes"), (False, "no"), ("yes", "yes"),
                            ("no", "no"), ("ask", "ask"), ("accept-new", "accept-new"),
                            ("off", "off"), (" YES ", "yes")]:
            for program, port_flag in (("ssh", "-p"), ("scp", "-P")):
                with self.subTest(value=value, program=program):
                    settings = ConnectionSettings.from_arguments(
                        {"target": "example", "strict_host_key_checking": value})
                    self.assertEqual(settings.build_transport_argv(program, port_flag=port_flag),
                                     [program, "-o", f"StrictHostKeyChecking={mode}"])
        self.assertEqual(ConnectionSettings.from_arguments({"target": "example"})
                         .build_transport_argv("ssh", port_flag="-p"), ["ssh"])
        for value in ("invalid", "", 0, 1, [], {}):
            with self.subTest(invalid=value), self.assertRaises(ValidationError):
                ConnectionSettings.from_arguments({"target": "example", "strict_host_key_checking": value})

    def test_discovered_tools_register_through_openai_sdk_offline(self):
        tools = [{"type": "function", "name": t["name"], "description": t["description"],
                  "parameters": t["inputSchema"], "strict": False} for t in discover_tools()]
        captured = []

        def registration(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_test", "object": "response",
                "created_at": 0, "model": "gpt-4.1", "output": []})

        # Exercise SDK serialization and the HTTP boundary, without an API key,
        # model call, or claim of live OpenAI server-side schema validation.
        with httpx.Client(transport=httpx.MockTransport(registration)) as http_client:
            with OpenAI(api_key="offline-test", http_client=http_client) as client:
                client.responses.create(model="gpt-4.1", input="List available tools", tools=tools)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["tools"], tools)
