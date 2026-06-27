"""Smoke test for the CLI entry and app construction."""
from starlette.applications import Starlette

from review_mate.__main__ import main
from review_mate.server.app import create_app


def test_main_is_callable():
    assert callable(main)


def test_create_app_builds_a_starlette_app(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_MATE_HOME", str(tmp_path))
    app = create_app()
    assert isinstance(app, Starlette)
