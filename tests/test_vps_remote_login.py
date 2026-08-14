from dashboard.server import DashboardServer
from vps_orchestrator import create_app


def test_vps_login_post_accepts_one_time_key():
    app = create_app()
    dashboard = DashboardServer()
    app.orchestrator.dashboard_server = dashboard
    key = dashboard.new_key()

    client = app.test_client()
    response = client.post('/login', json={'key': key})

    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload['ok'] is True
    assert 'token' in payload


def test_vps_request_key_returns_live_url_and_key():
    app = create_app()
    dashboard = DashboardServer()
    app.orchestrator.dashboard_server = dashboard
    app.orchestrator.public_entry = 'https://jarvis.jarvisyourdomain.com'

    client = app.test_client()
    response = client.post('/api/request-key')

    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload['ok'] is True
    assert len(payload['key']) == 6
    assert payload['url'].startswith('https://')
    assert 'auto_login_url' in payload
