from __future__ import annotations

import asyncio
import random
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .errors import FlightsError, ProviderError

BASE_URL = "https://www.skyscanner.pl"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in value if not unicodedata.combining(c)).strip()


@dataclass(frozen=True)
class Culture:
    market: str = "PL"
    locale: str = "pl-PL"
    currency: str = "PLN"


@dataclass(frozen=True)
class ExploreResult:
    code: str
    name: str
    continent_code: str
    continent_name: str
    cheapest_price: dict[str, str] | None
    cheapest_direct_price: dict[str, str] | None
    direct_flights_available: bool
    provider_tags: tuple[str, ...]
    observed_at: str | None = None


@dataclass(frozen=True)
class PublicPlaceIdentity:
    name: str
    code: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {key: value for key, value in (("code", self.code), ("name", self.name)) if value}


@dataclass(frozen=True)
class ResolvedExplorePlace:
    entity_id: str
    public: PublicPlaceIdentity


@dataclass(frozen=True)
class ExploreSnapshot:
    results: list[ExploreResult]
    total_results: int
    complete: bool
    session_id: str | None = None
    polls: int = 0


class SkyscannerWebProvider:
    def __init__(self, client: httpx.AsyncClient, *, culture: Culture = Culture(), retries: int = 2):
        self.client = client
        self.culture = culture
        self.retries = retries

    def _headers(self, view_id: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Language": f"{self.culture.locale},{self.culture.locale.split('-')[0]};q=0.9",
        }
        if view_id:
            headers.update({
                "Content-Type": "application/json",
                "X-Skyscanner-ChannelId": "website",
                "X-Skyscanner-Market": self.culture.market,
                "X-Skyscanner-Locale": self.culture.locale,
                "X-Skyscanner-Currency": self.culture.currency,
                "X-Skyscanner-ViewId": view_id,
            })
        return headers

    async def _request(self, method: str, path: str, *, view_id: str | None = None, json: Any = None, deadline: float | None = None) -> Any:
        for attempt in range(self.retries + 1):
            try:
                request = self.client.request(method, path, headers=self._headers(view_id), json=json)
                if deadline is None:
                    response = await request
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    response = await asyncio.wait_for(request, remaining)
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self.retries:
                    await _sleep_with_deadline(random.uniform(0.2, min(2.0, 0.5 * 2**attempt)), deadline)
                    continue
                raise ProviderError("PROVIDER_TIMEOUT", "Skyscanner request timed out", retryable=True) from exc
            content_type = response.headers.get("content-type", "").lower()
            location = response.headers.get("location", "").lower()
            if response.status_code in {307, 403} or "captcha" in location:
                reason = ""
                try:
                    reason = str(response.json().get("reason", ""))
                except Exception:
                    pass
                if response.status_code == 403 or "captcha" in location or reason == "blocked":
                    raise ProviderError("BOT_CHALLENGE", "Skyscanner blocked this network session; complete its browser challenge and retry")
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                wait = _retry_after_seconds(retry_after, fallback=0.5 * 2**attempt)
                if attempt < self.retries:
                    await _sleep_with_deadline(wait, deadline)
                    continue
                raise ProviderError("RATE_LIMITED", "Skyscanner rate limit reached", retryable=True)
            if response.status_code in {500, 502, 503, 504} and attempt < self.retries:
                await _sleep_with_deadline(random.uniform(0.2, min(2.0, 0.5 * 2**attempt)), deadline)
                continue
            if response.status_code == 404 and "/web-unified-search/" in path and method == "GET":
                raise ProviderError("SESSION_EXPIRED", "Skyscanner search session expired", retryable=True)
            if response.status_code >= 400:
                raise ProviderError("PROVIDER_UNAVAILABLE", f"Skyscanner returned HTTP {response.status_code}", retryable=response.status_code >= 500)
            if "html" in content_type or response.text.lstrip().lower().startswith("<!doctype html"):
                if "captcha" in response.text.lower() or "perimeterx" in response.text.lower():
                    raise ProviderError("BOT_CHALLENGE", "Skyscanner returned a browser challenge")
                raise ProviderError("CONTRACT_CHANGED", "Skyscanner returned HTML instead of JSON")
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Skyscanner returned invalid JSON") from exc
        raise AssertionError("unreachable")

    async def autosuggest(self, query: str, *, destination: bool = False) -> list[dict[str, Any]]:
        path = f"/g/autosuggest-search/api/v1/search-flight/{quote(self.culture.market)}/{quote(self.culture.locale)}/{quote(query, safe='')}"
        path += f"?isDestination={'true' if destination else 'false'}&enable_general_search_v2=true&autosuggestExp="
        data = await self._request("GET", path)
        if not isinstance(data, list):
            raise ProviderError("CONTRACT_CHANGED", "Autosuggest response is no longer a list")
        return data

    async def resolve_place(self, query: str, *, destination: bool = False) -> dict[str, Any]:
        choices = await self.autosuggest(query, destination=destination)
        if not choices:
            raise FlightsError("INVALID_ARGUMENT", f"No place found for {query!r}")
        q = _fold(query)
        iata_query = len(query) == 3 and query.isalpha() and query.upper() == query
        if iata_query:
            exact = [p for p in choices if str(p.get("IataCode", "")).upper() == query]
        else:
            exact = [p for p in choices if _fold(str(p.get("PlaceName", ""))) == q]
            city = [p for p in exact if not p.get("IataCode") or (p.get("PlaceId") and str(p.get("PlaceId")) == str(p.get("CityId")))]
            if city:
                exact = city
        candidates = exact or choices[:1]
        distinct = {(str(p.get("GeoId")), str(p.get("CountryId"))) for p in candidates}
        if len(distinct) > 1:
            raise FlightsError("AMBIGUOUS_PLACE", f"Place {query!r} is ambiguous", details={"choices": [place_summary(p) for p in candidates[:10]]})
        selected = candidates[0]
        if not selected.get("GeoId"):
            raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Resolved place has no GeoId")
        return selected

    async def resolve_explore_place(self, query: str, *, level: str) -> ResolvedExplorePlace:
        if level not in {"country", "city"}:
            raise FlightsError("INVALID_ARGUMENT", "Explore place level must be country or city")
        try:
            raw = await self.resolve_place(query, destination=True)
        except FlightsError as exc:
            if exc.code != "AMBIGUOUS_PLACE":
                raise
            choices = [
                identity.as_dict()
                for choice in exc.details.get("choices", [])
                if isinstance(choice, dict)
                if (identity := _public_explore_identity(choice, level=level)) is not None
            ]
            raise FlightsError(
                exc.code, exc.message, retryable=exc.retryable, details={"choices": choices}
            ) from None
        identity = _public_explore_identity(raw, level=level)
        if identity is None:
            raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Resolved Explore place has no public identity")
        return ResolvedExplorePlace(entity_id=str(raw["GeoId"]), public=identity)

    async def search_one(self, origin: dict[str, Any], destination: dict[str, Any], *, depart: date,
                         return_date: date | None, adults: int, child_ages: list[int], cabin: str,
                         deadline: float, _recreated: bool = False) -> tuple[list[dict[str, Any]], int, bool]:
        view_id = str(uuid.uuid4())
        legs = [_radar_leg(origin, destination, depart, place_of_stay=str(destination.get("GeoContainerId") or destination["GeoId"]))]
        if return_date:
            legs.append(_radar_leg(destination, origin, return_date))
        body = {"cabinClass": cabin.upper(), "childAges": child_ages, "adults": adults, "legs": legs}
        data = await self._request("POST", "/g/radar/api/v2/web-unified-search/", view_id=view_id, json=body, deadline=deadline)
        snapshot = _results(data)
        status, session_id = _context(data)
        polls = 0
        delay = 0.45
        while status != "complete" and session_id and time.monotonic() + delay < deadline:
            await asyncio.sleep(delay)
            encoded = quote(session_id, safe="")
            try:
                data = await self._request("GET", f"/g/radar/api/v2/web-unified-search/{encoded}", view_id=view_id, deadline=deadline)
            except ProviderError as exc:
                if exc.code == "SESSION_EXPIRED" and not _recreated and time.monotonic() < deadline:
                    return await self.search_one(origin, destination, depart=depart, return_date=return_date, adults=adults, child_ages=child_ages, cabin=cabin, deadline=deadline, _recreated=True)
                raise
            polls += 1
            current = _results(data)
            if current:
                snapshot = current
            status, next_session_id = _context(data)
            session_id = next_session_id or session_id
            delay = min(2.5, delay * 1.6)
        return snapshot, polls, status == "complete"

    async def alternative_dates(self, origin: dict[str, Any], destination: dict[str, Any], *, depart: date,
                                return_date: date | None, adults: int, child_ages: list[int], cabin: str,
                                deadline: float) -> tuple[list[dict[str, Any]], int, bool]:
        view_id = str(uuid.uuid4())
        legs = [_alt_leg(origin, destination, depart)]
        if return_date:
            legs.append(_alt_leg(destination, origin, return_date))
        context = {"localisationContext": {"currency": self.culture.currency, "locale": self.culture.locale, "market": self.culture.market},
                   "trustedFunnelId": view_id, "viewId": view_id, "channelId": "website"}
        search = {"adults": adults, "childAges": child_ages, "legs": legs,
                  "nearbyAirports": {"includeOriginNearbyAirports": False, "includeDestinationNearbyAirports": False}, "cabinClass": cabin.upper()}
        body: dict[str, Any] = {"requestContext": context, "searchRequest": search}
        polls = 0
        latest: list[dict[str, Any]] = []
        complete = False
        delay = 0.5
        while time.monotonic() < deadline:
            data = await self._request("POST", "/g/radar/api/v1/alternative-dates", view_id=view_id, json=body, deadline=deadline)
            if not isinstance(data, dict):
                raise ProviderError("CONTRACT_CHANGED", "Alternative-dates response must be an object")
            found = data.get("alternativeDates", [])
            if not isinstance(found, list) or any(not isinstance(item, dict) for item in found):
                raise ProviderError("CONTRACT_CHANGED", "alternativeDates must be a list of objects")
            if found:
                latest = found
            polling = data.get("pollingSession", {})
            if not isinstance(polling, dict):
                raise ProviderError("CONTRACT_CHANGED", "pollingSession must be an object")
            status = str(polling.get("status") or data.get("pollingSessionStatus", ""))
            session = polling.get("pollingSessionId") or data.get("pollingSessionId")
            complete = _alt_session_complete(status)
            if status and not (complete or _alt_session_incomplete(status)):
                raise ProviderError("CONTRACT_CHANGED", f"Unknown alternative-dates status: {status!r}")
            if complete or not session:
                break
            body["pollingSessionId"] = session
            polls += 1
            await _sleep_with_deadline(delay, deadline)
            delay = min(2.5, delay * 1.6)
        return latest, polls, complete

    async def explore_one(
        self,
        origin: dict[str, Any],
        destination: ResolvedExplorePlace | None,
        *,
        depart: dict[str, str],
        return_date: dict[str, str] | None,
        adults: int,
        child_ages: list[int],
        cabin: str,
        deadline: float,
    ) -> ExploreSnapshot:
        """Return provider Explore rows without exposing the provider collection shape."""
        view_id = str(uuid.uuid4())
        destination_ref = _explore_place(destination)
        origin_ref = {"@type": "entity", "entityId": str(origin["GeoId"])}
        legs = [{"legOrigin": origin_ref, "legDestination": destination_ref, "dates": depart}]
        if return_date:
            legs.append({"legOrigin": destination_ref, "legDestination": origin_ref, "dates": return_date})
        body = {
            "cabinClass": cabin.upper(),
            "childAges": child_ages,
            "adults": adults,
            "legs": legs,
            "options": {"maxDestinations": 200},
        }
        expected = "countryDestination" if destination else "everywhereDestination"
        data = await self._request(
            "POST", "/g/radar/api/v2/web-unified-search/", view_id=view_id, json=body, deadline=deadline
        )
        snapshot = _explore_collection(data, expected=expected, currency=self.culture.currency)
        polls = 0
        session_id = snapshot.session_id
        delay = 0.45
        while not snapshot.complete and isinstance(session_id, str) and time.monotonic() + delay < deadline:
            await asyncio.sleep(delay)
            data = await self._request(
                "GET",
                f"/g/radar/api/v2/web-unified-search/{quote(session_id, safe='')}",
                view_id=view_id,
                deadline=deadline,
            )
            current = _explore_collection(data, expected=expected, currency=self.culture.currency)
            if current.results:
                snapshot = current
            polls += 1
            session_id = current.session_id or session_id
            if current.complete and not current.results:
                snapshot = ExploreSnapshot(
                    results=snapshot.results,
                    total_results=current.total_results,
                    complete=True,
                    session_id=session_id,
                )
            delay = min(2.5, delay * 1.6)
        return ExploreSnapshot(
            results=snapshot.results,
            total_results=snapshot.total_results,
            complete=snapshot.complete,
            session_id=None,
            polls=polls,
        )


