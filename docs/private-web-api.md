# Prywatne API Skyscannera — potwierdzony kontrakt eksperymentalny

Stan na **2026-07-21**. Ten dokument opisuje requesty faktycznie wykonane przez działającą stronę i odtworzone przez klienta HTTP. API jest prywatne, bez SLA i może zmienić endpoint, payload lub reguły ochrony.

## Explore / Everywhere

Explore używa tego samego `POST /g/radar/api/v2/web-unified-search/`. Dla country discovery outbound ma `legDestination: {"@type":"everywhere"}`, a inbound odwrotny kierunek. Dla city expansion miejsce Everywhere zastępuje dynamicznie rozwiązaną encję wybranego kraju. Body zawiera `options.maxDestinations: 200`.

Potwierdzone reprezentacje zakresu dat to `date` z rokiem, miesiącem i dniem, `month` z rokiem i miesiącem oraz `anytime` bez dodatkowych pól. Odpowiedź country ma korzeń `everywhereDestination`, a city — `countryDestination`. Kolekcje mają własny `context`, `features`, `buckets` i `results`. Publicznie użyteczne pola location to `skyCode`, `name`, `type` oraz kontynent; `flightQuotes` rozdziela cheapest/direct, a `flightRoutes.directFlightsAvailable` informuje o bezpośredniej trasie. Bucket `category: VIBES` jest zachowywany jako nieautorytatywny tag.

Providerowe `result.id`, `location.id` i sessionId nigdy nie wychodzą w kontrakcie CLI. Ceny Explore są obserwacjami orientacyjnymi, a nie ofertami live; odpowiedź nie dostarcza autorytatywnego czasu obserwacji.

## Najważniejszy wynik

Po ręcznym przejściu CAPTCHA:

- browserowy Radar create: `200`, początkowo 10 ofert;
- polle: `200`, kolejno wynik niepełny, 492 oferty, następnie `complete` z 496 ofertami;
- czysty `curl` bez cookies: `200`, `complete`, 496 ofert;
- czysty HTTP dla POZ→ROM i GDN→ROM: `200` z niepełnymi wynikami create;
- Autosuggest bez cookies i z browserowym User-Agent: `200`;
- domyślny User-Agent curl: `403 {"reason":"blocked",...}`.

Cookies nie były potrzebne w udanych replayach. Nie oznacza to, że nigdy nie będą wymagane. Ręczne CAPTCHA mogło poprawić reputację bieżącego IP lub przeglądarki; test nie rozstrzyga mechanizmu.

Ponowna weryfikacja **2026-07-21** wykazała ważne ograniczenie operacyjne. Pierwszy challenge ukończony przez wrapper `flights browser unlock` nie odblokował bezcookie HTTP używanego przez `flights doctor`. Dopiero challenge ukończony w bezpośrednio otwartej, nazwanej sesji `playwright-cli` z jawnym trwałym profilem pozwolił wykonać pojedynczy uzgodniony retry: country Explore, city expansion i live follow-up zakończyły się `complete`. Unlock jest zatem pomocą dla człowieka, a nie gwarancją odblokowania stateless fast path. Agent powinien najpierw potwierdzić działanie tej samej interaktywnej sesji, wykonać oryginalny request tylko raz i jawnie zgłosić kolejną blokadę zamiast zapętlać recovery.

Porównanie UI Explore i CLI dla tego samego originu i dat, wykonane w odstępie kilkudziesięciu sekund, pokazało różne kwoty orientacyjne (np. Włochy). Jest to oczekiwany dowód na cache, aktualizację lub kontekst prezentacji, a nie błąd arytmetyczny CLI. Takich wartości nie wolno utrwalać jako stabilnych asercji ani przedstawiać jako ofert live.

## Minimalny zestaw nagłówków Radar

Potwierdzony działający zestaw:

```http
Accept: application/json
Content-Type: application/json
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36
X-Skyscanner-ChannelId: website
X-Skyscanner-Market: PL
X-Skyscanner-Locale: pl-PL
X-Skyscanner-Currency: PLN
X-Skyscanner-ViewId: <losowy UUID v4 na wyszukiwanie>
```

