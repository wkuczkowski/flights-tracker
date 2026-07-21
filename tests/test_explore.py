from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from flights_tracker.cli import dispatch, parser
from flights_tracker.errors import FlightsError, ProviderError
from flights_tracker.provider import SkyscannerWebProvider, _explore_collection
from flights_tracker.service import run_explore, validate_explore_request


FIXTURES = Path(__file__).parent / "fixtures"


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def request(*, level: str = "country") -> dict:
    scope = {"level": level, "anywhere": level == "country"}
    if level == "city":
        scope["countries"] = [{"code": "IT"}]
    return {
        "schema_version": "1.0",
        "origins": [{"iata": "WAW"}, {"iata": "POZ"}],
        "destination_scope": scope,
        "trip": {
            "type": "round_trip",
            "depart": {"scope": "exact", "date": future(40)},
            "return": {"scope": "exact", "date": future(47)},
        },
        "passengers": {"adults": 1, "children_ages": []},
        "cabin": "economy",
        "stay": {"min_nights": 5, "max_nights": 9},
        "filters": {"direct_only": False},
        "sort": "price",
        "limit": 50,
        "market": "PL",
        "locale": "pl-PL",
        "currency": "PLN",
    }


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def autosuggest(query: str) -> dict:
    values = {
        "WAW": {"PlaceName": "Warszawa", "IataCode": "WAW", "GeoId": "origin-waw", "CountryId": "PL"},
        "POZ": {"PlaceName": "Poznań", "IataCode": "POZ", "GeoId": "origin-poz", "CountryId": "PL"},
        "IT": {"PlaceName": "Włochy", "GeoId": "country-it", "CountryId": "IT", "CountryName": "Włochy"},
    }
    return values[query]


def explore_transport(level: str = "country", *, fail_poz: bool = False) -> httpx.MockTransport:
    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[autosuggest(query)])
        body = json.loads(call.content)
        origin_id = body["legs"][0]["legOrigin"]["entityId"]
        if fail_poz and origin_id == "origin-poz":
            return httpx.Response(503, json={"error": "temporary"})
        if level == "country":
            payload = fixture("explore_everywhere.json")
            if origin_id == "origin-poz":
                payload["everywhereDestination"]["results"][0]["content"]["flightQuotes"]["cheapest"]["rawPrice"] = 199
                payload["everywhereDestination"]["results"] = payload["everywhereDestination"]["results"][:1]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=fixture("explore_country.json"))

    return httpx.MockTransport(handler)


def test_explore_parser_requires_structured_request() -> None:
    args = parser().parse_args(["explore", "--request", "-", "--json"])
    assert args.command == "explore" and args.request == "-"


@pytest.mark.asyncio
async def test_cli_dispatches_explore_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request()))
    seen = {}

    async def fake(req, **kwargs):
        seen.update(req)
        return {"schema_version": "1.0", "status": "complete", "results": []}

    monkeypatch.setattr("flights_tracker.cli.run_explore", fake)
    output = await dispatch(parser().parse_args(["explore", "--request", str(path), "--json"]))
    assert output["status"] == "complete"
    assert seen["destination_scope"]["level"] == "country"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["trip"].pop("type"),
        lambda r: r["trip"].update(type="one_way", return_={"scope": "anytime"}),
        lambda r: r["trip"]["depart"].update(scope="month", month="bad"),
        lambda r: r["destination_scope"].update(level="city", anywhere=True),
        lambda r: r.update(limit=201),
        lambda r: r.update(sort="popularity"),
    ],
)
def test_explore_validation_is_explicit(mutation) -> None:
    req = request()
    mutation(req)
    if "return_" in req["trip"]:
        req["trip"]["return"] = req["trip"].pop("return_")
    with pytest.raises(FlightsError) as caught:
        validate_explore_request(req)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_explore_date_scopes_map_to_provider_contract() -> None:
    req = request()
    req["trip"] = {"type": "one_way", "depart": {"scope": "month", "month": "2026-09"}}
    assert validate_explore_request(req)["_depart"] == {"@type": "month", "year": "2026", "month": "09"}
    req["trip"]["depart"] = {"scope": "anytime"}
    assert validate_explore_request(req)["_depart"] == {"@type": "anytime"}


