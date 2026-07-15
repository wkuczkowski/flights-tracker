# Rozpoznanie strony WWW i zabezpieczeń

## Zakres i reguły

Badanie wykonano 2026-07-15 przez trzech niezależnych wykonawców z użyciem `playwright-cli`, `curl` oraz kontrolowanych prób odtworzenia requestów. W pierwszej fazie nie przechodzono CAPTCHA; w drugiej użytkownik rozwiązał ją ręcznie w widocznej przeglądarce. Identyfikatory sesji, JWT, UUID i wartości cookies zostały celowo pominięte.

Rozdzielamy:

- **zaobserwowane** — request/status widziany w przeglądarce lub HTTP;
- **wyczytane z aktualnego klienta** — kontrakt znaleziony w bundle/source map, bez gwarancji publicznej stabilności;
- **hipoteza** — niepotwierdzony payload; nie wolno budować na nim produkcji.

## Wyniki

| Cel | Metoda | Wynik | Wniosek |
|---|---|---|---|
| `GET https://www.skyscanner.pl/` | Playwright, headless Chrome | `307` do `/sttc/px/captcha-v2/index.html?...`, następnie CAPTCHA `200` | Blokada przed aplikacją |
| ten sam dokument | pojedynczy curl z desktop UA | jeden przebieg zwrócił SSR HTML `200` | Odpowiedź zależy od reputacji/sesji; nie jest gwarancją |
| deep link WAW–Rzym | Playwright i curl | `307` do CAPTCHA | Nie udało się uruchomić prawdziwego UI search |
| autosuggest legacy | pojedynczy GET bez cookies | raz `200 JSON`, po dalszej aktywności `403 blocked` | Endpoint istnieje, ale jest niestabilny i prywatny |
| autosuggest v1 | browser `fetch`, cookies sesji | `403 {"reason":"blocked",...}` | Cookies zwykłej sesji nie wystarczają |
| Radar create | browser/curl POST | routing do usługi `radar`, następnie `403 blocked` | Brak sesji wyszukiwania i prawdziwych wyników |

Warstwa ochronna ładowała zasoby PerimeterX/HUMAN z prefiksem `/rf8vapwA/` i domen `px-cloud.net`, `px-client.net` oraz `perimeterx.net`. Są to endpointy ochrony, **nie API lotów**. Nie należy dokumentować ich payloadów ani próbować ich replayować.

Zaobserwowane cookies obejmowały m.in. `_pxhd`, `_pxvid`, `_px3`, `pxcts`, `__Secure-session_id`, anonimowy token i CSRF. Są dynamiczne i/lub krótkotrwałe. Nie mogą być wejściem CLI, fixture testową ani elementem logów.

## Prywatne endpointy WWW

Poniższe wpisy służą wyłącznie jako zapis rozpoznania. Nie są rekomendacją implementacji.

### Autosuggest

Zaobserwowano działający pojedynczo legacy GET:

```http
GET /g/autosuggest-flights/{marketName}/{locale}/{query}?isDestination=false&enable_general_search_v2=true
Host: www.skyscanner.pl
```

Aktualny klient zawiera także:

```http
GET /g/autosuggest-search/api/v1/search-flight/{market}/{locale}/{urlencodedQuery}?isDestination=false&enable_general_search_v2=true
```

oraz wariant zaobserwowany przez kontrolowany fetch:

```http
GET /g/autosuggest-search/api/v1/search-flight-places?isDestination=true&locale=pl-PL&market=PL&query=Rzym
```

W przeglądarce odpowiedź wyniosła `403` z `reason=blocked`. Jeden początkowy GET legacy zwrócił `200`, ale po kilku żądaniach ta sama ścieżka była stale blokowana na kilku domenach.

### Web Unified Search (Radar)

Kontrakt wyczytany z klienta:

```http
POST /g/radar/api/v2/web-unified-search/
GET  /g/radar/api/v2/web-unified-search/{urlencodedSessionId}
```

Skrócony body z aktualnego klienta:

```json
{
  "cabinClass": "CABIN_CLASS_ECONOMY",
  "childAges": [],
  "adults": 1,
  "legs": [{
    "legOrigin": {"@type": "entity", "entityId": "27547454"},
    "legDestination": {"@type": "entity", "entityId": "27539793"},
    "dates": {"@type": "date", "year": "2026", "month": "09", "day": "15"}
  }]
}
```

Klient dodaje nagłówki `X-Skyscanner-ChannelId`, `Market`, `Locale`, `Currency`, `ViewId`, `TrustedFunnelId` i zgody. Testowy POST trafił do backendu `radar`, ale filtr odpowiedział `403` przed walidacją biznesową. Nie uzyskano autentycznego `sessionId` ani wyników. Schemat może zmienić się bez ostrzeżenia.

Kod klienta wskazuje polling GET mniej więcej po 1 s z rosnącymi odstępami i świeżość sesji rzędu 60 s. To obserwacja implementacji WWW, nie SLA.

## Kolejny eksperyment po ręcznej CAPTCHA

W tej samej trwałej, widocznej sesji użytkownik ręcznie przeszedł challenge. Strona główna oraz deep link WAW–Rzym zaczęły działać, a przeglądarka wykonała prawdziwy Radar create i trzy polle zakończone `complete` z 496 ofertami. Następnie ten sam payload został odtworzony przez `curl` bez cookies: właściwy zestaw nagłówków zwrócił te same 496 ofert.

To zmienia wniosek techniczny: cookies nie są obecnie wymagane przez Radar ani Autosuggest, ale ochrona nadal rozpoznaje kształt klienta. `curl` z domyślnym User-Agent dostał `403`, natomiast browserowy User-Agent i wymagane `X-Skyscanner-*` przechodziły. Nie wiadomo, czy ręczna CAPTCHA odblokowała IP, profil, czy oba — to inference, nie dowiedziony kontrakt. Szczegóły są w [private-web-api.md](private-web-api.md).

## Decyzja

Prywatne API może zasilać provider eksperymentalny, ponieważ ręcznie odblokowana sesja pozwoliła na skuteczny czysty HTTP. Nie należy jednak przedstawiać go jako stabilnego publicznego API. CLI powinno rozpoznać `307` do CAPTCHA lub JSON `reason=blocked`, zakończyć bez retry storm i zwrócić `BOT_CHALLENGE`; ręczne odblokowanie pozostaje działaniem użytkownika.

Powtórne badanie ma sens tylko w zwykłej, legalnej sesji użytkownika lub po uzyskaniu pisemnej zgody Skyscanner. Nawet wtedy prywatny ruch należy traktować diagnostycznie, nie jako publiczny kontrakt.
