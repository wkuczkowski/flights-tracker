---
name: flights-tracker-cli
description: Use the local `flights` CLI to explore countries and cities, resolve airports, search and compare flights from one or many origins, inspect alternative dates, and return structured JSON for agent workflows. Trigger when Codex is asked to discover destinations, find flight deals, compare routes or prices, check flexible travel dates, diagnose provider access, or manually unlock a Skyscanner browser challenge.
---

# Flights Tracker CLI

Use `flights` as the stable interface. Do not call or reproduce provider endpoints directly.

## Install and inspect

From the repository root, install an isolated command when `flights` is unavailable:

```bash
uv tool install --editable .
flights --help
```

For repository development, prefer:

```bash
uv sync
uv run flights --help
```

Substitute `uv run flights` for `flights` in the examples when using the development environment.

## Resolve places

Resolve free-form city, airport, or IATA input before searching when the intended place is uncertain:

```bash
flights places resolve --query "Rzym" --market PL --locale pl-PL --json
```

On success, read the single returned `place`. Pass only its query text or IATA code to public search flags; do not pass provider-internal identifiers. When the response reports `AMBIGUOUS_PLACE`, inspect `choices` and ask the user only when context cannot disambiguate them.

## Search flights

Pass `--origin` repeatedly to compare several departure points in one invocation:

```bash
flights search \
  --origin WAW --origin POZ --origin GDN \
  --destination ROM \
  --depart 2026-09-10 --return 2026-09-17 \
  --adults 1 --cabin economy \
  --market PL --locale pl-PL --currency PLN \
  --depart-before 12:00 --return-after 17:00 \
  --sort price --limit 20 --json
```

Time filters use local airport clocks (`HH:MM`):

- `--depart-before` / `--depart-after` constrain the outbound departure
- `--return-before` / `--return-after` constrain the return departure

Treat a `partial` response with usable results as success. Read `partial_failures` and `warnings`, then tell the user which origins failed. Treat an empty `complete` result as a valid search with no offers.

Prefer structured stdin for agent-generated requests. It avoids shell quoting and preserves the exact query:

```bash
flights search --request - --json <<'JSON'
{
  "schema_version": "1.0",
  "origins": [{"iata": "WAW"}, {"iata": "POZ"}, {"iata": "GDN"}],
  "destination": {"query": "Rzym"},
  "trip": {
    "type": "round_trip",
    "depart": {"date": "2026-09-10"},
    "return": {"date": "2026-09-17"}
  },
  "passengers": {"adults": 1, "children_ages": []},
  "cabin": "economy",
  "market": "PL",
  "locale": "pl-PL",
  "currency": "PLN",
  "filters": {
    "direct_only": false,
    "max_stops": 1,
    "depart_before": "12:00",
    "return_after": "17:00"
  },
  "sort": "price",
  "limit": 20
}
JSON
```

Use `--request request.json` for an existing request file.

## Explore destinations

Use `explore` when the destination is open-ended. The CLI supplies objective indicative observations; you interpret subjective intent such as warm, interesting, or a deal. Work in two explicit stages:

1. Explore countries with `destination_scope: {"level":"country","anywhere":true}`.
2. Select promising countries yourself, then expand only those with `level: "city"`, `anywhere: false`, and public country `code` or `query` references.

```bash
flights explore --request - --json <<'JSON'
{
  "schema_version":"1.0",
  "origins":[{"query":"Gdańsk"},{"query":"Poznań"},{"query":"Warszawa"}],
  "destination_scope":{"level":"country","anywhere":true},
  "trip":{"type":"round_trip","depart":{"scope":"month","month":"2026-09"},"return":{"scope":"month","month":"2026-09"}},
  "passengers":{"adults":1,"children_ages":[]},
  "cabin":"economy",
  "filters":{"include_continents":["EU"],"direct_only":false},
  "sort":"price","limit":50,
  "market":"PL","locale":"pl-PL","currency":"PLN"
}
JSON
```

