import asyncio
import json
import re
from typing import Any
from pathlib import Path

try:
    from pywizlight import PilotBuilder, discovery, wizlight
    _WIZ_OK = True
except Exception:
    PilotBuilder = None
    discovery = None
    wizlight = None
    _WIZ_OK = False


_COLOR_MAP = {
    "white": (255, 255, 255),
    "warm white": (255, 214, 170),
    "cool white": (201, 226, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 128, 0),
    "purple": (160, 32, 240),
    "pink": (255, 105, 180),
    "cyan": (0, 255, 255),
}

_ACTION_ALIASES = {
    "discover": "discover",
    "list": "discover",
    "status": "status",
    "state": "status",
    "on": "on",
    "turn_on": "on",
    "turn on": "on",
    "switch_on": "on",
    "switch on": "on",
    "off": "off",
    "turn_off": "off",
    "turn off": "off",
    "switch_off": "off",
    "switch off": "off",
    "set": "on",
    "alias": "set_alias",
    "set alias": "set_alias",
    "add alias": "set_alias",
    "rename": "set_alias",
    "remove alias": "remove_alias",
    "delete alias": "remove_alias",
    "list aliases": "list_aliases",
    "aliases": "list_aliases",
}

CACHE_PATH = Path(__file__).resolve().parents[1] / "config" / "wiz_lights_cache.json"


def _log(msg: str, player=None) -> None:
    print(f"[WiZ] {msg}")
    if player:
        try:
            player.write_log(f"[WiZ] {msg}")
        except Exception:
            pass


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def _slug(text: str) -> str:
    s = str(text).strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-.]", "", s)
    return s


def _load_cache() -> dict[str, Any]:
    try:
        if not CACHE_PATH.exists():
            return {"ips": [], "aliases": {}}

        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"ips": [], "aliases": {}}

        ips = payload.get("ips", [])
        aliases = payload.get("aliases", {})
        if not isinstance(ips, list):
            ips = []
        if not isinstance(aliases, dict):
            aliases = {}

        clean_ips = [ip for ip in [str(x).strip() for x in ips] if ip]
        clean_aliases: dict[str, str] = {}
        for k, v in aliases.items():
            kk = _slug(k)
            vv = str(v).strip()
            if kk and vv:
                clean_aliases[kk] = vv

        return {"ips": clean_ips, "aliases": clean_aliases}
    except Exception:
        return {"ips": [], "aliases": {}}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        ips = cache.get("ips", []) if isinstance(cache, dict) else []
        aliases = cache.get("aliases", {}) if isinstance(cache, dict) else {}
        if not isinstance(ips, list):
            ips = []
        if not isinstance(aliases, dict):
            aliases = {}

        clean_ips = list(dict.fromkeys([str(ip).strip() for ip in ips if str(ip).strip()]))
        clean_aliases: dict[str, str] = {}
        for k, v in aliases.items():
            kk = _slug(k)
            vv = str(v).strip()
            if kk and vv:
                clean_aliases[kk] = vv

        payload = {"ips": clean_ips, "aliases": clean_aliases}
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _ensure_default_aliases(cache: dict[str, Any]) -> dict[str, Any]:
    ips = cache.get("ips", []) if isinstance(cache, dict) else []
    aliases = cache.get("aliases", {}) if isinstance(cache, dict) else {}
    if not isinstance(ips, list):
        ips = []
    if not isinstance(aliases, dict):
        aliases = {}

    used_numbers = set()
    for key in aliases.keys():
        m = re.fullmatch(r"bulb_(\d+)", str(key))
        if m:
            used_numbers.add(int(m.group(1)))

    aliased_ips = {str(v).strip() for v in aliases.values() if str(v).strip()}
    next_num = 1
    for ip in ips:
        ip_clean = str(ip).strip()
        if not ip_clean or ip_clean in aliased_ips:
            continue
        while next_num in used_numbers:
            next_num += 1
        alias_key = f"bulb_{next_num}"
        aliases[alias_key] = ip_clean
        used_numbers.add(next_num)
        aliased_ips.add(ip_clean)
        next_num += 1

    cache["ips"] = [str(x).strip() for x in ips if str(x).strip()]
    cache["aliases"] = aliases
    return cache


def _resolve_cached_identifier(value: str, aliases: dict[str, str], ips_set: set[str]) -> str | None:
    v = str(value).strip()
    if not v:
        return None
    if v in ips_set:
        return v
    key = _slug(v)
    if key in aliases:
        return aliases[key]
    return None


def _targets_from_params(params: dict[str, Any]) -> list[str]:
    targets: list[str] = []

    if "name" in params and params.get("name") is not None:
        targets.extend(_to_list(params.get("name")))
    if "names" in params and params.get("names") is not None:
        targets.extend(_to_list(params.get("names")))

    return [t for t in [str(x).strip() for x in targets] if t]


