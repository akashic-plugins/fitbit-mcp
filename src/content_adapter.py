from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

import requests

from agent.plugin_composition import Context, HealthHandle
from plugins.timer.contract import OneShotTimer, TimerHandle, TimerStatus
from plugins.watch.contract import WATCH_EVENTS, WatchEvents
from .adapter_store import FitbitAdapterStore

HEALTH_SOURCE = "fitbit-health-alerts"
SLEEP_SOURCE = "fitbit-sleep"
_SLEEP_TEXT = {"sleeping": "睡着了", "awake": "醒着"}


class FitbitTransientError(RuntimeError):
    """表示 monitor 或值班边界的临时失败，可在下一次 Timer 重试。"""


class FitbitMonitorClient:
    """读取 monitor 快照，并以目标状态完成 ACK。"""

    def __init__(self, base_url: str = "http://127.0.0.1:18765") -> None:
        self._base_url = base_url.rstrip("/")

    def snapshot(self) -> Mapping[str, object]:
        response = requests.get(f"{self._base_url}/api/agent", timeout=8)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("Fitbit monitor /api/agent 必须返回对象")
        return cast(Mapping[str, object], payload)

    def ensure_not_pending(self, event_id: str) -> None:
        """确保事件已离开 monitor 队列，并允许 ACK 重放。"""

        response = requests.post(
            f"{self._base_url}/api/agent/acknowledge/{quote(event_id, safe='')}",
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("acknowledged"), bool
        ):
            raise TypeError("Fitbit monitor ACK 必须返回 acknowledged bool")
        pending = self.snapshot().get("health_events")
        if not isinstance(pending, list):
            raise TypeError("Fitbit monitor health_events 必须是数组")
        if any(_event_id(item) == event_id for item in pending):
            raise RuntimeError(f"Fitbit event ACK 后仍在 pending 队列: {event_id}")


