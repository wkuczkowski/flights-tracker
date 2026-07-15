from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .errors import FlightsError
from .provider import BASE_URL, Culture, SkyscannerWebProvider
from .service import (
    alt_price as _alt_price,
    alternative_sort_key as _alternative_sort_key,
    request_id,
    resolve,
    run_alternative_dates,
    run_flexible_search,
    run_search,
)


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
    f = sub.add_parser("flexible-search", help="Pick top alternative dates then run live searches")
    _flexible_args(f); f.add_argument("--request", metavar="FILE", help="Read JSON request from FILE or -"); _json_flag(f)
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


def _search_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--origin", action="append", help="Origin query/IATA; repeat for fan-out")
    p.add_argument("--destination"); p.add_argument("--depart"); p.add_argument("--return", dest="return_date")
    p.add_argument("--adults", type=int, default=1); p.add_argument("--child-age", type=int, action="append", default=[])
    p.add_argument("--cabin", choices=["economy", "premium_economy", "business", "first"], default="economy")
    p.add_argument("--direct", action="store_true"); p.add_argument("--max-stops", type=int)
    p.add_argument("--depart-after", help="Keep outbound departures at/after HH:MM local")
    p.add_argument("--depart-before", help="Keep outbound departures at/before HH:MM local")
    p.add_argument("--return-after", help="Keep return departures at/after HH:MM local")
    p.add_argument("--return-before", help="Keep return departures at/before HH:MM local")
    p.add_argument("--sort", choices=["price", "duration"], default="price")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--timeout", type=float, default=60.0); _culture(p)


def _alt_dates_args(p: argparse.ArgumentParser) -> None:
    _search_args(p)
    p.add_argument("--min-nights", type=int, help="Keep round-trips with at least this many nights")
    p.add_argument("--max-nights", type=int, help="Keep round-trips with at most this many nights")


def _flexible_args(p: argparse.ArgumentParser) -> None:
    _alt_dates_args(p)
    p.add_argument("--date-candidates", type=int, default=5, help="How many alternative-date pairs to search live")
    # Flexible search often needs a longer overall budget.
    for action in p._actions:
        if action.dest == "timeout":
            action.default = 120.0


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
    filters: dict[str, Any] = {"direct_only": args.direct, "max_stops": args.max_stops}
    for key, attr in (
        ("depart_after", "depart_after"),
        ("depart_before", "depart_before"),
        ("return_after", "return_after"),
        ("return_before", "return_before"),
    ):
        value = getattr(args, attr, None)
        if value:
            filters[key] = value
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "origins": [{"iata": x.upper()} if len(x) == 3 and x.isalpha() else {"query": x} for x in args.origin],
        "destination": {"iata": args.destination.upper()} if len(args.destination) == 3 and args.destination.isalpha() else {"query": args.destination},
        "trip": {
            "type": "round_trip" if args.return_date else "one_way",
            "depart": {"date": args.depart},
            **({"return": {"date": args.return_date}} if args.return_date else {}),
        },
        "passengers": {"adults": args.adults, "children_ages": args.child_age},
        "cabin": args.cabin,
        "market": args.market,
        "locale": args.locale,
        "currency": args.currency,
        "filters": filters,
        "sort": args.sort,
        "limit": args.limit,
        "timeout": args.timeout,
    }
    if getattr(args, "min_nights", None) is not None or getattr(args, "max_nights", None) is not None:
        data["stay"] = {}
        if getattr(args, "min_nights", None) is not None:
            data["stay"]["min_nights"] = args.min_nights
        if getattr(args, "max_nights", None) is not None:
            data["stay"]["max_nights"] = args.max_nights
    if getattr(args, "date_candidates", None) is not None and args.command == "flexible-search":
        data["date_candidates"] = args.date_candidates
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
        response = await run_alternative_dates(_request_from_args(args), timeout=args.timeout)
        response.pop("_all_results", None)
        for row in response.get("results") or []:
            if isinstance(row, dict):
                row.pop("origin_place", None)
        if response.get("status") == "failed":
            error = response.get("error") or {}
            raise FlightsError(
                str(error.get("code", "PROVIDER_UNAVAILABLE")),
                str(error.get("message", "Alternative-dates failed")),
                retryable=bool(error.get("retryable")),
                details=error.get("details") or {},
            )
        return response
    if args.command == "flexible-search":
        response = await run_flexible_search(_request_from_args(args), timeout=args.timeout)
        if response.get("status") == "failed":
            error = response.get("error") or {}
            raise FlightsError(
                str(error.get("code", "PROVIDER_UNAVAILABLE")),
                str(error.get("message", "flexible-search failed")),
                retryable=bool(error.get("retryable")),
                details=error.get("details") or {},
            )
        return response
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