def _assign_alias(alias: str, ip: str) -> tuple[bool, str]:
    alias_key = _slug(alias)
    ip_clean = str(ip).strip()
    if not alias_key:
        return False, "Alias must contain letters or numbers."
    if not ip_clean:
        return False, "IP is required for alias assignment."

    cache = _load_cache()
    aliases = cache.get("aliases", {})
    aliases[alias_key] = ip_clean
    cache["aliases"] = aliases

    ips = cache.get("ips", [])
    if ip_clean not in ips:
        ips.append(ip_clean)
    cache["ips"] = ips
    cache = _ensure_default_aliases(cache)

    _save_cache(cache)
    return True, f"Saved alias '{alias_key}' -> {ip_clean}."


def _remove_alias(alias: str) -> tuple[bool, str]:
    alias_key = _slug(alias)
    if not alias_key:
        return False, "Alias must contain letters or numbers."

    cache = _load_cache()
    aliases = cache.get("aliases", {})
    if alias_key not in aliases:
        return False, f"Alias '{alias_key}' was not found."

    old = aliases.pop(alias_key)
    cache["aliases"] = aliases
    _save_cache(cache)
    return True, f"Removed alias '{alias_key}' (was {old})."


def _list_aliases() -> str:
    cache = _load_cache()
    aliases = cache.get("aliases", {})
    if not aliases:
        return "No WiZ aliases saved yet."
    pairs = [f"{k} -> {v}" for k, v in sorted(aliases.items())]
    return "WiZ aliases:\n" + "\n".join(pairs)


def _load_cached_ips() -> list[str]:
    cache = _load_cache()
    return cache.get("ips", [])


def _save_cached_ips(ips: list[str]) -> None:
    cache = _load_cache()
    merged = cache.get("ips", []) + [str(ip).strip() for ip in ips if str(ip).strip()]
    cache["ips"] = list(dict.fromkeys(merged))
    cache = _ensure_default_aliases(cache)
    _save_cache(cache)


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(value).strip()]


def _normalize_color(color: str | None) -> tuple[int, int, int] | None:
    if not color:
        return None

    raw = color.strip().lower()
    if raw in _COLOR_MAP:
        return _COLOR_MAP[raw]

    if raw.startswith("#") and len(raw) == 7:
        try:
            return (int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16))
        except ValueError:
            return None

    if "," in raw:
        try:
            parts = [int(x.strip()) for x in raw.split(",")]
            if len(parts) == 3 and all(0 <= n <= 255 for n in parts):
                return (parts[0], parts[1], parts[2])
        except ValueError:
            return None

    return None


def _normalize_action(raw_action: Any) -> str:
    if raw_action is None:
        return "status"
    action = str(raw_action).strip().lower().replace("-", " ").replace("_", " ")
    action = re.sub(r"\s+", " ", action)
    return _ACTION_ALIASES.get(action, action)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower().replace("%", "")
        if not text:
            return None
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    return None


def _extract_brightness_param(params: dict[str, Any]) -> int | None:
    direct = _coerce_int(params.get("brightness"))
    if direct is not None:
        return direct

    level = _coerce_int(params.get("level"))
    if level is not None:
        return level

    value = params.get("value")
    num = _coerce_int(value)
    if num is not None:
        return num

    if isinstance(value, str):
        m = re.search(r"(\d{1,3})\s*%", value)
        if m:
            return int(m.group(1))

    return None


def _extract_color_param(params: dict[str, Any]) -> str | None:
    color = params.get("color")
    if isinstance(color, str) and color.strip():
        return color.strip()

    value = params.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()

    description = params.get("description")
    if isinstance(description, str):
        text = description.lower()
        for name in _COLOR_MAP:
            if name in text:
                return name
        m = re.search(r"#[0-9a-fA-F]{6}", description)
        if m:
            return m.group(0)
        m = re.search(r"\b(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\b", description)
        if m:
            return f"{m.group(1)},{m.group(2)},{m.group(3)}"

    return None


def _brightness_to_wiz(level_0_100: int | None) -> int | None:
    if level_0_100 is None:
        return None
    clamped = max(1, min(100, int(level_0_100)))
    return max(1, round(clamped * 255 / 100))


async def _discover(wait_sec: float = 2.5):
    lights = await discovery.discover_lights(wait_time=wait_sec)
    discovered = lights or []
    if discovered:
        _save_cached_ips([getattr(l, "ip", "").strip() for l in discovered])
    return discovered


def _unwrap_state(state):
    """pywizlight >= 0.6 changed updateState() to return a list of PilotParser —
    one entry per head (dual-head bulbs get two, single-head bulbs one) — instead
    of a bare PilotParser. Entries can also be None when a head didn't report.
    Both shapes are accepted here because the Mac and the VPS can end up on
    different pywizlight versions; before this, every status call died with
    "'list' object has no attribute 'get_state'"."""
    if isinstance(state, (list, tuple)):
        state = next((entry for entry in state if entry is not None), None)
    if state is None:
        raise RuntimeError("bulb returned no state")
    return state


