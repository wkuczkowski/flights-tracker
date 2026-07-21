from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from flights_tracker.cli import dispatch, parser
from flights_tracker.errors import FlightsError, ProviderError
from flights_tracker.provider import (
    ExploreResult,
    ExploreSnapshot,
    SkyscannerWebProvider,
    _explore_collection,
    _public_explore_identity,
)
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
        "GDN": {"PlaceName": "Gdańsk", "IataCode": "GDN", "GeoId": "origin-gdn", "CountryId": "PL"},
        "IT": {"PlaceName": "Włochy", "GeoId": "country-it", "CountryId": "IT", "CountryName": "Włochy"},
        "Italy": {"PlaceName": "Włochy", "GeoId": "country-it", "CountryId": "IT", "CountryName": "Włochy"},
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
async def test_cli_dispatches_real_explore_seam_with_mixed_scopes_and_partial_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    req = request()
    req["trip"] = {
        "type": "round_trip",
        "depart": {"scope": "month", "month": "2026-09"},
        "return": {"scope": "anytime"},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req))

    class FakeProvider:
        def __init__(self, client, *, culture):
            self.culture = culture

        async def resolve_place(self, query, *, destination=False):
            return autosuggest(query)

        async def explore_one(self, origin, destination, **kwargs):
            assert kwargs["depart"] == {"@type": "month", "year": "2026", "month": "09"}
            assert kwargs["return_date"] == {"@type": "anytime"}
            if origin["GeoId"] == "origin-poz":
                raise ProviderError("PROVIDER_TIMEOUT", "timed out", retryable=True)
            return ExploreSnapshot(
                results=[ExploreResult(
                    code="IT", name="Włochy", continent_code="EU", continent_name="Europa",
                    cheapest_price={"amount": "250.00", "currency": "PLN"},
                    cheapest_direct_price=None, direct_flights_available=False,
                    provider_tags=("GREAT_FOOD",),
                )],
                total_results=1,
                complete=True,
            )

    monkeypatch.setattr("flights_tracker.service.SkyscannerWebProvider", FakeProvider)
    output = await dispatch(parser().parse_args(["explore", "--request", str(path), "--json"]))
    assert output["status"] == "partial"
    assert output["query"]["trip"] == req["trip"]
    assert [option["state"] for option in output["results"][0]["origin_options"]] == ["quoted", "failed"]
    assert output["partial_failures"] == [{"origin": "POZ", "code": "PROVIDER_TIMEOUT", "retryable": True}]


@pytest.mark.asyncio
async def test_completed_origin_resolution_survives_hanging_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = asyncio.Event()

    class FakeProvider:
        def __init__(self, client, *, culture):
            pass

        async def resolve_place(self, query, *, destination=False):
            if query == "POZ":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return autosuggest(query)

        async def explore_one(self, origin, destination, **kwargs):
            return ExploreSnapshot(
                results=[ExploreResult(
                    code="IT", name="Włochy", continent_code="EU", continent_name="Europa",
                    cheapest_price={"amount": "250.00", "currency": "PLN"},
                    cheapest_direct_price=None, direct_flights_available=False, provider_tags=(),
                )],
                total_results=1, complete=True,
            )

    monkeypatch.setattr("flights_tracker.service.SkyscannerWebProvider", FakeProvider)
    req = request()
    req["timeout"] = 0.4
    response = await run_explore(req)
    assert response["status"] == "partial"
    assert [option["state"] for option in response["results"][0]["origin_options"]] == ["quoted", "failed"]
    assert response["partial_failures"][0]["code"] == "PROVIDER_TIMEOUT"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_resolution_bot_challenge_promptly_cancels_hanging_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    hanging_started = asyncio.Event()
    hanging_cancelled = asyncio.Event()
    radar_calls = 0

    class FakeProvider:
        def __init__(self, client, *, culture):
            pass

        async def resolve_place(self, query, *, destination=False):
            if query == "POZ":
                hanging_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    hanging_cancelled.set()
                    raise
            await hanging_started.wait()
            raise ProviderError("BOT_CHALLENGE", "blocked")

        async def explore_one(self, *args, **kwargs):
            nonlocal radar_calls
            radar_calls += 1
            raise AssertionError("Radar must not start")

    monkeypatch.setattr("flights_tracker.service.SkyscannerWebProvider", FakeProvider)
    started = time.monotonic()
    with pytest.raises(FlightsError) as caught:
        await run_explore(request())
    assert caught.value.code == "BOT_CHALLENGE"
    assert time.monotonic() - started < 0.5
    assert hanging_cancelled.is_set()
    assert radar_calls == 0


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


