from __future__ import annotations

import json
import time
from datetime import date

import httpx
import pytest

from flights_tracker.errors import FlightsError, ProviderError
from flights_tracker.provider import Culture, SkyscannerWebProvider, USER_AGENT, _retry_after_seconds


@pytest.mark.asyncio
async def test_resolve_iata_and_required_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[{"PlaceName": "Warszawa Chopina", "IataCode": "WAW", "GeoId": "95673538", "GeoContainerId": "27547454"}])

    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(handler)) as client:
        place = await SkyscannerWebProvider(client).resolve_place("WAW")
    assert place["GeoId"] == "95673538"
    assert seen["user-agent"] == USER_AGENT
    assert seen["accept-language"].startswith("pl-PL")


@pytest.mark.asyncio
async def test_city_exact_match_beats_airport() -> None:
    choices = [
        {"PlaceName": "Rzym", "IataCode": "FCO", "GeoId": "airport", "CountryId": "IT"},
        {"PlaceName": "Rzym", "IataCode": "", "GeoId": "city", "CountryId": "IT"},
    ]
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=choices))) as client:
        result = await SkyscannerWebProvider(client).resolve_place("Rzym")
    assert result["GeoId"] == "city"


@pytest.mark.asyncio
async def test_ambiguous_city_returns_choices() -> None:
    choices = [
        {"PlaceName": "Springfield", "IataCode": "", "GeoId": "1", "CountryId": "US", "CountryName": "USA"},
        {"PlaceName": "Springfield", "IataCode": "", "GeoId": "2", "CountryId": "CA", "CountryName": "Canada"},
    ]
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=choices))) as client:
        with pytest.raises(FlightsError) as caught:
            await SkyscannerWebProvider(client).resolve_place("Springfield")
    assert caught.value.code == "AMBIGUOUS_PLACE"
    assert len(caught.value.details["choices"]) == 2


@pytest.mark.asyncio
async def test_bot_challenge_mapping() -> None:
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"reason": "blocked"}))) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client).autosuggest("WAW")
    assert caught.value.code == "BOT_CHALLENGE"
    assert caught.value.exit_code == 3
    assert caught.value.retryable is False
    assert caught.value.details["source"] == "provider_response"
    assert caught.value.details["network_attempted"] is True
    assert caught.value.details["provider_phase"] == "autosuggest"
    assert caught.value.details["challenge_kind"] == "provider_blocked"


@pytest.mark.asyncio
async def test_retry_after_cannot_cross_deadline() -> None:
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "10"}))) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client)._request("GET", "/limited", deadline=time.monotonic() + .01)
    assert caught.value.code == "PROVIDER_TIMEOUT"


def test_retry_after_http_date_is_supported() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    value = format_datetime(datetime.now(UTC) + timedelta(seconds=2), usegmt=True)
    assert 0 <= _retry_after_seconds(value, fallback=9) <= 2.1


class SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await __import__("asyncio").sleep(.06)
        return httpx.Response(200, json={"pollingSession": {"status": "POLLING_SESSION_STATUS_COMPLETE"}, "alternativeDates": []})


@pytest.mark.asyncio
async def test_alternative_dates_inflight_request_obeys_deadline() -> None:
    origin = {"GeoId": "1"}; destination = {"GeoId": "2"}
    started = time.monotonic()
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=SlowTransport()) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client, retries=0).alternative_dates(origin, destination, depart=date(2027, 1, 1), return_date=None, adults=1, child_ages=[], cabin="economy", deadline=time.monotonic() + .01)
    assert caught.value.code == "PROVIDER_TIMEOUT"
    assert time.monotonic() - started < .05


def test_alt_session_status_does_not_treat_incomplete_as_complete() -> None:
    from flights_tracker.provider import _alt_session_complete, _alt_session_incomplete

    assert _alt_session_incomplete("POLLING_SESSION_STATUS_INCOMPLETE")
    assert not _alt_session_complete("POLLING_SESSION_STATUS_INCOMPLETE")
    assert _alt_session_complete("POLLING_SESSION_STATUS_COMPLETE")
    assert not _alt_session_incomplete("POLLING_SESSION_STATUS_COMPLETE")


@pytest.mark.asyncio
async def test_radar_create_challenge_has_phase_and_safe_kind() -> None:
    async with httpx.AsyncClient(
        base_url="https://www.skyscanner.pl",
        transport=httpx.MockTransport(lambda request: httpx.Response(403)),
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client, retries=0).search_one(
                {"GeoId": "origin"},
                {"GeoId": "destination"},
                depart=date(2027, 1, 1),
                return_date=None,
                adults=1,
                child_ages=[],
                cabin="economy",
                deadline=time.monotonic() + 1,
            )

    assert caught.value.details["provider_phase"] == "radar_create"
    assert caught.value.details["challenge_kind"] == "http_status"