def place_summary(place: dict[str, Any]) -> dict[str, Any]:
    return {k: place.get(k) for k in ("PlaceName", "IataCode", "CountryName", "CountryId", "GeoId", "GeoContainerId")}


def _radar_leg(origin: dict[str, Any], destination: dict[str, Any], day: date, place_of_stay: str | None = None) -> dict[str, Any]:
    leg: dict[str, Any] = {"legOrigin": {"@type": "entity", "entityId": str(origin["GeoId"])},
                           "legDestination": {"@type": "entity", "entityId": str(destination["GeoId"])},
                           "dates": {"@type": "date", "year": f"{day.year:04d}", "month": f"{day.month:02d}", "day": f"{day.day:02d}"}}
    if place_of_stay:
        leg["placeOfStay"] = place_of_stay
    return leg


def _alt_leg(origin: dict[str, Any], destination: dict[str, Any], day: date) -> dict[str, Any]:
    return {"date": {"year": day.year, "month": day.month, "day": day.day}, "origin": [str(origin["GeoId"])], "destination": [str(destination["GeoId"])]}


def _explore_place(place: ResolvedExplorePlace | None) -> dict[str, str]:
    if place is None:
        return {"@type": "everywhere"}
    return {"@type": "entity", "entityId": place.entity_id}