Date scopes are `exact` + `date`, `month` + `month`, or `anytime`. Treat prices as indicative complete-trip totals for the whole passenger group, not guaranteed offers. `no_quote` means no observed quote, not no flight. Provider tags are evidence, not authoritative destination qualities.

Read every origin state, `partial_failures`, `warnings`, `observed_at`, and `meta.truncated`. Tell the user about failed origins and freshness uncertainty. Apply preferences conversationally; the CLI never loads preference files. Verify selected candidates through `search`, `alternative-dates`, or `flexible-search`. Do not automatically expand all countries.

## Check alternative dates

Use the same route, passenger, market, locale, and currency conventions as search. Pass `--origin` repeatedly to compare date grids across several departure points:

```bash
flights alternative-dates \
  --origin WAW --origin POZ --origin GDN \
  --destination ROM \
  --depart 2026-09-25 --return 2026-09-29 \
  --adults 1 --market PL --locale pl-PL --currency PLN \
  --direct --min-nights 3 --max-nights 5 --limit 30 --json
```

Each result includes `origin`, `departure_date`, `return_date`, `nights`, `price`, and `direct_price`. With `--direct`, only pairs that have a direct quote are kept and sorting uses `direct_price`. Use `--min-nights` / `--max-nights` when the user wants a similar trip length rather than 1-night outliers from the provider grid.

## Flexible nearby-date search

When the user asks for cheaper nearby dates and live itineraries, prefer one `flexible-search` instead of manually fan-outing many `search` calls:

```bash
flights flexible-search \
  --origin WAW --origin POZ --origin GDN \
  --destination ROM \
  --depart 2026-09-25 --return 2026-09-29 \
  --adults 1 --market PL --locale pl-PL --currency PLN \
  --direct --min-nights 3 --max-nights 5 \
  --date-candidates 5 \
  --depart-before 12:00 --return-after 17:00 \
  --limit 20 --timeout 120 --json
```

This command:

1. builds an alternative-dates grid
2. takes the cheapest `--date-candidates` pairs
3. runs live `search` for each pair
4. returns live itineraries plus `date_candidates` and per-result `date_pair` / `guide_price`

Do not present an alternative-date guide quote as a guaranteed bookable fare. Use `flexible-search` first for flexible-date questions; fall back to separate `alternative-dates` + `search` only when you need more control.

## Process JSON

Keep `--json` enabled in agent workflows. Parse stdout as one JSON document; diagnostics belong to stderr.

```bash
flights search --request - --json < request.json \
  | jq '{status, currency, offers: [.results[] | {origin, price, legs}]}'
```

Check both the process exit code and the JSON `status` or `error.code`. Do not scrape human-readable diagnostics.

## Handle failures

Interpret exit codes as follows:

- `0`: success, including usable partial results.
- `2`: invalid input or ambiguous place.
- `3`: `BOT_CHALLENGE`; manual browser action is required.
- `4`: provider failure after retries.
- `5`: rate limit or deadline exhausted.
- `6`: internal, schema, or contract error.

On exit `3` or JSON error `BOT_CHALLENGE`, first open the persistent visible browser:

```bash
flights browser unlock
```

Then ask the user to complete the challenge in that browser. After the user confirms completion, rerun the original command once and report another challenge instead of looping. Never automate CAPTCHA solving or print browser state and cookies.

Use the diagnostic command when access or configuration is uncertain:

```bash
flights doctor --json
```

Preserve the user's dates, passengers, cabin, filters, and locale across retries. Summarize the best results with total price, origin, airports, times, stops, carriers, and any self-transfer or partial-result warnings.

For live results, use `booking_options[].total_price` as the complete price. When `requires_multiple_bookings` is true, nested `booking_items[].price` values are components and must not be compared with complete trips. Treat legacy `agents[]` as deprecated.
