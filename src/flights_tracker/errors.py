from __future__ import annotations


class FlightsError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = False if code == "BOT_CHALLENGE" else retryable
        self.details = details or {}

    @property
    def exit_code(self) -> int:
        if self.code in {"INVALID_ARGUMENT", "AMBIGUOUS_PLACE"}:
            return 2
        if self.code == "BOT_CHALLENGE":
            return 3
        if self.code in {"RATE_LIMITED", "PROVIDER_TIMEOUT", "REQUEST_BUDGET_EXCEEDED"}:
            return 5
        if self.code in {"INTERNAL_ERROR", "CONTRACT_CHANGED"}:
            return 6
        return 4


class ProviderError(FlightsError):
    pass
