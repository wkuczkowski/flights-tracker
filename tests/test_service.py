from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from flights_tracker.errors import FlightsError
from flights_tracker.cli import dispatch, main, parser
from flights_tracker.provider import ProviderError, SkyscannerWebProvider, _context, _results
from flights_tracker.coordination import circuit_status, open_circuit
from flights_tracker.service import (
    _agents,
    _booking_options,
    alt_price,
    alternative_sort_key,
    matches_time_filters,
    normalize_result,
    run_alternative_dates,
    run_flexible_search,
    run_search,
    select_balanced_candidates,
    validate_request,
)


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def request() -> dict:
    return {"origins": [{"iata": "WAW"}, {"iata": "POZ"}], "destination": {"query": "Rzym"},
            "trip": {"depart": {"date": future(30)}, "return": {"date": future(37)}},
            "passengers": {"adults": 1, "children_ages": []}, "currency": "PLN", "limit": 2}


def test_validation_rejects_bad_return() -> None:
    req = request(); req["trip"]["return"]["date"] = future(20)
    with pytest.raises(FlightsError) as caught:
        validate_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_skill_documented_cli_flags_parse() -> None:
    resolved = parser().parse_args(["places", "resolve", "--query", "Rzym", "--json"])
    assert resolved.query == "Rzym"
    search = parser().parse_args(["search", "--origin", "WAW", "--destination", "ROM", "--depart", future(30), "--json"])
    assert search.json and search.origin == ["WAW"]
    unlock = parser().parse_args(["browser", "unlock", "--json"])
    assert unlock.browser_command == "unlock"


@pytest.mark.asyncio
async def test_fanout_normalizes_sorts_and_limits() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "autosuggest" in req.url.path:
            query = req.url.path.rsplit("/", 1)[-1]
            place = {"WAW": ("Warszawa Chopina", "11"), "POZ": ("Poznań", "22"), "Rzym": ("Rzym", "33")}[query]
            return httpx.Response(200, json=[{"PlaceName": place[0], "IataCode": query if query != "Rzym" else "ROM", "GeoId": place[1], "GeoContainerId": place[1], "CountryId": "PL" if query != "Rzym" else "IT"}])
        body = __import__("json").loads(req.content)
        origin = body["legs"][0]["legOrigin"]["entityId"]
        amount = 200 if origin == "11" else 100
        result = {"price": {"raw": amount}, "legs": [{"departure": "2027-01-01T10:00:00", "arrival": "2027-01-01T12:00:00", "durationInMinutes": 120, "stopCount": 0, "segments": []}]}
        return httpx.Response(200, json={"context": {"status": "complete", "sessionId": "not-logged"}, "itineraries": {"results": [result]}})

    response = await run_search(request(), transport=httpx.MockTransport(handler))
    assert response["status"] == "complete"
    assert [x["price"]["amount"] for x in response["results"]] == ["100.00", "200.00"]
    assert response["meta"]["origins_succeeded"] == 2
    assert response["meta"]["request_budget"] == {
        "limit": 30, "started": 5, "remaining": 25
    }


@pytest.mark.asyncio
async def test_default_multi_origin_workflow_is_strictly_serial_and_ordered() -> None:
    order: list[str] = []
    active = 0
    max_active = 0

    async def handler(call: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            if "autosuggest" in call.url.path:
                query = call.url.path.rsplit("/", 1)[-1]
                order.append(f"resolve:{query}")
                return httpx.Response(
                    200,
                    json=[{
                        "PlaceName": query,
                        "IataCode": query if len(query) == 3 else "ROM",
                        "GeoId": query,
                        "GeoContainerId": query,
                        "CountryId": "PL",
                    }],
                )
            origin = json.loads(call.content)["legs"][0]["legOrigin"]["entityId"]
            order.append(f"radar:{origin}")
            return httpx.Response(
                200,
                json={
                    "context": {"status": "complete"},
                    "itineraries": {"results": []},
                },
            )
        finally:
            active -= 1

    await run_search(request(), transport=httpx.MockTransport(handler))

    assert max_active == 1
    assert order == [
        "resolve:Rzym",
        "resolve:WAW",
        "resolve:POZ",
        "radar:WAW",
        "radar:POZ",
    ]


@pytest.mark.asyncio
async def test_all_creates_blocked_is_failed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "autosuggest" in req.url.path:
            query = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[{"PlaceName": query, "IataCode": query if len(query) == 3 else "ROM", "GeoId": query, "CountryId": query}])
        return httpx.Response(403, json={"reason": "blocked"})

    with pytest.raises(FlightsError) as caught:
        await run_search(request(), transport=httpx.MockTransport(handler))
    assert caught.value.code == "BOT_CHALLENGE"


