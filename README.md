# flights-tracker

Eksperymentalne CLI JSON dla Agentów AI, korzystające wyłącznie z prywatnego webowego API `skyscanner.pl` i bez cookies.

```bash
uv sync
uv run flights places resolve WAW
uv run flights search --origin WAW --origin POZ --destination Rzym --depart 2026-09-10 --return 2026-09-17 --limit 10
uv run flights flexible-search --origin WAW --origin GDN --destination Rzym --depart 2026-09-25 --return 2026-09-29 --direct --min-nights 3 --max-nights 5 --depart-before 12:00 --return-after 17:00 --date-candidates 5
uv run flights doctor
```

CLI jest zaimplementowane w `src/flights_tracker/`; komendy `places resolve`, `search`, `alternative-dates`, `flexible-search`, `doctor` i `browser unlock` są dostępne po `uv sync`. `doctor` sprawdza DNS/TLS/HTTP i kontrakt Autosuggest, ale celowo nie tworzy kosztownego wyszukiwania Radar.

Preferowane wejście agenta:

```bash
uv run flights search --request - < request.json
```

`stdout` zawiera dokładnie jeden dokument JSON, a diagnostyka trafia do `stderr`. Prywatny kontrakt nie ma SLA; `BOT_CHALLENGE` (exit 3) oznacza, że bieżące IP musi ręcznie przejść weryfikację w przeglądarce. Szczegóły kontraktu: [docs/cli-contract.md](docs/cli-contract.md).

```bash
uv run flights browser unlock --profile ~/.local/share/flights-browser --json
# ukończ weryfikację w widocznym oknie, następnie:
uv run flights doctor --json
```
