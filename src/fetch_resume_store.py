from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

LOCK = threading.RLock()
_CHECKPOINTS: dict[str, dict[str, Any]] = {}


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


def load_checkpoint(workspace_id: str) -> dict[str, Any] | None:
    with LOCK:
        payload = _CHECKPOINTS.get(str(workspace_id).strip())
        if not isinstance(payload, dict):
            return None
        return dict(payload)


def save_checkpoint(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    checkpoint = dict(payload or {})
    checkpoint["workspace_id"] = workspace_id
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        _CHECKPOINTS[str(workspace_id).strip()] = dict(checkpoint)
    return checkpoint


def clear_checkpoint(workspace_id: str) -> None:
    with LOCK:
        _CHECKPOINTS.pop(str(workspace_id).strip(), None)


def is_checkpoint_compatible(checkpoint: dict[str, Any] | None, fingerprint: str) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return str(checkpoint.get("fingerprint") or "").strip() == str(fingerprint).strip()


def export_state() -> dict[str, Any]:
    with LOCK:
        return {key: dict(value) for key, value in _CHECKPOINTS.items()}


def import_state(payload: dict[str, Any] | None) -> None:
    with LOCK:
        _CHECKPOINTS.clear()
        if not isinstance(payload, dict):
            return
        for workspace_id, checkpoint in payload.items():
            if isinstance(checkpoint, dict):
                _CHECKPOINTS[str(workspace_id).strip()] = dict(checkpoint)