`X-Skyscanner-ViewId` był funkcjonalnie istotny: bez niego backend odpowiadał `200 complete`, ale z pustymi itineraries. Sam `TrustedFunnelId` nie zastępował ViewId; `TrustedFunnelId`, referer, cookies, consent i traveller context nie były konieczne w udanym replayu.

Brak browserowego User-Agent powodował `403 blocked`. Brak `ChannelId` albo parametrów culture powodował `400`. `Accept` nie został niezależnie wyeliminowany, więc pozostaje w kontrakcie minimalnym.

UUID należy generować lokalnie dla logicznego wyszukiwania i używać konsekwentnie w jego create/poll. Nie kopiujemy identyfikatora użytkownika ani sesji z przeglądarki.

## Autosuggest

```http
GET /g/autosuggest-search/api/v1/search-flight/{market}/{locale}/{urlencodedQuery}
    ?isDestination={true|false}
    &enable_general_search_v2=true
    &autosuggestExp=
Host: www.skyscanner.pl
User-Agent: <browser UA>
Accept-Language: pl-PL,pl;q=0.9
```

Przykład:

```http
GET /g/autosuggest-search/api/v1/search-flight/PL/pl-PL/Rzym?isDestination=true&enable_general_search_v2=true&autosuggestExp=
```

Odpowiedź jest tablicą. Pierwszy kandydat dla Rzymu:

```json
{
  "Tags": ["NEARBY_CITY"],
  "PlaceId": "ROME",
  "PlaceName": "Rzym",
  "CountryId": "IT",
  "CityId": "ROME",
  "IataCode": "ROM",
  "CountryName": "Włochy",
  "GeoId": "27539793",
  "GeoContainerId": "27539793",
  "Location": "41.88569536124836,12.460805822413622"
}
```

Do Radar przekazujemy `GeoId` miasta/lotniska, nie `PlaceId`. Potwierdzone city IDs:

| Miejsce | IATA | GeoId |
|---|---:|---:|
| Warszawa | WARS | `27547454` |
| Poznań | POZ | `27546015` |
| Gdańsk | GDN | `27541787` |
| Rzym | ROM | `27539793` |

IDs powinny być rozwiązywane dynamicznie przez Autosuggest, nie kodowane na stałe.

Autosuggest może zwrócić osobno dokładną encję miasta i jego główne lotnisko. Dla zapytania będącego nazwą miasta wybieramy exact city match, nawet jeśli ma pusty `IataCode`; dla jawnego IATA lub nazwy lotniska wybieramy airport match. Niejednoznaczne miasta w różnych krajach wymagają odpowiedzi z kandydatami. Exact match porównujemy przez Unicode casefold/normalizację.

## Radar create

```http
POST https://www.skyscanner.pl/g/radar/api/v2/web-unified-search/
```

Autentyczny payload Warszawa–Rzym, round trip:

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

Pierwszy leg ma `placeOfStay`; powrotny nie. `cabinClass` strony to `ECONOMY`, a nie wcześniejsza hipoteza `CABIN_CLASS_ECONOMY`.

`placeOfStay` ustawiamy na `GeoContainerId` wybranej destination, z fallbackiem do jej `GeoId`. Dla destination będącej miastem wartości zwykle są identyczne. Jest to reguła odtworzona z payloadu WWW i wymaga testu kontraktowego przy zmianie schematu.

Odpowiedź:

```json
{
  "context": {"status": "incomplete", "sessionId": "<redacted>"},
  "itineraries": {
    "filterStats": {"total": 10},
    "results": [],
    "agents": [],
    "buckets": []
  }
}
```

`results[]` zawiera m.in. `id`, `price`, `score`, `eco`, `tags`, `legs`, `pricingOptions`, `isSelfTransfer`. Webowa cena ma gotowe pola `raw` i `formatted`; np. `{"raw":384.76,"formatted":"385 zł"}`.

