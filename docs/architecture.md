# Architektura, multi-origin i odporność

## Warstwy

```text
argv/stdin -> walidacja -> place resolver -> search orchestrator
                                      -> SkyscannerWebProvider
                                      -> create/poll per origin
                                      -> normalizer -> merge/rank -> JSON
```

`explore` używa równoległej ścieżki discovery: Anywhere zwraca kraje, a jawnie wybrane encje krajów zwracają miasta. Orchestrator grupuje obserwacje po publicznej destynacji, zachowuje stany każdego originu (`quoted`, `no_quote`, `failed`), filtruje, sortuje po cenie i dopiero wtedy stosuje limit. Providerowe identyfikatory służą wyłącznie wewnętrznie.

Interfejs `FlightProvider` oddziela modele domenowe od prywatnego JSON: `resolve_place()`, `create_search()`, `poll_search()` i `alternative_dates()`. Jedynym providerem jest `skyscanner_web`. Fast path używa klienta HTTP bez cookies; pomocniczy browser służy wyłącznie do ręcznego unlock.

## WAW, POZ lub GDN

Alternatywne originy nie są trzema kolejnymi legs. Orchestrator uruchamia osobny create+poll dla każdego originu:

```text
WAW -> ROM -> create -> poll --\
POZ -> ROM -> create -> poll ----> normalize -> global sort -> limit
GDN -> ROM -> create -> poll --/
```

Podróż powrotna dla jednego originu ma dwa elementy `legs` w tym samym create. Fan-out ma ograniczoną współbieżność (np. 2–3), osobny ViewId per origin i wspólny deadline. Awaria jednego originu trafia do `partial_failures`, nie usuwa wyników pozostałych.

Deduplikacja po znormalizowanym zestawie: origin/destination, czasy i segmenty, przewoźnik, agent i cena. Następnie globalny ranking oraz `limit`. Stabilne `id` może być hashem tych pól, ale nie tokenów sesji/deeplinków.

## Polling

1. `create` zwraca `context.sessionId`, `context.status` i początkowe `itineraries`.
2. Zachowaj ostatni niepusty snapshot `itineraries.results`.
3. Poll GET z tym samym ViewId do `context.status == "complete"` albo deadline.
4. Pusty poll nie czyści wyniku; każdy niepusty snapshot zastępuje poprzedni, również gdy liczba ofert maleje.
5. Niedziałająca sesja pozwala najwyżej na jedno ponowne `create`, o ile mieści się w deadline.
6. Wynik niepełny po deadline ma `partial`, jeśli jest użyteczny.

## Timeouty, retry i limity

Rekomendowane wartości startowe, konfigurowalne:

- connect: 5 s;
- create/read: 20–30 s;
- cały search: 60 s;
- exponential backoff z full jitter, np. base 0,5 s, cap 8 s;
- retry tylko connect reset/timeout i `429/500/502/503/504`;
- bez retry dla `400/403/404`; `403 reason=blocked` mapuj na `BOT_CHALLENGE`;
- respektuj `Retry-After` i lokalny limiter per endpoint.

Nie znamy limitów prywatnego API, więc stosuj konserwatywną współbieżność, cache Autosuggest z TTL i brak retry storm. Wyników Radar nie przedstawiaj jako aktualne po odczycie z trwałego cache.

## Normalizacja

- Radar przekazuje lokalne komponenty czasu bez offsetu. Przechowuj je jako `*_local`; `timezone`/offset pozostaje `null`, chyba że osobny airport→IANA resolver jawnie wzbogaci wynik;
- `duration_minutes` bierz z pola duration API, nie obliczaj z lokalnych timestampów;
- dla itineraries używaj `price.raw` przez `Decimal`, a `price.formatted` zachowuj pomocniczo; dla alternative dates interpretuj `amount` zgodnie z `unit` (potwierdzone `UNIT_WHOLE`);
- policz `arrival_day_offset` dla lotów przez północ/date line;
- normalizuj zagnieżdżone legs/segments/carriers oraz słownik `itineraries.agents`; zgłoś `PROVIDER_PROTOCOL_ERROR` przy brakującej referencji;
- deeplink traktuj jako sekretopodobny URL z trackingiem: zwracaj użytkownikowi, ale redaguj w logach;
- pełną cenę live bierz z `pricingOptions[].price`; `items[]` są komponentami jednej opcji. Wieloelementowe opcje trafiają do `booking_options[]`, ale ich części nigdy nie trafiają do deprecated `agents[]` jako rzekome kompletne oferty;

## Telemetria

Logi zawierają `request_id`, endpoint logiczny, status HTTP, próbę, czas i origin. Nie zawierają cookies, sessionId, tokenów wynikowych ani deeplinków. Metryki: latency create/poll, liczba polli, 403/429, partial ratio, provider schema errors i skuteczność per origin.
