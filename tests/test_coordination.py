from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from flights_tracker.coordination import (
    allow_manual_half_open,
    before_provider_request,
    circuit_status,
    open_circuit,
    provider_workflow,
)
from flights_tracker.cli import dispatch, parser
from flights_tracker.errors import FlightsError, ProviderError
from flights_tracker.provider import SkyscannerWebProvider
from flights_tracker.service import run_search


def _hold_workflow(
    state_directory: str,
    label: str,
    hold_seconds: float,
    output: Any,
    acquired: Any | None = None,
    crash: bool = False,
) -> None:
    os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_directory

    async def run() -> None:
        async with provider_workflow(time.monotonic() + 5):
            output.put((label, "acquired", time.monotonic()))
            if acquired is not None:
                acquired.set()
            if crash:
                os._exit(0)
            await asyncio.sleep(hold_seconds)
        output.put((label, "released", time.monotonic()))

    asyncio.run(run())


def test_provider_workflows_are_serialized_between_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    state_directory = str(tmp_path / "shared")
    first = context.Process(target=_hold_workflow, args=(state_directory, "first", 0.25, output))
    first.start()
    first_acquired = output.get(timeout=2)
    second = context.Process(target=_hold_workflow, args=(state_directory, "second", 0.0, output))
    second.start()
    first_released = output.get(timeout=2)
    second_acquired = output.get(timeout=2)
    second_released = output.get(timeout=2)
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.exitcode == second.exitcode == 0
    assert first_acquired[:2] == ("first", "acquired")
    assert first_released[:2] == ("first", "released")
    assert second_acquired[:2] == ("second", "acquired")
    assert second_acquired[2] >= first_released[2]
    assert second_released[:2] == ("second", "released")


def test_workflow_lock_is_released_after_process_crash(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    acquired = context.Event()
    state_directory = str(tmp_path / "shared")
    crashed = context.Process(
        target=_hold_workflow,
        args=(state_directory, "crashed", 0.0, output, acquired, True),
    )
    crashed.start()
    assert acquired.wait(timeout=2)
    crashed.join(timeout=2)
    survivor = context.Process(
        target=_hold_workflow, args=(state_directory, "survivor", 0.0, output)
    )
    survivor.start()
    survivor.join(timeout=2)
    assert survivor.exitcode == 0


def test_workflow_lock_wait_respects_deadline(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    state_directory = str(tmp_path / "shared")
    holder = context.Process(
        target=_hold_workflow, args=(state_directory, "holder", 0.3, output)
    )
    holder.start()
    assert output.get(timeout=2)[:2] == ("holder", "acquired")
    os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_directory

    async def contend() -> None:
        async with provider_workflow(time.monotonic() + 0.05):
            raise AssertionError("contender must not acquire the lock")

    with pytest.raises(ProviderError) as caught:
        asyncio.run(contend())
    holder.join(timeout=2)
    assert caught.value.code == "PROVIDER_TIMEOUT"
    assert holder.exitcode == 0


def test_local_validation_does_not_wait_for_provider_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    state_directory = str(tmp_path / "shared")
    holder = context.Process(
        target=_hold_workflow, args=(state_directory, "holder", 0.3, output)
    )
    holder.start()
    assert output.get(timeout=2)[:2] == ("holder", "acquired")
    os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_directory
    started = time.monotonic()

    with pytest.raises(FlightsError) as caught:
        asyncio.run(run_search({"origins": []}))

    assert getattr(caught.value, "code", None) == "INVALID_ARGUMENT"
    assert time.monotonic() - started < 0.15
    holder.join(timeout=2)
    assert holder.exitcode == 0


@pytest.mark.asyncio
async def test_open_circuit_fails_fast_without_provider_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=[])

    await open_circuit()
    with pytest.raises(ProviderError) as caught:
        await run_search(
            {
                "origins": [{"iata": "WAW"}],
                "destination": {"iata": "ROM"},
                "trip": {"type": "one_way", "depart": {"date": "2027-01-01"}},
            },
            transport=httpx.MockTransport(handler),
        )
    assert caught.value.code == "BOT_CHALLENGE"
    assert requests == 0


def test_open_circuit_fails_fast_in_another_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    state_directory = str(tmp_path / "shared")
    os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_directory
    asyncio.run(open_circuit())
    output = context.Queue()

    def child(state_dir: str, out: Any) -> None:
        os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_dir
        os.environ["FLIGHTS_TRACKER_BOT_COOLDOWN_SECONDS"] = "60"
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, json=[])

        async def run() -> None:
            try:
                await run_search(
                    {
                        "origins": [{"iata": "WAW"}],
                        "destination": {"iata": "ROM"},
                        "trip": {"type": "one_way", "depart": {"date": "2027-01-01"}},
                    },
                    transport=httpx.MockTransport(handler),
                )
                out.put(("ok", requests))
            except ProviderError as exc:
                out.put((exc.code, requests))

        asyncio.run(run())

    process = context.Process(target=child, args=(state_directory, output))
    process.start()
    code, requests = output.get(timeout=5)
    process.join(timeout=5)
    assert process.exitcode == 0
    assert code == "BOT_CHALLENGE"
    assert requests == 0


