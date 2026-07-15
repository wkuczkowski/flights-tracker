from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .errors import FlightsError, ProviderError
from .provider import BASE_URL, Culture, SkyscannerWebProvider, decimal_string, place_summary

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TIME_FILTER_KEYS = ("depart_after", "depart_before", "return_after", "return_before")


def parse_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise FlightsError("INVALID_ARGUMENT", f"{field} must be YYYY-MM-DD") from None
    if parsed < date.today():
        raise FlightsError("INVALID_ARGUMENT", f"{field} is in the past")
    return parsed


def parse_local_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value.strip()):
        raise FlightsError("INVALID_ARGUMENT", f"{field} must be HH:MM")
    hour, minute = value.strip().split(":")
    return f"{int(hour):02d}:{int(minute):02d}"


def validate_request(req: dict[str, Any]) -> dict[str, Any]:
    if req.get("schema_version", "1.0") != "1.0":
        raise FlightsError("INVALID_ARGUMENT", "schema_version must be '1.0'")
    origins = req.get("origins")
    if not isinstance(origins, list) or not 1 <= len(origins) <= 6:
        raise FlightsError("INVALID_ARGUMENT", "origins must contain 1 to 6 places")
    for place in origins:
        _validate_place(place, "origin")
    destination = req.get("destination")
    _validate_place(destination, "destination")
    trip = req.get("trip", {})
    if not isinstance(trip, dict):
        raise FlightsError("INVALID_ARGUMENT", "trip must be an object")
    trip_type = trip.get("type", "round_trip" if trip.get("return") else "one_way")
    if trip_type not in {"one_way", "round_trip"}:
        raise FlightsError("INVALID_ARGUMENT", "trip.type must be one_way or round_trip")
    depart_value = trip.get("depart")
    if not isinstance(depart_value, dict):
        raise FlightsError("INVALID_ARGUMENT", "trip.depart must be an object")
    depart = parse_date(depart_value.get("date"), "depart")
    return_object = trip.get("return")
    if return_object is not None and not isinstance(return_object, dict):
        raise FlightsError("INVALID_ARGUMENT", "trip.return must be an object")
    return_value = return_object.get("date") if return_object else None
    return_date = parse_date(return_value, "return") if return_value else None
    if return_date and return_date < depart:
        raise FlightsError("INVALID_ARGUMENT", "return must not be before depart")
    if trip_type == "round_trip" and not return_date:
        raise FlightsError("INVALID_ARGUMENT", "round_trip requires return.date")
    if trip_type == "one_way" and return_object is not None:
        raise FlightsError("INVALID_ARGUMENT", "one_way must not include trip.return")
    passengers = req.get("passengers")
    if passengers is None:
        passengers = {"adults": 1, "children_ages": []}
        req["passengers"] = passengers
    if not isinstance(passengers, dict):
        raise FlightsError("INVALID_ARGUMENT", "passengers must be an object")
    adults = passengers.get("adults", 1)
    children = passengers.get("children_ages", [])
    if not isinstance(adults, int) or adults < 1 or adults > 8 or not isinstance(children, list) or any(not isinstance(x, int) or x < 0 or x > 17 for x in children) or adults + len(children) > 9:
        raise FlightsError("INVALID_ARGUMENT", "passengers must be 1-9 people with valid child ages")
    limit = req.get("limit", 20)
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise FlightsError("INVALID_ARGUMENT", "limit must be from 1 to 100")
    if req.get("cabin", "economy") not in {"economy", "premium_economy", "business", "first"}:
        raise FlightsError("INVALID_ARGUMENT", "unsupported cabin")
    if not re.fullmatch(r"[A-Z]{2}", str(req.get("market", "PL"))):
        raise FlightsError("INVALID_ARGUMENT", "market must be a two-letter uppercase code")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", str(req.get("locale", "pl-PL"))):
        raise FlightsError("INVALID_ARGUMENT", "locale must resemble a BCP-47 language tag")
    if not re.fullmatch(r"[A-Z]{3}", str(req.get("currency", "PLN"))):
        raise FlightsError("INVALID_ARGUMENT", "currency must be an uppercase ISO-4217 code")
    filters = req.get("filters", {})
    if not isinstance(filters, dict) or not isinstance(filters.get("direct_only", False), bool):
        raise FlightsError("INVALID_ARGUMENT", "filters.direct_only must be boolean")
    stops = filters.get("max_stops")
    if stops is not None and (not isinstance(stops, int) or isinstance(stops, bool) or stops < 0):
        raise FlightsError("INVALID_ARGUMENT", "filters.max_stops must be a non-negative integer")
    for key in _TIME_FILTER_KEYS:
        if key in filters and filters[key] is not None:
            filters[key] = parse_local_time(filters[key], f"filters.{key}")
    if filters.get("direct_only"):
        filters["max_stops"] = 0
    req["filters"] = filters
    stay = req.get("stay")
    if stay is not None:
        if not isinstance(stay, dict):
            raise FlightsError("INVALID_ARGUMENT", "stay must be an object")
        for key in ("min_nights", "max_nights"):
            if key in stay and stay[key] is not None:
                value = stay[key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise FlightsError("INVALID_ARGUMENT", f"stay.{key} must be a non-negative integer")
        min_nights = stay.get("min_nights")
        max_nights = stay.get("max_nights")
        if min_nights is not None and max_nights is not None and min_nights > max_nights:
            raise FlightsError("INVALID_ARGUMENT", "stay.min_nights cannot exceed stay.max_nights")
    if req.get("sort", "price") not in {"price", "duration"}:
        raise FlightsError("INVALID_ARGUMENT", "sort must be price or duration")
    if "timeout" in req and (not isinstance(req["timeout"], (int, float)) or isinstance(req["timeout"], bool) or req["timeout"] <= 0):
        raise FlightsError("INVALID_ARGUMENT", "timeout must be greater than zero")
    candidates = req.get("date_candidates")
    if candidates is not None and (not isinstance(candidates, int) or isinstance(candidates, bool) or not 1 <= candidates <= 15):
        raise FlightsError("INVALID_ARGUMENT", "date_candidates must be from 1 to 15")
    req["_depart"] = depart
    req["_return"] = return_date
    return req


def _validate_place(place: Any, label: str) -> None:
    if not isinstance(place, dict) or bool(place.get("query")) == bool(place.get("iata")):
        raise FlightsError("INVALID_ARGUMENT", f"{label} requires exactly one of query or iata")
    if place.get("iata"):
        value = str(place["iata"]).upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise FlightsError("INVALID_ARGUMENT", f"{label}.iata must be three letters")
        place["iata"] = value
    elif not isinstance(place.get("query"), str) or not place["query"].strip():
        raise FlightsError("INVALID_ARGUMENT", f"{label}.query must be non-empty text")


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _leg_departure_clock(leg: dict[str, Any]) -> str | None:
    local = leg.get("departure_local")
    if not isinstance(local, str) or "T" not in local:
        return None
    clock = local.split("T", 1)[1][:5]
    if not _TIME_RE.fullmatch(clock):
        return None
    return clock


def matches_time_filters(result: dict[str, Any], filters: dict[str, Any]) -> bool:
    legs = result.get("legs") or []
    if not legs:
        return False
    outbound = _leg_departure_clock(legs[0])
    if filters.get("depart_after") and (outbound is None or _clock_minutes(outbound) < _clock_minutes(filters["depart_after"])):
        return False
    if filters.get("depart_before") and (outbound is None or _clock_minutes(outbound) > _clock_minutes(filters["depart_before"])):
        return False
    if filters.get("return_after") or filters.get("return_before"):
        if len(legs) < 2:
            return False
        inbound = _leg_departure_clock(legs[1])
        if filters.get("return_after") and (inbound is None or _clock_minutes(inbound) < _clock_minutes(filters["return_after"])):
            return False
        if filters.get("return_before") and (inbound is None or _clock_minutes(inbound) > _clock_minutes(filters["return_before"])):
            return False
    return True


def trip_nights(depart: Any, ret: Any) -> int | None:
    if not isinstance(depart, str) or not isinstance(ret, str):
        return None
    try:
        return (date.fromisoformat(ret) - date.fromisoformat(depart)).days
    except ValueError:
        return None


def alt_price(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or value.get("amount") is None:
        return None
    try:
        number = Decimal(str(value["amount"]))
    except (InvalidOperation, ValueError):
        raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Alternative-date result has invalid price") from None
    unit = value.get("unit")
    if unit == "UNIT_CENTI":
        number /= Decimal(100)
    elif unit != "UNIT_WHOLE":
        raise ProviderError("PROVIDER_PROTOCOL_ERROR", f"Unknown alternative-date price unit: {unit!r}")
    amount = format(number.quantize(Decimal("0.01")), "f")
    return {"amount": amount, "currency": value.get("currencyCode")}


def alternative_sort_key(item: dict[str, Any], *, direct_only: bool = False) -> tuple[Decimal, str, str]:
    price = item.get("direct_price") if direct_only else item.get("price")
    if direct_only and not price:
        price = item.get("price")
    amount = Decimal((price or {}).get("amount", "Infinity"))
    return amount, item.get("departure_date") or "", str(item.get("origin") or "")


def _origin_request_place(resolved: dict[str, Any], original: dict[str, Any]) -> dict[str, str]:
    iata = resolved.get("IataCode")
    if isinstance(iata, str) and len(iata) == 3:
        return {"iata": iata.upper()}
    if original.get("iata"):
        return {"iata": str(original["iata"]).upper()}
    return {"query": str(original.get("query") or resolved.get("PlaceName") or iata or "")}


async def run_search(req: dict[str, Any], *, timeout: float = 60.0, concurrency: int = 2,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    started = time.monotonic()
    req = validate_request(req)
    timeout = float(req.get("timeout", timeout))
    if timeout <= 0:
        raise FlightsError("INVALID_ARGUMENT", "timeout must be greater than zero")
    deadline = started + timeout
    culture = Culture(req.get("market", "PL"), req.get("locale", "pl-PL"), req.get("currency", "PLN"))
    timeout_config = httpx.Timeout(25, connect=5)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout_config, follow_redirects=False, transport=transport) as client:
        provider = SkyscannerWebProvider(client, culture=culture)
        destination_query = req["destination"].get("query") or req["destination"].get("iata")
        try:
            destination = await asyncio.wait_for(provider.resolve_place(destination_query, destination=True), max(0.001, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "Deadline reached while resolving destination", retryable=True) from exc
        resolve_semaphore = asyncio.Semaphore(max(1, min(concurrency, 3)))

        async def resolve_origin(value: dict[str, Any]) -> dict[str, Any] | FlightsError:
            try:
                async with resolve_semaphore:
                    return await provider.resolve_place(value.get("query") or value.get("iata"), destination=False)
            except FlightsError as exc:
                return exc

        try:
            origin_places = await asyncio.wait_for(asyncio.gather(*(resolve_origin(x) for x in req["origins"])), max(0.001, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "Deadline reached while resolving places", retryable=True) from exc
        semaphore = asyncio.Semaphore(max(1, min(concurrency, 3)))
        bot_event = asyncio.Event()
        passengers = req["passengers"]

        async def one(place: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int, bool] | FlightsError:
            async with semaphore:
                if bot_event.is_set():
                    return ProviderError("BOT_CHALLENGE", "Skyscanner blocked this network session")
                try:
                    found, polls, complete = await provider.search_one(place, destination, depart=req["_depart"], return_date=req["_return"], adults=passengers.get("adults", 1), child_ages=passengers.get("children_ages", []), cabin=req.get("cabin", "economy"), deadline=deadline)
                    return place, found, polls, complete
                except FlightsError as exc:
                    if exc.code == "BOT_CHALLENGE":
                        bot_event.set()
                    return exc

        outcomes: list[Any] = [p if isinstance(p, FlightsError) else None for p in origin_places]

        async def indexed(index: int, place: dict[str, Any]) -> tuple[int, Any]:
            return index, await one(place)

        tasks = [asyncio.create_task(indexed(index, place)) for index, place in enumerate(origin_places) if isinstance(place, dict)]
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                for completed in asyncio.as_completed(tasks):
                    index, outcome = await completed
                    outcomes[index] = outcome
                    if isinstance(outcome, FlightsError) and outcome.code == "BOT_CHALLENGE":
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise outcome
        except TimeoutError as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise ProviderError("PROVIDER_TIMEOUT", "Flight search deadline reached", retryable=True) from exc

    results: list[dict[str, Any]] = []
    failures = []
    polls = 0
    incomplete = False
    filters = req.get("filters", {})
    max_stops = 0 if filters.get("direct_only") else filters.get("max_stops")
    time_filtered = 0
    for original, outcome in zip(req["origins"], outcomes, strict=True):
        if isinstance(outcome, FlightsError):
            failures.append({"origin": original.get("iata") or original.get("query"), "code": outcome.code, "retryable": outcome.retryable})
            continue
        place, raw_results, count, complete = outcome
        polls += count
        incomplete |= not complete
        if not complete and not raw_results:
            failures.append({"origin": original.get("iata") or original.get("query"), "code": "PROVIDER_TIMEOUT", "retryable": True})
            continue
        for raw in raw_results:
            normalized = normalize_result(raw, place, destination, culture.currency)
            if max_stops is not None and any(leg.get("stops", 0) > int(max_stops) for leg in normalized["legs"]):
                continue
            if not matches_time_filters(normalized, filters):
                time_filtered += 1
                continue
            results.append(normalized)
    results = deduplicate(results)
    sort = req.get("sort", "price")
    if sort == "duration":
        results.sort(key=lambda r: sum(l.get("duration_minutes") or 10**9 for l in r["legs"]))
    else:
        results.sort(key=lambda r: (float(r["price"]["amount"]), r["id"]))
    results = results[: req.get("limit", 20)]
    succeeded = len(outcomes) - len(failures)
    status = "failed" if not succeeded else ("partial" if failures or incomplete else "complete")
    warnings = []
    if incomplete:
        warnings.append("Polling deadline reached; results are the latest non-empty snapshots")
    if time_filtered and not results:
        warnings.append("No itineraries matched the requested departure/return time filters")
    elif time_filtered:
        warnings.append(f"Excluded {time_filtered} itineraries that missed departure/return time filters")
    response = {"schema_version": "1.0", "request_id": request_id(), "status": status, "provider": "skyscanner_web", "price_kind": "live", "currency": culture.currency,
                "searched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "query": {"origins": [(p.get("IataCode") or p.get("PlaceName")) if isinstance(p, dict) else (original.get("iata") or original.get("query")) for p, original in zip(origin_places, req["origins"], strict=True)], "destination": destination.get("IataCode") or destination.get("PlaceName")},
                "results": results, "partial_failures": failures, "warnings": warnings,
                "meta": {"result_count": len(results), "origins_succeeded": succeeded, "origins_failed": len(failures), "polls": polls, "time_filtered": time_filtered, "elapsed_ms": round((time.monotonic() - started) * 1000)}}
    if status == "failed":
        response["error"] = {"code": failures[0]["code"] if failures else "PROVIDER_UNAVAILABLE", "message": "All origin searches failed", "retryable": any(x["retryable"] for x in failures), "details": {}}
    return response


async def run_alternative_dates(req: dict[str, Any], *, timeout: float = 60.0, concurrency: int = 2,
                                transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    started = time.monotonic()
    req = validate_request(req)
    timeout = float(req.get("timeout", timeout))
    if timeout <= 0:
        raise FlightsError("INVALID_ARGUMENT", "timeout must be greater than zero")
    deadline = started + timeout
    culture = Culture(req.get("market", "PL"), req.get("locale", "pl-PL"), req.get("currency", "PLN"))
    direct_only = bool((req.get("filters") or {}).get("direct_only"))
    stay = req.get("stay") or {}
    min_nights = stay.get("min_nights")
    max_nights = stay.get("max_nights")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(25, connect=5), follow_redirects=False, transport=transport) as client:
        provider = SkyscannerWebProvider(client, culture=culture)
        try:
            destination = await asyncio.wait_for(
                provider.resolve_place(req["destination"].get("iata") or req["destination"].get("query"), destination=True),
                max(0.001, deadline - time.monotonic()),
            )
            origins = await asyncio.wait_for(
                asyncio.gather(*(provider.resolve_place(o.get("iata") or o.get("query")) for o in req["origins"])),
                max(0.001, deadline - time.monotonic()),
            )
        except TimeoutError as exc:
            raise ProviderError("PROVIDER_TIMEOUT", "Deadline reached while resolving places", retryable=True) from exc
        passengers = req["passengers"]
        semaphore = asyncio.Semaphore(max(1, min(concurrency, 3)))

        async def one(origin: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int, bool]:
            async with semaphore:
                dates, polls, complete = await provider.alternative_dates(
                    origin,
                    destination,
                    depart=req["_depart"],
                    return_date=req["_return"],
                    adults=passengers.get("adults", 1),
                    child_ages=passengers.get("children_ages", []),
                    cabin=req.get("cabin", "economy"),
                    deadline=deadline,
                )
                return origin, dates, polls, complete

        outcomes = await asyncio.gather(*(one(origin) for origin in origins), return_exceptions=True)

    normalized: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    polls = 0
    incomplete = False
    for original, outcome in zip(req["origins"], outcomes, strict=True):
        label = original.get("iata") or original.get("query")
        if isinstance(outcome, Exception):
            if isinstance(outcome, FlightsError):
                failures.append({"origin": label, "code": outcome.code, "retryable": outcome.retryable})
                continue
            raise outcome
        origin, dates, count, complete = outcome
        polls += count
        incomplete |= not complete
        origin_label = origin.get("IataCode") or origin.get("PlaceName") or label
        origin_place = _origin_request_place(origin, original)
        for item in dates:
            row = {
                "origin": origin_label,
                "origin_place": origin_place,
                "departure_date": item.get("departureDate"),
                "return_date": item.get("returnDate"),
                "availability": item.get("availability"),
                "price": alt_price(item.get("cheapestPrice")),
                "direct_availability": item.get("directAvailability"),
                "direct_price": alt_price(item.get("cheapestDirectPrice")),
            }
            nights = trip_nights(row.get("departure_date"), row.get("return_date"))
            if nights is not None:
                row["nights"] = nights
            if direct_only and not row["direct_price"]:
                continue
            if min_nights is not None and (nights is None or nights < min_nights):
                continue
            if max_nights is not None and (nights is None or nights > max_nights):
                continue
            normalized.append(row)
    normalized.sort(key=lambda item: alternative_sort_key(item, direct_only=direct_only))
    limit = req.get("limit", 20)
    status = "complete" if not incomplete and not failures else ("partial" if normalized else "failed")
    response = {
        "schema_version": "1.0",
        "request_id": request_id(),
        "status": status,
        "provider": "skyscanner_web",
        "origins": [place_summary(o) for o in origins],
        "destination": place_summary(destination),
        "results": normalized[:limit],
        "partial_failures": failures,
        "meta": {
            "result_count": len(normalized),
            "polls": polls,
            "direct_only": direct_only,
            "min_nights": min_nights,
            "max_nights": max_nights,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        },
    }
    if status == "failed":
        response["error"] = {
            "code": failures[0]["code"] if failures else "PROVIDER_UNAVAILABLE",
            "message": "Alternative-dates failed for all origins",
            "retryable": any(x.get("retryable") for x in failures),
            "details": {"partial_failures": failures},
        }
    # Keep untruncated rows for flexible-search candidate selection.
    response["_all_results"] = normalized
    return response


async def run_flexible_search(req: dict[str, Any], *, timeout: float = 120.0, concurrency: int = 2,
                              transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    started = time.monotonic()
    req = validate_request(copy.deepcopy(req))
    if not req.get("_return"):
        raise FlightsError("INVALID_ARGUMENT", "flexible-search requires a round-trip return date")
    date_candidates = req.get("date_candidates", 5)
    timeout = float(req.get("timeout", timeout))
    if timeout <= 0:
        raise FlightsError("INVALID_ARGUMENT", "timeout must be greater than zero")
    # Spend part of the budget on the date grid, the rest on live searches.
    alt_timeout = max(15.0, min(timeout * 0.35, 45.0))
    alt_req = copy.deepcopy(req)
    alt_req["timeout"] = alt_timeout
    alt_req["limit"] = max(req.get("limit", 20), date_candidates)
    alt = await run_alternative_dates(alt_req, timeout=alt_timeout, concurrency=concurrency, transport=transport)
    if alt.get("status") == "failed" and not alt.get("_all_results"):
        return {
            "schema_version": "1.0",
            "request_id": request_id(),
            "status": "failed",
            "provider": "skyscanner_web",
            "price_kind": "live",
            "error": alt.get("error") or {"code": "PROVIDER_UNAVAILABLE", "message": "Alternative-dates failed", "retryable": True, "details": {}},
            "date_candidates": [],
            "results": [],
            "partial_failures": alt.get("partial_failures") or [],
            "warnings": ["flexible-search stopped because alternative-dates failed"],
            "meta": {"elapsed_ms": round((time.monotonic() - started) * 1000), "date_candidates": 0, "searches": 0},
        }
    direct_only = bool((req.get("filters") or {}).get("direct_only"))
    candidates = (alt.get("_all_results") or alt.get("results") or [])[:date_candidates]
    searches = 0
    live_results: list[dict[str, Any]] = []
    failures = list(alt.get("partial_failures") or [])
    warnings = []
    if alt.get("status") == "partial":
        warnings.append("Alternative-dates response was partial; live search used the available grid")
    remaining = max(5.0, timeout - (time.monotonic() - started))
    per_search_timeout = max(8.0, remaining / max(1, len(candidates)))
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 2)))

    async def search_candidate(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | FlightsError]:
        search_req = copy.deepcopy(req)
        search_req["origins"] = [candidate["origin_place"]]
        search_req["trip"] = {
            "type": "round_trip",
            "depart": {"date": candidate["departure_date"]},
            "return": {"date": candidate["return_date"]},
        }
        search_req["timeout"] = per_search_timeout
        search_req["limit"] = max(req.get("limit", 20), 20)
        async with semaphore:
            try:
                return candidate, await run_search(search_req, timeout=per_search_timeout, concurrency=1, transport=transport)
            except FlightsError as exc:
                return candidate, exc

    outcomes = await asyncio.gather(*(search_candidate(c) for c in candidates)) if candidates else []
    for candidate, outcome in outcomes:
        searches += 1
        guide = candidate.get("direct_price") if direct_only else candidate.get("price")
        if isinstance(outcome, FlightsError):
            failures.append({
                "origin": candidate.get("origin"),
                "departure_date": candidate.get("departure_date"),
                "return_date": candidate.get("return_date"),
                "code": outcome.code,
                "retryable": outcome.retryable,
            })
            continue
        if outcome.get("status") == "failed":
            err = outcome.get("error") or {}
            failures.append({
                "origin": candidate.get("origin"),
                "departure_date": candidate.get("departure_date"),
                "return_date": candidate.get("return_date"),
                "code": err.get("code", "PROVIDER_UNAVAILABLE"),
                "retryable": bool(err.get("retryable")),
            })
            continue
        for warning in outcome.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
        for failure in outcome.get("partial_failures") or []:
            failures.append({
                **failure,
                "departure_date": candidate.get("departure_date"),
                "return_date": candidate.get("return_date"),
            })
        for result in outcome.get("results") or []:
            enriched = dict(result)
            enriched["date_pair"] = {
                "origin": candidate.get("origin"),
                "departure_date": candidate.get("departure_date"),
                "return_date": candidate.get("return_date"),
                "nights": candidate.get("nights"),
                "guide_price": guide,
            }
            live_results.append(enriched)
    live_results = deduplicate(live_results)
    if req.get("sort", "price") == "duration":
        live_results.sort(key=lambda r: sum(l.get("duration_minutes") or 10**9 for l in r["legs"]))
    else:
        live_results.sort(key=lambda r: (float(r["price"]["amount"]), r["id"]))
    live_results = live_results[: req.get("limit", 20)]
    if not candidates:
        warnings.append("No alternative-date candidates matched stay/direct filters")
    status = "complete"
    if not live_results and failures and not candidates:
        status = "failed"
    elif failures or alt.get("status") == "partial":
        status = "partial" if live_results or candidates else "failed"
    elif not live_results and candidates:
        status = "complete"
    response = {
        "schema_version": "1.0",
        "request_id": request_id(),
        "status": status,
        "provider": "skyscanner_web",
        "price_kind": "live",
        "currency": req.get("currency", "PLN"),
        "searched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "query": {
            "origins": [o.get("iata") or o.get("query") for o in req["origins"]],
            "destination": req["destination"].get("iata") or req["destination"].get("query"),
            "anchor_depart": req["trip"]["depart"]["date"],
            "anchor_return": req["trip"]["return"]["date"],
        },
        "date_candidates": [
            {
                "origin": c.get("origin"),
                "departure_date": c.get("departure_date"),
                "return_date": c.get("return_date"),
                "nights": c.get("nights"),
                "price": c.get("price"),
                "direct_price": c.get("direct_price"),
            }
            for c in candidates
        ],
        "results": live_results,
        "partial_failures": failures,
        "warnings": warnings,
        "meta": {
            "result_count": len(live_results),
            "date_candidates": len(candidates),
            "searches": searches,
            "alt_polls": (alt.get("meta") or {}).get("polls"),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        },
    }
    if status == "failed":
        response["error"] = {
            "code": failures[0]["code"] if failures else "PROVIDER_UNAVAILABLE",
            "message": "flexible-search found no live itineraries",
            "retryable": any(x.get("retryable") for x in failures),
            "details": {},
        }
    return response


async def resolve(query: str, *, destination: bool, market: str, locale: str, currency: str,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(20, connect=5), follow_redirects=False, transport=transport) as client:
        place = await SkyscannerWebProvider(client, culture=Culture(market, locale, currency)).resolve_place(query, destination=destination)
    return {"schema_version": "1.0", "status": "complete", "provider": "skyscanner_web", "place": place_summary(place)}


def normalize_result(raw: dict[str, Any], origin: dict[str, Any], destination: dict[str, Any], currency: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("legs", []), list):
        raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Itinerary must contain a legs list")
    price = raw.get("price", {})
    legs = [_normalize_leg(x) for x in raw.get("legs", []) if isinstance(x, dict)]
    amount = decimal_string(price.get("raw", price.get("amount")))
    agents = _agents(raw, currency)
    stable = {"origin": origin.get("IataCode") or origin.get("GeoId"), "destination": destination.get("IataCode") or destination.get("GeoId"), "price": amount,
              "legs": [[l.get("departure_local"), l.get("arrival_local"), [(s.get("flight_number"), s.get("origin"), s.get("destination"), s["carrier"].get("iata"), s["carrier"].get("name")) for s in l["segments"]]] for l in legs],
              "agents": [(a.get("name"), a["price"]["amount"]) for a in agents]}
    result = {"id": hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24], "origin": origin.get("IataCode") or origin.get("PlaceName"),
              "destination": destination.get("IataCode") or destination.get("PlaceName"), "price": {"amount": amount, "currency": currency}, "legs": legs,
              "agents": agents, "is_self_transfer": bool(raw.get("isSelfTransfer", False)),
              "sustainability": {"is_eco_contender": bool(raw.get("eco")), "eco_contender_delta_percent": (raw.get("eco") or {}).get("ecoContenderDelta")}}
    return result


