import json
from pathlib import Path

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None


def _get_gemini_api_key() -> str:
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        if cfg_path.exists():
            return str(json.loads(cfg_path.read_text(encoding="utf-8")).get("gemini_api_key", "") or "").strip()
    except Exception:
        pass
    return ""


_MODEL = "models/gemini-flash-latest"

_PROMPT_TEMPLATE = """You are JARVIS, glancing at a single frame from a camera watching James's room to notice \
what he's currently doing — not to identify people or run security checks, just ambient situational awareness.

Look at the image and decide: is there something CLEARLY specific and potentially useful to mention right now \
(e.g. he's visibly working on a particular device, project, or task where a natural, helpful comment or offer of \
assistance would fit) — or is this just an ordinary, unremarkable moment not worth interrupting for?

Be conservative: only say should_comment=true for something concrete and identifiable, never for generic states \
like "sitting at a desk" or "using a computer" alone. If the scene looks essentially the same as last time with \
nothing new to add, say should_comment=false.
{last_comment_context}
Return ONLY valid JSON, no markdown, in exactly this shape:
{{"should_comment": true or false, "comment": "one short, natural sentence if should_comment is true, else empty string"}}
"""


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def describe_scene(frame, last_comment: str | None = None) -> dict:
    """One Gemini vision call: briefly judge whether the current camera frame
    shows something specific enough to proactively mention, and if so, what.
    Deliberately conservative (see prompt) — this feeds an ambient assistant
    comment, not a security or identity decision. Returns
    {"should_comment": False} on any failure, so callers can just check that
    one key without needing to handle exceptions themselves."""
    if cv2 is None or frame is None:
        return {"should_comment": False}

    api_key = _get_gemini_api_key()
    if not api_key:
        return {"should_comment": False}

    try:
        ok, jbuf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return {"should_comment": False}

        from google import genai
        from google.genai import types as gtypes

        last_comment_context = (
            f'\nYour last comment was: "{last_comment}" — do not repeat it or say something too similar.\n'
            if last_comment
            else ""
        )
        prompt = _PROMPT_TEMPLATE.format(last_comment_context=last_comment_context)

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_MODEL,
            contents=[gtypes.Part.from_bytes(data=jbuf.tobytes(), mime_type="image/jpeg"), prompt],
        )
        raw = _strip_fences(getattr(response, "text", "") or "")
        parsed = json.loads(raw)
        return {
            "should_comment": bool(parsed.get("should_comment", False)),
            "comment": str(parsed.get("comment", "") or "").strip(),
        }
    except Exception:
        return {"should_comment": False}
