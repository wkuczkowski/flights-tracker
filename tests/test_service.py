from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from flights_tracker.errors import FlightsError
from flights_tracker.cli import dispatch, main, parser
from flights_tracker.provider import ProviderError, SkyscannerWebProvider, _context, _results
from flights_tracker.service import _agents, _booking_options, alt_price, alternative_sort_key, matches_time_filters, normalize_result, run_flexible_search, run_search, validate_request


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