def test_filter_public_identity_never_promotes_private_place_or_country_ids() -> None:
    country = _public_explore_identity(
        {"CountryId": "private-country", "CountryName": "Włochy"}, level="country"
    )
    city = _public_explore_identity(
        {"PlaceId": "private-city", "PlaceName": "Mediolan"}, level="city"
    )
    assert country is not None and country.as_dict() == {"name": "Włochy"}
    assert city is not None and city.as_dict() == {"name": "Mediolan"}


def test_provider_contract_fixture_is_sanitized_and_strict() -> None:
    snapshot = _explore_collection(fixture("explore_everywhere.json"), expected="everywhereDestination")
    assert snapshot.complete and snapshot.total_results == 2
    assert snapshot.results[0].code == "IT"
    assert (snapshot.results[0].continent_code, snapshot.results[0].continent_name) == ("EU", "Europa")
    assert snapshot.results[0].provider_tags == ("BEACH", "GREAT_FOOD")
    assert "private-continent" not in repr(snapshot.results)


def test_provider_contract_allows_omitted_quotes_and_routes() -> None:
    payload = fixture("explore_everywhere.json")
    payload["everywhereDestination"]["results"][0]["content"].pop("flightRoutes")
    payload["everywhereDestination"]["results"][1]["content"].pop("flightQuotes")

    snapshot = _explore_collection(payload, expected="everywhereDestination")

    assert snapshot.results[0].cheapest_price == {"amount": "259.00", "currency": "PLN"}
    assert snapshot.results[0].direct_flights_available is True
    assert snapshot.results[1].cheapest_price is None
    assert snapshot.results[1].cheapest_direct_price is None
    assert snapshot.results[1].direct_flights_available is False

    without_both = fixture("explore_everywhere.json")
    without_both["everywhereDestination"]["results"][0]["content"].pop("flightQuotes")
    without_both["everywhereDestination"]["results"][0]["content"].pop("flightRoutes")
    result = _explore_collection(without_both, expected="everywhereDestination").results[0]
    assert result.cheapest_price is None and result.direct_flights_available is False


def test_direct_price_overrides_conflicting_route_availability_with_warning_state() -> None:
    payload = fixture("explore_everywhere.json")
    content = payload["everywhereDestination"]["results"][0]["content"]
    content["flightRoutes"]["directFlightsAvailable"] = False

    result = _explore_collection(payload, expected="everywhereDestination").results[0]

    assert result.cheapest_direct_price is not None
    assert result.direct_flights_available is True
    assert result.direct_availability_conflict is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["ES", "GR", "HR", "PT"])
async def test_country_public_code_exact_matches_provider_country_identity(code: str) -> None:
    choices = [
        {
            "PlaceName": "Other",
            "CountryName": "Other",
            "CountryId": "ZZ",
            "GeoId": "other",
        },
        {
            "PlaceName": f"Country {code}",
            "CountryName": f"Country {code}",
            "CountryId": code,
            "GeoId": f"country-{code.lower()}",
        },
    ]
    async with httpx.AsyncClient(
        base_url="https://www.skyscanner.pl",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=choices)),
    ) as client:
        resolved = await SkyscannerWebProvider(client).resolve_explore_place(code, level="country")

    assert resolved.public.code == code
    assert resolved.entity_id == f"country-{code.lower()}"


