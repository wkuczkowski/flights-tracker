## Agent skills

### Issue tracker

Issues and PRDs are tracked in this repository's GitHub Issues. See `docs/agents/issue-tracker.md`.

### Domain docs

This repository uses the single-context domain documentation layout. See `docs/agents/domain.md`.

### User preferences

General user travel preferences are recorded in `docs/user-preferences.md`. Agents should apply them when relevant, while treating explicit instructions in the current request as authoritative. The CLI does not load this file automatically.

## Cursor Cloud specific instructions

- This is a single Python 3.14 CLI (`flights-tracker`) managed by `uv`; there are no backing services (no DB/server/queue) to start. Dependencies are pre-installed by the startup update script (`uv sync`), so just run commands with `uv run`.
- Standard commands (see `README.md`): tests `uv run pytest`, build `uv build`, run `uv run flights <subcommand>`. There is no linter/formatter configured in this repo.
- Live commands (`search`, `explore`, `places resolve`, `flexible-search`, `alternative-dates`, live `doctor`) hit Skyscanner's private web API and need outbound internet. They are non-deterministic and can return `BOT_CHALLENGE` (exit 3), which trips a cross-process circuit breaker and makes subsequent live commands fail fast until cooldown. Prefer `flights circuit status --json` and `flights doctor` for quick, low-risk connectivity checks; the full pytest suite is fully offline (HTTP mocked) and safe to run anytime.
- `stdout` is exactly one JSON document per command; diagnostics go to `stderr`. Redirect `stderr` separately when capturing JSON.
