# Kontrakt CLI dla Agenta Codex

## Zasady procesu

- `stdout`: dokładnie jeden dokument JSON; bez progress barów i logów.
- `stderr`: krótkie diagnostyki, nigdy cookies, JWT, sessionId, token oferty ani trackingowy deeplink.
- JSON ma `schema_version`; nowe pola mogą być dodawane kompatybilnie.
- kwoty są decimal-string, nie `float`; daty są `YYYY-MM-DD`. Czasy lotów są lokalne dla lotniska, bo provider nie zwraca offsetów; pole offset/timezone pozostaje `null`, dopóki autorytatywny resolver stref nie wzbogaci wyniku.
- `--request -` jest preferowany dla Agenta, bo omija quoting shella.

## Wejście i wyjście explore

`explore` jest obiektywnym prymitywem discovery. Agent interpretuje intencję typu „ciepło” albo „okazja”, wybiera kraje do rozwinięcia i osobno uruchamia live `search`. CLI nie ładuje preferencji użytkownika.

```json
{
  "schema_version": "1.0",
  "origins": [{"query": "Gdańsk"}, {"query": "Poznań"}, {"query": "Warszawa"}],
  "destination_scope": {"level": "country", "anywhere": true},
  "trip": {
    "type": "round_trip",
    "depart": {"scope": "month", "month": "2026-09"},
    "return": {"scope": "month", "month": "2026-09"}
  },
  "passengers": {"adults": 1, "children_ages": []},
  "cabin": "economy",
  "stay": {"min_nights": 5, "max_nights": 9},
  "filters": {
    "include_continents": ["Europe"],
    "exclude_destinations": [],
    "max_price": {"amount": "700.00", "currency": "PLN"},
    "direct_only": false
  },
  "sort": "price",
  "limit": 50,
  "market": "PL", "locale": "pl-PL", "currency": "PLN"
}
```

Zakres daty to jeden z: `{"scope":"exact","date":"YYYY-MM-DD"}`, `{"scope":"month","month":"YYYY-MM"}` albo `{"scope":"anytime"}`. `trip.type` jest obowiązkowe. Round trip wymaga niezależnego `trip.return`; one way je odrzuca. `anytime` nie gwarantuje długości pobytu.

Country discovery wymaga `level: "country"` i `anywhere: true`. City expansion używa `level: "city"`, `anywhere: false` oraz `countries`, np. `[{"code":"IT"},{"query":"Hiszpania"}]`. Publiczne requesty i odpowiedzi nie zawierają providerowych entity IDs. Tekstowe referencje `{query: ...}` w filtrach include/exclude przechodzą przez Autosuggest dla bieżącego locale; CLI dopasowuje dopiero rozwiązaną publiczną nazwę/code i zwraca `AMBIGUOUS_PLACE` zamiast lokalnie zgadywać. Jawne publiczne `code` pozostaje fast path bez dodatkowego resolvera. `limit` domyślnie wynosi 50 i przyjmuje 1–200. Filtry kontynentów, destynacji, ceny całej grupy i `direct_only` są stosowane przed limitem. Jedyny ranking CLI to deterministyczna cena.

Każdy wynik ma publiczną `destination`, `best_price`, `best_direct_price`, nieautorytatywne `provider_tags` oraz wszystkie `origin_options`. Stan originu to `quoted`, `no_quote` albo `failed`. `no_quote` oznacza brak obserwacji ceny, nie brak lotu; `failed` zawiera stabilny kod i retryability. Cena jest orientacyjnym totalem kompletnej podróży całej grupy.

`searched_at` jest czasem pobrania. Gdy provider nie podaje czasu obserwacji, `observed_at` pozostaje `null`. Stay jest adnotacją: konkretne daty dają `nights` i boolowskie `stay_match`, a month/anytime — `unknown`. `meta` zawiera `total_candidates`, `returned_candidates` i `truncated`. Agent powinien ujawnić partial failures i niepewność świeżości, a wybrany wynik sprawdzić przez `alternative-dates`, `flexible-search` albo live `search`.

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
  "filters": {"direct_only": false, "max_stops": 1, "depart_before": "12:00", "return_after": "17:00"},
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
- filtry godzinowe `depart_after`, `depart_before`, `return_after`, `return_before` przyjmują lokalne `HH:MM` i są stosowane po normalizacji itineraries.
- `flexible-search` najpierw buduje siatkę `alternative-dates`, bierze `date_candidates` (1-15) najtańszych par i dla każdej robi live `search`; wynik zawiera `date_candidates` oraz `date_pair`/`guide_price` przy ofertach.

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
    "booking_options": [{
      "total_price": {"amount": "499.00", "currency": "PLN"},
      "requires_multiple_bookings": false,
      "transfer_type": null,
      "booking_items": [{
        "agent_name": "Example Agent",
        "price": {"amount": "499.00", "currency": "PLN"},
        "deeplink": "https://example.invalid/redacted"
      }]
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

`booking_options[].total_price` jest autorytatywną ceną kompletnej opcji. Przy `requires_multiple_bookings: true` ceny `booking_items[]` są częściami podróży i nie wolno przedstawiać ich jako pełnej ceny. `agents[]` jest deprecated i zawiera wyłącznie jednoelementowe opcje, których cena reprezentuje całą opcję.

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
