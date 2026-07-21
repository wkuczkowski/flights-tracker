# Serialize provider workflows and share challenge state

Provider-facing CLI workflows are serialized across processes with an advisory `flock` stored in the user's runtime/state directory. The kernel releases the lock when a process exits, including abnormal exits, and lock acquisition is bounded by the workflow deadline. Request validation and other local-only work happens before lock acquisition.

The same directory stores a small, non-secret `BOT_CHALLENGE` circuit state guarded by a separate short-lived file lock. The first challenge opens the circuit. Later workflows check it before and after acquiring the workflow lock and fail fast without provider traffic. Cooldown expiry or an explicit successful `browser unlock --probe` permits one serialized half-open workflow; another challenge reopens the circuit. Corrupt state is treated as closed rather than permanently blocking the CLI.

One workflow may still use bounded internal concurrency. Its tasks share an in-memory stop signal, Autosuggest cache, and HTTP client. A challenge sets the stop signal immediately, and fan-out owners cancel and retrieve pending tasks before returning.

This mechanism reduces accidental provider traffic; it does not bypass anti-bot controls, transfer browser state, or establish that Radar search is available.
