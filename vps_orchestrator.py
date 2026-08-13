import os
import threading
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, Response

def _build_root_page() -> str:
    public_entry = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "https://jarvis.internal"
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


class VPSOrchestrator:
    def __init__(self):
        self.queue = deque()
        self.lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.public_entry = os.getenv("JARVIS_PUBLIC_URL") or os.getenv("PUBLIC_ENTRY_URL") or "https://jarvis.internal"
        self.mode = "vps"
        self.dashboard_server = None
        if DashboardServer is not None:
            try:
                self.dashboard_server = DashboardServer()
            except Exception:
                self.dashboard_server = None
        self.status = {
            "service": "jarvis-vps-orchestrator",
            "mode": self.mode,
            "public_entry": self.public_entry,
            "uptime_started": self.started_at,
            "queue_size": 0,
            "status": "online",
            "dashboard": "vps-managed" if self.dashboard_server is not None else "disabled",
        }

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
            "connected": True,
        }

    def reboot(self) -> dict:
        with self.lock:
            self.status["status"] = "restarting"
            self.status["reboot_requested_at"] = datetime.now(timezone.utc).isoformat()
        return {
            "status": "restarting",
            "service": "jarvis-vps-orchestrator",
            "reboot_requested_at": self.status.get("reboot_requested_at"),
        }

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
    app = Flask(__name__)
    orchestrator = VPSOrchestrator()

    @app.get("/")
    def index():
        return _build_root_page(), 200, {"Content-Type": "text/html; charset=utf-8"}

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

    @app.get("/dashboard")
    def dashboard_index():
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
