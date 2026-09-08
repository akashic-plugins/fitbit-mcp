from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from agent.plugin_composition import MCP_SERVERS, Context
from agent.plugins.mcp_generation_host import McpRoute
from plugins.tools.api import BoundTool, CallSource, Result
from plugins.tools.plugin import TOOLS
from session.message import ContentPart
from session.message_codec import json_value


class McpTool:
    """一次绑定只持有插件自己声明的 MCP 路由。"""

    def __init__(self, route: McpRoute, name: str, *, idempotent: bool):
        self._route = route
        self._name = name
        self.idempotent = idempotent

    async def prepare(
        self, arguments: Mapping[str, object], source: CallSource | None = None,
    ) -> Mapping[str, object]:
        # MCP 服务在执行前校验自己的 schema；这里不复制其参数规则。
        return arguments

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        value = await self._route.call(self._name, arguments)
        return Result(
            "success" if value.success else "error",
            (ContentPart("text", value.output),),
        )

    async def query(self, key: str) -> Result | None:
        # MCP 不提供按调用 key 查询回执；写操作不能据此重放。
        return None


async def register_tools(ctx: Context) -> None:
    """发布随源码交付的发现目录；只有实际调用才打开 MCP。"""
    catalog = json.loads(Path(__file__).with_name("tool_catalog.json").read_text())
    for item in catalog:
        await _register_tool(ctx, item)


async def _register_tool(ctx: Context, item: dict) -> None:
    """固定单个工具的描述、风险和所属 generation 路由。"""
    @asynccontextmanager
    async def open_tool(_options: Mapping[str, object]) -> AsyncIterator[BoundTool]:
        async with ctx.require(MCP_SERVERS).open(ctx, "fitbit") as server:
            if json_value(server.tools[item["name"]].input_schema) != item["parameters"]:
                raise RuntimeError(f"MCP 工具 schema 与发现目录不一致: {item['name']}")
            async with server.route() as route:
                yield McpTool(route, item["name"], idempotent=item["read_only"])

    await ctx.require(TOOLS).register(
        ctx,
        name=f"mcp_fitbit__{item['name']}",
        description=f"[MCP:fitbit] {item['description']}",
        parameters=item["parameters"],
        open=open_tool,
        idempotent=item["read_only"],
        risk="read-only" if item["read_only"] else "read-write",
    )