@pytest.mark.asyncio
async def test_radar_poll_challenge_has_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flights_tracker.provider.asyncio.sleep", lambda _: _noop())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "context": {"status": "incomplete", "sessionId": "opaque-session"},
                    "itineraries": {"results": []},
                },
            )
        return httpx.Response(403, json={"reason": "blocked"})

    async with httpx.AsyncClient(
        base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client, retries=0).search_one(
                {"GeoId": "origin"},
                {"GeoId": "destination"},
                depart=date(2027, 1, 1),
                return_date=None,
                adults=1,
                child_ages=[],
                cabin="economy",
                deadline=time.monotonic() + 1,
            )

    assert caught.value.details["provider_phase"] == "radar_poll"


@pytest.mark.asyncio
async def test_alternative_dates_create_challenge_has_phase() -> None:
    async with httpx.AsyncClient(
        base_url="https://www.skyscanner.pl",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"reason": "blocked"})
        ),
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client, retries=0).alternative_dates(
                {"GeoId": "origin"},
                {"GeoId": "destination"},
                depart=date(2027, 1, 1),
                return_date=None,
                adults=1,
                child_ages=[],
                cabin="economy",
                deadline=time.monotonic() + 1,
            )

    assert caught.value.details["provider_phase"] == "alternative_dates_create"


@pytest.mark.asyncio
async def test_alternative_dates_poll_challenge_has_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("flights_tracker.provider.asyncio.sleep", lambda _: _noop())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "pollingSession": {
                        "status": "POLLING_SESSION_STATUS_INCOMPLETE",
                        "pollingSessionId": "opaque-session",
                    },
                    "alternativeDates": [],
                },
            )
        return httpx.Response(403, json={"reason": "blocked"})

    async with httpx.AsyncClient(
        base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await SkyscannerWebProvider(client, retries=0).alternative_dates(
                {"GeoId": "origin"},
                {"GeoId": "destination"},
                depart=date(2027, 1, 1),
                return_date=None,
                adults=1,
                child_ages=[],
                cabin="economy",
                deadline=time.monotonic() + 1,
            )

    assert caught.value.details["provider_phase"] == "alternative_dates_poll"


@pytest.mark.asyncio
async def test_alternative_dates_polls_until_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flights_tracker.provider.asyncio.sleep", lambda _: _noop())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            assert "pollingSessionId" not in body
            return httpx.Response(
                200,
                json={
                    "pollingSession": {
                        "status": "POLLING_SESSION_STATUS_INCOMPLETE",
                        "pollingSessionId": "alt-session",
                    },
                    "alternativeDates": [],
                },
            )
        assert body.get("pollingSessionId") == "alt-session"
        return httpx.Response(
            200,
            json={
                "pollingSession": {"status": "POLLING_SESSION_STATUS_COMPLETE", "pollingSessionId": "alt-session"},
                "alternativeDates": [
                    {
                        "departureDate": "2026-09-26",
                        "returnDate": "2026-09-30",
                        "availability": "AVAILABILITY_PRICE_AVAILABLE",
                        "cheapestPrice": {"currencyCode": "PLN", "amount": "300", "unit": "UNIT_WHOLE"},
                    }
                ],
            },
        )

    origin = {"GeoId": "1"}
    destination = {"GeoId": "2"}
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(handler)) as client:
        dates, polls, complete = await SkyscannerWebProvider(client).alternative_dates(
            origin,
            destination,
            depart=date(2026, 9, 25),
            return_date=date(2026, 9, 29),
            adults=1,
            child_ages=[],
            cabin="economy",
            deadline=time.monotonic() + 10,
        )
    assert complete
    assert polls == 1
    assert calls == 2
    assert dates[0]["departureDate"] == "2026-09-26"


@pytest.mark.asyncio
async def test_poll_empty_keeps_previous_and_nonempty_replaces_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flights_tracker.provider.asyncio.sleep", lambda _: _noop())
    calls = 0
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"context": {"status": "incomplete", "sessionId": "secret-session"}, "itineraries": {"results": [{"id": "create"}]}})
        calls += 1
        assert "secret-session" in str(request.url)
        if calls == 1:
            return httpx.Response(200, json={"context": {"status": "incomplete", "sessionId": "secret-session"}, "itineraries": {"results": []}})
        return httpx.Response(200, json={"context": {"status": "complete", "sessionId": "secret-session"}, "itineraries": {"results": [{"id": "final"}]}})

    origin = {"GeoId": "1"}; destination = {"GeoId": "2", "GeoContainerId": "22"}
    async with httpx.AsyncClient(base_url="https://www.skyscanner.pl", transport=httpx.MockTransport(handler)) as client:
        results, polls, complete = await SkyscannerWebProvider(client).search_one(origin, destination, depart=date(2027, 1, 1), return_date=date(2027, 1, 2), adults=1, child_ages=[], cabin="economy", deadline=time.monotonic() + 10)
    assert results == [{"id": "final"}]
    assert polls == 2 and complete
    assert bodies[0]["legs"][0]["placeOfStay"] == "22"
    assert "placeOfStay" not in bodies[0]["legs"][1]


async def _noop() -> None:
    return None