@pytest.mark.asyncio
async def test_corrupt_circuit_state_is_treated_as_closed(tmp_path: Path) -> None:
    state_file = tmp_path / "provider-state" / "provider-circuit.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"state":"open"}')
    assert (await circuit_status())["state"] == "closed"
    state_file.write_text("{not-json")
    assert (await circuit_status())["state"] == "closed"
    state_file.write_text('{"state":"melted","opened_at":1}')
    assert (await circuit_status())["state"] == "closed"
    async with provider_workflow(time.monotonic() + 1):
        pass


@pytest.mark.asyncio
async def test_cooldown_expiry_claims_half_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHTS_TRACKER_BOT_COOLDOWN_SECONDS", "1")
    await open_circuit()
    assert (await circuit_status())["state"] == "open"
    await asyncio.sleep(1.05)
    async with provider_workflow(time.monotonic() + 1):
        await before_provider_request()
    assert (await circuit_status())["state"] == "closed"


@pytest.mark.asyncio
async def test_manual_half_open_allows_one_probe_and_closes_on_success() -> None:
    await open_circuit()
    await allow_manual_half_open()
    assert (await circuit_status())["state"] == "half_open"
    async with provider_workflow(time.monotonic() + 1):
        await before_provider_request()
    assert (await circuit_status())["state"] == "closed"


@pytest.mark.asyncio
async def test_request_scoped_autosuggest_cache_deduplicates_inflight_calls() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=[{"PlaceName": "Warszawa", "GeoId": "1"}])

    async with provider_workflow(time.monotonic() + 1):
        async with httpx.AsyncClient(
            base_url="https://www.skyscanner.pl",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SkyscannerWebProvider(client)
            first, second = await asyncio.gather(
                provider.autosuggest("Warszawa"),
                provider.autosuggest("Warszawa"),
            )
    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_bot_stop_signal_blocks_later_provider_requests() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(403, json={"reason": "blocked"})
        return httpx.Response(200, json=[{"PlaceName": "X", "GeoId": "1"}])

    async with provider_workflow(time.monotonic() + 2):
        async with httpx.AsyncClient(
            base_url="https://www.skyscanner.pl",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SkyscannerWebProvider(client)
            with pytest.raises(ProviderError) as first:
                await provider.autosuggest("first")
            assert first.value.code == "BOT_CHALLENGE"
            with pytest.raises(ProviderError) as second:
                await provider.autosuggest("second")
            assert second.value.code == "BOT_CHALLENGE"
    assert requests == 1
    assert (await circuit_status())["state"] == "open"


@pytest.mark.asyncio
async def test_fresh_challenge_and_local_gate_have_stable_distinct_diagnostics() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if "autosuggest" in request.url.path:
            query = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json=[{
                    "PlaceName": query,
                    "IataCode": query,
                    "GeoId": query,
                    "GeoContainerId": query,
                    "CountryId": "PL",
                }],
            )
        return httpx.Response(403, json={"reason": "blocked"})

    request = {
        "origins": [{"iata": "WAW"}],
        "destination": {"iata": "ROM"},
        "trip": {"type": "one_way", "depart": {"date": "2027-01-01"}},
    }
    with pytest.raises(ProviderError) as fresh:
        await run_search(request, transport=httpx.MockTransport(handler))

    fresh_details = fresh.value.details
    assert fresh.value.retryable is False
    assert fresh_details["source"] == "provider_response"
    assert fresh_details["network_attempted"] is True
    assert fresh_details["provider_phase"] == "radar_create"
    assert fresh_details["challenge_kind"] == "provider_blocked"
    assert fresh_details["request_budget"] == {"limit": 30, "started": 3, "remaining": 27}
    assert fresh_details["circuit_breaker"]["state"] == "open"
    assert fresh_details["circuit_breaker"]["opened_at"]
    assert fresh_details["circuit_breaker"]["next_probe_at"]
    first_request_count = requests

    with pytest.raises(ProviderError) as local:
        await run_search(
            request,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("local circuit must not create an HTTP request")
                )
            ),
        )

    local_details = local.value.details
    assert requests == first_request_count
    assert local.value.retryable is False
    assert local_details["source"] == "local_circuit"
    assert local_details["network_attempted"] is False
    assert local_details["provider_phase"] == "local_gate"
    assert local_details["challenge_kind"] == "local_cooldown"
    assert local_details["request_budget"] == {"limit": 30, "started": 0, "remaining": 30}


