import json
import os
import queue as thread_queue
import threading
import time
import uuid
import base64
import hashlib
import secrets
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, Response, redirect, send_file

try:
    from flask_sock import Sock
except Exception:  # pragma: no cover
    Sock = None

def _build_root_page() -> str:
    public_entry = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "https://jarvis.jarvisyourdomain.com"
    return f"""
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>JARVIS VPS</title>
    <style>
      body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #07111f;
        color: #e8f3ff;
        display: grid;
        place-items: center;
        min-height: 100vh;
        margin: 0;
      }}
      .card {{
        max-width: 760px;
        width: min(90vw, 760px);
        background: rgba(17, 31, 49, 0.92);
        border: 1px solid rgba(147, 197, 253, 0.35);
        border-radius: 16px;
        padding: 2rem 2.25rem;
        box-shadow: 0 24px 60px rgba(0,0,0,0.35);
      }}
      h1 {{
        margin: 0 0 0.5rem;
        font-size: clamp(2rem, 5vw, 3rem);
        letter-spacing: 0.08em;
      }}
      p {{
        margin: 0.5rem 0;
        line-height: 1.55;
        color: #d5e6ff;
      }}
      code {{
        background: rgba(148, 163, 184, 0.12);
        border: 1px solid rgba(148, 163, 184, 0.2);
        border-radius: 6px;
        padding: 0.15rem 0.45rem;
      }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>JARVIS VPS</h1>
      <p>System online. Remote brain active.</p>
      <p>Public endpoint: <code>{public_entry}</code></p>
      <p>Health: <code>/api/health</code></p>
      <p>Operations: <code>/api/ops</code></p>
      <p>Dashboard: <code>/dashboard</code></p>
    </div>
  </body>
</html>
"""

try:
    from dashboard.server import DashboardServer
except Exception:  # pragma: no cover
    DashboardServer = None

try:
    from twilio.rest import Client
    from twilio.twiml.voice_response import VoiceResponse
except Exception:  # pragma: no cover
    Client = None
    VoiceResponse = None

from memory.document_ingestion import index_codebase, ingest_document, search_document_index
from memory.memory_manager import load_memory, save_memory, update_memory


BASE_DIR = Path(__file__).resolve().parent


def handle_dashboard_ws_message(payload, *, dashboard=None, token: str = "", queue=None, wake_callback=None):
    if not isinstance(payload, dict):
        return None
    message_type = str(payload.get("type") or "").strip().lower()

    if message_type == "ping":
        return {"type": "pong"}

    if message_type == "pong":
        return {"type": "pong"}

    if message_type == "command":
        text = str(payload.get("text") or "").strip()
        enc = str(payload.get("enc") or "")
        if enc and dashboard is not None and hasattr(dashboard, "_decrypt"):
            text = dashboard._decrypt(token, enc) or text
        if text:
            if queue is not None:
                try:
                    queue.put_nowait(text)
                except Exception:
                    pass
            if callable(wake_callback):
                try:
                    wake_callback()
                except Exception:
                    pass
        return {"type": "ack", "ok": True}

    return None


def enqueue_phone_audio_payload(dashboard, data, *, queue=None) -> bool:
    if dashboard is None or not isinstance(data, (bytes, bytearray)) or not data:
        return False
    target_queue = queue if queue is not None else getattr(dashboard, "_phone_audio_queue", None)
    if target_queue is None:
        return False
    payload = {"data": bytes(data), "mime_type": "audio/pcm"}
    try:
        target_queue.put_nowait(payload)
        return True
    except Exception:
        try:
            target_queue.get_nowait()
        except Exception:
            pass
        try:
            target_queue.put_nowait(payload)
            return True
        except Exception:
            return False


