from __future__ import annotations

import json
from pathlib import Path

import pytest

from shuttlelib.sessions import Registry


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "shuttle-home")


@pytest.fixture
def launch(registry: Registry) -> dict:
    return registry.create_launch(
        launch_id="launch-1",
        provider="codex",
        provider_version="0.144.1",
        mode="go",
        tmux_session="shuttle-registry",
        pane_id="%7",
        pid=12345,
        cwd="/home/patrick/projects/shuttle",
        brief="brief-20260114-u6ce",
        title="Session registry",
    )


@pytest.fixture
def codex_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "codex-0.144.1"


@pytest.fixture
def codex_payload(codex_fixture_dir: Path):
    def load(name: str) -> tuple[bytes, dict]:
        raw = (codex_fixture_dir / f"{name}.json").read_bytes()
        return raw, json.loads(raw)

    return load
