from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import ACTIVE_DATA_DIR

LOCK = threading.RLock()
WORKSPACES_DIR = None
STATE_FILE = ACTIVE_DATA_DIR / "fetch_resume_state.json"
_DEFAULT_SCOPE = "anonymous"
_CHECKPOINTS_BY_SCOPE: dict[str, dict[str, dict[str, Any]]] = {}


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{datetime.now().timestamp():.0f}.{threading.get_ident()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _load_state_from_disk() -> None:
    if not STATE_FILE.exists():
        return
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    scopes = payload.get("scopes") if isinstance(payload.get("scopes"), dict) else payload
    if not isinstance(scopes, dict):
        return
    for scope, checkpoints in scopes.items():
        if not isinstance(checkpoints, dict):
            continue
        _CHECKPOINTS_BY_SCOPE[str(scope).strip() or _DEFAULT_SCOPE] = {
            str(workspace_id).strip(): dict(checkpoint)
            for workspace_id, checkpoint in checkpoints.items()
            if isinstance(checkpoint, dict)
        }


def _persist_state() -> None:
    payload = {
        "scopes": {
            scope: {key: dict(value) for key, value in checkpoints.items()}
            for scope, checkpoints in _CHECKPOINTS_BY_SCOPE.items()
        }
    }
    _atomic_write_text(STATE_FILE, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


_load_state_from_disk()


def _scope_key(client_id: str | None) -> str:
    key = str(client_id or "").strip()
    return key or _DEFAULT_SCOPE


def _scope_checkpoints(client_id: str | None) -> dict[str, dict[str, Any]]:
    scope = _scope_key(client_id)
    checkpoints = _CHECKPOINTS_BY_SCOPE.get(scope)
    if checkpoints is None:
        checkpoints = {}
        _CHECKPOINTS_BY_SCOPE[scope] = checkpoints
    return checkpoints


def build_fetch_fingerprint(
    *,
    keyword: str,
    countries: list[str],
    use_topic_mode: bool,
    language: str,
    country_keywords: dict[str, str],
) -> str:
    payload = {
        "keyword": str(keyword).strip(),
        "countries": [str(item).strip().upper() for item in countries],
        "use_topic_mode": bool(use_topic_mode),
        "language": str(language).strip().lower(),
        "country_keywords": {str(k).strip().upper(): str(v).strip() for k, v in sorted((country_keywords or {}).items())},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_checkpoint(workspace_id: str, client_id: str | None = None) -> dict[str, Any] | None:
    with LOCK:
        payload = _scope_checkpoints(client_id).get(str(workspace_id).strip())
        if not isinstance(payload, dict):
            return None
        return dict(payload)


def save_checkpoint(workspace_id: str, payload: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    checkpoint = dict(payload or {})
    checkpoint["workspace_id"] = workspace_id
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        _scope_checkpoints(client_id)[str(workspace_id).strip()] = dict(checkpoint)
        _persist_state()
    return checkpoint


def clear_checkpoint(workspace_id: str, client_id: str | None = None) -> None:
    with LOCK:
        _scope_checkpoints(client_id).pop(str(workspace_id).strip(), None)
        _persist_state()


def is_checkpoint_compatible(checkpoint: dict[str, Any] | None, fingerprint: str) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return str(checkpoint.get("fingerprint") or "").strip() == str(fingerprint).strip()


def export_state(client_id: str | None = None) -> dict[str, Any]:
    with LOCK:
        if client_id is not None:
            return {key: dict(value) for key, value in _scope_checkpoints(client_id).items()}
        return {
            scope: {key: dict(value) for key, value in checkpoints.items()}
            for scope, checkpoints in _CHECKPOINTS_BY_SCOPE.items()
        }


def import_state(payload: dict[str, Any] | None, client_id: str | None = None) -> None:
    with LOCK:
        if client_id is None:
            _CHECKPOINTS_BY_SCOPE.clear()
        checkpoints = _scope_checkpoints(client_id)
        checkpoints.clear()
        if not isinstance(payload, dict):
            _persist_state()
            return
        for workspace_id, checkpoint in payload.items():
            if isinstance(checkpoint, dict):
                checkpoints[str(workspace_id).strip()] = dict(checkpoint)
        _persist_state()