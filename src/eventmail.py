from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from agent.plugin_composition import ServiceKey


class BoundAlertSource(Protocol):
    def report(
        self,
        *,
        event_id: str,
        payload: Mapping[str, object],
        observed_at: datetime,
        expires_at: datetime | None = None,
    ) -> Mapping[str, object]: ...

    def status(self, *, event_id: str) -> str | None: ...


class AlertSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundAlertSource: ...


class BoundContextSource(Protocol):
    def report(
        self,
        *,
        event_id: str,
        payload: Mapping[str, object],
        observed_at: datetime,
        expires_at: datetime | None = None,
    ) -> Mapping[str, object]: ...


class ContextSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundContextSource: ...


EVENTMAIL_ALERT_SOURCE = ServiceKey[AlertSourceServices](
    "eventmail.alert_source.v1"
)
EVENTMAIL_CONTEXT_SOURCE = ServiceKey[ContextSourceServices](
    "eventmail.context_source.v1"
)