@pytest.mark.asyncio
async def test_bot_challenge_cancels_queued_origins_before_create() -> None:
    creates = []
    baseline_tasks = set(asyncio.all_tasks())
    req = request()
    req["origins"].append({"iata": "GDN"})

    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[{"PlaceName": query, "IataCode": query if len(query) == 3 else "ROM", "GeoId": query, "CountryId": query}])
        body = __import__("json").loads(call.content)
        creates.append(body["legs"][0]["legOrigin"]["entityId"])
        return httpx.Response(403, json={"reason": "blocked"})

    with pytest.raises(FlightsError) as caught:
        await run_search(req, concurrency=1, transport=httpx.MockTransport(handler))
    assert caught.value.code == "BOT_CHALLENGE"
    assert creates == ["WAW"]
    await asyncio.sleep(0)
    assert not {
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    }


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(schema_version="2.0"),
    lambda r: r.update(cabin="cargo"),
    lambda r: r.update(currency="pln"),
    lambda r: r.update(locale="bad_locale"),
    lambda r: r.update(filters={"direct_only": "yes"}),
    lambda r: r.update(sort="random"),
    lambda r: r.update(timeout=0),
])
def test_full_request_validation(mutation) -> None:
    req = request(); mutation(req)
    with pytest.raises(FlightsError) as caught:
        validate_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("field,value", [
    ("passengers", []), ("trip", []),
])
def test_nested_request_types_never_leak_attribute_error(field, value) -> None:
    req = request(); req[field] = value
    with pytest.raises(FlightsError) as caught:
        validate_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("depart,returned", [([], None), ({"date": future(30)}, []), ({"date": future(30)}, "bad")])
def test_trip_nested_values_are_validated(depart, returned) -> None:
    req = request(); req["trip"]["depart"] = depart
    if returned is not None:
        req["trip"]["return"] = returned
    with pytest.raises(FlightsError) as caught:
        validate_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_one_way_rejects_return() -> None:
    req = request(); req["trip"]["type"] = "one_way"
    with pytest.raises(FlightsError) as caught:
        validate_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_multi_item_booking_option_uses_authoritative_total_and_not_component_agents() -> None:
    data = json.loads((Path(__file__).parent / "fixtures" / "live_pricing_options.json").read_text())
    raw = _results(data)[0]
    assert _agents(raw, "PLN") == []
    options = _booking_options(raw, "PLN")
    assert options[0]["total_price"]["amount"] == "22.00"
    assert options[0]["requires_multiple_bookings"] is True
    assert options[0]["transfer_type"] == "SELF_TRANSFER"
    assert [item["price"]["amount"] for item in options[0]["booking_items"]] == ["10.00", "12.00"]


def test_single_item_booking_option_remains_in_deprecated_agents() -> None:
    raw = {
        "price": {"raw": 10}, "legs": [], "_agent_lookup": {"a": {"name": "Agent A"}},
        "pricingOptions": [{"price": {"raw": 10}, "items": [{"agentId": "a", "price": {"raw": 10}, "deepLink": "https://example.invalid"}]}],
    }
    normalized = normalize_result(raw, {"IataCode": "WAW"}, {"IataCode": "ROM"}, "PLN")
    assert normalized["agents"][0]["price"]["amount"] == "10.00"
    assert normalized["booking_options"][0]["total_price"]["amount"] == "10.00"
    assert normalized["is_self_transfer"] is None
    assert normalized["airport_change"] is None


