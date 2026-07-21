from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_provider_coordination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("FLIGHTS_TRACKER_STATE_DIR", str(tmp_path / "provider-state"))
    monkeypatch.setenv("FLIGHTS_TRACKER_BOT_COOLDOWN_SECONDS", "60")
