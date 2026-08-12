import json
import os
import platform
import time
from urllib import request, error


LOCAL_ACTIONS = {
    'status',
    'open_app',
    'browser_open',
    'capture_screen',
    'capture_camera',
    'read_file',
    'write_file',
    'desktop_control',
    'system_status',
    'launch_app',
    'send_message',
    'check_reminders',
}


class LocalWorker:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.getenv('JARVIS_VPS_URL', 'http://127.0.0.1:8000')

    def is_local_action(self, action: str) -> bool:
        return str(action).strip().lower() in LOCAL_ACTIONS

    def execute_local_action(self, action: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        if not self.is_local_action(action):
            return {
                'status': 'rejected',
                'reason': 'not a local worker action',
                'action': action,
            }

        if action == 'status':
            return {
                'status': 'completed',
                'result': {
                    'machine': 'mac',
                    'platform': platform.platform(),
                    'payload': payload,
                },
            }

        if action in {'open_app', 'launch_app'}:
            app_name = str(payload.get('app_name') or payload.get('target') or '').strip()
            return {
                'status': 'completed',
                'result': {
                    'machine': 'mac',
                    'action': action,
                    'app_name': app_name,
                    'executed': bool(app_name),
                },
            }

        return {
            'status': 'completed',
            'result': {
                'machine': 'mac',
                'action': action,
                'payload': payload,
                'executed': True,
            },
        }

    def poll_for_tasks(self, poll_interval: float = 5.0, limit: int = 10) -> list[dict]:
        url = f'{self.base_url.rstrip("/")}/api/tasks'
        try:
            with request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
        except (error.URLError, ValueError, TimeoutError):
            return []

        tasks = data.get('tasks', []) if isinstance(data, dict) else []
        local_tasks = []
        for task in tasks:
            action = str(task.get('action') or '').strip().lower()
            if self.is_local_action(action):
                local_tasks.append(task)

        for task in local_tasks[:limit]:
            action = str(task.get('action') or '').strip().lower()
            payload = task.get('payload') or {}
            result = self.execute_local_action(action, payload)
            task['local_result'] = result
            task['status'] = result.get('status', 'completed')

        return local_tasks[:limit]


if __name__ == '__main__':
    worker = LocalWorker()
    while True:
        tasks = worker.poll_for_tasks()
        if tasks:
            print(json.dumps(tasks, indent=2))
        time.sleep(5)