@pytest.mark.parametrize("mutation", [
    lambda data: data["everywhereDestination"].pop("context"),
    lambda data: data["everywhereDestination"].pop("features"),
    lambda data: data["everywhereDestination"]["features"].update(flightsIndicative="MYSTERY"),
    lambda data: data["everywhereDestination"].update(results={}),
    lambda data: data["everywhereDestination"]["results"][0].update(type="MYSTERY"),
    lambda data: data["everywhereDestination"]["results"][0]["content"]["location"].update(type="Region"),
    lambda data: data["everywhereDestination"]["buckets"][0].update(category="MYSTERY"),
    lambda data: data["everywhereDestination"]["results"][0]["content"].update(flightQuotes=None),
    lambda data: data["everywhereDestination"]["results"][0]["content"].update(flightRoutes=None),
    lambda data: data["everywhereDestination"]["results"][0]["content"]["flightQuotes"]["cheapest"].pop("rawPrice"),
    lambda data: data["everywhereDestination"]["results"][0]["content"]["flightRoutes"].update(directFlightsAvailable="yes"),
])
def test_provider_contract_rejects_missing_fields_unknown_enums_and_shape_drift(mutation) -> None:
    payload = fixture("explore_everywhere.json")
    mutation(payload)
    with pytest.raises(ProviderError):
        _explore_collection(payload, expected="everywhereDestination")


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
    assert all(private not in serialized for private in ("private-it", "private-continent", "origin-waw", "result-it"))


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
async def test_explore_surfaces_direct_availability_conflict_warning() -> None:
    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[autosuggest(query)])
        payload = fixture("explore_everywhere.json")
        content = payload["everywhereDestination"]["results"][0]["content"]
        content["flightRoutes"]["directFlightsAvailable"] = False
        return httpx.Response(200, json=payload)

    response = await run_explore(request(), transport=httpx.MockTransport(handler))

    italy = next(row for row in response["results"] if row["destination"]["code"] == "IT")
    assert italy["origin_options"][0]["direct_flights_available"] is True
    assert italy["origin_options"][0]["direct_availability_conflict"] is True
    assert any("conflicted" in warning for warning in response["warnings"])


@pytest.mark.asyncio
async def test_text_destination_filter_is_resolved_before_localized_matching() -> None:
    req = request()
    req["filters"] = {"direct_only": False, "include_destinations": [{"query": "Italy"}]}
    response = await run_explore(req, transport=explore_transport())
    assert [result["destination"]["code"] for result in response["results"]] == ["IT"]
    assert "country-it" not in json.dumps(response)


@pytest.mark.asyncio
async def test_ambiguous_text_destination_filter_propagates_choices_without_radar() -> None:
    radar_calls = 0

    def handler(call: httpx.Request) -> httpx.Response:
        nonlocal radar_calls
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            if query == "Springfield":
                return httpx.Response(200, json=[
                    {"PlaceName": "Springfield", "GeoId": "private-us", "CountryId": "private-country", "CountryName": "USA"},
                    {"PlaceName": "Springfield", "GeoId": "private-ca", "CountryId": "CA", "CountryName": "Kanada"},
                ])
            return httpx.Response(200, json=[autosuggest(query)])
        radar_calls += 1
        return httpx.Response(500)

    req = request()
    req["filters"] = {"direct_only": False, "include_destinations": [{"query": "Springfield"}]}
    with pytest.raises(FlightsError) as caught:
        await run_explore(req, transport=httpx.MockTransport(handler))
    assert caught.value.code == "AMBIGUOUS_PLACE"
    assert caught.value.details["choices"] == [
        {"name": "USA"}, {"code": "CA", "name": "Kanada"},
    ]
    assert "private-" not in json.dumps(caught.value.details)
    assert radar_calls == 0


@pytest.mark.asyncio
async def test_fuzzy_destination_filter_does_not_silently_choose_first_suggestion() -> None:
    radar_calls = 0

    def handler(call: httpx.Request) -> httpx.Response:
        nonlocal radar_calls
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            if query == "warm":
                return httpx.Response(200, json=[
                    {
                        "PlaceName": "Włochy", "CountryName": "Włochy",
                        "CountryId": "IT", "GeoId": "private-it",
                    },
                    {
                        "PlaceName": "Hiszpania", "CountryName": "Hiszpania",
                        "CountryId": "ES", "GeoId": "private-es",
                    },
                ])
            return httpx.Response(200, json=[autosuggest(query)])
        radar_calls += 1
        return httpx.Response(500)

    req = request()
    req["filters"] = {"direct_only": False, "include_destinations": [{"query": "warm"}]}
    with pytest.raises(FlightsError) as caught:
        await run_explore(req, transport=httpx.MockTransport(handler))

    assert caught.value.code == "AMBIGUOUS_PLACE"
    assert caught.value.details["choices"] == [
        {"code": "IT", "name": "Włochy"},
        {"code": "ES", "name": "Hiszpania"},
    ]
    assert "private-" not in json.dumps(caught.value.details)
    assert radar_calls == 0


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
async def test_city_expansion_and_partial_failure_never_expose_opaque_country_identity() -> None:
    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            if query == "Włochy":
                return httpx.Response(200, json=[{
                    "PlaceName": "Włochy",
                    "CountryName": "Włochy",
                    "CountryId": "opaque-country-id",
                    "GeoId": "private-country-entity",
                }])
            return httpx.Response(200, json=[autosuggest(query)])
        origin_id = json.loads(call.content)["legs"][0]["legOrigin"]["entityId"]
        if origin_id == "origin-poz":
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json=fixture("explore_country.json"))

    req = request(level="city")
    req["destination_scope"]["countries"] = [{"query": "Włochy"}]
    response = await run_explore(req, transport=httpx.MockTransport(handler))

    assert response["status"] == "partial"
    assert response["results"][0]["destination"]["country"] == {"name": "Włochy"}
    assert response["partial_failures"][0]["country"] == {"name": "Włochy"}
    serialized = json.dumps(response)
    assert "opaque-country-id" not in serialized
    assert "private-country-entity" not in serialized


