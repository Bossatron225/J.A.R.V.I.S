import json
import hashlib
import importlib
import os
import platform
import time
from pathlib import Path
from urllib import request, error


LOCAL_ACTIONS = {
    'status',
    'open_app',
    'browser_control',
    'capture_screen',
    'capture_camera',
    'capture_visual',
    'desktop_control',
    'system_status',
    'computer_control',
    'computer_settings',
    'file_controller',
    'find_my',
    'imessage_control',
    'mail_control',
    'reminder',
    'security_biometrics',
    'send_message',
    'youtube_video',
}

# Actions that just fetch data or a frame — never worth a visible "wake" on the Mac.
QUIET_ACTIONS = {
    'status',
    'capture_camera',
    'capture_visual',
    'capture_screen',
    'system_status',
    'find_my',
}

ACTION_HANDLERS = {
    'open_app': ('actions.open_app', 'open_app'),
    'browser_control': ('actions.browser_control', 'browser_control'),
    'computer_control': ('actions.computer_control', 'computer_control'),
    'computer_settings': ('actions.computer_settings', 'computer_settings'),
    'desktop_control': ('actions.desktop', 'desktop_control'),
    'file_controller': ('actions.file_controller', 'file_controller'),
    'find_my': ('actions.find_my', 'find_my'),
    'imessage_control': ('actions.imessage_integration', 'imessage_control'),
    'mail_control': ('actions.mail_integration', 'mail_control'),
    'reminder': ('actions.reminder', 'reminder'),
    'send_message': ('actions.send_message', 'send_message'),
    'system_status': ('actions.system_monitor', 'get_system_status'),
    'youtube_video': ('actions.youtube_video', 'youtube_video'),
    'capture_camera': ('actions.screen_processor', 'capture_camera_b64'),
    'capture_visual': ('actions.screen_processor', 'capture_targeted_visual_b64'),
}


class LocalWorker:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.getenv('JARVIS_VPS_URL', 'http://127.0.0.1:8000')

    def is_local_action(self, action: str) -> bool:
        return str(action).strip().lower() in LOCAL_ACTIONS

    @staticmethod
    def _worker_token() -> str:
        explicit = str(os.getenv('JARVIS_WORKER_TOKEN') or '').strip()
        if explicit:
            return explicit
        try:
            config_path = Path(__file__).resolve().parent / 'config' / 'api_keys.json'
            config = json.loads(config_path.read_text(encoding='utf-8'))
            explicit = str(config.get('local_worker_token') or '').strip()
            if explicit:
                return explicit
            seed = str(config.get('gemini_api_key') or '').strip()
        except Exception:
            seed = ''
        return hashlib.sha256(f'jarvis-local-worker-v1:{seed}'.encode('utf-8')).hexdigest() if seed else ''

    @staticmethod
    def _load_handler(action: str):
        target = ACTION_HANDLERS.get(action)
        if not target:
            return None
        module_name, attribute_name = target
        return getattr(importlib.import_module(module_name), attribute_name)

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

        handler = self._load_handler(action)
        if not callable(handler):
            return {'status': 'unavailable', 'action': action, 'reason': 'handler unavailable on Mac'}
        try:
            if action == 'system_status':
                value = handler()
            else:
                value = handler(parameters=payload, response=None, player=None, session_memory=None)
            return {'status': 'completed', 'machine': 'mac', 'action': action, 'result': value}
        except TypeError:
            try:
                value = handler(parameters=payload, player=None)
                return {'status': 'completed', 'machine': 'mac', 'action': action, 'result': value}
            except Exception as exc:
                return {'status': 'error', 'machine': 'mac', 'action': action, 'error': str(exc)}
        except Exception as exc:
            return {'status': 'error', 'machine': 'mac', 'action': action, 'error': str(exc)}

    def poll_for_tasks(self, poll_interval: float = 5.0, limit: int = 10) -> list[dict]:
        token = self._worker_token()
        if not token:
            return []
        url = f'{self.base_url.rstrip("/")}/api/local-worker/tasks?limit={max(1, min(limit, 20))}'
        try:
            req = request.Request(url, headers={'X-Jarvis-Worker-Token': token})
            with request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
        except (error.URLError, ValueError, TimeoutError):
            return []

        tasks = data.get('tasks', []) if isinstance(data, dict) else []
        completed = []
        for task in tasks[:limit]:
            action = str(task.get('action') or '').strip().lower()
            payload = task.get('payload') or {}
            result = self.execute_local_action(action, payload)
            result_url = f'{self.base_url.rstrip("/")}/api/local-worker/results/{task.get("id")}'
            body = json.dumps(result).encode('utf-8')
            try:
                result_req = request.Request(
                    result_url,
                    data=body,
                    method='POST',
                    headers={
                        'Content-Type': 'application/json',
                        'X-Jarvis-Worker-Token': token,
                    },
                )
                with request.urlopen(result_req, timeout=10):
                    pass
                completed.append({**task, 'local_result': result})
            except (error.URLError, TimeoutError):
                continue

        return completed


if __name__ == '__main__':
    worker = LocalWorker()
    while True:
        tasks = worker.poll_for_tasks()
        if tasks:
            print(json.dumps(tasks, indent=2))
        time.sleep(5)
