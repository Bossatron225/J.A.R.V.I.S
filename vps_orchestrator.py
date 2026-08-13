import os
import threading
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

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
        self.status = {
            "service": "jarvis-vps-orchestrator",
            "mode": self.mode,
            "public_entry": self.public_entry,
            "uptime_started": self.started_at,
            "queue_size": 0,
            "status": "online",
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
        return {
            "service": "jarvis-vps-orchestrator",
            "mode": self.mode,
            "status": payload.get("status", "online"),
            "public_entry": self.public_entry,
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
