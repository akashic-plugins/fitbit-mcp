from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent.plugin_composition import ServiceKey


class WatchEvents(Protocol):
    async def emit(
        self, source: str, *, title: str = "", text: str = "", url: str = "", at: datetime | None = None,
        state: bool = False,
    ) -> str: ...


# 值班（watch）是可选的：在的话把当前状态交给它，不在时这个键没有提供方。
WATCH_EVENTS = ServiceKey[WatchEvents]("watch.events.v1")
