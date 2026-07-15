from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .errors import FlightsError, ProviderError
from .provider import BASE_URL, Culture, SkyscannerWebProvider, place_summary
from .service import parse_date, request_id, resolve, run_search, validate_request


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise FlightsError("INVALID_ARGUMENT", message)


def parser() -> argparse.ArgumentParser:
    p = JsonArgumentParser(prog="flights", description="Agent-friendly Skyscanner flight search (JSON stdout)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    places = sub.add_parser("places", help="Resolve flight places")
    places_sub = places.add_subparsers(dest="places_command", required=True)
    r = places_sub.add_parser("resolve", help="Resolve a city, airport name, or IATA code")
    r.add_argument("query_positional", nargs="?"); r.add_argument("--query"); r.add_argument("--destination", action="store_true"); _culture(r); _json_flag(r)
    s = sub.add_parser("search", help="Search live flights")
    _search_args(s); s.add_argument("--request", metavar="FILE", help="Read complete JSON request from FILE or -"); _json_flag(s)
    a = sub.add_parser("alternative-dates", help="Get nearby date/price combinations")
    _alt_dates_args(a); a.add_argument("--request", metavar="FILE", help="Read JSON request from FILE or -"); _json_flag(a)
    d = sub.add_parser("doctor", help="Probe endpoint access and contract without creating a flight search")
    _culture(d); _json_flag(d)
    b = sub.add_parser("browser", help="Manual browser challenge helpers")
    browser_sub = b.add_subparsers(dest="browser_command", required=True)
    unlock = browser_sub.add_parser("unlock", help="Open a persistent visible browser for manual verification")
    unlock.add_argument("--profile", help="Persistent browser profile path"); unlock.add_argument("--probe", action="store_true"); _json_flag(unlock)
    return p


def _culture(p: argparse.ArgumentParser) -> None:
    p.add_argument("--market", default="PL"); p.add_argument("--locale", default="pl-PL"); p.add_argument("--currency", default="PLN")


def _json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit JSON (the default; accepted for explicit agent contracts)")


def _search_args(p: argparse.ArgumentParser, single_origin: bool = False) -> None:
    p.add_argument("--origin", action="append", help="Origin query/IATA; repeat for fan-out")
    p.add_argument("--destination"); p.add_argument("--depart"); p.add_argument("--return", dest="return_date")
    p.add_argument("--adults", type=int, default=1); p.add_argument("--child-age", type=int, action="append", default=[])
    p.add_argument("--cabin", choices=["economy", "premium_economy", "business", "first"], default="economy")
    p.add_argument("--direct", action="store_true"); p.add_argument("--max-stops", type=int); p.add_argument("--sort", choices=["price", "duration"], default="price")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--timeout", type=float, default=60.0); _culture(p)


def _alt_dates_args(p: argparse.ArgumentParser) -> None:
    _search_args(p)
    p.add_argument("--min-nights", type=int, help="Keep round-trips with at least this many nights")
    p.add_argument("--max-nights", type=int, help="Keep round-trips with at most this many nights")


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.request:
        try:
            text = sys.stdin.read() if args.request == "-" else Path(args.request).read_text()
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise FlightsError("INVALID_ARGUMENT", f"Cannot read JSON request: {exc}") from None
        if not isinstance(data, dict):
            raise FlightsError("INVALID_ARGUMENT", "JSON request must be an object")
        return data
    if not args.origin or not args.destination or not args.depart:
        raise FlightsError("INVALID_ARGUMENT", "--origin, --destination and --depart are required without --request")
    data = {"schema_version": "1.0", "origins": [{"iata": x.upper()} if len(x) == 3 and x.isalpha() else {"query": x} for x in args.origin],
            "destination": {"iata": args.destination.upper()} if len(args.destination) == 3 and args.destination.isalpha() else {"query": args.destination},
            "trip": {"type": "round_trip" if args.return_date else "one_way", "depart": {"date": args.depart}, **({"return": {"date": args.return_date}} if args.return_date else {})},
            "passengers": {"adults": args.adults, "children_ages": args.child_age}, "cabin": args.cabin,
            "market": args.market, "locale": args.locale, "currency": args.currency, "filters": {"direct_only": args.direct, "max_stops": args.max_stops}, "sort": args.sort, "limit": args.limit}
    if getattr(args, "min_nights", None) is not None or getattr(args, "max_nights", None) is not None:
        data["stay"] = {}
        if getattr(args, "min_nights", None) is not None:
            data["stay"]["min_nights"] = args.min_nights
        if getattr(args, "max_nights", None) is not None:
            data["stay"]["max_nights"] = args.max_nights
    return data


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "places":
        query = args.query or args.query_positional
        if not query:
            raise FlightsError("INVALID_ARGUMENT", "--query is required")
        return await resolve(query, destination=args.destination, market=args.market, locale=args.locale, currency=args.currency)
    if args.command == "search":
        return await run_search(_request_from_args(args), timeout=args.timeout)
    if args.command == "alternative-dates":
        started = time.monotonic(); req = validate_request(_request_from_args(args)); culture = Culture(req.get("market", "PL"), req.get("locale", "pl-PL"), req.get("currency", "PLN"))
        timeout = float(req.get("timeout", args.timeout))
        if timeout <= 0:
            raise FlightsError("INVALID_ARGUMENT", "timeout must be greater than zero")
        deadline = started + timeout
        direct_only = bool((req.get("filters") or {}).get("direct_only"))
        stay = req.get("stay") or {}
        min_nights = stay.get("min_nights")
        max_nights = stay.get("max_nights")
        if min_nights is not None and (not isinstance(min_nights, int) or isinstance(min_nights, bool) or min_nights < 0):
            raise FlightsError("INVALID_ARGUMENT", "stay.min_nights must be a non-negative integer")
        if max_nights is not None and (not isinstance(max_nights, int) or isinstance(max_nights, bool) or max_nights < 0):
            raise FlightsError("INVALID_ARGUMENT", "stay.max_nights must be a non-negative integer")
        if min_nights is not None and max_nights is not None and min_nights > max_nights:
            raise FlightsError("INVALID_ARGUMENT", "stay.min_nights cannot exceed stay.max_nights")
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(25, connect=5), follow_redirects=False) as client:
            provider = SkyscannerWebProvider(client, culture=culture)
            try:
                destination = await asyncio.wait_for(provider.resolve_place(req["destination"].get("iata") or req["destination"].get("query"), destination=True), max(.001, deadline-time.monotonic()))
                origins = await asyncio.wait_for(
                    asyncio.gather(*(provider.resolve_place(o.get("iata") or o.get("query")) for o in req["origins"])),
                    max(.001, deadline - time.monotonic()),
                )
            except TimeoutError as exc:
                raise ProviderError("PROVIDER_TIMEOUT", "Deadline reached while resolving places", retryable=True) from exc
            passengers = req["passengers"]
            semaphore = asyncio.Semaphore(2)

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
            for item in dates:
                row = {
                    "origin": origin_label,
                    "departure_date": item.get("departureDate"),
                    "return_date": item.get("returnDate"),
                    "availability": item.get("availability"),
                    "price": _alt_price(item.get("cheapestPrice")),
                    "direct_availability": item.get("directAvailability"),
                    "direct_price": _alt_price(item.get("cheapestDirectPrice")),
                }
                nights = _trip_nights(row.get("departure_date"), row.get("return_date"))
                if nights is not None:
                    row["nights"] = nights
                if direct_only and not row["direct_price"]:
                    continue
                if min_nights is not None and (nights is None or nights < min_nights):
                    continue
                if max_nights is not None and (nights is None or nights > max_nights):
                    continue
                normalized.append(row)
        normalized.sort(key=lambda item: _alternative_sort_key(item, direct_only=direct_only))
        limit = req.get("limit", 20)
        status = "complete" if not incomplete and not failures else ("partial" if normalized else "failed")
        if status == "failed" and failures:
            raise FlightsError(failures[0]["code"], f"Alternative-dates failed for all origins ({failures[0]['code']})", retryable=bool(failures[0].get("retryable")), details={"partial_failures": failures})
        return {
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
            },
        }
    if args.command == "doctor":
        started = time.monotonic()
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
            data = await SkyscannerWebProvider(client, culture=Culture(args.market, args.locale, args.currency), retries=0).autosuggest("Warszawa")
        return {"schema_version": "1.0", "status": "ok", "provider": "skyscanner_web", "checks": {"dns": "ok", "tls": "ok", "http": "ok", "autosuggest_contract": "ok", "radar": "not_checked", "candidate_count": len(data)}, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if args.command == "browser":
        executable = shutil.which("playwright-cli")
        if not executable:
            raise FlightsError("PROVIDER_UNAVAILABLE", "playwright-cli is required for browser unlock")
        command = [executable, "-s=skyscanner-unlock", "open", BASE_URL, "--headed"]
        command.append(f"--profile={args.profile}" if args.profile else "--persistent")
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FlightsError("PROVIDER_UNAVAILABLE", "Could not open the manual unlock browser") from exc
        probe = "not_requested"
        if args.probe:
            try:
                async with httpx.AsyncClient(base_url=BASE_URL, timeout=10, follow_redirects=False) as client:
                    await SkyscannerWebProvider(client, retries=0).autosuggest("Warszawa")
                probe = "ok"
            except FlightsError as exc:
                probe = exc.code
        return {"schema_version": "1.0", "status": "human_action_required", "provider": "skyscanner_web", "probe": probe, "action": {"code": "COMPLETE_BROWSER_CHALLENGE", "message": "Complete any verification in the opened headed browser, then run 'flights doctor --json' and retry"}}
    raise FlightsError("INVALID_ARGUMENT", "Unknown command")


def _trip_nights(depart: Any, ret: Any) -> int | None:
    if not isinstance(depart, str) or not isinstance(ret, str):
        return None
    try:
        return (parse_date(ret, "return") - parse_date(depart, "depart")).days
    except FlightsError:
        return None


def _alt_price(value: Any) -> dict[str, str] | None:
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


def _alternative_sort_key(item: dict[str, Any], *, direct_only: bool = False) -> tuple[Decimal, str, str]:
    price = item.get("direct_price") if direct_only else item.get("price")
    if direct_only and not price:
        price = item.get("price")
    amount = Decimal((price or {}).get("amount", "Infinity"))
    return amount, item.get("departure_date") or "", str(item.get("origin") or "")


def failure(exc: FlightsError) -> dict[str, Any]:
    return {"schema_version": "1.0", "request_id": request_id(), "status": "failed", "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "details": exc.details}}


def main() -> None:
    try:
        args = parser().parse_args()
        output = asyncio.run(dispatch(args))
        if output.get("status") == "failed":
            error = output.get("error", {})
            code = FlightsError(str(error.get("code", "PROVIDER_UNAVAILABLE")), str(error.get("message", "Provider failed"))).exit_code
        else:
            code = 0
    except FlightsError as exc:
        output, code = failure(exc), exc.exit_code
        print(f"flights: {exc.code}: {exc.message}", file=sys.stderr)
    except KeyboardInterrupt:
        exc = FlightsError("INTERNAL_ERROR", "Interrupted")
        output, code = failure(exc), 130
    except Exception:
        exc = FlightsError("INTERNAL_ERROR", "Unexpected internal error")
        output, code = failure(exc), 6
        print("flights: INTERNAL_ERROR: unexpected failure", file=sys.stderr)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
