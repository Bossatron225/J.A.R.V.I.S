import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "config" / "alexa_routines.json"


_DEFAULT_CFG = {
    "provider": "webhook",
    "timeout_seconds": 15,
    "webhook": {
        "auth_header": "Authorization",
        "auth_token": "",
        "verify_ssl": True,
        "routines": {
            "plug_on": {
                "url": "",
                "method": "POST",
                "body": {}
            },
            "plug_off": {
                "url": "",
                "method": "POST",
                "body": {}
            }
        }
    },
    "plugs": {
        "smart_plug_1": {
            "on": "plug_on",
            "off": "plug_off",
            "status": ""
        }
    }
}


def _log(msg: str, player=None) -> None:
    print(f"[IFTTTWebhooks] {msg}")
    if player:
        try:
            player.write_log(f"[IFTTTWebhooks] {msg}")
        except Exception:
            pass


def _merge_dict(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _normalize_name(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_")


def _ensure_cfg_exists() -> None:
    if CFG_PATH.exists():
        return
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(_DEFAULT_CFG, indent=2), encoding="utf-8")


def _load_cfg() -> dict[str, Any]:
    _ensure_cfg_exists()
    try:
        raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(_DEFAULT_CFG)
        return _merge_dict(_DEFAULT_CFG, raw)
    except Exception:
        return dict(_DEFAULT_CFG)


def _save_cfg(cfg: dict[str, Any]) -> None:
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _masked(s: str) -> str:
    s = str(s or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + ("*" * (len(s) - 8)) + s[-4:]


def _http_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    timeout_seconds: int = 15,
    verify_ssl: bool = True,
) -> tuple[bool, int, str]:
    req_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url=url, data=data, method=(method or "GET").upper())
    for k, v in req_headers.items():
        req.add_header(k, v)

    ctx = None
    if not verify_ssl:
        ctx = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, timeout=max(3, int(timeout_seconds or 15)), context=ctx) as resp:
            status = int(getattr(resp, "status", 200))
            payload = (resp.read() or b"").decode("utf-8", errors="replace")
            return True, status, payload
    except urllib.error.HTTPError as e:
        payload = (e.read() or b"").decode("utf-8", errors="replace")
        return False, int(e.code), payload
    except Exception as e:
        return False, 0, str(e)


def _help_text() -> str:
    return (
        "IFTTT smart-home webhook setup:\n"
        "1) In IFTTT, create two applets for your plug or Smart Life scenes (on/off).\n"
        "2) Use Webhooks as the trigger and your smart-home action/scene as the destination.\n"
        "3) Configure webhook URLs in config/alexa_routines.json or with set_routine.\n"
        "4) Map plug alias to webhook routines using set_plug.\n"
        "5) Use actions on/off/status with target alias.\n"
        "Note: this integration is generic webhook-to-smart-home automation; Alexa is optional and not required."
    )


def _list_summary(cfg: dict[str, Any]) -> str:
    wh = cfg.get("webhook", {}) if isinstance(cfg.get("webhook"), dict) else {}
    routines = wh.get("routines", {}) if isinstance(wh.get("routines"), dict) else {}
    plugs = cfg.get("plugs", {}) if isinstance(cfg.get("plugs"), dict) else {}

    routine_lines = []
    for name, rcfg in sorted((str(k), v) for k, v in routines.items()):
        if not isinstance(rcfg, dict):
            continue
        method = str(rcfg.get("method", "POST") or "POST").upper()
        url = str(rcfg.get("url", "") or "")
        routine_lines.append(f"- {name}: {method} {url or '(not set)'}")

    plug_lines = []
    for alias, pcfg in sorted((str(k), v) for k, v in plugs.items()):
        if not isinstance(pcfg, dict):
            continue
        plug_lines.append(
            f"- {alias}: on={pcfg.get('on', '') or '(none)'} off={pcfg.get('off', '') or '(none)'} status={pcfg.get('status', '') or '(none)'}"
        )

    return (
        "Configured webhook routines:\n"
        + ("\n".join(routine_lines) if routine_lines else "(none)")
        + "\n\nConfigured plug mappings:\n"
        + ("\n".join(plug_lines) if plug_lines else "(none)")
    )


def _trigger_routine(cfg: dict[str, Any], routine_name: str) -> str:
    routine_key = _normalize_name(routine_name)
    if not routine_key:
        return "Routine name is required."

    wh = cfg.get("webhook", {}) if isinstance(cfg.get("webhook"), dict) else {}
    routines = wh.get("routines", {}) if isinstance(wh.get("routines"), dict) else {}
    auth_header = str(wh.get("auth_header", "Authorization") or "Authorization").strip()
    auth_token = str(wh.get("auth_token", "") or "")
    verify_ssl = bool(wh.get("verify_ssl", True))
    timeout_seconds = int(cfg.get("timeout_seconds", 15) or 15)

    rcfg = routines.get(routine_key, {}) if isinstance(routines.get(routine_key), dict) else {}
    url = str(rcfg.get("url", "") or "").strip()
    method = str(rcfg.get("method", "POST") or "POST").strip().upper()
    body = rcfg.get("body", None)

    if not url:
        return f"Routine '{routine_key}' has no URL configured."

    headers: dict[str, str] = {}
    if auth_token:
        headers[auth_header] = auth_token

    ok, code, payload = _http_json(
        url=url,
        method=method,
        headers=headers,
        body=(body if isinstance(body, (dict, list)) else None),
        timeout_seconds=timeout_seconds,
        verify_ssl=verify_ssl,
    )
    if not ok:
        return f"Routine '{routine_key}' failed: HTTP {code} {payload[:200]}"
    short = payload.strip().replace("\n", " ")
    return f"Routine '{routine_key}' triggered. {short[:140] if short else ''}".strip()


