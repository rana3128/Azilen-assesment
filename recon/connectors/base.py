from __future__ import annotations

import copy
import json
import logging
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from recon.models import Remediation, SystemRecord

log = logging.getLogger(__name__)
T = TypeVar("T")


class ConnectorError(Exception):
    pass


class TransientError(ConnectorError):
    """Retryable: timeouts, 429, 5xx."""


class PermanentError(ConnectorError):
    """Not retryable: 4xx auth/validation. Retrying only burns rate limit."""


class SystemUnavailable(ConnectorError):
    """Retries exhausted. The run continues; this system's checks are suppressed."""


@dataclass
class FailureSpec:
    """Fault injection for demos/tests. mode: 'transient' (fail N calls then recover), 'down', 'auth'."""

    mode: str
    count: int = 0

    @classmethod
    def parse(cls, spec: str) -> "FailureSpec":
        mode, _, n = spec.partition(":")
        return cls(mode=mode, count=int(n or 0))


class MockStore:
    """Stand-in for each vendor's API: one JSON file per system under a state dir, seeded from fixtures.

    Writes persist across CLI runs so re-running after --apply shows real convergence.
    """

    def __init__(self, state_dir: Path, fixtures_dir: Path):
        self.state_dir = state_dir
        self.fixtures_dir = fixtures_dir

    def reset(self) -> None:
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True)
        for f in self.fixtures_dir.glob("*.json"):
            shutil.copy(f, self.state_dir / f.name)

    def ensure(self) -> None:
        if not self.state_dir.exists() or not any(self.state_dir.glob("*.json")):
            self.reset()

    def load(self, system: str) -> list[dict[str, Any]]:
        return json.loads((self.state_dir / f"{system}.json").read_text())

    def save(self, system: str, rows: list[dict[str, Any]]) -> None:
        path = self.state_dir / f"{system}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2, default=str))
        tmp.replace(path)


def get_path(obj: dict[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class Connector(ABC):
    system: str
    id_path: str
    # canonical field -> raw path, used for writes and read-after-write verification
    field_paths: dict[str, str] = {}
    writable: frozenset[str] = frozenset()

    def __init__(
        self,
        store: MockStore,
        failure: FailureSpec | None = None,
        max_attempts: int = 3,
        base_delay: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.failure = failure
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._sleep = sleep
        self._calls = 0
        self.last_attempts = 0

    # ---- simulated transport -------------------------------------------------
    def _maybe_fail(self) -> None:
        self._calls += 1
        f = self.failure
        if not f:
            return
        if f.mode == "down":
            raise TransientError(f"{self.system}: 503 Service Unavailable")
        if f.mode == "auth":
            raise PermanentError(f"{self.system}: 401 invalid_client (token expired or revoked)")
        if f.mode == "transient" and self._calls <= f.count:
            raise TransientError(f"{self.system}: 429 Too Many Requests (call {self._calls})")

    def _with_retry(self, op: Callable[[], T]) -> T:
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            try:
                return op()
            except PermanentError:
                raise
            except TransientError as e:
                if attempt == self.max_attempts:
                    raise SystemUnavailable(f"{e} after {attempt} attempts") from e
                delay = self.base_delay * 2 ** (attempt - 1)
                log.warning("%s transient error (attempt %d), retrying in %.2fs: %s", self.system, attempt, delay, e)
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _read_raw(self) -> list[dict[str, Any]]:
        self._maybe_fail()
        return self.store.load(self.system)

    # ---- public API ------------------------------------------------------------
    def fetch_all(self) -> list[SystemRecord]:
        raw = self._with_retry(self._read_raw)
        return [self.transform(copy.deepcopy(r)) for r in raw]

    def current_value(self, record_id: str, field: str) -> Any:
        row = self._find(self._with_retry(self._read_raw), record_id)
        return None if row is None else get_path(row, self.field_paths[field])

    def apply(self, rem: Remediation) -> None:
        if rem.action == "update_field" and rem.field not in self.writable:
            raise PermanentError(f"{self.system}: field {rem.field!r} is not writable by this integration")

        def op() -> None:
            self._maybe_fail()
            rows = self.store.load(self.system)
            row = self._find(rows, rem.record_id)
            if row is None:
                raise PermanentError(f"{self.system}: record {rem.record_id} not found")
            self._mutate(row, rem)
            self.store.save(self.system, rows)

        self._with_retry(op)

    def _find(self, rows: list[dict[str, Any]], record_id: str | None) -> dict[str, Any] | None:
        return next((r for r in rows if get_path(r, self.id_path) == record_id), None)

    def _mutate(self, row: dict[str, Any], rem: Remediation) -> None:
        if rem.action == "update_field":
            set_path(row, self.field_paths[rem.field], self.to_raw(rem.field, rem.to_value))
        elif rem.action == "link_identity" and "workday_id" in self.field_paths:
            set_path(row, self.field_paths["workday_id"], rem.to_value)
        else:
            self.lifecycle_action(row, rem.action)

    def to_raw(self, field: str, value: Any) -> Any:
        return value

    def lifecycle_action(self, row: dict[str, Any], action: str) -> None:
        raise PermanentError(f"{self.system}: action {action} not supported")

    @abstractmethod
    def transform(self, raw: dict[str, Any]) -> SystemRecord: ...
