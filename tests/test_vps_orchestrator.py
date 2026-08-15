import asyncio
import json
import os
import sys
import types
from pathlib import Path

import vps_orchestrator as vps_module
from vps_orchestrator import VPSRuntimeBridge, create_app


def test_vps_runtime_bridge_keeps_input_and_output_in_one_process():
    bridge = VPSRuntimeBridge(command_limit=1, audio_limit=1, client_limit=2)
    client_id, outbound = bridge.register_client()

    assert bridge.enqueue_command('hello from browser') is True
    assert bridge.get_command(0.01) == 'hello from browser'
    assert bridge.enqueue_audio({'data': b'\x00\x01', 'mime_type': 'audio/pcm'}) is True
    assert bridge.get_audio(0.01)['data'] == b'\x00\x01'

    bridge.publish_event({'type': 'log', 'speaker': 'jarvis', 'text': 'Online.'})
    bridge.publish_audio(b'\x02\x03')

    assert outbound.get_nowait() == ('json', {'type': 'log', 'speaker': 'jarvis', 'text': 'Online.'})
    assert outbound.get_nowait() == ('bytes', b'\x02\x03')
    bridge.unregister_client(client_id)
    assert bridge.has_clients() is False


def test_vps_starts_one_embedded_brain_with_public_dashboard(monkeypatch):
    monkeypatch.setenv('JARVIS_RUN_VPS_BRAIN', '1')
    orchestrator = vps_module.VPSOrchestrator()
    created = []

    class FakeUI:
        pass

    class FakeBrain:
        def __init__(self, ui, dashboard, remote_bridge):
            created.append((ui, dashboard, remote_bridge))

        async def run(self):
            return None

    class ImmediateThread:
        def __init__(self, target=None, **_kwargs):
            self._target = target
            self._alive = False

        def start(self):
            self._alive = True
            self._target()

        def is_alive(self):
            return self._alive

    monkeypatch.setitem(sys.modules, 'main', types.SimpleNamespace(
        JarvisLive=FakeBrain,
        _HeadlessUI=FakeUI,
    ))
    monkeypatch.setattr(vps_module.threading, 'Thread', ImmediateThread)

    assert orchestrator.ensure_brain_started() is True
    assert orchestrator.ensure_brain_started() is True
    assert len(created) == 1
    _, dashboard, bridge = created[0]
    assert dashboard is orchestrator.dashboard_server
    assert bridge is orchestrator.runtime_bridge


def test_login_success_keeps_session_tokens_for_dashboard_redirect():
    html = (Path(__file__).resolve().parents[1] / 'dashboard' / 'static' / 'login.html').read_text(encoding='utf-8')
    success_block = "if (data.ok && data.token) {"
    assert success_block in html
    assert "sessionStorage.setItem('jarvis_key', key);" in html
    assert "sessionStorage.setItem('jarvis_token', data.token);" in html
    assert "_clearStaleSession();\n        sessionStorage.setItem('jarvis_key', key);" not in html


def test_vps_runtime_decls_require_websocket_stack():
    req_path = Path(__file__).resolve().parents[1] / 'requirements.txt'
    text = req_path.read_text(encoding='utf-8').lower()
    for package in ('flask-sock', 'gunicorn', 'gevent'):
        assert package in text
    assert 'gevent-websocket' not in text


def test_vps_systemd_service_runs_the_embedded_brain_with_gevent():
    service = (Path(__file__).resolve().parents[1] / 'deploy' / 'jarvis-vps.service').read_text(encoding='utf-8')
    assert 'WorkingDirectory=/root/jarvis-vps-runtime' in service
    assert 'Environment=JARVIS_HEADLESS=1' in service
    assert 'Environment=JARVIS_RUN_VPS_BRAIN=1' in service
    assert 'Environment=JARVIS_DATA_DIR=/var/lib/jarvis' in service
    assert 'StateDirectory=jarvis' in service
    assert 'ExecStart=/root/jarvis-vps-runtime/.venv/bin/gunicorn' in service
    assert '--workers 1 --worker-class gevent vps_orchestrator:app' in service
    assert 'geventwebsocket' not in service


def test_mutable_vps_memory_supports_external_data_directory():
    root = Path(__file__).resolve().parents[1]
    ingestion = (root / 'memory' / 'document_ingestion.py').read_text(encoding='utf-8')
    manager = (root / 'memory' / 'memory_manager.py').read_text(encoding='utf-8')

    assert 'os.getenv("JARVIS_DATA_DIR")' in ingestion
    assert 'os.getenv("JARVIS_DATA_DIR")' in manager


def test_vps_exposes_dashboard_websocket_route():
    app = create_app()
    routes = sorted(str(rule) for rule in app.url_map.iter_rules())
    assert '/ws' in routes


def test_vps_public_websocket_route_owns_outbound_delivery():
    source = (Path(__file__).resolve().parents[1] / 'vps_orchestrator.py').read_text(encoding='utf-8')
    assert 'payload = ws.receive(timeout=0.05)' in source
    assert 'kind, outbound_payload = outbound.get_nowait()' in source
    assert '_start_outbound_sender' not in source


def test_vps_public_websocket_replays_current_brain_status():
    source = (Path(__file__).resolve().parents[1] / 'vps_orchestrator.py').read_text(encoding='utf-8')
    assert 'event.get("type") == "status"' in source
    assert '{"type": "status", "state": "active"}' in source


def test_public_dashboard_websocket_accepts_assistant_audio_frames():
    html = (Path(__file__).resolve().parents[1] / 'dashboard' / 'static' / 'app.html').read_text(encoding='utf-8')
    assert "ws.binaryType = 'arraybuffer';" in html
    assert '_playRemoteAudioChunk(e.data);' in html
    assert 'if (!_audioCtx || !(_audioCtx instanceof Ctx)) {' in html
    assert 'instanceof webkitAudioContext' not in html
    assert 'createBuffer(1, bytes.length, 24000)' in html


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
    assert app.orchestrator.runtime_bridge.get_command(0.01) == 'hello from browser'

    wake = client.post('/api/wake', headers={'Authorization': f'Bearer {token}'})
    assert wake.status_code == 200, wake.get_data(as_text=True)
    assert wake.get_json()['ok'] is True


def test_vps_phone_audio_websocket_forwards_pcm_to_dashboard_queue():
    app = create_app()
    queue = app.orchestrator.runtime_bridge.audio_queue
    while not queue.empty():
        queue.get_nowait()

    from vps_orchestrator import enqueue_phone_audio_payload
    payload = b'\x00\x01\x02\x03'
    ok = enqueue_phone_audio_payload(
        app.orchestrator.dashboard_server,
        payload,
        queue=queue,
    )

    assert ok is True
    assert queue.qsize() == 1
    item = queue.get_nowait()
    assert item['mime_type'] == 'audio/pcm'
    assert item['data'] == payload


def test_vps_dashboard_output_is_forwarded_to_public_runtime_bridge():
    app = create_app()
    client_id, outbound = app.orchestrator.runtime_bridge.register_client()

    asyncio.run(app.orchestrator.dashboard_server.broadcast({
        'type': 'log', 'speaker': 'jarvis', 'text': 'Bridge confirmed.',
    }))
    asyncio.run(app.orchestrator.dashboard_server.send_audio_to_clients(b'\x04\x05'))

    assert outbound.get_nowait() == (
        'json', {'type': 'log', 'speaker': 'jarvis', 'text': 'Bridge confirmed.'},
    )
    assert outbound.get_nowait() == ('bytes', b'\x04\x05')
    app.orchestrator.runtime_bridge.unregister_client(client_id)


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
