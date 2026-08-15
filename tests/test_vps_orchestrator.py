import json
import os
from pathlib import Path

from vps_orchestrator import create_app


def test_vps_runtime_decls_require_websocket_stack():
    req_path = Path(__file__).resolve().parents[1] / 'requirements.txt'
    text = req_path.read_text(encoding='utf-8').lower()
    for package in ('flask-sock', 'gunicorn', 'gevent-websocket', 'gevent'):
        assert package in text


def test_vps_exposes_dashboard_websocket_route():
    app = create_app()
    routes = sorted(str(rule) for rule in app.url_map.iter_rules())
    assert '/ws' in routes


def test_vps_serves_dashboard_static_assets():
    app = create_app()
    client = app.test_client()

    resp = client.get('/static/crypto.js')

    assert resp.status_code == 200
    assert 'CryptoJS' in resp.get_data(as_text=True)


def test_vps_remote_access_prefers_public_url_over_local_ip(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_URL", "https://public.example.com")
    monkeypatch.setenv("PUBLIC_ENTRY_URL", "https://public.example.com")
    app = create_app()
    client = app.test_client()

    payload = client.get('/api/remote_access').get_json()

    assert payload['ok'] is True
    assert payload['url'] == 'https://public.example.com'
    assert payload['auto_login_url'].startswith('https://public.example.com/auto-login?key=')


def test_vps_remote_access_security_reports_public_on_for_live_url(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_URL", "http://161.35.38.152:8000")
    app = create_app()
    client = app.test_client()

    payload = client.get('/api/remote_access').get_json()

    assert payload['ok'] is True
    assert 'PUBLIC=ON' in payload['security']
    assert 'PIN=OFF' in payload['security']


def test_vps_auto_login_route_serves_page_for_live_key(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_URL", "http://161.35.38.152:8000")
    app = create_app()
    client = app.test_client()

    response = client.get('/auto-login?key=TESTKEY')

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'JARVIS' in text
    assert 'TESTKEY' in text


def test_vps_public_dashboard_command_routes_accept_auth_and_queue_text():
    app = create_app()
    client = app.test_client()

    key = next(iter(app.orchestrator.dashboard_server._pending_keys.keys())) if app.orchestrator.dashboard_server and app.orchestrator.dashboard_server._pending_keys else None
    if key is None:
        key = app.orchestrator.dashboard_server.new_key()

    login = client.post('/login', json={'key': key})
    assert login.status_code == 200, login.get_data(as_text=True)
    token = login.get_json()['token']

    cmd = client.post('/api/command', headers={'Authorization': f'Bearer {token}'}, json={'text': 'hello from browser'})
    assert cmd.status_code == 200, cmd.get_data(as_text=True)
    assert cmd.get_json()['ok'] is True
    assert cmd.get_json()['text'] == 'hello from browser'

    wake = client.post('/api/wake', headers={'Authorization': f'Bearer {token}'})
    assert wake.status_code == 200, wake.get_data(as_text=True)
    assert wake.get_json()['ok'] is True


def test_vps_phone_audio_websocket_forwards_pcm_to_dashboard_queue():
    app = create_app()
    token = app.orchestrator.dashboard_server._issue_token('session-key')
    queue = app.orchestrator.dashboard_server._phone_audio_queue
    while not queue.empty():
        queue.get_nowait()

    route = app.view_functions['phone_audio_route']
    class DummyWS:
        def __init__(self):
            self.payloads = [b'\x00\x01\x02\x03']
        def receive(self):
            if self.payloads:
                return self.payloads.pop(0)
            return None
        def close(self, code=1000):
            return None

    with app.test_request_context('/ws/phone-audio?token=' + token):
        route(DummyWS())

    assert queue.qsize() == 1
    payload = queue.get_nowait()
    assert payload['mime_type'] == 'audio/pcm'
    assert payload['data'] == b'\x00\x01\x02\x03'


def test_vps_orchestrator_health_and_task_queue():
    app = create_app()
    client = app.test_client()

    root = client.get('/')
    assert root.status_code == 200
    assert 'text/html' in root.content_type
    assert 'JARVIS' in root.get_data(as_text=True)

    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert 'text/html' in dashboard.content_type
    assert 'JARVIS' in dashboard.get_data(as_text=True)

    health = client.get('/health')
    assert health.status_code == 200
    payload = health.get_json()
    assert payload['ok'] is True
    assert payload['service'] == 'jarvis-vps-orchestrator'

    task = client.post(
        '/api/tasks',
        json={
            'action': 'sync_memory',
            'payload': {'source': 'mac-local'},
            'source': 'local-worker',
        },
    )
    assert task.status_code == 201
    task_payload = task.get_json()
    assert task_payload['status'] == 'queued'
    assert task_payload['task']['action'] == 'sync_memory'

    tasks = client.get('/api/tasks')
    assert tasks.status_code == 200
    tasks_payload = tasks.get_json()
    assert any(item['action'] == 'sync_memory' for item in tasks_payload['tasks'])

    status = client.get('/api/status')
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload['mode'] == 'vps'
    assert status_payload['queue_size'] >= 1

    reboot = client.post('/api/reboot')
    assert reboot.status_code == 200
    reboot_payload = reboot.get_json()
    assert reboot_payload['status'] in {'restarting', 'accepted'}
    assert reboot_payload['service'] == 'jarvis-vps-orchestrator'

    ops = client.get('/api/ops')
    assert ops.status_code == 200
    ops_payload = ops.get_json()
    assert ops_payload['service'] == 'jarvis-vps-orchestrator'
    assert ops_payload['status'] in {'online', 'restarting'}
    assert 'uptime_seconds' in ops_payload
    assert 'queue_size' in ops_payload
    assert 'queue' in ops_payload
    assert 'public_entry' in ops_payload

    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert b'JARVIS Dashboard' in dashboard.data or b'JARVIS' in dashboard.data

    remote_chat = client.post('/api/remote_chat', json={'text': 'hello from remote'})
    assert remote_chat.status_code == 202
    remote_payload = remote_chat.get_json()
    assert remote_payload['accepted'] is True
    assert remote_payload['text'] == 'hello from remote'

    remote_access = client.get('/api/remote_access')
    assert remote_access.status_code == 200
    access_payload = remote_access.get_json()
    assert access_payload['ok'] is True
    assert 'url' in access_payload
    assert 'key' in access_payload
    assert 'auto_login_url' in access_payload
