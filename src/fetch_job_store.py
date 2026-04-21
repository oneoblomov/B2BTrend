from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import ACTIVE_DATA_DIR

FETCH_JOB_STATE_FILE = ACTIVE_DATA_DIR / "fetch_job_state.json"
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
FINAL_STATUSES = {"completed", "failed", "cancelled"}
STALE_ACTIVE_MAX_AGE = timedelta(minutes=20)
STALE_CANCELLING_MAX_AGE = timedelta(minutes=2)


class JobConflictError(RuntimeError):
    def __init__(self, active_job: dict[str, Any]) -> None:
        super().__init__("Fetch already running")
        self.active_job = active_job


class FetchJobStore:
    def __init__(self, path: Path = FETCH_JOB_STATE_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _default_state(self) -> dict[str, Any]:
        now = self._now()
        return {"active": None, "latest": None, "updated_at": now}

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._default_state()
        if not isinstance(payload, dict):
            return self._default_state()
        payload.setdefault("active", None)
        payload.setdefault("latest", None)
        payload["updated_at"] = str(payload.get("updated_at") or self._now())
        return payload

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.path)

    def _parse_iso(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _recover_stale_active_job(self, state: dict[str, Any]) -> bool:
        active = self._normalize_job(state.get("active"))
        if not active or active.get("status") not in ACTIVE_STATUSES:
            return False

        now_dt = datetime.now()
        updated_dt = self._parse_iso(active.get("updated_at")) or self._parse_iso(active.get("started_at")) or self._parse_iso(active.get("created_at"))
        if updated_dt is None:
            return False

        age = now_dt - updated_dt
        max_age = STALE_CANCELLING_MAX_AGE if active.get("status") == "cancelling" else STALE_ACTIVE_MAX_AGE
        if age <= max_age:
            return False

        now = self._now()
        if active.get("status") == "cancelling":
            active["status"] = "cancelled"
            active["phase"] = "cancelled"
            active["message"] = "Veri cekimi iptal edildi (zaman asimi nedeniyle otomatik kapatildi)."
        else:
            active["status"] = "failed"
            active["phase"] = "failed"
            active["message"] = "Veri cekimi beklenmedik sekilde durdu ve otomatik temizlendi."
            active["error"] = "stale-active-job"

        active["progress"] = 1.0
        active["cancel_requested"] = bool(active.get("cancel_requested") or active.get("status") == "cancelled")
        active["finished_at"] = now
        active["updated_at"] = now

        state["active"] = None
        state["latest"] = deepcopy(active)
        state["updated_at"] = now
        return True

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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            if self._recover_stale_active_job(state):
                self._write_state(state)
            active = self._normalize_job(state.get("active"))
            latest = self._normalize_job(state.get("latest"))
            if active is None and latest is not None and latest.get("status") in ACTIVE_STATUSES:
                active = deepcopy(latest)
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
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            if self._recover_stale_active_job(state):
                self._write_state(state)
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
            self._write_state(state)
            return deepcopy(job)

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            state = self._read_state()
            if self._recover_stale_active_job(state):
                self._write_state(state)
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
            self._write_state(state)
            return deepcopy(job)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read_state()
            if self._recover_stale_active_job(state):
                self._write_state(state)
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
            self._write_state(state)
            return deepcopy(active)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        message: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "status": status,
            "phase": status,
            "message": message,
            "progress": 1.0,
            "result": result,
            "error": error,
        }
        return self.update_job(job_id, **payload)
