# Endpointy prywatnego API używane przez CLI

Bazowy host: `https://www.skyscanner.pl`. Szczegółowe payloady i wyniki eksperymentów znajdują się w [private-web-api.md](private-web-api.md).

## Lista endpointów

| Zastosowanie | Metoda i ścieżka | Zakres |
|---|---|---|
| rozwiązywanie nazw miejsc | `GET /g/autosuggest-search/api/v1/search-flight/{market}/{locale}/{query}` | MVP |
| uruchomienie wyszukiwania | `POST /g/radar/api/v2/web-unified-search/` | MVP |
| polling wyszukiwania | `GET /g/radar/api/v2/web-unified-search/{sessionId}` | MVP |
| create/poll alternatywnych dat | `POST /g/radar/api/v1/alternative-dates` | opcjonalnie |

Endpointy ochrony PerimeterX/HUMAN, telemetryczne, reklamowe i `conductor` nie należą do kontraktu CLI.

## Wspólne nagłówki Radar

```http
Accept: application/json
Content-Type: application/json
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36
X-Skyscanner-ChannelId: website
X-Skyscanner-Market: PL
X-Skyscanner-Locale: pl-PL
X-Skyscanner-Currency: PLN
X-Skyscanner-ViewId: <UUID v4>
```

Browserowy User-Agent, `ChannelId`, culture i `ViewId` były wymagane w testach. Cookies, `TrustedFunnelId`, traveller context i referer nie były konieczne. Jeden ViewId przypada na logiczne wyszukiwanie i jest ponownie używany w jego pollach.

## Autosuggest

```http
GET /g/autosuggest-search/api/v1/search-flight/PL/pl-PL/Rzym?isDestination=true&enable_general_search_v2=true&autosuggestExp=
User-Agent: <browser UA>
Accept-Language: pl-PL,pl;q=0.9
```

Odpowiedź jest tablicą miejsc z polami `PlaceId`, `PlaceName`, `IataCode`, `GeoId`, `GeoContainerId`, `CountryId`, `CityId` i `Location`. Radar przyjmuje `GeoId`. Zapytanie należy URL-encode'ować jako segment ścieżki.

Deterministyczny wybór:

- jeśli użytkownik podał IATA lub pełną nazwę lotniska, wybierz dokładnie pasujące lotnisko;
- jeśli podał nazwę miasta bez wskazania lotniska, wybierz dokładnie pasującą encję miasta, nawet gdy `IataCode` jest puste; obejmuje to wszystkie lotniska miasta;
- jeśli istnieje więcej niż jedno dokładne dopasowanie miasta w różnych krajach/regionach, zwróć `AMBIGUOUS_PLACE` z kandydatami;
- porównuj nazwy po Unicode casefold i normalizacji znaków, ale nie wybieraj częściowego dopasowania, gdy istnieje exact match;
- użyj `GeoId` wybranej encji jako `entityId` w Radar.

## Radar create

```http
POST /g/radar/api/v2/web-unified-search/
```

```json
{
  "cabinClass": "ECONOMY",
  "childAges": [],
  "adults": 1,
  "legs": [
    {
      "legOrigin": {"@type": "entity", "entityId": "27547454"},
      "legDestination": {"@type": "entity", "entityId": "27539793"},
      "dates": {"@type": "date", "year": "2026", "month": "09", "day": "10"},
      "placeOfStay": "27539793"
    },
    {
      "legOrigin": {"@type": "entity", "entityId": "27539793"},
      "legDestination": {"@type": "entity", "entityId": "27547454"},
      "dates": {"@type": "date", "year": "2026", "month": "09", "day": "17"}
    }
  ]
}
```

Create zwraca `context.status`, `context.sessionId` i `itineraries`. Może od razu zwrócić pełny snapshot albo niewielką partię początkową.

W pierwszym, wychodzącym leg ustaw `placeOfStay` na `GeoContainerId` destination, a jeśli go brak — na `GeoId` destination. Dla encji miasta oba zwykle są równe. Strona nie wysyła `placeOfStay` w legu powrotnym.

## Radar poll

```http
GET /g/radar/api/v2/web-unified-search/{urlencodedSessionId}
```

Poll używa tych samych nagłówków oraz ViewId. Kończymy na `context.status == "complete"` lub deadline. Pusty poll nie kasuje wcześniejszych ofert. Każdy niepusty snapshot zastępuje poprzedni w całości, nawet gdy ma mniej wyników — w teście lista zmalała z 498 do końcowych 454. Nie appendujemy snapshotów i nie logujemy sessionId.

## Alternative Dates

```http
POST /g/radar/api/v1/alternative-dates
```

Create przesyła `requestContext` i `searchRequest`. Odpowiedź daje `pollingSessionId`; kolejne POST-y przesyłają ten sam request oraz ten identyfikator do `POLLING_SESSION_STATUS_COMPLETE`. Wyniki `alternativeDates[]` zawierają daty, availability oraz ceny `amount`, `currencyCode`, `unit`.

## Statusy i zachowanie klienta

| Wynik | Zachowanie |
|---|---|
| `200` JSON | waliduj kształt i status domenowy |
| `307` do `/sttc/px/captcha-v2/` | `BOT_CHALLENGE`, bez automatycznego retry |
| `403 {"reason":"blocked"}` | `BOT_CHALLENGE`, zaoferuj ręczny unlock |
| `400` | `INVALID_REQUEST` lub `CONTRACT_CHANGED`, bez retry |
| `429` | respektuj `Retry-After`, ograniczony backoff |
| `500/502/503/504` | ograniczony retry z jitterem i deadline |
| HTML zamiast JSON | wykryj CAPTCHA/zmianę kontraktu, nie parsuj jako wynik |

CLI może udostępnić `flights browser unlock`, który otwiera widoczny trwały profil. CAPTCHA przechodzi ręcznie użytkownik; po sukcesie CLI wykonuje jeden HTTP probe. Fast path wyszukiwania pozostaje czystym HTTP bez cookies.