@pytest.mark.asyncio
async def test_ambiguous_selected_country_uses_sanitized_provider_choices_without_radar() -> None:
    radar_calls = 0

    def handler(call: httpx.Request) -> httpx.Response:
        nonlocal radar_calls
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            if query == "Congo":
                return httpx.Response(200, json=[
                    {
                        "PlaceName": "Congo", "CountryName": "Kongo A",
                        "CountryId": "opaque-a", "GeoId": "private-a",
                        "GeoContainerId": "private-container-a",
                    },
                    {
                        "PlaceName": "Congo", "CountryName": "Kongo B",
                        "CountryId": "123456", "GeoId": "private-b",
                        "GeoContainerId": "private-container-b",
                    },
                ])
            return httpx.Response(200, json=[autosuggest(query)])
        radar_calls += 1
        return httpx.Response(500)

    req = request(level="city")
    req["destination_scope"]["countries"] = [{"query": "Congo"}]
    with pytest.raises(FlightsError) as caught:
        await run_explore(req, transport=httpx.MockTransport(handler))

    assert caught.value.code == "AMBIGUOUS_PLACE"
    assert caught.value.details["choices"] == [{"name": "Kongo A"}, {"name": "Kongo B"}]
    assert "private-" not in json.dumps(caught.value.details)
    assert "123456" not in json.dumps(caught.value.details)
    assert radar_calls == 0


@pytest.mark.asyncio
async def test_partial_origin_failure_is_explicit_while_no_quote_is_not_failure() -> None:
    response = await run_explore(request(), transport=explore_transport(fail_poz=True))
    assert response["status"] == "partial"
    assert response["partial_failures"][0]["origin"] == "POZ"
    assert response["results"][0]["origin_options"][1] == {"origin": "POZ", "state": "failed", "error": {"code": "PROVIDER_UNAVAILABLE", "retryable": True}}


@pytest.mark.asyncio
async def test_completed_origin_survives_when_other_origin_hits_overall_deadline() -> None:
    async def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[autosuggest(query)])
        origin_id = json.loads(call.content)["legs"][0]["legOrigin"]["entityId"]
        if origin_id == "origin-poz":
            await asyncio.sleep(1)
        return httpx.Response(200, json=fixture("explore_everywhere.json"))

    req = request()
    req["timeout"] = 0.1
    response = await run_explore(req, transport=httpx.MockTransport(handler))
    assert response["status"] == "partial"
    assert response["results"]
    assert response["results"][0]["origin_options"][0]["state"] == "quoted"
    assert response["results"][0]["origin_options"][1]["error"]["code"] == "PROVIDER_TIMEOUT"


@pytest.mark.asyncio
async def test_waw_poz_gdn_success_timeout_503_matrix() -> None:
    def handler(call: httpx.Request) -> httpx.Response:
        if "autosuggest" in call.url.path:
            query = call.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[autosuggest(query)])
        origin_id = json.loads(call.content)["legs"][0]["legOrigin"]["entityId"]
        if origin_id == "origin-poz":
            raise httpx.ReadTimeout("simulated timeout")
        if origin_id == "origin-gdn":
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json=fixture("explore_everywhere.json"))

    req = request()
    req["origins"].append({"iata": "GDN"})
    req["timeout"] = 10
    response = await run_explore(req, transport=httpx.MockTransport(handler))
    assert response["status"] == "partial"
    assert {(failure["origin"], failure["code"]) for failure in response["partial_failures"]} == {
        ("POZ", "PROVIDER_TIMEOUT"), ("GDN", "PROVIDER_UNAVAILABLE"),
    }
    assert [option["state"] for option in response["results"][0]["origin_options"]] == ["quoted", "failed", "failed"]


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