Leg zawiera `origin`, `destination`, `durationInMinutes`, `stopCount`, lokalne `departure`/`arrival`, `timeDeltaInDays`, `carriers` i `segments`. Czasy nie mają offsetu strefy. `eco.ecoContenderDelta` jest wartością procentową, nie kilogramami CO2e.

## Radar poll

```http
GET https://www.skyscanner.pl/g/radar/api/v2/web-unified-search/{urlencodedSessionId}
```

Używamy tego samego ViewId i nagłówków culture. Poll aż `context.status == "complete"`, z deadline i rosnącym odstępem. Ważna semantyka potwierdzona testem:

- poll może zwrócić zero `results`, mimo że create miał wyniki;
- poll po już ukończonym create również zwrócił pusty `complete`;
- dlatego pusty poll oznacza „brak nowych danych”, a nie wyczyszczenie wcześniejszych ofert;
- każdy niepusty poll zastępuje snapshot wyniku; lista może także zmaleć (w niezależnym teście 332 → 498 → końcowe 454), więc nie appendujemy ani nie zachowujemy większej tylko dlatego, że była większa;
- zachowujemy ostatni niepusty snapshot do `complete` lub deadline.

SessionId jest krótkotrwały i nie powinien trafiać do logów ani cache trwałego.

## Alternative Dates

Create i poll używają tej samej ścieżki oraz metody POST:

```http
POST https://www.skyscanner.pl/g/radar/api/v1/alternative-dates
```

Pierwszy body:

```json
{
  "requestContext": {
    "localisationContext": {"currency": "PLN", "locale": "pl-PL", "market": "PL"},
    "trustedFunnelId": "<ViewId>",
    "viewId": "<ViewId>",
    "channelId": "website"
  },
  "searchRequest": {
    "adults": 1,
    "childAges": [],
    "legs": [
      {"date":{"year":2026,"month":9,"day":10},"origin":["27547454"],"destination":["27539793"]},
      {"date":{"year":2026,"month":9,"day":17},"origin":["27539793"],"destination":["27547454"]}
    ],
    "nearbyAirports": {"includeOriginNearbyAirports": false, "includeDestinationNearbyAirports": false},
    "cabinClass": "ECONOMY"
  }
}
```

Odpowiedź create zawiera `pollingSession.status` i `pollingSessionId`. Poll wysyła ponownie `requestContext`, `searchRequest` oraz `pollingSessionId`. Końcowy test zwrócił `POLLING_SESSION_STATUS_COMPLETE` oraz 225 par dat. Element:

```json
{
  "departureDate": "2026-09-03",
  "returnDate": "2026-09-10",
  "availability": "AVAILABILITY_PRICE_AVAILABLE",
  "cheapestPrice": {"currencyCode": "PLN", "amount": "195", "unit": "UNIT_WHOLE"},
  "priceCategory": "PRICE_CATEGORY_LOWEST",
  "directAvailability": "AVAILABILITY_PRICE_AVAILABLE",
  "cheapestDirectPrice": {"currencyCode": "PLN", "amount": "195", "unit": "UNIT_WHOLE"}
}
```

## Strategia providera

1. `resolve-place` przez Autosuggest z browserowym UA.
2. Osobny Radar create dla WAW/POZ/GDN, każdy z własnym ViewId.
3. Poll sesji z ograniczoną współbieżnością i wspólnym deadline.
4. Zachowaj ostatni niepusty snapshot per origin.
5. Znormalizuj, scal, globalnie posortuj i dopiero wtedy zastosuj limit.
6. Na `307`, `403 reason=blocked` lub HTML CAPTCHA zwróć `BOT_CHALLENGE`, bez automatycznego retry storm.
7. Opcjonalna komenda `flights browser unlock` otwiera widoczny trwały profil; użytkownik ręcznie przechodzi challenge, po czym CLI ponawia jeden request diagnostyczny.

Provider powinien wykonywać zwykłe HTTP jako fast path. Browser jest potrzebny tylko do ręcznego unlock i okresowej ponownej inspekcji kontraktu, nie do każdego wyszukiwania.
