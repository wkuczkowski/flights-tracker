# Testowanie, bezpieczeństwo i zgodność

## Macierz testów

### Unit

- round trip, one way, leap day, DST i daty przez północ;
- IATA/BCP-47/ISO-4217, dzieci i cabin enum;
- tekst „Rzym” -> resolver, niejednoznaczne choices;
- exact city vs exact airport: nazwa miasta wybiera city, jawny IATA wybiera airport;
- `placeOfStay` z destination `GeoContainerId`, fallback do `GeoId`, tylko w legu wychodzącym;
- Decimal i stabilne JSON `schema_version`;
- `price.raw` przez Decimal i jednostki alternative dates, w tym `UNIT_WHOLE`;
- lokalne czasy bez fałszywego offsetu oraz duration przepisane z API;
- redakcja sessionId, tokenów wynikowych i deeplinków.

### Orchestrator

- WAW/POZ/GDN uruchamiają trzy osobne sesje z bounded concurrency;
- jeden origin sukces, drugi timeout, trzeci `503` -> użyteczny partial z poprawnymi failures;
- `403 reason=blocked` -> globalny `BOT_CHALLENGE`, przerwanie fan-out;
- create z wynikami -> pusty poll -> niepusty poll -> `complete`, bez utraty snapshotu;
- snapshot 332 -> 498 -> 454 complete: końcowy wynik ma dokładnie 454, bez appendowania;
- expired session -> najwyżej jedno re-create;
- `429 Retry-After` i jitter testowane fake clockiem;
- globalne sortowanie/deduplikacja przed `limit`.

Jawna macierz regresyjna Explore dla preferowanych originów:

| Origin | Symulowany wynik providera | Oczekiwany stan originu | Wpływ na odpowiedź |
|---|---|---|---|
| WAW | `complete` z obserwacjami | `quoted` | wyniki zostają zachowane |
| POZ | timeout po wyczerpaniu retry/deadline | `failed / PROVIDER_TIMEOUT` | useful `partial`, bez utraty WAW |
| GDN | powtarzalne HTTP `503` | `failed / PROVIDER_UNAVAILABLE` | useful `partial`, bez utraty WAW |

Osobna regresja utrzymuje wynik ukończonego originu, gdy drugi task nadal trwa w momencie wyczerpania wspólnego deadline. Niedokończony task otrzymuje jawny per-origin timeout; orchestrator nie odrzuca ukończonych outcomes.

### Contract/fixtures

Fixture prywatnego Radar/Autosuggest musi być sanitizowana i walidowana przeciw własnemu modelowi. Testy sprawdzają brakujące pola, nieznane enumy i zmianę kształtu odpowiedzi. CI domyślnie używa mock HTTP bez sieci. Smoke test live ma niski fan-out, osobny marker i nie działa domyślnie.

Test procesu potwierdza, że stdout jest pojedynczym JSON, a stderr nie zawiera sekretów. Test kompatybilności utrzymuje parsowanie wszystkich fixture'ów z obsługiwanych wersji schematu CLI.

Web contract test uznaje `307 CAPTCHA`/`403 blocked` za spodziewany stan środowiskowy, zwraca `BOT_CHALLENGE` i nie próbuje automatycznie rozwiązać challenge.

## Dane sesyjne

- HTTPS zawsze;
- body błędu i logi przechodzą redakcję;
- brak trwałego zapisu cookies użytkownika i tokenów PerimeterX;
- minimalizuj dane podróżnych — do wyszukiwania zwykle wystarczą liczby/wieki, nie tożsamość;
- określ retencję telemetryczną i usuń query, jeśli może ujawniać plany podróży.

## Zgodność

Prywatne endpointy nie są publicznym, stabilnym kontraktem. Przed dystrybucją poza użytek własny należy sprawdzić aktualne zasady Skyscannera. CLI nie może automatyzować CAPTCHA, generować sensorów ochronnych ani powodować agresywnego ruchu. Wyniki powinny zawierać źródło `Skyscanner`, czas pobrania oraz informacje o trasie, lokalnych czasach, przesiadkach, przewoźniku, sprzedawcy i cenie.

## Zakazane zachowania projektu

- automatyczne rozwiązywanie CAPTCHA;
- generowanie/replay sensorów `_px*` lub collector payloadów;
- podszywanie się pod użytkownika i harvesting cookies;
- retry storm na `307/403`;
- publikowanie sessionId, tokenów ofert lub trackingowych URL-i w logach/fixture'ach.

## Kryteria gotowości

CLI jest gotowy dopiero, gdy istnieją: test `doctor`, ręczny workflow unlock, unit/contract tests, konserwatywne limity lokalne, redakcja logów i test live WAW/POZ/GDN.