class VPSRuntimeBridge:
    """Thread-safe handoff between the public web worker and the live brain."""

    def __init__(self, *, command_limit: int = 128, audio_limit: int = 96, client_limit: int = 192):
        self.command_queue: thread_queue.Queue[str] = thread_queue.Queue(maxsize=command_limit)
        self.audio_queue: thread_queue.Queue[dict] = thread_queue.Queue(maxsize=audio_limit)
        self._client_limit = client_limit
        self._clients: dict[str, thread_queue.Queue[tuple[str, object]]] = {}
        self._clients_lock = threading.Lock()
        self._local_tasks: dict[str, dict] = {}
        self._local_tasks_lock = threading.Lock()
        self._local_worker_last_seen = 0.0

    @staticmethod
    def _offer(target: thread_queue.Queue, value: object) -> bool:
        try:
            target.put_nowait(value)
            return True
        except thread_queue.Full:
            try:
                target.get_nowait()
            except thread_queue.Empty:
                return False
            try:
                target.put_nowait(value)
                return True
            except thread_queue.Full:
                return False

    def enqueue_command(self, command) -> bool:
        if isinstance(command, str):
            command = command.strip()
        if not command:
            return False
        try:
            self.command_queue.put_nowait(command)
            return True
        except thread_queue.Full:
            return False

    def enqueue_audio(self, payload: dict) -> bool:
        if not isinstance(payload, dict) or not payload.get("data"):
            return False
        return self._offer(self.audio_queue, payload)

    def get_command(self, timeout: float) -> str:
        return self.command_queue.get(timeout=max(0.01, float(timeout)))

    def get_audio(self, timeout: float) -> dict:
        return self.audio_queue.get(timeout=max(0.01, float(timeout)))

    def register_client(self) -> tuple[str, thread_queue.Queue[tuple[str, object]]]:
        client_id = uuid.uuid4().hex
        outbound: thread_queue.Queue[tuple[str, object]] = thread_queue.Queue(maxsize=self._client_limit)
        with self._clients_lock:
            self._clients[client_id] = {
                "outbound": outbound,
                "registered_at": time.time(),
            }
        return client_id, outbound

    def unregister_client(self, client_id: str) -> None:
        with self._clients_lock:
            self._clients.pop(client_id, None)

    def has_clients(self) -> bool:
        with self._clients_lock:
            return bool(self._clients)

    def publish_to_client(self, client_id: str, message: dict) -> bool:
        with self._clients_lock:
            client = self._clients.get(client_id)
            outbound = client.get("outbound") if isinstance(client, dict) else client
        return bool(outbound) and self._offer(outbound, ("json", message))

    def publish_to_latest_client(self, message: dict) -> str | None:
        with self._clients_lock:
            if not self._clients:
                return None
            client_id, client = max(
                self._clients.items(),
                key=lambda item: float(item[1].get("registered_at") or 0.0),
            )
            outbound = client["outbound"]
        return client_id if self._offer(outbound, ("json", message)) else None

    def _publish(self, kind: str, payload: object) -> None:
        with self._clients_lock:
            clients = [
                client.get("outbound") if isinstance(client, dict) else client
                for client in self._clients.values()
            ]
        for outbound in clients:
            self._offer(outbound, (kind, payload))

    def publish_event(self, message: dict) -> None:
        if isinstance(message, dict):
            self._publish("json", message)

    def publish_audio(self, payload: bytes) -> None:
        if payload:
            self._publish("bytes", bytes(payload))

    def request_local_action(self, action: str, payload: dict, timeout: float = 20.0) -> dict:
        task_id = uuid.uuid4().hex
        done = threading.Event()
        task = {
            "id": task_id,
            "action": str(action or "").strip(),
            "payload": payload if isinstance(payload, dict) else {},
            "status": "queued",
            "created_at": time.time(),
            "done": done,
            "result": None,
        }
        with self._local_tasks_lock:
            self._local_tasks[task_id] = task
        if not done.wait(timeout=max(1.0, float(timeout))):
            with self._local_tasks_lock:
                self._local_tasks.pop(task_id, None)
            return {
                "status": "mac_offline",
                "action": action,
                "message": "The Mac worker did not respond. Wake the Mac and retry this action.",
            }
        with self._local_tasks_lock:
            completed = self._local_tasks.pop(task_id, task)
        result = completed.get("result")
        return result if isinstance(result, dict) else {"status": "error", "result": result}

    def claim_local_tasks(self, limit: int = 5) -> list[dict]:
        now = time.time()
        self._local_worker_last_seen = now
        claimed = []
        with self._local_tasks_lock:
            for task in self._local_tasks.values():
                if (
                    task.get("status") == "claimed"
                    and now - float(task.get("claimed_at") or now) > 45
                ):
                    task["status"] = "queued"
                if task.get("status") != "queued":
                    continue
                task["status"] = "claimed"
                task["claimed_at"] = now
                claimed.append({
                    "id": task["id"],
                    "action": task["action"],
                    "payload": task["payload"],
                })
                if len(claimed) >= max(1, min(int(limit), 20)):
                    break
        return claimed

    def complete_local_task(self, task_id: str, result: dict) -> bool:
        self._local_worker_last_seen = time.time()
        with self._local_tasks_lock:
            task = self._local_tasks.get(task_id)
            if task is None:
                return False
            task["status"] = "completed"
            task["result"] = result
            task["done"].set()
            return True

    def local_worker_status(self) -> dict:
        age = time.time() - self._local_worker_last_seen if self._local_worker_last_seen else None
        return {
            "connected": age is not None and age < 20,
            "last_seen_seconds": round(age, 1) if age is not None else None,
        }


