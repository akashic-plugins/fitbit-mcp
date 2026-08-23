from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import quote

import requests

from agent.control.timer import TimerHandle, TimerStatus
from agent.plugin_composition import PluginTimers
from src.sleep_context import FitbitAdapterStore


class BoundContentSource(Protocol):
    def submit(
        self, batch_id: str, items: Sequence[Mapping[str, object]]
    ) -> Mapping[str, object]: ...

    def unsettled(self, limit: int = 100) -> tuple[Mapping[str, object], ...]: ...

    def ack(self, settlement_ref: str) -> Mapping[str, object]: ...


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


class FitbitContentRuntime:
    """结算已投递 ACK，提交一次 monitor 快照，再登记一个 Timer。"""

    def __init__(
        self,
        store: FitbitAdapterStore,
        timers: PluginTimers,
        content: BoundContentSource,
        monitor: FitbitMonitorClient,
        *,
        poll_interval: timedelta,
        sleep_ttl: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        after_provider_ack: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._timers = timers
        self._content = content
        self._monitor = monitor
        self._poll_interval = poll_interval
        self._sleep_ttl = sleep_ttl
        self._now = now
        self._after_provider_ack = after_provider_ack
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """恢复来源 deadline，并登记唯一正式 Timer。"""

        if self._closed:
            raise RuntimeError("Fitbit Content runtime 已关闭")
        if self._handle is not None:
            return
        self._arm(self._store.next_due())

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

    def _arm(self, deadline: datetime) -> None:
        if self._closed or self._handle is not None:
            return
        handle = self._timers.schedule(deadline)
        self._handle = handle
        self._task = asyncio.create_task(
            self._wait_tick_rearm(handle), name="fitbit:content-poll"
        )

    async def _wait_tick_rearm(self, handle: TimerHandle) -> None:
        """Timer 到点后执行一次采集，再从持久状态重新登记。"""

        try:
            receipt = await handle.result()
            if receipt.status is TimerStatus.CANCELLED or self._closed:
                return
            await asyncio.to_thread(self.tick)
        finally:
            self._handle = None
            self._task = None
            await handle.cleanup()
        if not self._closed:
            self._arm(self._store.next_due())

    def tick(self) -> None:
        """先结算历史投递，再发布当前 monitor 快照。"""

        # 1. 先完成外部 ACK，再结算 Content
        self._drain_unsettled()

        # 2. 只拉取一次，独立归一化健康事件，并优先提交 Content
        snapshot = self._monitor.snapshot()
        items = normalize_health_events(snapshot)
        batch_id = stable_batch_id(items)
        _ = self._content.submit(batch_id, items)

        # 3. Content 接受批次后，才推进私有缓存和 deadline
        now = _aware(self._now())
        sleep = normalize_sleep(snapshot)
        self._store.commit_snapshot(
            sleep,
            observed_at=now,
            expires_at=now + self._sleep_ttl,
            next_due=now + self._poll_interval,
        )

    def _drain_unsettled(self) -> None:
        while True:
            rows = self._content.unsettled(limit=100)
            for row in rows:
                payload = _mapping(row, "payload")
                event_id = _string(payload, "upstream_event_id")
                settlement_ref = _string(row, "settlement_ref")
                self._monitor.ensure_not_pending(event_id)
                if self._after_provider_ack is not None:
                    self._after_provider_ack()
                settled = self._content.ack(settlement_ref)
                if settled.get("settled") is not True:
                    raise RuntimeError(
                        f"Fitbit Content ACK 未结算: {dict(settled)!r}"
                    )
            if len(rows) < 100:
                return


def normalize_health_events(
    snapshot: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """把 monitor 队列转换成身份稳定的 Content revisions。"""

    raw_events = snapshot.get("health_events")
    if not isinstance(raw_events, list):
        raise TypeError("Fitbit monitor health_events 必须是数组")
    items: list[Mapping[str, object]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise TypeError("Fitbit health event 必须是对象")
        event_id = _event_id(raw)
        payload: dict[str, object] = {
            "upstream_event_id": event_id,
            "source_type": "health_event",
            "source_name": "fitbit",
            "title": _string(raw, "type"),
            "content": _string(raw, "message"),
            "severity": _string(raw, "severity"),
            "published_at": raw.get("created_at"),
            "suggested_tone": raw.get("suggested_tone", ""),
            "metrics": raw.get("metrics", {}),
        }
        revision = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        items.append(
            {
                "item_id": event_id,
                "revision": revision,
                "payload": payload,
                "not_before": None,
                "requires_ack": True,
            }
        )
    return tuple(items)


def normalize_sleep(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    sleep = snapshot.get("sleep")
    if not isinstance(sleep, Mapping):
        raise TypeError("Fitbit monitor sleep 必须是对象")
    return {
        "state": _string(sleep, "state"),
        "prob": sleep.get("prob"),
        "prob_source": sleep.get("prob_source"),
        "data_lag_min": sleep.get("data_lag_min"),
        "sleep_24h": snapshot.get("sleep_24h", {}),
        "last_updated": snapshot.get("last_updated"),
    }


def stable_batch_id(items: Sequence[Mapping[str, object]]) -> str:
    identity = sorted(
        (
            {"item_id": item["item_id"], "revision": item["revision"]}
            for item in items
        ),
        key=lambda item: (str(item["item_id"]), str(item["revision"])),
    )
    return "fitbit-monitor:" + hashlib.sha256(
        _canonical(identity).encode("utf-8")
    ).hexdigest()


def _event_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("Fitbit health event 必须是对象")
    return _string(cast(Mapping[str, object], value), "id")


def _mapping(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"Fitbit {name} 必须是对象")
    return cast(Mapping[str, object], value)


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Fitbit {name} 必须是非空字符串")
    return value


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Fitbit Content clock 必须带时区")
    return value.astimezone(UTC)
