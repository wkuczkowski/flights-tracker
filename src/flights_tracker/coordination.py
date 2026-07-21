from __future__ import annotations

import asyncio
import contextvars
import fcntl
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    except (OSError, ValueError, TypeError):
        return {"state": "closed"}
    if not isinstance(data, dict) or data.get("state") not in {"closed", "open", "half_open"}:
        return {"state": "closed"}
    # Open without a numeric opened_at cannot cool down; treat as corrupt/closed.
    if data.get("state") == "open" and not isinstance(data.get("opened_at"), (int, float)):
        return {"state": "closed"}
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
    return {
        "state": state.get("state", "closed"),
        "cooldown_seconds": int(cooldown),
        "remaining_seconds": round(remaining, 1),
        "manual_half_open": bool(state.get("manual_half_open", False)),
    }


async def circuit_status() -> dict[str, Any]:
    return await asyncio.to_thread(_with_state_lock, lambda state: _public_state(state))


def _bot_error(state: dict[str, Any]) -> ProviderError:
    public = _public_state(state)
    return ProviderError(
        "BOT_CHALLENGE",
        "Skyscanner browser challenge cooldown is active; complete 'flights browser unlock' before one controlled retry",
        details={"circuit_breaker": public},
    )


def _check_or_claim_circuit() -> bool:
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
        raise _bot_error(state)

    return _with_state_lock(operation)


async def assert_circuit_allows_workflow() -> None:
    await asyncio.to_thread(_check_or_claim_circuit)


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
async def provider_workflow(deadline: float) -> AsyncIterator[None]:
    existing = _runtime.get()
    if existing is not None:
        if existing.blocked.is_set():
            raise _bot_error({"state": "open", "opened_at": time.time()})
        yield
        return

    await assert_circuit_allows_workflow()
    lock = await _acquire_workflow_lock(deadline)
    runtime = _WorkflowRuntime(deadline=deadline)
    token: contextvars.Token[_WorkflowRuntime | None] | None = None
    try:
        runtime.half_open = await asyncio.to_thread(_check_or_claim_circuit)
        token = _runtime.set(runtime)
        yield
    except ProviderError as exc:
        if exc.code == "BOT_CHALLENGE":
            runtime.blocked.set()
            await open_circuit()
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


async def before_provider_request() -> None:
    runtime = _runtime.get()
    if runtime is None:
        return
    if runtime.blocked.is_set():
        raise _bot_error({"state": "open", "opened_at": time.time()})
    runtime.request_started = True


def workflow_deadline(fallback: float) -> float:
    runtime = _runtime.get()
    return min(fallback, runtime.deadline) if runtime is not None else fallback


async def record_bot_challenge() -> None:
    runtime = _runtime.get()
    if runtime is None:
        return
    runtime.blocked.set()
    await open_circuit()


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
