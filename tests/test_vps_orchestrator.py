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
