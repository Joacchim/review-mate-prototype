"""The fixture must not drift from the application it stands in for.

Conformance of the *protocol* needs no test: the fixture server is the production `create_app`, so
every frame is built by the real scope builders. What can drift is the manager beneath it — a
method renamed on one side and not the other — and that would show up as a broken page rather than
a failing test. These are browser-free by design, so they run once, not once per browser.
"""
from review_mate.session.manager import SessionManager
from webui.fixtures.manager import MANAGER_SURFACE, FakeManager


def test_the_fake_carries_the_whole_surface():
    missing = [name for name in MANAGER_SURFACE if not hasattr(FakeManager(), name)]
    assert missing == [], f"FakeManager is missing {missing}"


def test_the_real_manager_still_has_that_surface():
    """The other half: a rename in SessionManager must break here, not in a page."""
    missing = [name for name in MANAGER_SURFACE
               if not hasattr(SessionManager, name) and not hasattr(SessionManager(), name)]
    assert missing == [], f"SessionManager no longer has {missing} — the fixture describes a shape it does not have"


def test_the_fixture_app_is_the_production_app(staged_app):
    """Not a copy of it: the scope families come from create_app, so they cannot be a subset."""
    bus = staged_app.state.bus
    assert bus.scopes == {"hub"}
    assert bus.families == {"diff", "blob", "rail", "chat"}