def _normalize_leg(leg: dict[str, Any]) -> dict[str, Any]:
    departure = _local_time(leg.get("departure"))
    arrival = _local_time(leg.get("arrival"))
    segments = [_normalize_segment(x) for x in leg.get("segments", []) if isinstance(x, dict)]
    return {"departure_local": departure, "arrival_local": arrival, "departure_timezone": None, "arrival_timezone": None,
            "arrival_day_offset": int(leg.get("timeDeltaInDays", 0) or 0), "duration_minutes": leg.get("durationInMinutes"),
            "stops": int(leg.get("stopCount", max(0, len(segments) - 1)) or 0), "segments": segments}


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    carrier = segment.get("marketingCarrier") or segment.get("carrier") or {}
    flight = segment.get("flightNumber") or segment.get("flightNumberDisplay")
    return {"flight_number": str(flight) if flight is not None else None,
            "carrier": {"id": carrier.get("id"), "name": carrier.get("name"), "iata": carrier.get("alternateId") or carrier.get("iata")},
            "origin": _place_code(segment.get("origin")), "destination": _place_code(segment.get("destination")),
            "departure_local": _local_time(segment.get("departure")), "arrival_local": _local_time(segment.get("arrival"))}


def _local_time(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return f"{int(value['year']):04d}-{int(value['month']):02d}-{int(value['day']):02d}T{int(value.get('hour', 0)):02d}:{int(value.get('minute', 0)):02d}:{int(value.get('second', 0)):02d}"
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _place_code(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayCode") or value.get("iata") or value.get("id")
    return value if isinstance(value, str) else None


def _agents(raw: dict[str, Any], currency: str) -> list[dict[str, Any]]:
    output = []
    lookup = raw.get("_agent_lookup", {})
    for option in raw.get("pricingOptions", []) or []:
        if not isinstance(option, dict):
            continue
        items = option.get("items") or option.get("pricingItems") or [{}]
        if not isinstance(items, list):
            raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Pricing items must be a list")
        for item in items:
            if not isinstance(item, dict):
                raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Pricing item must be an object")
            agent = option.get("agent") or {}
            agent_ids = option.get("agentIds") or []
            agent_id = item.get("agentId") or (agent_ids[0] if agent_ids else None)
            if not agent and agent_id is not None and isinstance(lookup, dict):
                agent = lookup.get(str(agent_id), {})
                if not agent:
                    raise ProviderError("PROVIDER_PROTOCOL_ERROR", "Pricing item references a missing agent")
            price = item.get("price") or option.get("price") or raw.get("price") or {}
            try:
                amount = decimal_string(price.get("raw", price.get("amount")))
            except ProviderError:
                continue
            output.append({"name": agent.get("name") or option.get("agentName"), "price": {"amount": amount, "currency": currency}, "deeplink": item.get("deepLink") or item.get("deeplink")})
    return output


def deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({item["id"]: item for item in results}.values())


def request_id() -> str:
    return f"req_{int(time.time() * 1000):x}"
