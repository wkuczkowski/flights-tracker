# Serialize provider workflows and share challenge state

Provider-facing CLI workflows are serialized across processes with an advisory `flock` stored in the user's runtime/state directory. The kernel releases the lock when a process exits, including abnormal exits, and lock acquisition is bounded by the workflow deadline. Request validation and other local-only work happens before lock acquisition.

The same directory stores a small, non-secret `BOT_CHALLENGE` circuit state guarded by a separate short-lived file lock. The first challenge opens the circuit. Later workflows check it before and after acquiring the workflow lock and fail fast without provider traffic. Cooldown expiry or an explicit successful `browser unlock --probe` permits one serialized half-open workflow; another challenge reopens the circuit. Corrupt state is treated as closed rather than permanently blocking the CLI.

Ordinary provider workflows default to sequential origin resolution and sequential origin/date fan-out inside the process as well. Explicit library callers may still request bounded internal concurrency, but CLI defaults do not. All paths share an in-memory stop signal, Autosuggest cache, deduplication and one HTTP client. A challenge sets the stop signal immediately, and fan-out owners cancel and retrieve pending tasks before returning.

Each coordinated workflow has an explicit request budget. The counter advances immediately before a provider request starts, including bounded retry attempts, and is reported as `limit`, `started` and `remaining`. Exhaustion is a local failure and cannot start another request. This counter is authoritative only inside the coordinated workflow and is omitted where that guarantee is unavailable.

`BOT_CHALLENGE` diagnostics are structural rather than message-based. A provider response records `source: provider_response`, `network_attempted: true`, a safe provider phase and a non-sensitive challenge class before returning the newly opened circuit snapshot. A circuit fail-fast records `source: local_circuit`, `network_attempted: false` and `local_gate`. Neither form records URLs, headers, cookies, tokens or response bodies.

The offline `circuit status` command only reads local non-secret state. Missing or corrupt state is treated as closed; an open state whose cooldown elapsed is reported as stale and eligible for one controlled retry rather than silently reset. There is no unconditional reset command.

`browser unlock --probe` acquires the same cross-process workflow lock for the browser-open/probe sequence and observes one deadline. It bypasses the open-circuit gate only for that controlled probe. Success permits half-open; it does not transfer browser profile state or establish Radar readiness.

This mechanism reduces accidental provider traffic; it does not bypass anti-bot controls, transfer browser state, or establish that Radar search is available.
