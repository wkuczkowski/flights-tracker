from __future__ import annotations

import asyncio
import contextvars
import fcntl
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx

from .errors import ProviderError

DEFAULT_COOLDOWN_SECONDS = 15 * 60
_POLL_INTERVAL = 0.05
_T = TypeVar("_T")


@dataclass
class _WorkflowRuntime:
    deadline: float = float("inf")
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    cache: dict[tuple[Any, ...], asyncio.Task[Any]] = field(default_factory=dict)
    clients: dict[tuple[int | None, str], httpx.AsyncClient] = field(default_factory=dict)
    request_started: bool = False
    requests_started: int = 0
    request_budget: int | None = None
    current_phase: str = "local_gate"
    half_open: bool = False


_runtime: contextvars.ContextVar[_WorkflowRuntime | None] = contextvars.ContextVar(
    "flights_provider_workflow", default=None
)


def _state_directory() -> Path:
    override = os.environ.get("FLIGHTS_TRACKER_STATE_DIR")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "flights-tracker"
    return Path.home() / ".cache" / "flights-tracker"


def _cooldown_seconds() -> float:
    value = os.environ.get("FLIGHTS_TRACKER_BOT_COOLDOWN_SECONDS")
    if value is None:
        return float(DEFAULT_COOLDOWN_SECONDS)
    try:
        return max(1.0, float(value))
    except ValueError:
        return float(DEFAULT_COOLDOWN_SECONDS)


def _prepare_directory() -> Path:
    directory = _state_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _open_lock(name: str) -> Any:
    path = _prepare_directory() / name
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    return os.fdopen(descriptor, "a+")


def _read_state_unlocked() -> dict[str, Any]:
    path = _state_directory() / "provider-circuit.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"state": "closed", "_storage_status": "missing"}
    except (OSError, ValueError, TypeError):
        return {"state": "closed", "_storage_status": "corrupt"}
    if not isinstance(data, dict) or data.get("state") not in {"closed", "open", "half_open"}:
        return {"state": "closed", "_storage_status": "corrupt"}
    # Open without a numeric opened_at cannot cool down; treat as corrupt/closed.
    if data.get("state") == "open" and not isinstance(data.get("opened_at"), (int, float)):
        return {"state": "closed", "_storage_status": "corrupt"}
    data["_storage_status"] = "valid"
    return data


def _write_state_unlocked(state: dict[str, Any]) -> None:
    directory = _prepare_directory()
    path = directory / "provider-circuit.json"
    temporary = directory / f".provider-circuit-{os.getpid()}.tmp"
    temporary.write_text(json.dumps(state, separators=(",", ":")))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _with_state_lock(operation: Callable[[dict[str, Any]], _T]) -> _T:
    with _open_lock("provider-state.lock") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return operation(_read_state_unlocked())


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    opened_at = state.get("opened_at")
    cooldown = _cooldown_seconds()
    remaining = 0.0
    if state.get("state") == "open" and isinstance(opened_at, (int, float)):
        remaining = max(0.0, cooldown - (now - float(opened_at)))
    storage_status = str(state.get("_storage_status", "valid"))
    if state.get("state") == "open" and remaining == 0:
        storage_status = "stale"
    opened_at_iso = _timestamp(opened_at)
    next_probe_at = _timestamp(float(opened_at) + cooldown) if isinstance(opened_at, (int, float)) else None
    return {
        "state": state.get("state", "closed"),
        "opened_at": opened_at_iso,
        "next_probe_at": next_probe_at,
        "cooldown_seconds": int(cooldown),
        "cooldown_remaining": round(remaining, 1),
        # Kept for backward compatibility with the original JSON contract.
        "remaining_seconds": round(remaining, 1),
        "manual_half_open": bool(state.get("manual_half_open", False)),
        "storage_status": storage_status,
    }


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value), UTC).isoformat().replace("+00:00", "Z")


def circuit_search_readiness(circuit: dict[str, Any]) -> dict[str, str]:
    state = circuit.get("state")
    if state == "closed":
        return {"status": "allowed", "reason": "circuit_closed"}
    if state == "half_open":
        reason = "manual_half_open" if circuit.get("manual_half_open") else "cooldown_elapsed"
        return {"status": "controlled_retry", "reason": reason}
    if float(circuit.get("cooldown_remaining") or 0) > 0:
        return {"status": "blocked", "reason": "local_cooldown"}
    return {"status": "controlled_retry", "reason": "cooldown_elapsed"}


