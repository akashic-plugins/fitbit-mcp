"""本插件使用的工具与 MCP 结构合同；不导入工具 owner 的实现。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from agent.plugin_composition import Context, ServiceKey
from agent.plugin_contracts import CallRef, ContentPart, Message


Outcome = Literal["success", "denied", "error", "interrupted"]


@dataclass(frozen=True, slots=True)
class Result:
    """工具 owner 接受的最小结果结构。"""

    outcome: Outcome
    parts: tuple[ContentPart, ...]


class CallSource(Protocol):
    """已提交调用的只读来源；工具不取得任意 MessageLog。"""

    @property
    def call_ref(self) -> CallRef: ...

    @property
    def messages(self) -> tuple[Message, ...]: ...


class BoundTool(Protocol):
    """一次工具 binding 的窄执行接口。"""

    idempotent: bool

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: CallSource | None = None,
    ) -> Mapping[str, object] | str: ...

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result: ...

    async def query(self, key: str) -> Result | None: ...


class ToolRef(Protocol):
    """工具 owner 发布的不可变发现引用。"""

    name: str
    description: Mapping[str, object]


class ToolView(Protocol):
    """已授予的一组工具引用；插件只保存发现结果。"""

    refs: tuple[ToolRef, ...]


class ToolCatalog(Protocol):
    """工具 owner 的注册与 binding 入口。"""

    async def declare_group(
        self,
        ctx: Context,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> object: ...

    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: Callable[[Mapping[str, object]], AbstractAsyncContextManager[BoundTool]],
        capture: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        public: bool = True,
        idempotent: bool = False,
        risk: Literal["read-only", "read-write", "external-side-effect"] = "read-write",
        search_hint: str | None = None,
    ) -> ToolRef: ...

    def view(self, *refs: ToolRef) -> ToolView: ...


TOOLS = ServiceKey[ToolCatalog]("tools.v1")


class McpCallResult(Protocol):
    """MCP 路由调用的最小返回结构。"""

    @property
    def success(self) -> bool: ...

    @property
    def output(self) -> str: ...


class McpRoute(Protocol):
    """当前 MCP server binding 的短生命周期路由。"""

    async def call(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> McpCallResult: ...

    async def __aenter__(self) -> McpRoute: ...

    async def __aexit__(self, *args: object) -> None: ...


class McpToolDescription(Protocol):
    @property
    def input_schema(self) -> object: ...


class McpServerView(Protocol):
    tools: Mapping[str, McpToolDescription]

    def route(self) -> McpRoute: ...


class McpServers(Protocol):
    def open(
        self,
        ctx: Context,
        name: str,
        *,
        expected_catalog_digest: str | None = None,
    ) -> AbstractAsyncContextManager[McpServerView]: ...


__all__ = [
    "BoundTool",
    "CallSource",
    "McpCallResult",
    "McpRoute",
    "McpServerView",
    "McpServers",
    "Result",
    "ToolCatalog",
    "ToolRef",
    "ToolView",
    "TOOLS",
]
