import json

from vps_orchestrator import create_app


def test_vps_orchestrator_health_and_task_queue():
    app = create_app()
    client = app.test_client()

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
    assert b'JARVIS VPS Dashboard' in dashboard.data

    remote_chat = client.post('/api/remote_chat', json={'text': 'hello from remote'})
    assert remote_chat.status_code == 202
    remote_payload = remote_chat.get_json()
    assert remote_payload['accepted'] is True
    assert remote_payload['text'] == 'hello from remote'