async def circuit_status() -> dict[str, Any]:
    return await asyncio.to_thread(_with_state_lock, lambda state: _public_state(state))


def _request_usage(runtime: _WorkflowRuntime | None, budget: int | None = None) -> dict[str, int] | None:
    limit = runtime.request_budget if runtime is not None else budget
    if limit is None:
        return None
    started = runtime.requests_started if runtime is not None else 0
    return {"limit": limit, "started": started, "remaining": max(0, limit - started)}


def workflow_request_usage() -> dict[str, int] | None:
    return _request_usage(_runtime.get())


def _bot_error(
    state: dict[str, Any],
    *,
    budget: int | None = None,
    runtime: _WorkflowRuntime | None = None,
) -> ProviderError:
    public = _public_state(state)
    details: dict[str, Any] = {
        "source": "local_circuit",
        "network_attempted": False,
        "provider_phase": "local_gate",
        "challenge_kind": "local_cooldown",
        "circuit_breaker": public,
    }
    if usage := _request_usage(runtime, budget):
        details["request_budget"] = usage
    return ProviderError(
        "BOT_CHALLENGE",
        "Local provider cooldown blocked this workflow; no request reached the provider",
        retryable=False,
        details=details,
    )


def _check_or_claim_circuit(request_budget: int | None = None) -> bool:
    def operation(state: dict[str, Any]) -> bool:
        if state.get("state") == "closed":
            return False
        if state.get("state") == "half_open":
            return True
        opened_at = state.get("opened_at")
        if isinstance(opened_at, (int, float)) and time.time() - float(opened_at) >= _cooldown_seconds():
            _write_state_unlocked({
                "state": "half_open",
                "opened_at": opened_at,
                "manual_half_open": False,
                "updated_at": time.time(),
            })
            return True
        raise _bot_error(state, budget=request_budget)

    return _with_state_lock(operation)


async def assert_circuit_allows_workflow(request_budget: int | None = None) -> None:
    await asyncio.to_thread(_check_or_claim_circuit, request_budget)


async def open_circuit() -> None:
    now = time.time()

    def operation(state: dict[str, Any]) -> None:
        _write_state_unlocked({
            "state": "open",
            "opened_at": now,
            "manual_half_open": False,
            "updated_at": now,
        })

    await asyncio.to_thread(_with_state_lock, operation)


async def allow_manual_half_open() -> None:
    now = time.time()

    def operation(state: dict[str, Any]) -> None:
        _write_state_unlocked({
            "state": "half_open",
            "opened_at": state.get("opened_at", now),
            "manual_half_open": True,
            "updated_at": now,
        })

    await asyncio.to_thread(_with_state_lock, operation)


async def _close_circuit_if_half_open() -> None:
    def operation(state: dict[str, Any]) -> None:
        if state.get("state") == "half_open":
            _write_state_unlocked({"state": "closed", "updated_at": time.time()})

    await asyncio.to_thread(_with_state_lock, operation)


async def _acquire_workflow_lock(deadline: float) -> Any:
    lock = await asyncio.to_thread(_open_lock, "provider-workflow.lock")
    try:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError(
                        "PROVIDER_TIMEOUT",
                        "Deadline reached while waiting for another provider workflow",
                        retryable=True,
                    )
                await asyncio.sleep(min(_POLL_INTERVAL, remaining))
    except BaseException:
        lock.close()
        raise


@asynccontextmanager
async def provider_workflow_lock(deadline: float) -> AsyncIterator[None]:
    lock = await _acquire_workflow_lock(deadline)
    try:
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