def test_airport_change_and_transfer_type_preserve_unknown_self_transfer() -> None:
    raw = {
        "price": {"raw": 10},
        "legs": [{
            "segments": [
                {
                    "origin": {"displayCode": "WAW", "iata": "WAW"},
                    "destination": {"displayCode": "LHR", "iata": "LHR"},
                    "marketingCarrier": {"id": "carrier", "alternateId": "W6"},
                },
                {
                    "origin": {"displayCode": "LGW", "iata": "LGW"},
                    "destination": {"displayCode": "FCO", "iata": "FCO"},
                    "marketingCarrier": {"id": "carrier", "alternateId": "W6"},
                },
            ]
        }],
    }
    normalized = normalize_result(raw, {"IataCode": "WAW"}, {"IataCode": "ROM"}, "PLN")
    carrier = normalized["legs"][0]["segments"][0]["carrier"]

    assert normalized["airport_change"] is True
    assert normalized["is_self_transfer"] is None
    assert carrier["iata"] is None
    assert carrier["alternate_code"] == "W6"

    self_transfer = {
        **raw,
        "pricingOptions": [{
            "price": {"raw": 10},
            "transferType": "SELF_TRANSFER",
            "items": [{"price": {"raw": 10}}],
        }],
    }
    assert normalize_result(
        self_transfer, {"IataCode": "WAW"}, {"IataCode": "ROM"}, "PLN"
    )["is_self_transfer"] is True


def test_single_item_without_authoritative_option_total_is_protocol_error() -> None:
    raw = {
        "price": {"raw": 10}, "legs": [], "_agent_lookup": {"a": {"name": "Agent A"}},
        "pricingOptions": [{"items": [{"agentId": "a", "price": {"raw": 10}}]}],
    }
    with pytest.raises(ProviderError) as caught:
        _booking_options(raw, "PLN")
    assert "authoritative total" in caught.value.message


def test_missing_agent_reference_is_protocol_error() -> None:
    with pytest.raises(ProviderError):
        _agents({"price": {"raw": 1}, "pricingOptions": [{"items": [{"agentId": "missing"}]}], "_agent_lookup": {}}, "PLN")


def test_alternative_price_units_are_decimal_and_strict() -> None:
    assert alt_price({"amount": "123", "unit": "UNIT_CENTI", "currencyCode": "PLN"})["amount"] == "1.23"
    assert alt_price({"amount": "123", "unit": "UNIT_WHOLE", "currencyCode": "PLN"})["amount"] == "123.00"
    with pytest.raises(ProviderError):
        alt_price({"amount": "1", "unit": "UNKNOWN"})


def test_alternative_sort_does_not_lose_precision_for_huge_prices() -> None:
    values = [{"price": {"amount": "999999999999999999999999.02"}}, {"price": {"amount": "999999999999999999999999.01"}}]
    values.sort(key=alternative_sort_key)
    assert values[0]["price"]["amount"].endswith(".01")


def test_time_filters_match_outbound_and_return_clocks() -> None:
    result = {
        "legs": [
            {"departure_local": "2026-09-25T06:10:00"},
            {"departure_local": "2026-09-29T18:30:00"},
        ]
    }
    assert matches_time_filters(result, {"depart_before": "12:00", "return_after": "17:00"})
    assert not matches_time_filters(result, {"depart_before": "05:00"})
    assert not matches_time_filters(result, {"return_after": "19:00"})


def test_time_filter_validation_normalizes_hhmm() -> None:
    req = request()
    req["filters"] = {"direct_only": False, "depart_before": "9:05", "return_after": "17:00"}
    validated = validate_request(req)
    assert validated["filters"]["depart_before"] == "09:05"
    assert validated["filters"]["return_after"] == "17:00"


