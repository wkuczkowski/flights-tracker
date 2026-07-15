from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from flights_tracker.errors import FlightsError
from flights_tracker.cli import _alt_price, _alternative_sort_key, dispatch, main, parser
from flights_tracker.provider import ProviderError, SkyscannerWebProvider, _context, _results
from flights_tracker.service import _agents
from flights_tracker.service import run_search, validate_request


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


def test_agents_dict_and_all_pricing_items() -> None:
    data = {"itineraries": {"agents": {"a": {"name": "Agent A"}, "b": {"name": "Agent B"}}, "results": [{"price": {"raw": 10}, "legs": [], "pricingOptions": [{"items": [{"agentId": "a", "price": {"raw": 10}}, {"agentId": "b", "price": {"raw": 12}}]}]}]}}
    raw = _results(data)[0]
    assert [a["name"] for a in _agents(raw, "PLN")] == ["Agent A", "Agent B"]


def test_missing_agent_reference_is_protocol_error() -> None:
    with pytest.raises(ProviderError):
        _agents({"price": {"raw": 1}, "pricingOptions": [{"items": [{"agentId": "missing"}]}], "_agent_lookup": {}}, "PLN")


def test_alternative_price_units_are_decimal_and_strict() -> None:
    assert _alt_price({"amount": "123", "unit": "UNIT_CENTI", "currencyCode": "PLN"})["amount"] == "1.23"
    assert _alt_price({"amount": "123", "unit": "UNIT_WHOLE", "currencyCode": "PLN"})["amount"] == "123.00"
    with pytest.raises(ProviderError):
        _alt_price({"amount": "1", "unit": "UNKNOWN"})


def test_alternative_sort_does_not_lose_precision_for_huge_prices() -> None:
    values = [{"price": {"amount": "999999999999999999999999.02"}}, {"price": {"amount": "999999999999999999999999.01"}}]
    values.sort(key=_alternative_sort_key)
    assert values[0]["price"]["amount"].endswith(".01")


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
