"""Shared pytest fixtures.

Isolates cuttlefish's on-disk caches from the developer's real ones, so test
runs don't pollute (or get polluted by) ~/.cache/cuttlefish.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg_dirs(tmp_path_factory, monkeypatch):
    """Point XDG_CACHE_HOME and XDG_DATA_HOME at unique tmp dirs per test."""
    cache = tmp_path_factory.mktemp("xdg-cache")
    data = tmp_path_factory.mktemp("xdg-data")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
