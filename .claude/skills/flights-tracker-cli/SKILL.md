---
name: flights-tracker-cli
description: Use the local `flights` CLI to resolve airports and cities, search and compare flights from one or many origins, inspect alternative dates, and return structured JSON for agent workflows. Trigger when Codex is asked to find flights, compare routes or prices, check flexible travel dates, diagnose provider access, or manually unlock a Skyscanner browser challenge.
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
  --sort price --limit 20 --json
```

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
  "filters": {"direct_only": false, "max_stops": 1},
  "sort": "price",
  "limit": 20
}
JSON
```

Use `--request request.json` for an existing request file.

## Check alternative dates

Use the same route, passenger, market, locale, and currency conventions as search:

```bash
flights alternative-dates \
  --origin WAW --destination ROM \
  --depart 2026-09-10 --return 2026-09-17 \
  --adults 1 --market PL --locale pl-PL --currency PLN --json
```

Use alternative dates to identify promising date pairs, then run `flights search` for live itineraries and current prices. Do not present an alternative-date quote as a guaranteed bookable fare.

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