def _wiz_to_brightness(raw: int | None) -> int | None:
    """Inverse of _brightness_to_wiz: get_brightness() reports the raw 0-255 value,
    but every brightness the user sets is 1-100%. Reporting the raw number made
    status read "brightness=5" right after a "set to 2%" command."""
    if raw is None:
        return None
    return max(1, round(int(raw) * 100 / 255))


async def _status_for(light) -> dict[str, Any]:
    state = _unwrap_state(await light.updateState())
    return {
        "ip": getattr(light, "ip", "unknown"),
        "on": bool(state.get_state()),
        "brightness": _wiz_to_brightness(state.get_brightness()),
        "rgb": state.get_rgb(),
        "kelvin": state.get_colortemp(),
        "scene": state.get_scene(),
    }


async def _resolve_lights(params: dict[str, Any]):
    ips = _to_list(params.get("ips"))
    ip_single = str(params.get("ip", "")).strip()
    target = str(params.get("target", "all")).strip().lower()
    refresh = _parse_bool(params.get("refresh"))
    cache_only = _parse_bool(params.get("cache_only"))

    if ip_single:
        ips.append(ip_single)

    target_names = _targets_from_params(params)
    if target_names:
        cache = _load_cache()
        aliases = cache.get("aliases", {})
        ips_set = set(cache.get("ips", []))
        resolved = []
        unresolved = []
        for t in target_names:
            ip = _resolve_cached_identifier(t, aliases, ips_set)
            if ip:
                resolved.append(ip)
            else:
                unresolved.append(t)

        if unresolved:
            return [], f"missing_alias:{', '.join(unresolved)}"

        unique_ips = list(dict.fromkeys(resolved))
        return [wizlight(ip) for ip in unique_ips], "alias"

    if ips:
        unique_ips = list(dict.fromkeys(ips))
        return [wizlight(ip) for ip in unique_ips], "explicit"

    if target in ("cache", "cached", "saved") and not refresh:
        cached = _load_cached_ips()
        if cached:
            return [wizlight(ip) for ip in cached], "cache"
        return [], "cache"

    if target in ("all", "", "any") and not refresh:
        cached = _load_cached_ips()
        if cached:
            return [wizlight(ip) for ip in cached], "cache"
        if cache_only:
            return [], "cache"

    if target in ("all", "", "any"):
        return await _discover(), "discover"

    return [], "none"


