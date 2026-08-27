from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

import requests

from agent.control.timer import TimerHandle, TimerStatus
from agent.plugin_composition import Context, HealthHandle, PluginTimers
from plugins.wake.contracts import WakeAlertSource, WakeContextSource
from .sleep_context import FitbitAdapterStore


class FitbitMonitorTransientError(RuntimeError):
    """表示 monitor HTTP/IO 边界可在下一次 Timer 重试。"""


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


class FitbitWakeRuntime:
    """上报 Fitbit Alert 与 Context，再登记一个 Timer。"""

    def __init__(
        self,
        store: FitbitAdapterStore,
        timers: PluginTimers,
        alerts: WakeAlertSource,
        context: WakeContextSource,
        monitor: FitbitMonitorClient,
        *,
        poll_interval: timedelta,
        sleep_ttl: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        after_provider_ack: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._timers = timers
        self._alerts = alerts
        self._context = context
        self._monitor = monitor
        self._poll_interval = poll_interval
        self._sleep_ttl = sleep_ttl
        self._now = now
        self._after_provider_ack = after_provider_ack
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

        deadline = self._store.next_due()
        while not self._closed:
            handle = self._timers.schedule(deadline)
            self._handle = handle
            try:
                receipt = await handle.result()
                if receipt.status is TimerStatus.CANCELLED or self._closed:
                    return
                try:
                    await asyncio.to_thread(self.tick)
                except FitbitMonitorTransientError as error:
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

    def tick(self) -> None:
        """先结算历史投递，再发布当前 monitor 快照。"""

        # 1. 只拉取一次；终态 Alert 先 ACK，其余按稳定身份上报。
        snapshot = self._monitor_snapshot()
        items = normalize_health_events(snapshot)
        now = _aware(self._now())
        for item in items:
            event_id = str(item["item_id"])
            status = self._alerts.status(
                source_id="fitbit-health-alerts",
                event_id=event_id,
            )
            if status in {"delivered", "skipped"}:
                self._ensure_not_pending(event_id)
                if self._after_provider_ack is not None:
                    self._after_provider_ack()
                continue
            _ = self._alerts.report(
                source_id="fitbit-health-alerts",
                event_id=event_id,
                payload=_mapping(item, "payload"),
                observed_at=now,
            )

        # 2. 睡眠状态是可覆盖、会过期的 Context，不参与 Content 初筛。
        sleep = normalize_sleep(snapshot)
        expires_at = now + self._sleep_ttl
        _ = self._context.report(
            source_id="fitbit-sleep",
            event_id="current",
            payload=sleep,
            observed_at=now,
            expires_at=expires_at,
        )
        self._store.commit_snapshot(
            sleep,
            observed_at=now,
            expires_at=expires_at,
            next_due=now + self._poll_interval,
        )

    def _monitor_snapshot(self) -> Mapping[str, object]:
        try:
            return self._monitor.snapshot()
        except (OSError, requests.RequestException) as error:
            raise FitbitMonitorTransientError(str(error)) from error

    def _ensure_not_pending(self, event_id: str) -> None:
        try:
            self._monitor.ensure_not_pending(event_id)
        except (OSError, requests.RequestException) as error:
            raise FitbitMonitorTransientError(str(error)) from error


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
    return tuple(
        sorted(
            items,
            key=lambda item: (str(item["item_id"]), str(item["revision"])),
        )
    )


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
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Fitbit Wake clock 必须带时区")
    return value.astimezone(UTC)
