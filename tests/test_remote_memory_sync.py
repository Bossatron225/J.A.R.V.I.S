import json
from unittest.mock import patch

from memory.remote_sync import fetch_remote_memory, merge_memory, push_memory_to_vps


def test_merge_memory_prefers_remote_long_term_data():
    local = {
        'identity': {'language': {'value': 'English', 'updated': '2024-01-01'}},
        'projects': {'project_x': {'value': 'old', 'updated': '2024-01-01'}},
    }
    remote = {
        'identity': {'language': {'value': 'Turkish', 'updated': '2025-01-01'}},
        'projects': {'project_x': {'value': 'new', 'updated': '2025-01-01'}},
        'notes': {'vps_fact': {'value': 'stored on server', 'updated': '2025-01-01'}},
    }

    merged = merge_memory(local, remote)
    assert merged['identity']['language']['value'] == 'Turkish'
    assert merged['projects']['project_x']['value'] == 'new'
    assert merged['notes']['vps_fact']['value'] == 'stored on server'


@patch('memory.remote_sync.request.urlopen')
def test_push_and_fetch_memory_round_trip(mock_urlopen):
    class DummyResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._payload

    mock_urlopen.side_effect = [
        DummyResponse({'memory': {'notes': {'server_sync': {'value': 'ok'}}}}),
        DummyResponse({'status': 'synced'}),
    ]

    fetched = fetch_remote_memory('https://example.test')
    assert fetched['notes']['server_sync']['value'] == 'ok'

    pushed = push_memory_to_vps('https://example.test', {'notes': {'server_sync': {'value': 'ok'}}})
    assert pushed['status'] == 'synced'