class BorrowedWatchEvents:
    """每次 emit 时临时借用值班服务；值班已卸载则按临时失败处理。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    async def emit(
        self,
        source: str,
        *,
        title: str = "",
        text: str = "",
        url: str = "",
        at: datetime | None = None,
        state: bool = False,
        key: str = "",
    ) -> str:
        async with self._ctx.runtime_scope():
            with self._ctx.borrow(WATCH_EVENTS) as events:
                if events is None:
                    raise FitbitTransientError("值班服务当前不可用")
                return await events.emit(
                    source, title=title, text=text, url=url, at=at, state=state, key=key
                )


@dataclass(frozen=True)
class HealthEvent:
    """monitor 队列里的一条健康事件，id 即交给值班的 key。"""

    id: str
    title: str
    text: str
    at: datetime | None


class FitbitWakeRuntime:
    """把 Fitbit 健康事件与睡眠状态交给值班，再登记一个 Timer。"""

    def __init__(
        self,
        store: FitbitAdapterStore,
        timers: OneShotTimer,
        events: WatchEvents,
        monitor: FitbitMonitorClient,
        *,
        poll_interval: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monitor_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._timers = timers
        self._events = events
        self._monitor = monitor
        self._poll_interval = poll_interval
        self._now = now
        self._monitor_ready = monitor_ready
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self, ctx: Context, health: HealthHandle) -> None:
        """恢复来源 deadline，并启动唯一 Fiber-owned 采集循环。"""

        if self._closed:
            raise RuntimeError("Fitbit Wake runtime 已关闭")
        if self._task is None:
            self._task = await ctx.spawn(
                self._run(ctx, health), name="fitbit-wake-poll"
            )

    async def close(self) -> None:
        """取消自有等待，不改写 Content 或来源事实。"""

        self._closed = True
        handle = self._handle
        task = self._task
        self._handle = None
        self._task = None
        if handle is not None:
            _ = await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            _ = await asyncio.gather(task, return_exceptions=True)
        if handle is not None:
            await handle.cleanup()

    async def _run(self, ctx: Context, health: HealthHandle) -> None:
        """轮询、显式记录临时失败，并只在可恢复结果后重臂。"""

        # monitor 在后台就绪；首轮前等它可用，避免开机第一轮必然失败。
        if self._monitor_ready is not None:
            await self._monitor_ready()
        deadline = self._store.next_due()
        while not self._closed:
            handle = self._timers.schedule(deadline)
            self._handle = handle
            try:
                receipt = await handle.result()
                if receipt.status is TimerStatus.CANCELLED or self._closed:
                    return
                try:
                    await self.tick()
                except FitbitTransientError as error:
                    reason = f"{type(error).__name__}: {error}"
                    health.degrade(reason)
                    _ = ctx.report_incident("fitbit_content_retry", reason)
                    deadline = _aware(self._now()) + self._poll_interval
                else:
                    health.recover()
                    deadline = self._store.next_due()
            finally:
                self._handle = None
                await handle.cleanup()

    # 每条健康事件 emit 成功后立刻向 monitor 确认；睡眠只交一次当前状态。
    async def tick(self) -> None:
        # 1. 只拉取一次 monitor 快照。
        snapshot = await asyncio.to_thread(self._monitor_snapshot)
        # 2. 升级后的第一轮，队列里是 EventMail 时代已交过的旧事件，只确认不重交。
        cutover = self._store.cutover_pending()
        for event in parse_health_events(snapshot):
            if not cutover:
                await self._events.emit(
                    HEALTH_SOURCE, title=event.title, text=event.text, at=event.at, key=event.id
                )
            await asyncio.to_thread(self._ensure_not_pending, event.id)
        # 3. 睡眠状态数据还没同步到时是 unknown：不交，值班侧保留上一次已知状态。
        state = sleep_state(snapshot)
        if state != "unknown":
            await self._events.emit(
                SLEEP_SOURCE, text="睡眠状态：" + _SLEEP_TEXT.get(state, state), state=True
            )
        # 4. 全部交付后才推进下一个 deadline。
        await asyncio.to_thread(
            self._store.commit_tick, next_due=_aware(self._now()) + self._poll_interval
        )

    def _monitor_snapshot(self) -> Mapping[str, object]:
        try:
            return self._monitor.snapshot()
        except (OSError, requests.RequestException) as error:
            raise FitbitTransientError(str(error)) from error

    def _ensure_not_pending(self, event_id: str) -> None:
        try:
            self._monitor.ensure_not_pending(event_id)
        except (OSError, requests.RequestException) as error:
            raise FitbitTransientError(str(error)) from error


# 把 monitor 队列转换成按 id 排序的健康事件。
def parse_health_events(snapshot: Mapping[str, object]) -> tuple[HealthEvent, ...]:
    raw_events = snapshot.get("health_events")
    if not isinstance(raw_events, list):
        raise TypeError("Fitbit monitor health_events 必须是数组")
    events: list[HealthEvent] = []
    for raw in cast(list[object], raw_events):
        if not isinstance(raw, Mapping):
            raise TypeError("Fitbit health event 必须是对象")
        item = cast(Mapping[str, object], raw)
        events.append(
            HealthEvent(
                id=_event_id(item),
                title=_string(item, "type"),
                text=_string(item, "message"),
                at=_parse_created_at(item.get("created_at")),
            )
        )
    return tuple(sorted(events, key=lambda event: event.id))


def sleep_state(snapshot: Mapping[str, object]) -> str:
    sleep = snapshot.get("sleep")
    if not isinstance(sleep, Mapping):
        raise TypeError("Fitbit monitor sleep 必须是对象")
    return _string(cast(Mapping[str, object], sleep), "state")


# monitor 的 created_at 是不带时区的本地时间，如 "2026-08-23 08:00"。
def _parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return None


def _event_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("Fitbit health event 必须是对象")
    return _string(cast(Mapping[str, object], value), "id")


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Fitbit {name} 必须是非空字符串")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Fitbit Wake clock 必须带时区")
    return value.astimezone(UTC)