@pytest.mark.asyncio
async def test_search_applies_depart_before_filter() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "autosuggest" in req.url.path:
            query = req.url.path.rsplit("/", 1)[-1]
            place = {"WAW": ("Warszawa", "11"), "POZ": ("Poznań", "22"), "Rzym": ("Rzym", "33")}[query]
            return httpx.Response(200, json=[{"PlaceName": place[0], "IataCode": query if query != "Rzym" else "ROM", "GeoId": place[1], "GeoContainerId": place[1], "CountryId": "PL" if query != "Rzym" else "IT"}])
        body = __import__("json").loads(req.content)
        origin = body["legs"][0]["legOrigin"]["entityId"]
        hour = 6 if origin == "11" else 18
        result = {
            "price": {"raw": 100 if origin == "11" else 80},
            "legs": [
                {"departure": f"2027-01-01T{hour:02d}:00:00", "arrival": f"2027-01-01T{hour+2:02d}:00:00", "durationInMinutes": 120, "stopCount": 0, "segments": []},
                {"departure": "2027-01-08T18:00:00", "arrival": "2027-01-08T20:00:00", "durationInMinutes": 120, "stopCount": 0, "segments": []},
            ],
        }
        return httpx.Response(200, json={"context": {"status": "complete", "sessionId": "not-logged"}, "itineraries": {"results": [result]}})

    req = request()
    req["filters"] = {"direct_only": False, "depart_before": "12:00"}
    response = await run_search(req, transport=httpx.MockTransport(handler))
    assert response["status"] == "complete"
    assert len(response["results"]) == 1
    assert response["results"][0]["origin"] == "WAW"
    assert response["meta"]["time_filtered"] == 1


@pytest.mark.asyncio
async def test_flexible_search_uses_top_date_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_alt(req, **kwargs):
        return {
            "status": "complete",
            "partial_failures": [],
            "meta": {"polls": 1},
            "_all_results": [
                {
                    "origin": "WAW",
                    "origin_place": {"iata": "WAW"},
                    "departure_date": future(40),
                    "return_date": future(44),
                    "nights": 4,
                    "price": {"amount": "300.00", "currency": "PLN"},
                    "direct_price": {"amount": "300.00", "currency": "PLN"},
                },
                {
                    "origin": "GDN",
                    "origin_place": {"iata": "GDN"},
                    "departure_date": future(41),
                    "return_date": future(45),
                    "nights": 4,
                    "price": {"amount": "310.00", "currency": "PLN"},
                    "direct_price": {"amount": "310.00", "currency": "PLN"},
                },
            ],
        }

    async def fake_search(req, **kwargs):
        origin = req["origins"][0]["iata"]
        depart = req["trip"]["depart"]["date"]
        return {
            "status": "complete",
            "warnings": [],
            "partial_failures": [],
            "results": [{
                "id": f"{origin}-{depart}",
                "origin": origin,
                "destination": "ROM",
                "price": {"amount": "350.00" if origin == "WAW" else "360.00", "currency": "PLN"},
                "legs": [
                    {"departure_local": f"{depart}T06:00:00", "duration_minutes": 120, "stops": 0, "segments": []},
                    {"departure_local": f"{req['trip']['return']['date']}T18:00:00", "duration_minutes": 120, "stops": 0, "segments": []},
                ],
            }],
        }

    monkeypatch.setattr("flights_tracker.service.run_alternative_dates", fake_alt)
    monkeypatch.setattr("flights_tracker.service.run_search", fake_search)
    req = request()
    req["date_candidates"] = 2
    req["filters"] = {"direct_only": True}
    response = await run_flexible_search(req)
    assert response["status"] == "complete"
    assert len(response["date_candidates"]) == 2
    assert len(response["results"]) == 2
    assert response["results"][0]["date_pair"]["origin"] == "WAW"
    assert response["meta"]["searches"] == 2


def test_balanced_candidate_selection_keeps_multiple_origins() -> None:
    rows = [
        {
            "origin": "WAW",
            "origin_place": {"iata": "WAW"},
            "departure_date": future(40 + index),
            "return_date": future(44 + index),
            "price": {"amount": f"{100 + index}.00"},
        }
        for index in range(3)
    ]
    rows.extend([
        {
            "origin": "GDN",
            "origin_place": {"iata": "GDN"},
            "departure_date": future(40),
            "return_date": future(44),
            "price": {"amount": "500.00"},
        },
        {
            "origin": "POZ",
            "origin_place": {"iata": "POZ"},
            "departure_date": future(40),
            "return_date": future(44),
            "price": {"amount": "600.00"},
        },
    ])

    selected = select_balanced_candidates(rows, 3)

    assert [row["origin"] for row in selected] == ["WAW", "GDN", "POZ"]