async def _run_wiz(params: dict[str, Any]) -> str:
    action = _normalize_action(params.get("action", "status"))

    if action == "list_aliases":
        return _list_aliases()

    if action == "set_alias":
        alias_name = str(params.get("alias") or params.get("name") or "").strip()
        ip_value = str(params.get("ip") or "").strip()

        if not alias_name:
            return "Alias name is required. Pass alias or name."

        if not ip_value:
            cache = _load_cache()
            aliases = cache.get("aliases", {})
            ips_set = set(cache.get("ips", []))
            resolved = _resolve_cached_identifier(str(params.get("target", "")), aliases, ips_set)
            if resolved:
                ip_value = resolved

        if not ip_value:
            return "IP is required for alias assignment. Example: action=set_alias, alias=desk_lamp, ip=192.168.1.45"

        ok, msg = _assign_alias(alias_name, ip_value)
        return msg if ok else msg

    if action == "remove_alias":
        alias_name = str(params.get("alias") or params.get("name") or "").strip()
        if not alias_name:
            return "Alias name is required. Pass alias or name."
        _, msg = _remove_alias(alias_name)
        return msg

    if action == "discover":
        discovered = await _discover(wait_sec=3.0)
        if not discovered:
            return "No WiZ lights found on the local network."
        ips = [getattr(l, "ip", "unknown") for l in discovered]

        cache = _load_cache()
        aliases = cache.get("aliases", {}) if isinstance(cache, dict) else {}
        alias_rows = []
        for ip in ips:
            names = [k for k, v in aliases.items() if str(v).strip() == str(ip).strip()]
            label = names[0] if names else "(no alias)"
            alias_rows.append(f"- {label}: {ip}")

        return (
            f"Found {len(ips)} WiZ light(s): " + ", ".join(ips) +
            "\nAliases:\n" + "\n".join(alias_rows)
        )

    if action == "status" and _parse_bool(params.get("cache_only")):
        cached_ips = _load_cached_ips()
        if not cached_ips:
            return "No cached WiZ lights found. Run discover first."

    lights, source = await _resolve_lights(params)

    if not lights:
        if source.startswith("missing_alias:"):
            missing = source.split(":", 1)[1]
            return f"Unknown WiZ alias/name: {missing}. Use action=list_aliases or action=set_alias first."
        return (
            "No WiZ target lights found. Use action=discover first, "
            "or pass ip/ips explicitly."
        )

    if action == "status":
        rows = []
        failed = 0
        for light in lights:
            try:
                s = await _status_for(light)
                rows.append(
                    f"{s['ip']}: {'on' if s['on'] else 'off'}, "
                    f"brightness={s['brightness']}, rgb={s['rgb']}, kelvin={s['kelvin']}"
                )
            except Exception as e:
                failed += 1
                rows.append(f"{getattr(light, 'ip', 'unknown')}: status failed ({e})")

        if failed == len(lights) and source == "cache":
            refreshed = await _discover(wait_sec=3.0)
            if refreshed:
                rows = []
                for light in refreshed:
                    try:
                        s = await _status_for(light)
                        rows.append(
                            f"{s['ip']}: {'on' if s['on'] else 'off'}, "
                            f"brightness={s['brightness']}, rgb={s['rgb']}, kelvin={s['kelvin']}"
                        )
                    except Exception as e:
                        rows.append(f"{getattr(light, 'ip', 'unknown')}: status failed ({e})")

        return "WiZ status:\n" + "\n".join(rows)

    if action in ("off", "turn_off"):
        ok, fail = 0, []
        for light in lights:
            try:
                await light.turn_off()
                ok += 1
            except Exception as e:
                fail.append(f"{getattr(light, 'ip', 'unknown')} ({e})")

        if ok == 0 and fail and source == "cache":
            refreshed = await _discover(wait_sec=3.0)
            if refreshed:
                lights = refreshed
                ok, fail = 0, []
                for light in refreshed:
                    try:
                        await light.turn_off()
                        ok += 1
                    except Exception as e:
                        fail.append(f"{getattr(light, 'ip', 'unknown')} ({e})")

        msg = f"Turned off {ok}/{len(lights)} WiZ light(s)."
        if fail:
            msg += " Failed: " + ", ".join(fail)
        return msg

    if action == "on":
        brightness = None
        brightness_raw = _extract_brightness_param(params)
        if brightness_raw is not None:
            if not (1 <= brightness_raw <= 100):
                return "Brightness must be an integer from 1 to 100."
            brightness = _brightness_to_wiz(brightness_raw)

        kelvin = None
        kelvin_raw = _coerce_int(params.get("kelvin"))
        if kelvin_raw is not None:
            kelvin = max(2200, min(6500, kelvin_raw))

        color_text = _extract_color_param(params)
        rgb = _normalize_color(color_text)
        if color_text and rgb is None:
            return (
                "Color format not recognized. Use a color name (red), "
                "hex (#FF0000), or RGB string (255,0,0)."
            )

        pb_kwargs: dict[str, Any] = {}
        if brightness is not None:
            pb_kwargs["brightness"] = brightness
        if kelvin is not None:
            pb_kwargs["colortemp"] = kelvin
        if rgb is not None:
            pb_kwargs["rgb"] = rgb

        ok, fail = 0, []
        for light in lights:
            try:
                if pb_kwargs:
                    await light.turn_on(PilotBuilder(**pb_kwargs))
                else:
                    await light.turn_on()
                ok += 1
            except Exception as e:
                fail.append(f"{getattr(light, 'ip', 'unknown')} ({e})")

        if ok == 0 and fail and source == "cache":
            refreshed = await _discover(wait_sec=3.0)
            if refreshed:
                ok, fail = 0, []
                lights = refreshed
                for light in lights:
                    try:
                        if pb_kwargs:
                            await light.turn_on(PilotBuilder(**pb_kwargs))
                        else:
                            await light.turn_on()
                        ok += 1
                    except Exception as e:
                        fail.append(f"{getattr(light, 'ip', 'unknown')} ({e})")

        details = []
        if brightness is not None:
            details.append(f"brightness={brightness_raw}%")
        if kelvin is not None:
            details.append(f"kelvin={kelvin}")
        if rgb is not None:
            details.append(f"rgb={rgb}")

        msg = f"Turned on {ok}/{len(lights)} WiZ light(s)."
        if details:
            msg += " " + ", ".join(details)
        if fail:
            msg += " Failed: " + ", ".join(fail)
        return msg

    return "Unknown WiZ action. Use discover, status, on, off, or set."


def wiz_lights(parameters: dict, response=None, player=None, session_memory=None) -> str:
    if not _WIZ_OK:
        return "pywizlight is not installed. Install it with your active Python interpreter and try again."

    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()
    _log(f"Action: {action} Params: {params}", player)

    try:
        return asyncio.run(_run_wiz(params))
    except Exception as e:
        return f"WiZ control failed: {e}"
