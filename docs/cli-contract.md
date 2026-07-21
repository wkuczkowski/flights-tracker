# Kontrakt CLI dla Agenta Codex

## Zasady procesu

- `stdout`: dokładnie jeden dokument JSON; bez progress barów i logów.
- `stderr`: krótkie diagnostyki, nigdy cookies, JWT, sessionId, token oferty ani trackingowy deeplink.
- JSON ma `schema_version`; nowe pola mogą być dodawane kompatybilnie.
- kwoty są decimal-string, nie `float`; daty są `YYYY-MM-DD`. Czasy lotów są lokalne dla lotniska, bo provider nie zwraca offsetów; pole offset/timezone pozostaje `null`, dopóki autorytatywny resolver stref nie wzbogaci wyniku.
- `--request -` jest preferowany dla Agenta, bo omija quoting shella.
- `request_budget` (1–200) opcjonalnie ogranicza liczbę requestów do providera rozpoczętych przez cały workflow. Domyślne limity to: `places resolve` 4, `search` 30, `alternative-dates` 30, `explore` 36 i `flexible-search` 60.

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

`searched_at` jest czasem pobrania. Gdy provider nie podaje czasu obserwacji, `observed_at` pozostaje `null`. Stay jest adnotacją: konkretne daty dają `nights` i boolowskie `stay_match`, a month/anytime — `unknown`. `meta` zawiera `total_candidates`, `returned_candidates`, `truncated` oraz `request_budget` z polami `limit`, `started` i `remaining`. Agent powinien ujawnić partial failures i niepewność świeżości, a wybrany wynik sprawdzić przez `alternative-dates`, `flexible-search` albo live `search`.

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
- `flexible-search` najpierw buduje siatkę `alternative-dates`, a następnie wybiera `date_candidates` (1-15, domyślnie 3) par do live `search`; wybór jest deterministycznie zbalansowany między originami (round-robin po posortowanych cenowo kolejkach per-origin), deduplikuje identyczne zapytania i nadal preferuje tańsze pary. Wynik zawiera `date_candidates` oraz `date_pair`/`guide_price` przy ofertach.
- Workflow providera są serializowane między procesami CLI. Oczekiwanie na globalny lock respektuje deadline; walidacja i inne operacje lokalne nie wymagają locka. Domyślne workflow `search`, `explore`, `alternative-dates` i `flexible-search` wykonują resolve oraz fan-out originów sekwencyjnie także w obrębie procesu. W obrębie jednego workflow Autosuggest/resolve nadal korzysta z request-scoped cache, deduplikacji i jednego współdzielonego klienta HTTP. Pierwszy challenge zatrzymuje kolejne originy; CLI nie usuwa ich po cichu ani nie ponawia `BOT_CHALLENGE`.
- Każdy rozpoczęty provider request zużywa jedną jednostkę `request_budget`, również retry dla timeoutów/5xx. Po wyczerpaniu limitu lokalna bramka zwraca `REQUEST_BUDGET_EXCEEDED` bez kolejnego requestu i raportuje wykorzystanie w `error.details.request_budget`.

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
    "is_self_transfer": null,
    "airport_change": false,
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

`is_self_transfer` ma trzy stany: `true`, `false` albo `null` (provider nie podał informacji i `transferType` nie pozwala jej wywnioskować). `airport_change` jest niezależnym, trójstanowym wykryciem zmiany lotniska między sąsiednimi segmentami. Segment zachowuje publiczny/display code w `origin`/`destination`, a zweryfikowane jawne pola providerowe w `origin_iata`/`destination_iata`. `carrier.iata` jest ustawiane wyłącznie z jawnego pola IATA; providerowy `alternateId` trafia do `carrier.alternate_code`, nie jest automatycznie nazywany IATA.

Dla zgodności istniejące stringi `origin`/`destination` pozostają w wynikach, ale nowe `origin_place`/`destination_place` rozdzielają `name` od `public_code`. Analogiczne tożsamości są dostępne w query search oraz w `origin_identity`/`destination_identity` alternative-dates. `public_code` jest publicznym kodem zapytania zwróconym przez Autosuggest; kod konkretnego lotniska dla segmentu należy czytać z `origin_iata`/`destination_iata`.

W Explore prawidłowa `cheapest_direct_price` implikuje `direct_flights_available: true`. Jeśli provider jednocześnie zwróci `flightRoutes.directFlightsAvailable: false`, origin option ma `direct_availability_conflict: true`, a odpowiedź zawiera warning. Pozostałe typy i wymagane pola nadal podlegają ścisłej walidacji.

`status` przyjmuje `complete`, `partial` lub `failed`. Użyteczny wynik częściowy ma exit `0` i ostrzeżenia. Awaria wszystkich originów ma `failed` i kod niezerowy. Brak ofert jest poprawną odpowiedzią `complete` z pustym `results`, a nie awarią providera.

## Błąd

