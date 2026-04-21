from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import DATA_DIR

WORKSPACES_DIR = DATA_DIR / "workspaces"
CHECKPOINT_FILE_NAME = "fetch_checkpoint.json"
LOCK = threading.RLock()


def _workspace_dir(workspace_id: str) -> Path:
    return WORKSPACES_DIR / str(workspace_id).strip()


def checkpoint_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / CHECKPOINT_FILE_NAME


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, path)


def load_checkpoint(workspace_id: str) -> dict[str, Any] | None:
    path = checkpoint_path(workspace_id)
    with LOCK:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    if not isinstance(payload, dict):
        return None
    payload["workspace_id"] = str(payload.get("workspace_id") or workspace_id)
    return payload


def save_checkpoint(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = checkpoint_path(workspace_id)
    checkpoint = dict(payload or {})
    checkpoint["workspace_id"] = workspace_id
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        _write_json_atomic(path, checkpoint)
    return checkpoint


def clear_checkpoint(workspace_id: str) -> None:
    path = checkpoint_path(workspace_id)
    with LOCK:
        path.unlink(missing_ok=True)


def is_checkpoint_compatible(checkpoint: dict[str, Any] | None, fingerprint: str) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return str(checkpoint.get("fingerprint") or "").strip() == str(fingerprint).strip()