def _public_explore_identity(place: dict[str, Any], *, level: str) -> PublicPlaceIdentity | None:
    if level == "country":
        raw_code = place.get("CountryId")
        code = raw_code.upper() if isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z]{2}", raw_code) else None
        name = place.get("CountryName") or place.get("PlaceName")
    else:
        raw_code = place.get("IataCode")
        code = raw_code.upper() if isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z]{3,4}", raw_code) else None
        name = place.get("PlaceName")
    if not isinstance(name, str) or not name:
        return None
    return PublicPlaceIdentity(name=name, code=code)


def _explore_collection(
    data: Any, *, expected: str, currency: str = "PLN"
) -> ExploreSnapshot:
    if not isinstance(data, dict) or not isinstance(data.get(expected), dict):
        raise ProviderError("CONTRACT_CHANGED", f"Radar Explore response has no {expected} object")
    collection = data[expected]
    context = collection.get("context")
    if not isinstance(context, dict):
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore response has no context")
    status = str(context.get("status", "")).lower()
    if status not in {"complete", "incomplete"}:
        raise ProviderError("CONTRACT_CHANGED", f"Unknown Radar Explore status: {status!r}")
    if status == "incomplete" and not isinstance(context.get("sessionId"), str):
        raise ProviderError("CONTRACT_CHANGED", "Incomplete Radar Explore response has no sessionId")
    features = collection.get("features")
    if not isinstance(features, dict):
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore response has no features object")
    indicative = features.get("flightsIndicative")
    if indicative not in {"AVAILABLE", "UNAVAILABLE"}:
        raise ProviderError("CONTRACT_CHANGED", f"Unknown flightsIndicative status: {indicative!r}")
    results = collection.get("results")
    if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore results must be a list of objects")
    buckets = collection.get("buckets")
    if not isinstance(buckets, list) or any(not isinstance(bucket, dict) for bucket in buckets):
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore buckets must be a list of objects")
    tags: dict[str, list[str]] = {}
    for bucket in buckets:
        category = bucket.get("category")
        if category not in {"VIBES", "NON_CATEGORIZED"}:
            raise ProviderError("CONTRACT_CHANGED", f"Unknown Radar Explore bucket category: {category!r}")
        tag = bucket.get("id")
        ids = bucket.get("resultIds")
        if not isinstance(tag, str) or not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ProviderError("CONTRACT_CHANGED", "Radar Explore bucket is malformed")
        if category == "VIBES":
            for result_id in ids:
                tags.setdefault(result_id, []).append(tag)
    locations = []
    expected_location_type = "City" if expected == "countryDestination" else "Nation"
    for row in results:
        row_type = row.get("type")
        if row_type not in {"LOCATION", "ADVERT", "ADVERTISEMENT"}:
            raise ProviderError("CONTRACT_CHANGED", f"Unknown Radar Explore result type: {row_type!r}")
        if row_type != "LOCATION":
            # Explore interleaves ads and other presentation cards with locations.
            continue
        content = row.get("content")
        location = content.get("location") if isinstance(content, dict) else None
        if not isinstance(row.get("id"), str) or not isinstance(location, dict):
            raise ProviderError("CONTRACT_CHANGED", "Radar Explore result has no public location data")
        if not isinstance(location.get("name"), str) or not isinstance(location.get("skyCode"), str):
            raise ProviderError("CONTRACT_CHANGED", "Radar Explore location has no name or skyCode")
        if location.get("type") != expected_location_type:
            raise ProviderError("CONTRACT_CHANGED", f"Unexpected Radar Explore location type: {location.get('type')!r}")
        continent = location.get("continent")
        if not isinstance(continent, dict) or not isinstance(continent.get("code"), str) or not isinstance(continent.get("name"), str):
            raise ProviderError("CONTRACT_CHANGED", "Radar Explore location has no public continent")
        quotes = content.get("flightQuotes")
        routes = content.get("flightRoutes")
        if not isinstance(quotes, dict) or not isinstance(routes, dict) or not isinstance(routes.get("directFlightsAvailable"), bool):
            raise ProviderError("CONTRACT_CHANGED", "Radar Explore result has malformed quotes or routes")
        cheapest = _explore_price(quotes.get("cheapest"), currency)
        direct = _explore_price(quotes.get("direct"), currency)
        locations.append(ExploreResult(
            code=location["skyCode"].upper(),
            name=location["name"],
            continent_code=continent["code"],
            continent_name=continent["name"],
            cheapest_price=cheapest,
            cheapest_direct_price=direct,
            direct_flights_available=routes["directFlightsAvailable"],
            provider_tags=tuple(sorted(tags.get(row["id"], []))),
        ))
    total = context.get("totalResults", len(results))
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore totalResults must be a non-negative integer")
    return ExploreSnapshot(
        results=locations,
        total_results=total,
        complete=status == "complete",
        session_id=context.get("sessionId"),
    )


