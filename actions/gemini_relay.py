"""Handler for the 'gemini_relay' local-worker action — see core/local_relay.py for
why this exists. Runs on the Mac (dispatched via local_worker.py), reusing the exact
same Gemini-calling functions the Mac already uses natively, just invoked on the VPS's
behalf instead of the Mac's own conversation."""


def gemini_relay(parameters: dict, response=None, player=None, session_memory=None) -> dict:
    params = parameters or {}
    kind = str(params.get("kind") or "").strip().lower()

    if kind == "search":
        from actions.web_search import _gemini_search
        try:
            text = _gemini_search(str(params.get("query") or ""))
            return {"ok": True, "text": text}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if kind == "embed":
        from memory.conversation_log import embed_text
        values = embed_text(
            str(params.get("text") or ""),
            task_type=str(params.get("task_type") or "RETRIEVAL_DOCUMENT"),
        )
        return {"ok": values is not None, "values": values}

    return {"ok": False, "error": f"unknown gemini_relay kind: {kind!r}"}