@pytest.mark.asyncio
async def test_circuit_status_command_is_offline_for_missing_and_corrupt_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class ForbiddenClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("offline circuit status must not create an HTTP client")

    monkeypatch.setattr("flights_tracker.cli.httpx.AsyncClient", ForbiddenClient)
    args = parser().parse_args(["circuit", "status", "--json"])
    missing = await dispatch(args)
    assert missing["network_checked"] is False
    assert missing["circuit_breaker"]["state"] == "closed"
    assert missing["circuit_breaker"]["storage_status"] == "missing"
    assert missing["search_readiness"] == {
        "status": "allowed", "reason": "circuit_closed"
    }

    state_file = tmp_path / "provider-state" / "provider-circuit.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{corrupt")
    corrupt = await dispatch(args)
    assert corrupt["circuit_breaker"]["state"] == "closed"
    assert corrupt["circuit_breaker"]["storage_status"] == "corrupt"

    state_file.write_text(json.dumps({
        "state": "open",
        "opened_at": time.time() - 120,
        "manual_half_open": False,
    }))
    stale = await dispatch(args)
    assert stale["circuit_breaker"]["state"] == "open"
    assert stale["circuit_breaker"]["storage_status"] == "stale"
    assert stale["circuit_breaker"]["cooldown_remaining"] == 0
    assert stale["search_readiness"] == {
        "status": "controlled_retry", "reason": "cooldown_elapsed"
    }


@pytest.mark.asyncio
async def test_request_budget_stops_network_and_is_reported() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        query = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json=[{
                "PlaceName": query,
                "IataCode": query,
                "GeoId": query,
                "GeoContainerId": query,
                "CountryId": "PL",
            }],
        )

    response = await run_search(
        {
            "origins": [{"iata": "WAW"}],
            "destination": {"iata": "ROM"},
            "trip": {"type": "one_way", "depart": {"date": "2027-01-01"}},
            "request_budget": 2,
        },
        transport=httpx.MockTransport(handler),
    )

    assert len(paths) == 2
    assert response["status"] == "failed"
    assert response["error"]["code"] == "REQUEST_BUDGET_EXCEEDED"
    assert response["error"]["details"]["source"] == "local_budget"
    assert response["error"]["details"]["network_attempted"] is False
    assert response["meta"]["request_budget"] == {"limit": 2, "started": 2, "remaining": 0}


def test_browser_probe_waits_for_cross_process_workflow_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    state_directory = str(tmp_path / "provider-state")
    holder = context.Process(
        target=_hold_workflow, args=(state_directory, "holder", 0.25, output)
    )
    holder.start()
    assert output.get(timeout=2)[:2] == ("holder", "acquired")
    os.environ["FLIGHTS_TRACKER_STATE_DIR"] = state_directory

    class FakeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def autosuggest(self, query: str) -> list[dict[str, str]]:
            return [{"PlaceName": query}]

    monkeypatch.setattr("flights_tracker.cli.shutil.which", lambda _: "/bin/playwright-cli")
    monkeypatch.setattr("flights_tracker.cli.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("flights_tracker.cli.SkyscannerWebProvider", FakeProvider)
    started = time.monotonic()
    result = asyncio.run(
        dispatch(parser().parse_args(["browser", "unlock", "--probe", "--json"]))
    )
    elapsed = time.monotonic() - started
    holder.join(timeout=2)

    assert holder.exitcode == 0
    assert elapsed >= 0.18
    assert result["probe"] == "ok"
