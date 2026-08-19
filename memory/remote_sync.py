import json
import os
from copy import deepcopy
from urllib import request, error


def merge_memory(local_memory: dict, remote_memory: dict) -> dict:
    merged = deepcopy(local_memory or {})
    remote = deepcopy(remote_memory or {})

    def choose_value(local_val, remote_val):
        if not isinstance(local_val, dict):
            return remote_val
        if not isinstance(remote_val, dict):
            return local_val
        if str(local_val.get('updated', '')) < str(remote_val.get('updated', '')):
            return remote_val
        return local_val

    def merge_dicts(base: dict, incoming: dict) -> dict:
        result = deepcopy(base)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge_dicts(result[key], value)
            elif isinstance(value, dict):
                result[key] = deepcopy(value)
            else:
                result[key] = deepcopy(value)
        return result

    for category in sorted(set(merged) | set(remote)):
        local_section = merged.get(category)
        remote_section = remote.get(category)
        if isinstance(local_section, dict) and isinstance(remote_section, dict):
            merged[category] = merge_dicts(local_section, remote_section)
            for key in set(local_section) | set(remote_section):
                local_item = local_section.get(key)
                remote_item = remote_section.get(key)
                if isinstance(local_item, dict) and isinstance(remote_item, dict):
                    merged[category][key] = choose_value(local_item, remote_item)
        elif remote_section is not None:
            merged[category] = deepcopy(remote_section)
    return merged


def fetch_remote_memory(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/memory"
    try:
        with request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8'))
            if isinstance(payload, dict):
                if 'memory' in payload and isinstance(payload['memory'], dict):
                    return payload['memory']
                return payload
            return {}
    except (error.URLError, TimeoutError, ValueError):
        return {}


def push_memory_to_vps(base_url: str, memory: dict) -> dict:
    url = f"{base_url.rstrip('/')}/api/memory/sync"
    payload = json.dumps({'memory': memory, 'source': 'mac-client'}).encode('utf-8')
    req = request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data if isinstance(data, dict) else {'status': 'synced'}
    except (error.URLError, TimeoutError, ValueError):
        return {'status': 'sync_failed'}


def load_memory_with_vps_sync(local_memory: dict | None = None, base_url: str | None = None) -> dict:
    base_url = (base_url or os.getenv('JARVIS_VPS_URL') or '').strip()
    if not base_url:
        return local_memory or {}
    remote = fetch_remote_memory(base_url)
    if not remote:
        return local_memory or {}
    return merge_memory(local_memory or {}, remote)


def merge_conversation_turns(local_turns: list, remote_turns: list) -> list:
    local = deepcopy(local_turns or [])
    remote = deepcopy(remote_turns or [])

    seen = {turn.get('id') for turn in local if isinstance(turn, dict)}
    for turn in remote:
        if not isinstance(turn, dict) or not turn.get('id'):
            continue
        if turn['id'] in seen:
            continue
        local.append(turn)
        seen.add(turn['id'])

    local.sort(key=lambda t: str(t.get('ts', '')))
    return local


def fetch_remote_conversations(base_url: str, since: str | None = None) -> list:
    url = f"{base_url.rstrip('/')}/api/conversations"
    if since:
        url += f"?since={since}"
    try:
        with request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8'))
            if isinstance(payload, dict) and isinstance(payload.get('turns'), list):
                return payload['turns']
            if isinstance(payload, list):
                return payload
            return []
    except (error.URLError, TimeoutError, ValueError):
        return []


def push_conversations_to_vps(base_url: str, turns: list) -> dict:
    url = f"{base_url.rstrip('/')}/api/conversations/sync"
    payload = json.dumps({'turns': turns, 'source': 'mac-client'}).encode('utf-8')
    req = request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data if isinstance(data, dict) else {'status': 'synced'}
    except (error.URLError, TimeoutError, ValueError):
        return {'status': 'sync_failed'}
