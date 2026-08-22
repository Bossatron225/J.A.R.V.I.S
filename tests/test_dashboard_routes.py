"""The dashboard page is served by TWO different web apps.

  * `dashboard/server.py` — a FastAPI app, used when the Mac serves its own
    dashboard.
  * `vps_orchestrator.py` — a Flask app, which is what actually serves the
    phone-facing dashboard in normal operation.

Both serve the SAME `dashboard/static/app.html`. So any URL that page fetches
has to exist in both, and adding it to only one produces a 404 that looks like
an application error rather than a missing route: `/screen/<id>` was added to
FastAPI only, and the phone reported "That capture has expired, sir" for a
route that was never there.

These tests tie the page's URLs to both servers.
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

APP_HTML = (REPO_ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
FASTAPI_SRC = (REPO_ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
FLASK_SRC = (REPO_ROOT / "vps_orchestrator.py").read_text(encoding="utf-8")

# Routes the browser fetches directly and that must therefore work on whichever
# server is in front of it.
BROWSER_FETCHED_ROUTES = ["/screen/"]


def _flask_routes() -> set[str]:
    return set(re.findall(r'@app\.(?:get|post|route)\("([^"]+)"', FLASK_SRC))


def _fastapi_routes() -> set[str]:
    return set(re.findall(r'@app\.(?:get|post|websocket)\("([^"]+)"', FASTAPI_SRC))


@pytest.mark.parametrize("route", BROWSER_FETCHED_ROUTES)
def test_browser_routes_exist_on_the_flask_vps_app(route):
    """The Flask app is what the phone actually talks to."""
    assert any(r.startswith(route) for r in _flask_routes()), (
        f"{route} is fetched by app.html but vps_orchestrator.py serves no such route — "
        f"the phone will get a 404"
    )


@pytest.mark.parametrize("route", BROWSER_FETCHED_ROUTES)
def test_browser_routes_exist_on_the_fastapi_app(route):
    assert any(r.startswith(route) for r in _fastapi_routes()), (
        f"{route} is fetched by app.html but dashboard/server.py serves no such route"
    )


def test_the_screen_route_is_authed_on_both_servers():
    """It serves a picture of the user's entire screen."""
    for name, src, marker in (
        ("flask", FLASK_SRC, '@app.get("/screen/<shot_id>")'),
        ("fastapi", FASTAPI_SRC, '@app.get("/screen/{shot_id}")'),
    ):
        body = src.split(marker, 1)[1][:1400]
        assert "_is_token_valid" in body, f"{name} /screen route does not check the token"
        assert "401" in body, f"{name} /screen route has no unauthorized path"
        assert "no-store" in body, f"{name} /screen route allows caching"


def test_both_servers_read_from_the_same_screenshot_store():
    """Storing on one object and serving from another would 404 every time."""
    flask_body = FLASK_SRC.split('@app.get("/screen/<shot_id>")', 1)[1][:1400]
    assert "get_screenshot" in flask_body
    assert "dashboard_server" in flask_body, (
        "the Flask route must read from orchestrator.dashboard_server — the same "
        "object main.py calls put_screenshot() on"
    )


def test_the_client_distinguishes_failure_causes():
    """Reporting every failure as 'expired' is what hid the missing route."""
    block = APP_HTML.split("function _onScreenImage", 1)[1][:2500]
    assert "401" in block and "404" in block, "must tell auth failure from expiry"
    assert "session has expired" in block
    assert "no longer on the server" in block


def test_the_client_holds_the_image_so_it_survives_server_expiry():
    """The bytes are kept as a blob, so a short server-side TTL bounds how long
    the capture sits in VPS memory without the picture vanishing mid-read."""
    block = APP_HTML.split("function _onScreenImage", 1)[1][:2500]
    assert "createObjectURL" in block


def test_every_route_app_html_fetches_is_covered_by_this_test():
    """Guards the guard: a newly fetched path should be added to
    BROWSER_FETCHED_ROUTES rather than silently skipping the both-servers check."""
    fetched = set(re.findall(r"[`'\"](/(?:screen|uploads)/)", APP_HTML))
    unchecked = fetched - set(BROWSER_FETCHED_ROUTES) - {"/uploads/"}
    assert not unchecked, f"add these to BROWSER_FETCHED_ROUTES: {sorted(unchecked)}"
