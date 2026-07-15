# CLI do wyszukiwania lotów — dokumentacja integracji

Stan rozpoznania: **2026-07-15**, środowisko: Linux/Polska, `playwright-cli` i kontrolowane żądania HTTP.

## Decyzja w skrócie

CLI korzysta wyłącznie z prywatnego API `skyscanner.pl`. Po ręcznym przejściu CAPTCHA 2026-07-15 udało się wykonać pełne create/poll przez czysty HTTP bez cookies. Kontrakt jest prywatny, chroniony i może zmienić się bez ostrzeżenia, dlatego provider musi wykrywać blokadę i zmianę schematu.

Nie automatyzujemy CAPTCHA ani nie generujemy tokenów `_px*`. Użytkownik może ręcznie odblokować stronę w trwałym profilu Chrome, a właściwe wyszukiwania wykonujemy przez HTTP.

## Dokumenty

- [reconnaissance.md](reconnaissance.md) — przebieg badania Playwright/HTTP i zachowanie ochrony.
- [private-web-api.md](private-web-api.md) — potwierdzony prywatny kontrakt, minimalne nagłówki i odpowiedzi.
- [endpoints.md](endpoints.md) — zwięzły indeks prywatnych endpointów używanych przez CLI.
- [cli-contract.md](cli-contract.md) — stabilne wejście/wyjście JSON dla Agenta Codex.
- [architecture.md](architecture.md) — provider, multi-origin, polling, normalizacja i odporność.
- [testing-and-compliance.md](testing-and-compliance.md) — testy, sekrety i wymagania użycia API.

## Minimalny przepływ

```text
Agent Codex
  -> flights places resolve "Rzym"
  -> 3 x search/create (WAW, POZ, GDN)
  -> poll każdej sesji do COMPLETE/deadline
  -> normalizacja, scalenie i globalny ranking
  -> jeden wersjonowany JSON na stdout
```

Warunkiem uruchomienia jest dostępność prywatnych endpointów z bieżącego IP. W razie `BOT_CHALLENGE` użytkownik wykonuje `flights browser unlock` i ręcznie przechodzi challenge.

## Docelowe komendy

```bash
flights places resolve --query "Rzym" --market PL --locale pl-PL --json

flights search \
  --origin WAW --origin POZ --origin GDN \
  --destination ROM \
  --depart 2026-09-10 --return 2026-09-17 \
  --adults 1 --cabin economy \
  --market PL --locale pl-PL --currency PLN \
  --sort price --limit 20 --json

flights search --request request.json --json
flights search --request - --json < request.json
flights browser unlock
flights doctor --json
```

`flights doctor` sprawdza DNS/TLS/HTTP i kontrakt Autosuggest bez tworzenia sesji Radar ani wypisywania identyfikatorów sesji. Implementacja znajduje się w `src/flights_tracker/`, a testy w `tests/`.