```json
{
  "schema_version": "1.0",
  "request_id": "01J...",
  "status": "failed",
  "error": {
    "code": "BOT_CHALLENGE",
    "message": "The provider returned a browser challenge after the request reached it",
    "retryable": false,
    "details": {
      "source": "provider_response",
      "network_attempted": true,
      "provider_phase": "radar_create",
      "challenge_kind": "provider_blocked",
      "request_budget": {"limit": 30, "started": 4, "remaining": 26},
      "circuit_breaker": {
        "state": "open",
        "opened_at": "2026-07-21T11:00:00Z",
        "next_probe_at": "2026-07-21T11:15:00Z",
        "cooldown_seconds": 900,
        "cooldown_remaining": 900.0,
        "remaining_seconds": 900.0,
        "manual_half_open": false,
        "storage_status": "valid"
      }
    }
  }
}
```

Stabilne kody: `INVALID_ARGUMENT`, `AMBIGUOUS_PLACE`, `BOT_CHALLENGE`, `REQUEST_BUDGET_EXCEEDED`, `RATE_LIMITED`, `PROVIDER_TIMEOUT`, `SESSION_EXPIRED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_PROTOCOL_ERROR`, `CONTRACT_CHANGED`, `INTERNAL_ERROR`.

Dla `BOT_CHALLENGE` pola diagnostyczne nie zależą od tekstu `message`:

- `source: provider_response` i `network_attempted: true` oznaczają świeżą odpowiedź challenge z providera;
- `source: local_circuit` i `network_attempted: false` oznaczają lokalny fail-fast bez HTTP;
- `provider_phase` należy do bezpiecznego zbioru `autosuggest`, `radar_create`, `radar_poll`, `alternative_dates_create`, `alternative_dates_poll`, `local_gate`;
- `challenge_kind` opisuje wyłącznie klasę zdarzenia (`http_status`, `redirect`, `provider_blocked`, `local_cooldown`) i nigdy nie zawiera URL, nagłówków, cookies ani tokenów;
- `request_budget.started` jest wiarygodną liczbą requestów rozpoczętych przez koordynowany workflow. Gdy provider jest wywołany poza workflow, pole może być pominięte zamiast udawać dokładność.

Exit codes: `0` sukces/użyteczny partial, `2` walidacja, `3` wymagany ręczny unlock, `4` provider po wyczerpaniu retry, `5` rate limit/deadline/request budget, `6` błąd wewnętrzny/schematu.

Pierwszy `BOT_CHALLENGE` otwiera współdzielony między procesami circuit breaker. Kolejne workflow kończą się fail-fast kodem 3 bez requestu do providera. Po cooldownie tylko zserializowany workflow przechodzi do half-open; ponowny challenge otwiera circuit ponownie. `browser unlock` samo otwiera widoczną przeglądarkę i nie twierdzi, że search działa. Po ręcznym ukończeniu challenge `browser unlock --probe` wykonuje pojedynczy lekki probe; jego sukces ustawia lokalny circuit na half-open i pozostawia użytkownikowi dokładnie jeden kontrolowany retry oryginalnej komendy. Cookies, tokeny i nagłówki przeglądarki nie są kopiowane.

## Circuit status (offline)

`flights circuit status --json` odczytuje wyłącznie lokalny, niesekretny plik stanu pod krótkim lockiem. Nie tworzy klienta HTTP i nie dotyka sieci. Zwraca `network_checked: false`, `search_readiness` oraz `circuit_breaker` z `state`, `opened_at`, `next_probe_at`, `cooldown_remaining`, kompatybilnym `remaining_seconds`, `manual_half_open` i `storage_status`.

Brak pliku daje bezpieczny stan `closed` + `storage_status: missing`; stan uszkodzony daje `closed` + `corrupt`; otwarty stan po cooldownie pozostaje obserwowalny jako `open` + `stale`, z readiness `controlled_retry`. Status `allowed` oznacza wyłącznie, że lokalny circuit nie blokuje workflow — nie gwarantuje działania prywatnego API. Nie istnieje bezwarunkowa komenda reset.

## Doctor

`doctor` nie tworzy domyślnie requestu Radar. Zwraca:

```json
{
  "status": "ok",
  "checks": {"http": "ok", "autosuggest_contract": "ok", "radar": "not_checked"},
  "search_readiness": {"status": "unknown", "reason": "radar_not_checked"},
  "circuit_breaker": {
    "state": "closed",
    "opened_at": null,
    "next_probe_at": null,
    "cooldown_seconds": 900,
    "cooldown_remaining": 0.0,
    "remaining_seconds": 0.0,
    "manual_half_open": false,
    "storage_status": "missing"
  }
}
```

Stan `ok` potwierdza tylko wykonane lekkie checki, nie gotowość live search. `network_checks_attempted` jawnie mówi, czy `doctor` próbował DNS/TLS/HTTP. Przy otwartym/half-open circuit `doctor` nie zużywa kontrolowanego retry, pomija HTTP i zwraca `degraded`, readiness wynikające z lokalnej bramki oraz bieżący stan circuit breakera.