@asynccontextmanager
async def provider_workflow(deadline: float, *, request_budget: int | None = None) -> AsyncIterator[None]:
    existing = _runtime.get()
    if existing is not None:
        if existing.blocked.is_set():
            state = await circuit_status()
            details: dict[str, Any] = {
                "source": "local_circuit",
                "network_attempted": False,
                "provider_phase": "local_gate",
                "challenge_kind": "local_cooldown",
                "circuit_breaker": state,
            }
            if usage := _request_usage(existing):
                details["request_budget"] = usage
            raise ProviderError(
                "BOT_CHALLENGE",
                "Local workflow stop signal blocked another provider request",
                retryable=False,
                details=details,
            )
        yield
        return

    await assert_circuit_allows_workflow(request_budget)
    lock = await _acquire_workflow_lock(deadline)
    runtime = _WorkflowRuntime(deadline=deadline, request_budget=request_budget)
    token: contextvars.Token[_WorkflowRuntime | None] | None = None
    try:
        runtime.half_open = await asyncio.to_thread(_check_or_claim_circuit, request_budget)
        token = _runtime.set(runtime)
        yield
    except ProviderError as exc:
        if exc.code == "BOT_CHALLENGE":
            runtime.blocked.set()
            details = dict(exc.details)
            if details.get("source") != "local_circuit" and "circuit_breaker" not in details:
                await open_circuit()
            details.setdefault("source", "provider_response")
            details.setdefault("network_attempted", details["source"] == "provider_response")
            details.setdefault("provider_phase", runtime.current_phase)
            details.setdefault("challenge_kind", "provider_blocked")
            details["circuit_breaker"] = await circuit_status()
            if usage := _request_usage(runtime):
                details["request_budget"] = usage
            exc.details = details
            exc.retryable = False
        raise
    finally:
        for task in runtime.cache.values():
            if not task.done():
                task.cancel()
        if runtime.cache:
            await asyncio.gather(*runtime.cache.values(), return_exceptions=True)
        if runtime.clients:
            await asyncio.gather(*(client.aclose() for client in runtime.clients.values()), return_exceptions=True)
        if token is not None:
            _runtime.reset(token)
        if runtime.half_open and runtime.request_started and not runtime.blocked.is_set():
            await _close_circuit_if_half_open()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


async def before_provider_request(provider_phase: str = "provider_request") -> None:
    runtime = _runtime.get()
    if runtime is None:
        return
    if runtime.blocked.is_set():
        raise _bot_error(
            {"state": "open", "opened_at": time.time(), "_storage_status": "valid"},
            runtime=runtime,
        )
    runtime.current_phase = provider_phase
    if runtime.request_budget is not None and runtime.requests_started >= runtime.request_budget:
        raise ProviderError(
            "REQUEST_BUDGET_EXCEEDED",
            "Provider request budget was exhausted before another request could start",
            retryable=False,
            details={
                "source": "local_budget",
                "network_attempted": False,
                "provider_phase": provider_phase,
                "request_budget": _request_usage(runtime),
            },
        )
    runtime.request_started = True
    runtime.requests_started += 1


def workflow_deadline(fallback: float) -> float:
    runtime = _runtime.get()
    return min(fallback, runtime.deadline) if runtime is not None else fallback


async def record_bot_challenge(
    *, provider_phase: str, challenge_kind: str,
) -> dict[str, Any]:
    runtime = _runtime.get()
    if runtime is not None:
        runtime.blocked.set()
    await open_circuit()
    details: dict[str, Any] = {
        "source": "provider_response",
        "network_attempted": True,
        "provider_phase": provider_phase,
        "challenge_kind": challenge_kind,
        "circuit_breaker": await circuit_status(),
    }
    if usage := _request_usage(runtime):
        details["request_budget"] = usage
    return details


async def cached_provider_call(key: tuple[Any, ...], call: Callable[[], Awaitable[_T]]) -> _T:
    runtime = _runtime.get()
    if runtime is None:
        return await call()
    task = runtime.cache.get(key)
    if task is None:
        task = asyncio.create_task(call())
        runtime.cache[key] = task
    return await asyncio.shield(task)


@asynccontextmanager
async def workflow_client(
    *,
    transport: httpx.AsyncBaseTransport | None,
    timeout: httpx.Timeout,
) -> AsyncIterator[httpx.AsyncClient]:
    runtime = _runtime.get()
    if runtime is None:
        async with httpx.AsyncClient(
            base_url="https://www.skyscanner.pl",
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            yield client
        return
    key = (id(transport) if transport is not None else None, repr(timeout))
    client = runtime.clients.get(key)
    if client is None:
        client = httpx.AsyncClient(
            base_url="https://www.skyscanner.pl",
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        runtime.clients[key] = client
    yield client
