# Kontrakt CLI dla Agenta Codex

## Zasady procesu

- `stdout`: dokładnie jeden dokument JSON; bez progress barów i logów.
- `stderr`: krótkie diagnostyki, nigdy cookies, JWT, sessionId, token oferty ani trackingowy deeplink.
- JSON ma `schema_version`; nowe pola mogą być dodawane kompatybilnie.
- kwoty są decimal-string, nie `float`; daty są `YYYY-MM-DD`. Czasy lotów są lokalne dla lotniska, bo provider nie zwraca offsetów; pole offset/timezone pozostaje `null`, dopóki autorytatywny resolver stref nie wzbogaci wyniku.
- `--request -` jest preferowany dla Agenta, bo omija quoting shella.

## Wejście search

```json
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
```

Reguły walidacji:

- przeszła data to `INVALID_ARGUMENT`; return musi być późniejszy lub równy depart zgodnie z przyjętą polityką;
- IATA uppercase, locale BCP-47, waluta ISO-4217;
- miejsce tekstowe zawsze przechodzi Autosuggest; przy niejednoznaczności zwracane są `choices`, nie ciche zgadywanie;
- maksymalna liczba originów i pasażerów powinna być jawnie ograniczona;
- `direct_only` implikuje `max_stops: 0`.

## Wyjście search

```json
{
  "schema_version": "1.0",
  "request_id": "01J...",
  "status": "partial",
  "provider": "skyscanner_web",
  "price_kind": "live",
  "currency": "PLN",
  "searched_at": "2026-07-15T16:00:00Z",
  "query": {"origins": ["WAW", "POZ", "GDN"], "destination": "ROM"},
  "results": [{
    "id": "stable-hash",
    "origin": "WAW",
    "destination": "ROM",
    "price": {"amount": "499.00", "currency": "PLN"},
    "legs": [{
      "departure_local": "2026-09-10T07:10:00",
      "arrival_local": "2026-09-10T09:20:00",
      "departure_timezone": null,
      "arrival_timezone": null,
      "arrival_day_offset": 0,
      "duration_minutes": 130,
      "stops": 0,
      "segments": [{
        "flight_number": "XX123",
        "carrier": {"id": "xx", "name": "Example", "iata": "XX"},
        "origin": "WAW",
        "destination": "FCO",
        "departure_local": "2026-09-10T07:10:00",
        "arrival_local": "2026-09-10T09:20:00"
      }]
    }],
    "agents": [{
      "name": "Example Agent",
      "price": {"amount": "499.00", "currency": "PLN"},
      "deeplink": "https://example.invalid/redacted"
    }],
    "is_self_transfer": false,
    "sustainability": {
      "is_eco_contender": false,
      "eco_contender_delta_percent": null
    }
  }],
  "partial_failures": [{
    "origin": "POZ",
    "code": "PROVIDER_TIMEOUT",
    "retryable": true
  }],
  "warnings": [],
  "meta": {
    "result_count": 1,
    "origins_succeeded": 2,
    "origins_failed": 1,
    "polls": 3,
    "elapsed_ms": 4200
  }
}
```

`status` przyjmuje `complete`, `partial` lub `failed`. Użyteczny wynik częściowy ma exit `0` i ostrzeżenia. Awaria wszystkich originów ma `failed` i kod niezerowy. Brak ofert jest poprawną odpowiedzią `complete` z pustym `results`, a nie awarią providera.

## Błąd

```json
{
  "schema_version": "1.0",
  "request_id": "01J...",
  "status": "failed",
  "error": {
    "code": "BOT_CHALLENGE",
    "message": "Run 'flights browser unlock' and complete the browser challenge",
    "retryable": false,
    "details": {}
  }
}
```

Stabilne kody: `INVALID_ARGUMENT`, `AMBIGUOUS_PLACE`, `BOT_CHALLENGE`, `RATE_LIMITED`, `PROVIDER_TIMEOUT`, `SESSION_EXPIRED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_PROTOCOL_ERROR`, `CONTRACT_CHANGED`, `INTERNAL_ERROR`.

Exit codes: `0` sukces/użyteczny partial, `2` walidacja, `3` wymagany ręczny unlock, `4` provider po wyczerpaniu retry, `5` rate limit/deadline, `6` błąd wewnętrzny/schematu.
