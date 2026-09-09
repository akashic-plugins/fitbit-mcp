from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.plugin_composition import MCP_SERVERS
from agent.plugins.mcp_generation_host import McpCallResult
from plugins.tools.plugin import TOOLS, ToolRef, ToolView
from fitbit_test_plugin.tools import register_tools


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_discovery_is_lazy_and_calls_keep_route_errors():
    records = json.loads((ROOT / "tool_catalog.json").read_text())
    registrations = {}
    calls = []
    opened = []

    class Catalog:
        async def declare_group(self, ctx, **kwargs):
            pass

        def view(self, *refs):
            return ToolView(refs)

        async def register(self, ctx, **record):
            registrations[record["name"]] = record
            return ToolRef(record["name"], record)

    class Route:
        async def call(self, name, arguments):
            calls.append((name, arguments))
            if arguments.get("fail_transport"):
                raise ConnectionError("MCP disconnected")
            return McpCallResult("tool_error" if arguments.get("fail_tool") else "success", "remote result")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            opened.append("route closed")

    server = SimpleNamespace(
        tools={item["name"]: SimpleNamespace(input_schema=item["parameters"]) for item in records},
        route=Route,
    )

    class Servers:
        @asynccontextmanager
        async def open(self, ctx, name):
            opened.append(name)
            yield server

    services = {TOOLS: Catalog(), MCP_SERVERS: Servers()}
    async def provide(key, value):
        services[key] = value
    ctx = SimpleNamespace(require=services.__getitem__, provide=provide)
    await register_tools(ctx, description="fixture MCP tools")
    assert not opened
    assert not calls
    assert len(registrations) == len(records)

    # 每个 opener 必须固定自己的目标，不能都指向循环的最后一项。
    for item in records:
        record = registrations[f"mcp_fitbit__{item['name']}"]
        async with record["open"]({}) as tool:
            assert tool.idempotent == record["idempotent"]
            arguments = await tool.prepare({})
            result = await tool.invoke("one", arguments)
            assert result.outcome == "success"
            assert result.parts[0].value == "remote result"
            assert calls[-1] == (item["name"], {})
            assert await tool.query("one") is None
            assert (await tool.invoke("two", {"fail_tool": True})).outcome == "error"
            with pytest.raises(ConnectionError, match="disconnected"):
                await tool.invoke("three", {"fail_transport": True})
        assert opened[-1] == "route closed"

    # 目录过期必须在远程调用前明确失败。
    item = records[0]
    server.tools[item["name"]].input_schema = {"type": "object", "properties": {"changed": {"type": "string"}}}
    count = len(calls)
    with pytest.raises(RuntimeError, match="schema"):
        async with registrations[f"mcp_fitbit__{item['name']}"]["open"]({}):
            pytest.fail("schema drift opened a tool")
    assert len(calls) == count