def _explore_price(value: Any, currency: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("rawPrice") is None:
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore quote has no rawPrice")
    if "direct" in value and not isinstance(value["direct"], bool):
        raise ProviderError("CONTRACT_CHANGED", "Radar Explore quote direct flag must be boolean")
    return {"amount": decimal_string(value["rawPrice"]), "currency": currency}


def _alt_session_incomplete(status: str) -> bool:
    return status.endswith("INCOMPLETE")


def _alt_session_complete(status: str) -> bool:
    # INCOMPLETE also ends with COMPLETE; check the longer suffix first.
    return status.endswith("COMPLETE") and not _alt_session_incomplete(status)


def _context(data: Any) -> tuple[str, str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("context"), dict):
        raise ProviderError("CONTRACT_CHANGED", "Radar response has no context")
    status = str(data["context"].get("status", "")).lower()
    if status not in {"incomplete", "complete"}:
        raise ProviderError("CONTRACT_CHANGED", f"Unknown Radar status: {status!r}")
    session_id = data["context"].get("sessionId")
    if status == "incomplete" and not isinstance(session_id, str):
        raise ProviderError("CONTRACT_CHANGED", "Incomplete Radar response has no sessionId")
    return status, session_id


def _results(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or "itineraries" not in data or not isinstance(data["itineraries"], dict):
        raise ProviderError("CONTRACT_CHANGED", "Radar response has no itineraries object")
    itineraries = data["itineraries"]
    if "results" not in itineraries:
        raise ProviderError("CONTRACT_CHANGED", "Radar itineraries has no results")
    results = itineraries["results"]
    if not isinstance(results, list):
        raise ProviderError("CONTRACT_CHANGED", "Radar results are no longer a list")
    if any(not isinstance(result, dict) for result in results):
        raise ProviderError("CONTRACT_CHANGED", "Radar result entries must be objects")
    agents = itineraries.get("agents", []) if isinstance(itineraries, dict) else []
    if isinstance(agents, dict):
        if any(not isinstance(value, dict) for value in agents.values()):
            raise ProviderError("CONTRACT_CHANGED", "Radar agent entries must be objects")
        lookup = {str(key): value for key, value in agents.items()}
    elif isinstance(agents, list):
        if any(not isinstance(agent, dict) for agent in agents):
            raise ProviderError("CONTRACT_CHANGED", "Radar agent entries must be objects")
        lookup = {str(a.get("id")): a for a in agents if isinstance(a, dict) and a.get("id") is not None}
    else:
        raise ProviderError("CONTRACT_CHANGED", "Radar agents must be a list or object")
    if not lookup:
        return results
    enriched = []
    for result in results:
        if isinstance(result, dict):
            result = dict(result)
            result["_agent_lookup"] = lookup
        enriched.append(result)
    return enriched


def decimal_string(value: Any) -> str:
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError):
        raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Result has an invalid price") from None


async def _sleep_with_deadline(delay: float, deadline: float | None) -> None:
    if deadline is not None and time.monotonic() + delay >= deadline:
        raise ProviderError("PROVIDER_TIMEOUT", "Flight search deadline reached", retryable=True)
    await asyncio.sleep(delay)


def _retry_after_seconds(value: str | None, *, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return min(8.0, max(0.0, float(value)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return min(8.0, max(0.0, (parsed - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return fallback
