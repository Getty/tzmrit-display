"""Shared fixtures."""

import pytest

from tzmrit_display import claude_limits


@pytest.fixture(autouse=True)
def isolated_limits_store(tmp_path, monkeypatch):
    """Keep the stored last usage reading out of the user's state directory:
    a test must neither show the real one nor overwrite it."""
    monkeypatch.setattr(claude_limits, "_store_path",
                        lambda: tmp_path / "limits-store.json")