class VPSOrchestrator:
    def __init__(self):
        self.queue = deque()
        self.lock = threading.Lock()
        self.runtime_bridge = VPSRuntimeBridge()
        self._brain_lock = threading.Lock()
        self._brain_thread = None
        self._brain_state = "stopped"
        self._brain_error = ""
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.public_entry = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "https://jarvis.jarvisyourdomain.com"
        self.mode = "vps"
        self.dashboard_server = None
        if DashboardServer is not None:
            try:
                self.dashboard_server = DashboardServer()
            except Exception:
                self.dashboard_server = None
        if self.dashboard_server is not None and hasattr(self.dashboard_server, "set_remote_output_sinks"):
            self.dashboard_server.set_remote_output_sinks(
                event_sink=self.runtime_bridge.publish_event,
                audio_sink=self.runtime_bridge.publish_audio,
                audio_available=self.runtime_bridge.has_clients,
            )
        self.status = {
            "service": "jarvis-vps-orchestrator",
            "mode": self.mode,
            "public_entry": self.public_entry,
            "uptime_started": self.started_at,
            "queue_size": 0,
            "status": "online",
            "dashboard": "vps-managed" if self.dashboard_server is not None else "disabled",
        }

    @staticmethod
    def _vps_brain_enabled() -> bool:
        return str(os.getenv("JARVIS_RUN_VPS_BRAIN") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @staticmethod
    def worker_token() -> str:
        explicit = str(os.getenv("JARVIS_WORKER_TOKEN") or "").strip()
        if explicit:
            return explicit
        try:
            config = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
            explicit = str(config.get("local_worker_token") or "").strip()
            if explicit:
                return explicit
            seed = str(config.get("gemini_api_key") or "").strip()
        except Exception:
            seed = ""
        return hashlib.sha256(f"jarvis-local-worker-v1:{seed}".encode("utf-8")).hexdigest() if seed else ""

    def ensure_brain_started(self) -> bool:
        """Start one headless live session inside this one-worker Gunicorn process."""
        if not self._vps_brain_enabled() or self.dashboard_server is None:
            return False
        with self._brain_lock:
            if self._brain_thread and self._brain_thread.is_alive():
                return True
            self._brain_state = "starting"
            self._brain_error = ""
            self._brain_thread = threading.Thread(
                target=self._run_vps_brain,
                name="jarvis-vps-brain",
                daemon=True,
            )
            self._brain_thread.start()
            return True

    def _run_vps_brain(self) -> None:
        try:
            # main.py decides which optional desktop integrations to import at module load.
            os.environ["JARVIS_HEADLESS"] = "1"
            import asyncio
            from main import JarvisLive, _HeadlessUI

            ui = _HeadlessUI()
            ui.dashboard = self.dashboard_server
            jarvis = JarvisLive(
                ui,
                dashboard=self.dashboard_server,
                remote_bridge=self.runtime_bridge,
            )
            self._brain_state = "running"
            asyncio.run(jarvis.run())
        except BaseException as exc:  # pragma: no cover - depends on live credentials
            self._brain_error = str(exc)
            self._brain_state = "error"
            print(f"[VPS Brain] Stopped: {exc}")
        else:  # pragma: no cover - run() is intentionally long-lived
            self._brain_state = "stopped"

    def enqueue_task(self, action: str, payload: dict | None = None, source: str = "system") -> dict:
        task = {
            "id": f"task-{int(time.time() * 1000)}-{len(self.queue) + 1}",
            "action": action,
            "payload": payload or {},
            "source": source,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            self.queue.append(task)
            self.status["queue_size"] = len(self.queue)
        return {"status": "queued", "task": task}

    def get_tasks(self) -> list[dict]:
        with self.lock:
            return [deepcopy(item) for item in self.queue]

    def get_status(self) -> dict:
        with self.lock:
            payload = deepcopy(self.status)
            payload["queue_size"] = len(self.queue)
            return payload

    def get_public_remote_snapshot(self) -> dict:
        env_public = (os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "").strip()
        public_entry = env_public or self.public_entry
        key = ""
        auto_login_url = ""
        public_like_url = bool(public_entry) and not public_entry.startswith((
            "http://127.0.0.1",
            "http://localhost",
            "http://10.",
            "http://172.",
            "http://192.168.",
            "https://127.0.0.1",
            "https://localhost",
            "https://10.",
            "https://172.",
            "https://192.168.",
        ))
        security_status = "SECURITY: PUBLIC=ON | PIN=OFF" if public_like_url else "SECURITY: PUBLIC=OFF | PIN=OFF"

        dashboard = self.dashboard_server
        if dashboard is not None:
            if hasattr(dashboard, "new_key"):
                key = str(dashboard.new_key()).strip()
            if hasattr(dashboard, "get_remote_url"):
                try:
                    dashboard_url = str(dashboard.get_remote_url() or "").strip()
                    if dashboard_url and not dashboard_url.startswith(("http://192.168.", "http://10.", "http://172.", "http://127.0.0.1", "https://192.168.", "https://10.", "https://172.", "https://127.0.0.1")):
                        public_entry = dashboard_url
                except Exception:
                    pass
            if hasattr(dashboard, "get_auto_login_url") and key:
                try:
                    candidate = str(dashboard.get_auto_login_url(key) or "").strip()
                    if candidate and not candidate.startswith(("http://192.168.", "http://10.", "http://172.", "http://127.0.0.1", "https://192.168.", "https://10.", "https://172.", "https://127.0.0.1")):
                        auto_login_url = candidate
                except Exception:
                    auto_login_url = ""
            if hasattr(dashboard, "get_remote_security_status"):
                try:
                    dashboard_security = str(dashboard.get_remote_security_status() or "").strip()
                    if dashboard_security and (not public_like_url or "PUBLIC=OFF" not in dashboard_security):
                        security_status = dashboard_security
                except Exception:
                    security_status = security_status

        if not public_entry:
            public_entry = self.public_entry

        if env_public:
            public_entry = env_public

        if key and not auto_login_url:
            auto_login_url = f"{public_entry.rstrip('/')}/auto-login?key={key}"

        if not auto_login_url and key:
            auto_login_url = f"{public_entry.rstrip('/')}/auto-login?key={key}"

        if auto_login_url and not auto_login_url.startswith(("http://", "https://")):
            auto_login_url = f"{public_entry.rstrip('/')}/auto-login?key={key}"

        if env_public and auto_login_url:
            auto_login_url = f"{env_public.rstrip('/')}/auto-login?key={key}"

        return {
            "ok": True,
            "url": public_entry,
            "key": key,
            "auto_login_url": auto_login_url,
            "security": security_status,
            "source": "vps",
        }

    def get_ops_snapshot(self) -> dict:
        with self.lock:
            queue_snapshot = [deepcopy(item) for item in self.queue]
            payload = deepcopy(self.status)

        uptime_seconds = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(self.started_at)).total_seconds())
        dashboard_url = None
        if self.dashboard_server is not None and hasattr(self.dashboard_server, "get_remote_url"):
            try:
                dashboard_url = self.dashboard_server.get_remote_url()
            except Exception:
                dashboard_url = None
        return {
            "service": "jarvis-vps-orchestrator",
            "mode": self.mode,
            "status": payload.get("status", "online"),
            "public_entry": self.public_entry,
            "dashboard_url": dashboard_url,
            "uptime_started": self.started_at,
            "uptime_seconds": round(uptime_seconds, 2),
            "queue_size": len(queue_snapshot),
            "queue": queue_snapshot,
            "backend": {
                "python": "flask",
                "worker_mode": "vps",
                "remote_control": True,
            },
            "brain": {
                "state": self._brain_state,
                "thread_alive": bool(self._brain_thread and self._brain_thread.is_alive()),
                "error": self._brain_error,
                "command_queue": self.runtime_bridge.command_queue.qsize(),
                "audio_queue": self.runtime_bridge.audio_queue.qsize(),
            },
            "local_worker": self.runtime_bridge.local_worker_status(),
            "connected": True,
        }

    def reboot(self) -> dict:
        with self.lock:
            self.status["status"] = "restarting"
            self.status["reboot_requested_at"] = datetime.now(timezone.utc).isoformat()
        result = {
            "status": "restarting",
            "service": "jarvis-vps-orchestrator",
            "reboot_requested_at": self.status.get("reboot_requested_at"),
        }
        if self._vps_brain_enabled():
            # Kill this gunicorn worker so the arbiter spawns a fresh one with a
            # clean brain thread. Previously this only flipped the status label,
            # so a stuck brain stayed stuck forever and /api/status stayed on
            # "restarting" permanently.
            def _exit_soon() -> None:
                time.sleep(0.5)
                os._exit(1)

            threading.Thread(target=_exit_soon, daemon=True).start()
        return result

    def process_task(self, task: dict) -> dict:
        action = str(task.get("action", "")).strip().lower()
        payload = task.get("payload") or {}
        if action == "sync_memory":
            memory = load_memory()
            update_memory({"notes": {"vps_sync": {"value": payload.get("source", "unknown")}}})
            return {"status": "completed", "result": {"memory_loaded": bool(memory), "action": action}}
        if action == "index_document":
            path = str(payload.get("path") or "")
            if not path:
                return {"status": "error", "error": "path is required"}
            result = ingest_document(path, source_name=payload.get("source_name") or Path(path).stem)
            return {"status": "completed", "result": result}
        if action == "index_codebase":
            root = str(payload.get("path") or str(BASE_DIR))
            result = index_codebase(root, source_name=payload.get("source_name") or Path(root).name)
            return {"status": "completed", "result": result}
        if action == "search_memory":
            query = str(payload.get("query") or "")
            matches = search_document_index(query, limit=int(payload.get("limit") or 5)) if query else []
            return {"status": "completed", "result": {"query": query, "matches": matches}}
        if action == "save_memory":
            memory = payload.get("memory") or {}
            save_memory(memory)
            return {"status": "completed", "result": {"saved": True}}
        if action == "remote_chat":
            text = str(payload.get("text") or payload.get("prompt") or "").strip()
            return {"status": "accepted", "result": {"action": action, "text": text, "note": "queued for active local session"}}
        return {"status": "completed", "result": {"action": action, "payload": payload, "note": "queued for local execution"}}

    def drain_queue(self, limit: int = 20) -> list[dict]:
        processed = []
        with self.lock:
            for _ in range(min(limit, len(self.queue))):
                task = self.queue.popleft()
                processed.append(task)
            self.status["queue_size"] = len(self.queue)
        for item in processed:
            try:
                result = self.process_task(item)
                item["status"] = result.get("status", "completed")
                item["result"] = result
            except Exception as exc:  # pragma: no cover - defensive guard
                item["status"] = "error"
                item["result"] = {"error": str(exc)}
        return processed


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(BASE_DIR / "dashboard" / "static"),
        static_url_path="/static",
    )
    orchestrator = VPSOrchestrator()
    app.orchestrator = orchestrator

    sock = Sock(app) if Sock is not None else None

    @app.before_request
    def ensure_vps_brain():
        orchestrator.ensure_brain_started()

    if sock is None:
        @app.route("/ws")
        def ws_fallback():
            return jsonify({"ok": False, "error": "WebSocket support unavailable"}), 503

        @app.route("/ws/phone-audio")
        def phone_audio_fallback():
            return jsonify({"ok": False, "error": "WebSocket support unavailable"}), 503

    def _valid_ws_token() -> str:
        token = str(request.args.get("token") or "").strip()
        dashboard = orchestrator.dashboard_server
        if dashboard is None or not hasattr(dashboard, "_is_fully_authorized"):
            return ""
        return token if dashboard._is_fully_authorized(token) else ""

    def _ws_reject_code() -> int:
        """4003 = key/token fine, just needs the face scan (client should show the
        lock overlay, not log out). 4001 = token itself is invalid/expired (client
        should return to /login)."""
        token = str(request.args.get("token") or "").strip()
        dashboard = orchestrator.dashboard_server
        if dashboard is not None and hasattr(dashboard, "_is_token_valid") and dashboard._is_token_valid(token):
            return 4003
        return 4001

    def _valid_worker_token() -> bool:
        expected = orchestrator.worker_token()
        supplied = str(request.headers.get("x-jarvis-worker-token") or "").strip()
        return bool(expected and supplied and secrets.compare_digest(expected, supplied))

    if sock is not None:
        @sock.route("/ws")
        def ws_route(ws):
            orchestrator.ensure_brain_started()
            tok = _valid_ws_token()
            if not tok:
                try:
                    ws.close(code=_ws_reject_code())
                except Exception:
                    pass
                return
            client_id, outbound = orchestrator.runtime_bridge.register_client()
            brain_state = orchestrator._brain_state
            if brain_state == "error":
                session_text = "VPS brain failed to start. Check /api/ops for its error."
            elif brain_state == "starting":
                session_text = "VPS brain is starting. Your first command will wait for it."
            else:
                session_text = "Remote session active."
            orchestrator.runtime_bridge.publish_to_client(
                client_id,
                {"type": "sys", "text": session_text},
            )
            history = getattr(orchestrator.dashboard_server, "_history", [])
            current_status = next(
                (
                    event
                    for event in reversed(history)
                    if isinstance(event, dict) and event.get("type") == "status"
                ),
                None,
            )
            if current_status is not None:
                orchestrator.runtime_bridge.publish_to_client(client_id, current_status)
            elif brain_state == "running":
                orchestrator.runtime_bridge.publish_to_client(
                    client_id,
                    {"type": "status", "state": "active"},
                )
            try:
                while True:
                    try:
                        kind, outbound_payload = outbound.get_nowait()
                    except thread_queue.Empty:
                        pass
                    else:
                        try:
                            ws.send(
                                json.dumps(outbound_payload)
                                if kind == "json"
                                else outbound_payload
                            )
                        except Exception as exc:
                            print(f"[Public WS] Send ended: {type(exc).__name__}: {exc}")
                            break

                    try:
                        payload = ws.receive(timeout=0.05)
                    except Exception as exc:
                        print(f"[Public WS] Receive ended: {type(exc).__name__}: {exc}")
                        break
                    if payload is None:
                        if not getattr(ws, "connected", True):
                            break
                        continue
                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue
                    response = handle_dashboard_ws_message(
                        data,
                        dashboard=orchestrator.dashboard_server,
                        token=tok,
                        queue=orchestrator.runtime_bridge.command_queue,
                        wake_callback=getattr(orchestrator.dashboard_server, "_wake_callback", None),
                    )
                    if response is not None:
                        orchestrator.runtime_bridge.publish_to_client(client_id, response)
            finally:
                orchestrator.runtime_bridge.unregister_client(client_id)

        @sock.route("/ws/phone-audio")
        def phone_audio_route(ws):
            orchestrator.ensure_brain_started()
            tok = _valid_ws_token()
            if not tok:
                try:
                    ws.close(code=_ws_reject_code())
                except Exception:
                    pass
                return
            try:
                while True:
                    data = ws.receive()
                    if data is None:
                        break
                    enqueue_phone_audio_payload(
                        orchestrator.dashboard_server,
                        data,
                        queue=orchestrator.runtime_bridge.audio_queue,
                    )
            except Exception:
                pass

    @app.get("/")
    def index():
        dashboard_path = BASE_DIR / "dashboard" / "static" / "app.html"
        if dashboard_path.exists():
            html = dashboard_path.read_text(encoding="utf-8")
            host = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or orchestrator.public_entry or "https://jarvis.jarvisyourdomain.com"
            html = html.replace("__IP__", "jarvis.jarvisyourdomain.com").replace("__PORT__", "443")
            resp = Response(html, mimetype="text/html")
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
        return jsonify({
            "ok": True,
            "service": "jarvis-vps-orchestrator",
            "mode": "vps",
            "status": "online",
            "title": "JARVIS VPS Dashboard",
            "public_entry": orchestrator.public_entry,
            "dashboard_url": (
                orchestrator.dashboard_server.get_remote_url() if orchestrator.dashboard_server is not None and hasattr(orchestrator.dashboard_server, "get_remote_url") else None
            ),
            "message": "The dashboard runs on the VPS and remains active even if the Mac app shuts down.",
        })

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        static_dir = BASE_DIR / "dashboard" / "static"
        safe_path = (static_dir / filename).resolve()
        if not str(safe_path).startswith(str(static_dir.resolve())):
            return jsonify({"ok": False, "error": "invalid path"}), 400
        if not safe_path.exists():
            return jsonify({"ok": False, "error": "not found"}), 404
        return send_file(str(safe_path), conditional=True)

    @app.get("/static/crypto.js")
    def crypto_js_alias():
        crypto_file = BASE_DIR / "dashboard" / "static" / "crypto-js.min.js"
        if not crypto_file.exists():
            return jsonify({"ok": False, "error": "crypto asset missing"}), 404
        return send_file(str(crypto_file), mimetype="application/javascript")

    @app.get("/login")
    def login():
        login_path = BASE_DIR / "dashboard" / "static" / "login.html"
        if login_path.exists():
            return send_file(str(login_path), mimetype="text/html")
        return jsonify({
            "ok": False,
            "error": "login page not available",
            "service": "jarvis-vps-orchestrator",
        }), 404

    @app.post("/login")
    def login_post():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        data = request.get_json(silent=True) or {}
        key = str(data.get("key") or data.get("pin") or "").strip().upper()
        remote_pin = str(data.get("remote_pin") or "").strip()
        pending = getattr(dashboard, "_pending_keys", {})
        now = time.time()
        key_ok = bool(key) and key in pending and pending[key] > now
        if hasattr(dashboard, "_verify_remote_pin"):
            pin_ok = dashboard._verify_remote_pin(remote_pin)
        else:
            pin_ok = True
        if key_ok and pin_ok:
            try:
                del pending[key]
            except Exception:
                pass
            issue_token_fn = getattr(dashboard, "_issue_token", None)
            token = issue_token_fn(key) if callable(issue_token_fn) else key
            return jsonify({"ok": True, "token": token, "key": key})
        return jsonify({"ok": False, "error": "Invalid or expired key"}), 401

    def _flask_client_ip() -> str:
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return xff or request.remote_addr or "unknown"

    @app.get("/api/biometric/status")
    def api_biometric_status():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_token_valid(tok):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        enrolled = dashboard._biometric_enrolled()
        return jsonify({
            "ok": True,
            "enrolled": enrolled,
            "verified": dashboard._is_biometric_verified(tok) if enrolled else True,
        })

    @app.post("/api/biometric/verify")
    def api_biometric_verify():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_token_valid(tok):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        ip = _flask_client_ip()
        blocked, retry_after = dashboard._is_biometric_blocked(ip)
        if blocked:
            return jsonify({"ok": False, "error": "Too many failed attempts", "retry_after": retry_after}), 429

        if not dashboard._biometric_enrolled():
            return jsonify({"ok": False, "error": "not-enrolled"}), 400

        data = request.get_json(silent=True) or {}
        frames_b64 = data.get("frames") or []
        if not isinstance(frames_b64, list) or not frames_b64 or len(frames_b64) > 16:
            return jsonify({"ok": False, "error": "expected 1-16 frames"}), 400

        frames = []
        for item in frames_b64:
            try:
                raw = base64.b64decode(str(item), validate=True)
            except Exception:
                continue
            if 0 < len(raw) <= 3 * 1024 * 1024:
                frames.append(raw)
        if not frames:
            return jsonify({"ok": False, "error": "no valid frames"}), 400

        try:
            from auth import load_face_model, verify_face_against_model_bytes
            from dashboard.server import BIOMETRIC_PROFILE_KEY
        except Exception as exc:
            return jsonify({"ok": False, "error": f"biometric module unavailable: {exc}"}), 500

        recognizer = load_face_model(BIOMETRIC_PROFILE_KEY)
        ok, reason = verify_face_against_model_bytes(recognizer, frames)
        dashboard._record_biometric_attempt(ip, ok)
        if ok:
            dashboard._mark_biometric_verified(tok)
            return jsonify({"ok": True, "reason": reason})
        return jsonify({"ok": False, "error": reason}), 401

    @app.post("/api/biometric/enroll")
    def api_biometric_enroll():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_token_valid(tok):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        frames_b64 = data.get("frames") or []
        if not isinstance(frames_b64, list) or not frames_b64 or len(frames_b64) > 24:
            return jsonify({"ok": False, "error": "expected 1-24 frames"}), 400

        frames = []
        for item in frames_b64:
            try:
                raw = base64.b64decode(str(item), validate=True)
            except Exception:
                continue
            if 0 < len(raw) <= 3 * 1024 * 1024:
                frames.append(raw)

        try:
            from auth import train_face_model, save_face_model
            from dashboard.server import BIOMETRIC_PROFILE_KEY, BIOMETRIC_MIN_ENROLL_SAMPLES
        except Exception as exc:
            return jsonify({"ok": False, "error": f"biometric module unavailable: {exc}"}), 500

        ok, model_bytes, message = train_face_model(frames, min_samples=BIOMETRIC_MIN_ENROLL_SAMPLES)
        if not ok or not model_bytes:
            return jsonify({"ok": False, "error": message}), 400
        save_face_model(BIOMETRIC_PROFILE_KEY, model_bytes)
        # Re-enrolling invalidates any already-passed face scans (including this
        # session's) so a stale/compromised token can't skip the new check.
        for t in list(dashboard._token_biometric_ok.keys()):
            dashboard._token_biometric_ok[t] = False
        return jsonify({"ok": True, "message": message})

    @app.post("/api/device-login")
    def device_login_post():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        data = request.get_json(silent=True) or {}
        device_token = str(data.get("device_token") or "").strip()
        sessions = getattr(dashboard, "_device_sessions", {})
        session = sessions.get(device_token, {}) if device_token else {}
        session_key = str((session or {}).get("session_key") or "").strip()
        if not device_token or not session_key:
            return jsonify({"ok": False, "error": "device not paired"}), 401
        issue_token_fn = getattr(dashboard, "_issue_token", None)
        token = issue_token_fn(session_key) if callable(issue_token_fn) else session_key
        return jsonify({"ok": True, "token": token, "key": session_key})

    @app.post("/api/request-key")
    def request_key_post():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"ok": False, "error": "remote access not initialized"}), 503
        new_key = getattr(dashboard, "new_key", None)
        if not callable(new_key):
            return jsonify({"ok": False, "error": "key generation unavailable"}), 500
        key = str(new_key()).strip().upper()
        url = (os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or orchestrator.public_entry or "https://jarvis.jarvisyourdomain.com").strip().rstrip('/')
        auto_login_url = f"{url}/auto-login?key={key}"
        return jsonify({
            "ok": True,
            "key": key,
            "url": url,
            "auto_login_url": auto_login_url,
            "security": "SECURITY: PUBLIC=ON | PIN=OFF",
        })

    @app.get("/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "jarvis-vps-orchestrator",
            "mode": "vps",
            "status": "online",
            "public_entry": orchestrator.public_entry,
            "queue_size": len(orchestrator.queue),
        })

    @app.get("/auto-login")
    def auto_login():
        key = str(request.args.get("key") or "").strip()
        dashboard = orchestrator.dashboard_server
        pending = getattr(dashboard, "_pending_keys", {}) if dashboard is not None else {}
        if key and key in pending:
            try:
                pending.pop(key, None)
            except Exception:
                pass

        html = f"""
        <!doctype html>
        <html lang=\"en\">
        <head>
          <meta charset=\"utf-8\" />
          <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
          <title>JARVIS Remote Access</title>
          <style>
            body {{
              margin: 0;
              min-height: 100vh;
              display: grid;
              place-items: center;
              background: #07111f;
              color: #e8f3ff;
              font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            }}
            .card {{
              width: min(92vw, 560px);
              background: rgba(17, 31, 49, 0.92);
              border: 1px solid rgba(147, 197, 253, 0.35);
              border-radius: 16px;
              padding: 2rem 2.25rem;
              box-shadow: 0 24px 60px rgba(0,0,0,0.35);
              text-align: center;
            }}
            h1 {{ margin: 0 0 0.6rem; font-size: clamp(1.8rem, 4vw, 2.6rem); letter-spacing: 0.08em; }}
            p {{ line-height: 1.6; color: #d5e6ff; }}
            a {{
              display: inline-block;
              margin-top: 0.8rem;
              padding: 0.85rem 1.2rem;
              border-radius: 10px;
              background: linear-gradient(135deg, #3b82f6, #1d4ed8);
              color: white;
              text-decoration: none;
              font-weight: 700;
            }}
            code {{
              background: rgba(148, 163, 184, 0.12);
              border: 1px solid rgba(148, 163, 184, 0.2);
              border-radius: 6px;
              padding: 0.15rem 0.5rem;
            }}
          </style>
        </head>
        <body>
          <div class=\"card\">
            <h1>JARVIS</h1>
            <p>Remote access link validated.</p>
            <p>Key: <code>{key or 'not provided'}</code></p>
            <p>Opening the live dashboard…</p>
            <a href=\"/dashboard\">Open dashboard</a>
          </div>
          <script>
            setTimeout(() => {{ window.location.href = '/dashboard'; }}, 800);
          </script>
        </body>
        </html>
        """
        return Response(html, mimetype="text/html")

    @app.get("/dashboard")
    def dashboard_index():
        dashboard_path = BASE_DIR / "dashboard" / "static" / "app.html"
        if dashboard_path.exists():
            html = dashboard_path.read_text(encoding="utf-8")
            html = html.replace("__IP__", "jarvis.jarvisyourdomain.com").replace("__PORT__", "443")
            resp = Response(html, mimetype="text/html")
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
        return jsonify({
            "ok": True,
            "service": "jarvis-vps-orchestrator",
            "mode": "vps",
            "status": "online",
            "title": "JARVIS VPS Dashboard",
            "public_entry": orchestrator.public_entry,
            "dashboard_url": (
                orchestrator.dashboard_server.get_remote_url() if orchestrator.dashboard_server is not None and hasattr(orchestrator.dashboard_server, "get_remote_url") else None
            ),
            "message": "The dashboard runs on the VPS and remains active even if the Mac app shuts down.",
        })

    @app.get("/api/status")
    def api_status():
        return jsonify(orchestrator.get_status())

    @app.post("/api/reboot")
    def api_reboot():
        return jsonify(orchestrator.reboot())

    @app.get("/api/tasks")
    def list_tasks():
        return jsonify({"tasks": orchestrator.get_tasks()})

    @app.post("/api/tasks")
    def create_task():
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip()
        if not action:
            return jsonify({"error": "action is required"}), 400
        task = orchestrator.enqueue_task(
            action=action,
            payload=data.get("payload") or {},
            source=str(data.get("source") or "remote-api"),
        )
        return jsonify(task), 201

    @app.post("/api/commands")
    def handle_command():
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip()
        if not action:
            return jsonify({"error": "action is required"}), 400
        response = orchestrator.enqueue_task(
            action=action,
            payload=data.get("payload") or {},
            source=str(data.get("source") or "command-api"),
        )
        return jsonify({"accepted": True, **response})

    @app.post("/api/command")
    def api_command():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"error": "remote command queue unavailable"}), 503

        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_fully_authorized(tok):
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        enc = data.get("enc", "")
        text = ""
        if enc:
            text = dashboard._decrypt(tok, enc) or ""
        else:
            text = str(data.get("text") or "").strip()

        if not text:
            return jsonify({"error": "text is required"}), 400

        if not orchestrator.runtime_bridge.enqueue_command(text):
            return jsonify({"error": "command queue full"}), 503

        if getattr(dashboard, "_wake_callback", None):
            try:
                dashboard._wake_callback()
            except Exception:
                pass

        return jsonify({"ok": True, "accepted": True, "text": text})

    @app.post("/api/mac-camera-feed")
    def api_mac_camera_feed():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"error": "remote dashboard unavailable"}), 503

        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_fully_authorized(tok):
            return jsonify({"error": "Unauthorized"}), 401

        result = orchestrator.runtime_bridge.request_local_action("capture_camera", {}, timeout=20.0)
        return jsonify(result)

    @app.post("/api/camera/analyze")
    def api_camera_analyze():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"error": "remote dashboard unavailable"}), 503

        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_fully_authorized(tok):
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        image_b64 = str(data.get("image_b64") or "").strip()
        mime_type = str(data.get("mime_type") or "image/jpeg").strip().lower()
        prompt = str(data.get("prompt") or "Analyze the iPhone camera image clearly.").strip()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            return jsonify({"error": "unsupported image type"}), 400
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
        except Exception:
            return jsonify({"error": "invalid image data"}), 400
        if not image_bytes or len(image_bytes) > 6 * 1024 * 1024:
            return jsonify({"error": "image must be between 1 byte and 6 MB"}), 400

        command = {
            "text": prompt,
            "image_b64": image_b64,
            "mime_type": mime_type,
            "source": "device_camera",
            "request_id": str(data.get("request_id") or "").strip(),
        }
        if not orchestrator.runtime_bridge.enqueue_command(command):
            return jsonify({"error": "command queue full"}), 503
        return jsonify({"ok": True, "accepted": True, "bytes": len(image_bytes)})

    @app.get("/api/local-worker/tasks")
    def local_worker_tasks():
        if not _valid_worker_token():
            return jsonify({"error": "Unauthorized"}), 401
        limit = int(request.args.get("limit") or 5)
        return jsonify({"ok": True, "tasks": orchestrator.runtime_bridge.claim_local_tasks(limit)})

    @app.post("/api/local-worker/results/<task_id>")
    def local_worker_result(task_id: str):
        if not _valid_worker_token():
            return jsonify({"error": "Unauthorized"}), 401
        result = request.get_json(silent=True) or {}
        if not orchestrator.runtime_bridge.complete_local_task(task_id, result):
            return jsonify({"error": "task not found"}), 404
        return jsonify({"ok": True})

    @app.post("/api/wake")
    def api_wake():
        dashboard = orchestrator.dashboard_server
        if dashboard is None:
            return jsonify({"error": "remote dashboard unavailable"}), 503

        tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not tok or not dashboard._is_fully_authorized(tok):
            return jsonify({"error": "Unauthorized"}), 401

        if getattr(dashboard, "_wake_callback", None):
            try:
                dashboard._wake_callback()
            except Exception:
                pass
        return jsonify({"ok": True})

    @app.post("/api/remote_chat")
    def remote_chat():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or data.get("prompt") or "").strip()
        if not text:
            return jsonify({"error": "text is required"}), 400
        task = orchestrator.enqueue_task(
            action="remote_chat",
            payload={"text": text, "source": str(data.get("source") or "remote-web")},
            source="remote-chat",
        )
        return jsonify({"accepted": True, "text": text, **task}), 202

    @app.post("/voice")
    def voice_webhook():
        if VoiceResponse is None:
            return Response("<Response><Say>Twilio voice support is unavailable.</Say></Response>", mimetype="text/xml")

        response = VoiceResponse()
        from_number = str(request.form.get("From") or "")
        allowed = [
            str(item).strip() for item in (os.getenv("ALLOWED_NUMBERS") or os.getenv("MY_NUMBER") or "").split(",")
            if item.strip()
        ]
        if allowed and from_number and from_number not in allowed:
            response.reject()
            return Response(str(response), mimetype="text/xml")

        response.say("Welcome back, Sir. Remote Jarvis is online and ready. Please speak after the tone.", voice="alice")
        response.gather(input="speech", action="/respond-to-command", timeout=5, speech_timeout="auto")
        return Response(str(response), mimetype="text/xml")

    @app.post("/respond-to-command")
    def respond_to_command():
        if VoiceResponse is None:
            return Response("<Response><Say>Twilio voice support is unavailable.</Say></Response>", mimetype="text/xml")

        response = VoiceResponse()
        user_speech = (request.form.get("SpeechResult") or request.form.get("speechResult") or "").strip()

        if not user_speech:
            gather = response.gather(input="speech", action="/respond-to-command", timeout=5, speech_timeout="auto")
            response.append(gather)
            return Response(str(response), mimetype="text/xml")

        orchestrator.enqueue_task(
            action="remote_chat",
            payload={"text": user_speech, "source": "twilio-voice"},
            source="twilio-voice",
        )
        response.say("I have sent your request to Jarvis. Please hold while he responds through the active session.", voice="alice")
        return Response(str(response), mimetype="text/xml")

    @app.post("/api/remote_call")
    def remote_call():
        if Client is None:
            return jsonify({"error": "Twilio is not configured. Set TWILIO_SID, TWILIO_AUTH_TOKEN, and TWILIO_NUMBER."}), 500

        twilio_sid = (os.getenv("TWILIO_SID") or "").strip()
        twilio_auth = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
        twilio_number = (os.getenv("TWILIO_NUMBER") or "+1").strip()
        target = str((request.get_json(silent=True) or {}).get("to") or os.getenv("MY_NUMBER") or "").strip()
        if not target:
            return jsonify({"error": "to is required"}), 400

        client = Client(twilio_sid, twilio_auth)
        response = VoiceResponse()
        response.say("Jarvis is calling you now.", voice="alice")
        call = client.calls.create(twiml=str(response), to=target, from_=twilio_number)
        return jsonify({"ok": True, "sid": call.sid, "to": target, "from": twilio_number})

    @app.post("/api/process")
    def process_queue():
        limit = max(1, min(25, int((request.get_json(silent=True) or {}).get("limit", 10))))
        processed = orchestrator.drain_queue(limit=limit)
        return jsonify({"processed": processed})

    @app.get("/public/health")
    def public_health():
        return jsonify({
            "ok": True,
            "service": "jarvis-vps-orchestrator",
            "secure_entry": orchestrator.public_entry,
            "status": "ready",
        })

    @app.get("/api/health")
    def api_health():
        return jsonify({
            "ok": True,
            "service": "jarvis-vps-orchestrator",
            "status": "online",
            "public_entry": orchestrator.public_entry,
        })

    @app.get("/api/remote_access")
    def api_remote_access():
        return jsonify(orchestrator.get_public_remote_snapshot())

    @app.post("/api/remote_uplink")
    @app.get("/api/remote_uplink")
    def api_remote_uplink():
        snapshot = orchestrator.get_public_remote_snapshot()
        message = [
            "JARVIS remote uplink accepted",
            f"Open: {snapshot.get('url') or 'remote dashboard unavailable'}",
            f"Key: {snapshot.get('key') or ''}",
            f"Auto: {snapshot.get('auto_login_url') or ''}",
            "Status: VPS remote voice active",
            "SECURITY: PUBLIC=ON | PIN=OFF",
        ]
        message = "\n".join(part for part in message if part and not (part.endswith(": ") or part.endswith(":")))
        payload = {
            "ok": True,
            "source": "vps",
            "url": snapshot.get("url"),
            "key": snapshot.get("key"),
            "auto_login_url": snapshot.get("auto_login_url"),
            "message": message,
            "security": snapshot.get("security"),
        }
        return jsonify(payload)

    @app.get("/api/ops")
    def api_ops():
        snapshot = orchestrator.get_ops_snapshot()
        return jsonify(snapshot)

    @app.get("/api/memory")
    def memory_snapshot():
        return jsonify(load_memory())

    @app.post("/api/memory/sync")
    def memory_sync():
        data = request.get_json(silent=True) or {}
        source = str(data.get("source") or "remote")
        payload = data.get("memory") or {}
        if payload:
            update_memory(payload)
        else:
            update_memory({"notes": {"vps_sync": {"value": source}}})
        return jsonify({"status": "synced", "source": source, "memory": load_memory()})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