@pytest.mark.asyncio
async def test_alternative_dates_bot_cancels_queued_provider_work() -> None:
    radar_calls = 0

    def handler(call: httpx.Request) -> httpx.Response:
        nonlocal radar_calls
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[{
                "PlaceName": query,
                "IataCode": query,
                "GeoId": query,
                "CountryId": "PL",
            }])
        radar_calls += 1
        return httpx.Response(403, json={"reason": "blocked"})

    req = request()
    req["origins"].append({"iata": "GDN"})
    with pytest.raises(FlightsError) as caught:
        await run_alternative_dates(
            req, concurrency=1, transport=httpx.MockTransport(handler)
        )
    assert caught.value.code == "BOT_CHALLENGE"
    assert radar_calls == 1


@pytest.mark.asyncio
async def test_flexible_search_bot_cancels_pending_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def fake_alt(req, **kwargs):
        return {
            "status": "complete",
            "partial_failures": [],
            "meta": {"polls": 0},
            "_all_results": [
                {
                    "origin": origin,
                    "origin_place": {"iata": origin},
                    "departure_date": future(40),
                    "return_date": future(44),
                    "price": {"amount": amount},
                    "direct_price": {"amount": amount},
                }
                for origin, amount in (("WAW", "100.00"), ("GDN", "200.00"))
            ],
        }

    calls = 0

    async def fake_search(req, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("BOT_CHALLENGE", "blocked")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("flights_tracker.service.run_alternative_dates", fake_alt)
    monkeypatch.setattr("flights_tracker.service.run_search", fake_search)
    req = request()
    req["date_candidates"] = 2

    with pytest.raises(FlightsError) as caught:
        await run_flexible_search(req, concurrency=2)

    assert caught.value.code == "BOT_CHALLENGE"
    assert cancelled.is_set()


def test_deep_radar_contract_validation() -> None:
    with pytest.raises(ProviderError):
        _context({"context": {"status": "incomplete"}})
    with pytest.raises(ProviderError):
        _context({"context": {"status": "mystery", "sessionId": "x"}})
    with pytest.raises(ProviderError):
        _results({"context": {"status": "complete"}})
    with pytest.raises(ProviderError):
        _results({"itineraries": {"results": [], "agents": ["bad"]}})


def test_argparse_error_is_single_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["flights", "--bogus"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and __import__("json").loads(lines[0])["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_browser_unlock_is_headed_and_persistent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr("flights_tracker.cli.shutil.which", lambda _: "/bin/playwright-cli")
    monkeypatch.setattr("flights_tracker.cli.subprocess.run", lambda command, **kwargs: seen.extend(command))
    output = await dispatch(parser().parse_args(["browser", "unlock", "--json"]))
    assert "--headed" in seen and "--persistent" in seen
    assert output["status"] == "human_action_required"


@pytest.mark.asyncio
async def test_browser_unlock_probe_only_enables_controlled_half_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def autosuggest(self, query):
            return [{"PlaceName": "Warszawa"}]

    await open_circuit()
    monkeypatch.setattr("flights_tracker.cli.shutil.which", lambda _: "/bin/playwright-cli")
    monkeypatch.setattr("flights_tracker.cli.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("flights_tracker.cli.SkyscannerWebProvider", FakeProvider)
    output = await dispatch(parser().parse_args(["browser", "unlock", "--probe", "--json"]))

    assert output["probe"] == "ok"
    assert output["search_readiness"]["status"] == "unknown"
    assert output["circuit_breaker"]["state"] == "half_open"


@pytest.mark.asyncio
async def test_doctor_reports_unknown_search_readiness_and_circuit_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def autosuggest(self, query):
            return [{"PlaceName": "Warszawa"}]

    monkeypatch.setattr("flights_tracker.cli.SkyscannerWebProvider", FakeProvider)
    output = await dispatch(parser().parse_args(["doctor", "--json"]))
    assert output["status"] == "ok"
    assert output["checks"]["radar"] == "not_checked"
    assert output["search_readiness"] == {
        "status": "unknown",
        "reason": "radar_not_checked",
    }
    assert output["circuit_breaker"]["state"] == "closed"

    await open_circuit()
    blocked = await dispatch(parser().parse_args(["doctor", "--json"]))
    assert blocked["status"] == "degraded"
    assert blocked["checks"]["http"] == "not_checked"
    assert blocked["circuit_breaker"]["state"] == "open"
    assert (await circuit_status())["state"] == "open"