def test_provider_contract_fixture_is_sanitized_and_strict() -> None:
    rows, tags, complete, total = _explore_collection(fixture("explore_everywhere.json"), expected="everywhereDestination")
    assert complete and total == 2
    assert rows[0]["content"]["location"]["skyCode"] == "IT"
    assert tags["result-it"] == ["BEACH", "GREAT_FOOD"]
    with pytest.raises(ProviderError):
        _explore_collection({"everywhereDestination": {"results": {}}}, expected="everywhereDestination")


@pytest.mark.asyncio
async def test_country_explore_groups_origins_sorts_and_never_leaks_private_ids() -> None:
    response = await run_explore(request(), transport=explore_transport())
    assert response["status"] == "complete"
    assert [row["destination"]["code"] for row in response["results"]] == ["IT", "ES"]
    italy = response["results"][0]
    assert italy["best_price"] == {"amount": "199.00", "currency": "PLN"}
    assert [option["state"] for option in italy["origin_options"]] == ["quoted", "quoted"]
    assert italy["provider_tags"] == ["BEACH", "GREAT_FOOD"]
    assert italy["origin_options"][0]["nights"] == 7
    assert italy["origin_options"][0]["stay_match"] is True
    assert response["results"][1]["origin_options"][1]["state"] == "no_quote"
    serialized = json.dumps(response)
    assert "private-it" not in serialized and "origin-waw" not in serialized and "result-it" not in serialized


@pytest.mark.asyncio
async def test_filters_run_before_limit_and_direct_only_uses_direct_price() -> None:
    req = request()
    req["limit"] = 1
    req["filters"] = {
        "direct_only": True,
        "include_continents": ["Europe"],
        "exclude_destinations": [{"code": "ES"}],
        "max_price": {"amount": "300.00", "currency": "PLN"},
    }
    response = await run_explore(req, transport=explore_transport())
    assert [row["destination"]["code"] for row in response["results"]] == ["IT"]
    assert response["meta"]["total_candidates"] == 1
    assert response["meta"]["returned_candidates"] == 1
    assert response["meta"]["truncated"] is False


@pytest.mark.asyncio
async def test_city_expansion_adds_selected_country_public_identity() -> None:
    response = await run_explore(request(level="city"), transport=explore_transport("city"))
    milan = response["results"][0]
    assert milan["destination"] == {
        "level": "city", "code": "MILA", "name": "Mediolan",
        "country": {"code": "IT", "name": "Włochy"},
        "continent": {"code": "EU", "name": "Europa"},
    }


@pytest.mark.asyncio
async def test_partial_origin_failure_is_explicit_while_no_quote_is_not_failure() -> None:
    response = await run_explore(request(), transport=explore_transport(fail_poz=True))
    assert response["status"] == "partial"
    assert response["partial_failures"][0]["origin"] == "POZ"
    assert response["results"][0]["origin_options"][1] == {"origin": "POZ", "state": "failed", "error": {"code": "PROVIDER_UNAVAILABLE", "retryable": True}}


@pytest.mark.asyncio
async def test_bot_challenge_stops_queued_explore_fanout() -> None:
    creates = []

    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[autosuggest(query)])
        creates.append(json.loads(call.content)["legs"][0]["legOrigin"]["entityId"])
        return httpx.Response(403, json={"reason": "blocked"})

    with pytest.raises(FlightsError) as caught:
        await run_explore(request(), concurrency=1, transport=httpx.MockTransport(handler))
    assert caught.value.code == "BOT_CHALLENGE"
    assert creates == ["origin-waw"]


@pytest.mark.asyncio
async def test_bot_challenge_during_resolution_stops_before_explore() -> None:
    radar_calls = 0

    def handler(call: httpx.Request) -> httpx.Response:
        nonlocal radar_calls
        if "autosuggest" in call.url.path:
            return httpx.Response(403, json={"reason": "blocked"})
        radar_calls += 1
        return httpx.Response(500)

    with pytest.raises(FlightsError) as caught:
        await run_explore(request(), transport=httpx.MockTransport(handler))
    assert caught.value.code == "BOT_CHALLENGE"
    assert radar_calls == 0


@pytest.mark.asyncio
async def test_anytime_warns_that_stay_length_is_unknown() -> None:
    req = request()
    req["trip"] = {"type": "one_way", "depart": {"scope": "anytime"}}
    response = await run_explore(req, transport=explore_transport())
    assert any("stay length" in warning.lower() for warning in response["warnings"])
    assert response["results"][0]["origin_options"][0]["stay_match"] == "unknown"