def alexa_routines(parameters: dict, response=None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "help") or "help").strip().lower()
    if action in {"initiate", "initiated", "engage", "start"}:
        action = "on"
    elif action in {"disengage", "disengaged", "stop", "shutdown"}:
        action = "off"
    cfg = _load_cfg()

    if action in {"setup", "help"}:
        return _help_text()

    if action == "provider":
        provider = str(cfg.get("provider", "webhook") or "webhook")
        wh = cfg.get("webhook", {}) if isinstance(cfg.get("webhook"), dict) else {}
        return (
            f"Provider: {provider}\n"
            f"Webhook auth header: {wh.get('auth_header', 'Authorization')}\n"
            f"Webhook token: {_masked(str(wh.get('auth_token', '') or '')) or '(not set)'}"
        )

    if action == "set_provider":
        provider = str(params.get("provider", "") or "").strip().lower()
        if provider != "webhook":
            return "Only provider 'webhook' is supported for this integration."
        cfg["provider"] = provider
        _save_cfg(cfg)
        return "IFTTT webhook provider set to webhook."

    if action == "set_auth":
        auth_header = str(params.get("auth_header", "Authorization") or "Authorization").strip()
        auth_token = str(params.get("auth_token", "") or "")
        wh = cfg.get("webhook", {}) if isinstance(cfg.get("webhook"), dict) else {}
        wh["auth_header"] = auth_header
        wh["auth_token"] = auth_token
        cfg["webhook"] = wh
        _save_cfg(cfg)
        return "Webhook auth saved."

    if action == "set_routine":
        routine = _normalize_name(str(params.get("routine", "") or ""))
        url = str(params.get("url", "") or "").strip()
        method = str(params.get("method", "POST") or "POST").strip().upper()
        body = params.get("body", {})

        if not routine:
            return "Provide routine name for set_routine."
        if not url:
            return "Provide url for set_routine."

        wh = cfg.get("webhook", {}) if isinstance(cfg.get("webhook"), dict) else {}
        routines = wh.get("routines", {}) if isinstance(wh.get("routines"), dict) else {}
        routines[routine] = {
            "url": url,
            "method": method,
            "body": body if isinstance(body, dict) else {},
        }
        wh["routines"] = routines
        cfg["webhook"] = wh
        _save_cfg(cfg)
        return f"Routine '{routine}' saved."

    if action == "set_plug":
        alias = _normalize_name(str(params.get("alias", "") or ""))
        on_routine = _normalize_name(str(params.get("on_routine", "") or ""))
        off_routine = _normalize_name(str(params.get("off_routine", "") or ""))
        status_routine = _normalize_name(str(params.get("status_routine", "") or ""))
        if not alias:
            return "Provide alias for set_plug."
        plugs = cfg.get("plugs", {}) if isinstance(cfg.get("plugs"), dict) else {}
        plugs[alias] = {
            "on": on_routine,
            "off": off_routine,
            "status": status_routine,
        }
        cfg["plugs"] = plugs
        _save_cfg(cfg)
        return f"Plug mapping '{alias}' saved."

    if action == "list":
        return _list_summary(cfg)

    if action == "trigger":
        routine = str(params.get("routine", "") or "")
        result = _trigger_routine(cfg, routine)
        _log(result, player)
        return result

    if action in {"on", "off", "status"}:
        target = _normalize_name(str(params.get("target", "ventilation_protocol") or "ventilation_protocol"))
        plugs = cfg.get("plugs", {}) if isinstance(cfg.get("plugs"), dict) else {}
        pcfg = plugs.get(target, {}) if isinstance(plugs.get(target), dict) else {}
        routine_name = str(pcfg.get(action, "") or "")
        if not routine_name:
            label = "initiate" if action == "on" else "disengage" if action == "off" else action
            return f"Protocol '{target}' has no '{label}' routine mapping."
        result = _trigger_routine(cfg, routine_name)
        if target == "ventilation_protocol":
            if action == "on":
                result = result.replace("Routine '", "Ventilation protocol initiation via routine '")
            elif action == "off":
                result = result.replace("Routine '", "Ventilation protocol disengagement via routine '")
        _log(result, player)
        return result

    return (
        "Unknown webhook action. Use: setup | help | provider | set_provider | "
        "set_auth | set_routine | set_plug | list | trigger | on | off | status | initiate | disengage"
    )


def ifttt_webhooks(parameters: dict, response=None, player=None) -> str:
    return alexa_routines(parameters=parameters, response=response, player=player)
