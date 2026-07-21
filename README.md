# flights-tracker

Eksperymentalne CLI JSON dla Agentów AI, korzystające wyłącznie z prywatnego webowego API `skyscanner.pl` i bez cookies.

```bash
uv sync
uv run flights places resolve WAW
uv run flights explore --request explore.json
uv run flights search --origin WAW --origin POZ --destination Rzym --depart 2026-09-10 --return 2026-09-17 --limit 10
uv run flights flexible-search --origin WAW --origin GDN --destination Rzym --depart 2026-09-25 --return 2026-09-29 --direct --min-nights 3 --max-nights 5 --depart-before 12:00 --return-after 17:00 --date-candidates 3
uv run flights circuit status --json
uv run flights doctor
```

CLI jest zaimplementowane w `src/flights_tracker/`; komendy `places resolve`, `explore`, `search`, `alternative-dates`, `flexible-search`, `doctor` i `browser unlock` są dostępne po `uv sync`. `explore` służy do dwuetapowego odkrywania krajów, a następnie miast; nie ocenia samodzielnie, co jest ciepłe albo stanowi okazję. `doctor` sprawdza DNS/TLS/HTTP i kontrakt Autosuggest, ale celowo nie tworzy kosztownego wyszukiwania Radar, dlatego raportuje `search_readiness.status: unknown`.

Preferowane wejście agenta:

```bash
uv run flights search --request - < request.json
uv run flights explore --request - < explore.json
```

`stdout` zawiera dokładnie jeden dokument JSON, a diagnostyka trafia do `stderr`. Prywatny kontrakt nie ma SLA; `BOT_CHALLENGE` (exit 3) oznacza, że bieżące IP musi ręcznie przejść weryfikację w przeglądarce. Szczegóły kontraktu: [docs/cli-contract.md](docs/cli-contract.md).

```bash
uv run flights browser unlock --profile ~/.local/share/flights-browser --json
# ukończ weryfikację w widocznym oknie, następnie włącz jeden kontrolowany retry:
uv run flights browser unlock --profile ~/.local/share/flights-browser --probe --json
```

Workflow providera są serializowane między procesami, a zwykły multi-origin resolve/fan-out jest domyślnie sekwencyjny także wewnątrz procesu. Cache, deduplikacja i współdzielona sesja HTTP pozostają aktywne. Każdy workflow ma jawny `request_budget`, raportowany w `meta`; opcjonalny `--request-budget` może go obniżyć lub podnieść w zakresie kontraktu.

Pierwszy `BOT_CHALLENGE` otwiera współdzielony cooldown/circuit breaker, więc kolejne komendy kończą się fail-fast bez dodatkowych requestów. Błąd odróżnia świeżą odpowiedź providera (`source: provider_response`, `network_attempted: true`) od lokalnej bramki (`source: local_circuit`, `network_attempted: false`) i podaje bezpieczną fazę oraz snapshot circuitu. `BOT_CHALLENGE` nie jest automatycznie ponawiany.

`flights circuit status --json` jest całkowicie offline: nie tworzy klienta HTTP, a brak/uszkodzony/stary stan raportuje bez blokowania CLI na zawsze. `doctor` nadal wykonuje jawne lekkie checki sieciowe tylko przy zamkniętym circuit. `browser unlock --probe` respektuje ten sam globalny lock co search/explore. Nie potwierdza działania Radar i nie kopiuje cookies, nagłówków, tokenów ani innego stanu profilu przeglądarki; sukces jedynie przełącza lokalny circuit do half-open przed pojedynczym retry.
