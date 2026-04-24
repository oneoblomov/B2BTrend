from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import ACTIVE_DATA_DIR

ACTIVE_STATUSES = {"queued", "running", "cancelling"}
FINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_SCOPE = "anonymous"
STATE_FILE = ACTIVE_DATA_DIR / "fetch_job_state.json"


class JobConflictError(RuntimeError):
    def __init__(self, active_job: dict[str, Any]) -> None:
        super().__init__("Fetch already running")
        self.active_job = active_job


class FetchJobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_by_scope: dict[str, dict[str, Any]] = {}
        self._load_state_from_disk()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _default_state(self) -> dict[str, Any]:
        now = self._now()
        return {"active": None, "latest": None, "updated_at": now}

    def _scope_key(self, client_id: str | None) -> str:
        key = str(client_id or "").strip()
        return key or DEFAULT_SCOPE

    def _state_for(self, client_id: str | None) -> dict[str, Any]:
        scope = self._scope_key(client_id)
        state = self._state_by_scope.get(scope)
        if state is None:
            state = self._default_state()
            self._state_by_scope[scope] = state
        return state

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)

    def _load_state_from_disk(self) -> None:
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
        for scope, state in scopes.items():
            if not isinstance(state, dict):
                continue
            self._state_by_scope[str(scope).strip() or DEFAULT_SCOPE] = {
                "active": self._normalize_job(state.get("active")),
                "latest": self._normalize_job(state.get("latest")),
                "updated_at": str(state.get("updated_at") or self._now()),
            }

    def _persist_state(self) -> None:
        payload = {
            "scopes": {
                scope: deepcopy(state)
                for scope, state in self._state_by_scope.items()
            }
        }
        self._atomic_write_text(STATE_FILE, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    def _normalize_job(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(job, dict):
            return None
        normalized = deepcopy(job)
        normalized["countries"] = list(normalized.get("countries") or [])
        normalized["progress"] = max(0.0, min(1.0, float(normalized.get("progress") or 0.0)))
        normalized["completed"] = int(normalized.get("completed") or 0)
        normalized["total"] = int(normalized.get("total") or 0)
        normalized["status"] = str(normalized.get("status") or "queued")
        normalized["phase"] = str(normalized.get("phase") or normalized["status"])
        normalized["message"] = str(normalized.get("message") or "")
        normalized["workspace_id"] = str(normalized.get("workspace_id") or "")
        normalized["job_id"] = str(normalized.get("job_id") or "")
        normalized["cancel_requested"] = bool(normalized.get("cancel_requested") or False)
        normalized["resume_requested"] = bool(normalized.get("resume_requested") or False)
        normalized["resume_enabled"] = bool(normalized.get("resume_enabled") or False)
        return normalized

    def snapshot(self, client_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._state_for(client_id)
            active = self._normalize_job(state.get("active"))
            latest = self._normalize_job(state.get("latest"))
            return {
                "active": active,
                "latest": latest,
                "has_active": bool(active and active.get("status") in ACTIVE_STATUSES),
                "updated_at": state.get("updated_at") or self._now(),
            }

    def start_job(
        self,
        *,
        workspace_id: str,
        keyword: str,
        countries: list[str],
        use_topic_mode: bool,
        language: str,
        country_keywords: dict[str, str],
        resume_requested: bool = False,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._state_for(client_id)
            active = self._normalize_job(state.get("active"))
            if active and active.get("status") in ACTIVE_STATUSES:
                raise JobConflictError(active)

            now = self._now()
            job = {
                "job_id": uuid.uuid4().hex,
                "workspace_id": workspace_id,
                "keyword": keyword,
                "countries": list(countries),
                "use_topic_mode": bool(use_topic_mode),
                "language": language,
                "country_keywords": dict(country_keywords or {}),
                "status": "queued",
                "phase": "queued",
                "message": "Arkaplan veri cekimi bekliyor",
                "progress": 0.0,
                "completed": 0,
                "total": 0,
                "created_at": now,
                "started_at": None,
                "updated_at": now,
                "finished_at": None,
                "result": None,
                "error": None,
                "cancel_requested": False,
                "resume_requested": bool(resume_requested),
                "resume_enabled": False,
            }
            state["active"] = deepcopy(job)
            state["latest"] = deepcopy(job)
            state["updated_at"] = now
            self._persist_state()
            return deepcopy(job)

    def update_job(self, job_id: str, client_id: str | None = None, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            state = self._state_for(client_id)
            active = self._normalize_job(state.get("active"))
            latest = self._normalize_job(state.get("latest"))

            job: dict[str, Any] | None = None
            if active and active.get("job_id") == job_id:
                job = active
            elif latest and latest.get("job_id") == job_id:
                job = latest

            if job is None:
                return None

            if job.get("status") in FINAL_STATUSES:
                return deepcopy(job)

            for key, value in changes.items():
                job[key] = value

            now = self._now()
            job["updated_at"] = now
            if job.get("status") == "running" and not job.get("started_at"):
                job["started_at"] = now
            if job.get("status") in FINAL_STATUSES and not job.get("finished_at"):
                job["finished_at"] = now
            if job.get("status") in FINAL_STATUSES:
                state["active"] = None
                state["latest"] = deepcopy(job)
            else:
                state["active"] = deepcopy(job)
                state["latest"] = deepcopy(job)
            state["updated_at"] = now
            self._persist_state()
            return deepcopy(job)

    def request_cancel(self, job_id: str, client_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            state = self._state_for(client_id)
            active = self._normalize_job(state.get("active"))
            if not active or active.get("job_id") != job_id or active.get("status") not in ACTIVE_STATUSES:
                return None

            now = self._now()
            active["cancel_requested"] = True
            active["status"] = "cancelled"
            active["phase"] = "cancelled"
            active["message"] = "Veri cekimi iptal edildi"
            active["updated_at"] = now
            active["finished_at"] = now
            state["active"] = None
            state["latest"] = deepcopy(active)
            state["updated_at"] = now
            self._persist_state()
            return deepcopy(active)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        message: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "status": status,
            "phase": status,
            "message": message,
            "progress": 1.0,
            "finished_at": self._now(),
        }
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        return self.update_job(job_id, client_id=client_id, **payload)

    def export_state(self, client_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if client_id is not None:
                return deepcopy(self._state_for(client_id))
            return deepcopy(self._state_by_scope)

    def import_state(self, payload: dict[str, Any] | None, client_id: str | None = None) -> None:
        with self._lock:
            if not isinstance(payload, dict):
                if client_id is None:
                    self._state_by_scope = {}
                else:
                    self._state_by_scope[self._scope_key(client_id)] = self._default_state()
                self._persist_state()
                return
            state = {
                "active": self._normalize_job(payload.get("active")),
                "latest": self._normalize_job(payload.get("latest")),
                "updated_at": str(payload.get("updated_at") or self._now()),
            }
            if client_id is None:
                self._state_by_scope = {DEFAULT_SCOPE: state}
            else:
                self._state_by_scope[self._scope_key(client_id)] = state
            self._persist_state()


fetch_job_store = FetchJobStore()