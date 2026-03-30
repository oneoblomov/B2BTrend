(function () {
  const STATUS_URL = "/api/fetch/status";
  const CANCEL_URL = "/api/fetch/cancel";
  const NOTIFICATION_STORAGE_KEY = "b2btrend-notifications";
  const MAX_NOTIFICATIONS = 30;
  const POLL_MS = 4000;
  const FINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
  const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

  const els = {
    dock: document.getElementById("global-job-dock"),
    badge: document.getElementById("global-job-badge"),
    label: document.getElementById("global-job-label"),
    status: document.getElementById("global-job-status"),
    meta: document.getElementById("global-job-meta"),
    cancelBtn: document.getElementById("global-job-cancel"),
    notificationToggle: document.getElementById("global-notification-toggle"),
    notificationCount: document.getElementById("global-notification-count"),
    notificationPanel: document.getElementById("global-notification-panel"),
    notificationList: document.getElementById("global-notification-list"),
    notificationClear: document.getElementById("global-notification-clear"),
    notificationClose: document.getElementById("global-notification-close"),
    localDot: document.getElementById("ws-indicator"),
    localLabel: document.getElementById("ws-label"),
  };

  const state = {
    snapshot: null,
    socket: null,
    reconnectTimer: null,
    pollTimer: null,
    lastSignature: "",
    notifications: loadNotifications(),
    panelOpen: false,
  };

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatDuration(milliseconds) {
    const totalSeconds = Math.max(0, Math.round(milliseconds / 1000));
    if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
      return "< 1 dk";
    }

    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (hours > 0) {
      return `${hours} sa ${minutes} dk`;
    }
    if (minutes > 0) {
      return `${minutes} dk ${seconds} sn`;
    }
    return `${seconds} sn`;
  }

  function estimateRemainingMs(job) {
    if (!job || !ACTIVE_STATUSES.has(job.status)) {
      return null;
    }

    const progress = jobProgress(job);
    if (!Number.isFinite(progress) || progress <= 0 || progress >= 1) {
      return null;
    }

    if (!job.started_at) {
      return null;
    }

    const startedAt = Date.parse(job.started_at);
    if (!Number.isFinite(startedAt)) {
      return null;
    }

    const elapsedMs = Date.now() - startedAt;
    if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) {
      return null;
    }

    return (elapsedMs * (1 - progress)) / progress;
  }

  function formatRemaining(job) {
    const remainingMs = estimateRemainingMs(job);
    if (remainingMs == null) {
      return "";
    }

    return `Tahmini kalan: ${formatDuration(remainingMs)}`;
  }

  function jobFromSnapshot(snapshot) {
    if (!snapshot) return null;
    return snapshot.active || snapshot.latest || null;
  }

  function jobProgress(job) {
    if (!job) return 0;
    if (Number.isFinite(Number(job.progress))) {
      return clamp(Number(job.progress), 0, 1);
    }
    const total = Number(job.total || 0);
    const completed = Number(job.completed || 0);
    if (total > 0) {
      return clamp(completed / total, 0, 1);
    }
    return job.status && ACTIVE_STATUSES.has(job.status) ? 0.12 : 1;
  }

  function loadNotifications() {
    try {
      const raw = window.localStorage.getItem(NOTIFICATION_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) {
        return [];
      }
      return parsed.map(normalizeNotification).filter(Boolean).slice(0, MAX_NOTIFICATIONS);
    } catch (_error) {
      return [];
    }
  }

  function persistNotifications() {
    try {
      window.localStorage.setItem(NOTIFICATION_STORAGE_KEY, JSON.stringify(state.notifications.slice(0, MAX_NOTIFICATIONS)));
    } catch (_error) {
      return;
    }
  }

  function normalizeNotification(item) {
    if (!item || typeof item !== "object") {
      return null;
    }

    const timestamp = item.timestamp || item.created_at || new Date().toISOString();
    const jobId = String(item.job_id || item.key || item.id || "").trim();
    const kind = String(item.kind || "info").trim();
    const status = String(item.status || "").trim();
    return {
      id: String(item.id || jobId || `note-${Date.now()}`),
      job_id: jobId,
      title: String(item.title || "Bildirim"),
      message: String(item.message || ""),
      kind,
      status,
      workspace_id: String(item.workspace_id || ""),
      progress: Number.isFinite(Number(item.progress)) ? clamp(Number(item.progress), 0, 1) : null,
      active: Boolean(item.active),
      seen: Boolean(item.seen),
      timestamp,
      updated_at: item.updated_at || timestamp,
      source: String(item.source || ""),
    };
  }

  function sortNotifications(items) {
    return items.slice().sort((a, b) => {
      const aActive = Boolean(a.active);
      const bActive = Boolean(b.active);
      if (aActive !== bActive) {
        return aActive ? -1 : 1;
      }
      return new Date(b.updated_at || b.timestamp).getTime() - new Date(a.updated_at || a.timestamp).getTime();
    });
  }

  function notificationCount() {
    return state.notifications.filter((item) => !item.seen || item.active).length;
  }

  function formatNotificationTime(value) {
    const date = new Date(value || Date.now());
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function notificationKindLabel(kind) {
    if (kind === "error") return "Hata";
    if (kind === "success") return "Basarili";
    if (kind === "warning") return "Uyari";
    if (kind === "running") return "Canli";
    return "Bilgi";
  }

  function renderNotifications() {
    if (els.notificationCount) {
      const count = notificationCount();
      els.notificationCount.textContent = count > 0 ? String(count) : "";
      els.notificationCount.classList.toggle("is-visible", count > 0);
    }

    if (!els.notificationList) {
      return;
    }

    const items = sortNotifications(state.notifications);
    if (!items.length) {
      els.notificationList.innerHTML = "<p class='notification-panel__empty'>Henüz bildirim yok.</p>";
      return;
    }

    els.notificationList.innerHTML = items
      .map((item) => {
        const progress = Number.isFinite(Number(item.progress)) ? clamp(Number(item.progress), 0, 1) : null;
        const percent = progress == null ? null : Math.round(progress * 100);
        const showProgress = item.active && percent != null;
        const cancelable = item.active && item.job_id && ACTIVE_STATUSES.has(item.status) && item.status !== "cancelling";

        return `
          <article class="notification-item notification-item--${escapeHtml(item.kind)} ${item.active ? "is-active" : ""}" data-job-id="${escapeHtml(item.job_id)}">
            <div class="notification-item__top">
              <span class="notification-item__kind">${escapeHtml(notificationKindLabel(item.kind))}</span>
              <span class="notification-item__time">${escapeHtml(formatNotificationTime(item.updated_at || item.timestamp))}</span>
            </div>
            <h4 class="notification-item__title">${escapeHtml(item.title)}</h4>
            <p class="notification-item__message">${escapeHtml(item.message)}</p>
            ${showProgress ? `<div class="notification-item__progress" aria-hidden="true"><span style="width:${percent}%"></span></div>` : ""}
            ${cancelable ? `<div class="notification-item__actions"><button type="button" class="btn btn-compact notification-item__cancel" data-cancel-job="${escapeHtml(item.job_id)}">Iptal Et</button></div>` : ""}
          </article>
        `;
      })
      .join("");
  }

  function setPanelOpen(open) {
    state.panelOpen = Boolean(open);
    if (els.notificationPanel) {
      els.notificationPanel.hidden = !state.panelOpen;
      els.notificationPanel.classList.toggle("is-open", state.panelOpen);
    }
    if (els.notificationToggle) {
      els.notificationToggle.setAttribute("aria-expanded", state.panelOpen ? "true" : "false");
    }
    if (state.panelOpen) {
      state.notifications = state.notifications.map((item) => ({
        ...item,
        seen: item.active ? false : true,
      }));
      persistNotifications();
      renderNotifications();
    }
  }

  function clearNotificationHistory() {
    state.notifications = state.notifications.filter((item) => item.active);
    persistNotifications();
    renderNotifications();
  }

  function togglePanel() {
    setPanelOpen(!state.panelOpen);
  }

  function upsertNotification(entry) {
    const item = normalizeNotification(entry);
    if (!item) {
      return null;
    }

    const existingIndex = item.job_id
      ? state.notifications.findIndex((current) => current.job_id && current.job_id === item.job_id)
      : -1;

    const merged = existingIndex >= 0
      ? {
          ...state.notifications[existingIndex],
          ...item,
          seen: item.active ? false : state.notifications[existingIndex].seen && item.seen,
          updated_at: item.updated_at || new Date().toISOString(),
        }
      : {
          ...item,
          seen: item.active ? false : false,
          updated_at: item.updated_at || new Date().toISOString(),
        };

    if (existingIndex >= 0) {
      state.notifications.splice(existingIndex, 1);
    }

    state.notifications.unshift(merged);
    state.notifications = sortNotifications(state.notifications).slice(0, MAX_NOTIFICATIONS);
    persistNotifications();
    renderNotifications();

    window.dispatchEvent(new CustomEvent("b2btrend:notification", { detail: merged }));
    return merged;
  }

  function pushGenericNotification({ title, message, kind = "info", workspace_id = "", source = "toast" }) {
    return upsertNotification({
      id: `note-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      title: title || "Bildirim",
      message: message || "",
      kind,
      workspace_id,
      source,
      active: false,
      seen: false,
      timestamp: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }

  function registerJobNotification(job, source) {
    if (!job || !job.job_id) {
      return null;
    }

    const active = ACTIVE_STATUSES.has(job.status);
    const kind = job.status === "failed" ? "error" : job.status === "cancelled" ? "warning" : job.status === "completed" ? "success" : active ? "running" : "info";
    const title = job.workspace_id ? `Workspace ${job.workspace_id}` : "Arkaplan veri cekimi";
    const message = job.message || (job.status === "cancelled" ? "Veri cekimi iptal edildi" : "Durum izleniyor");

    return upsertNotification({
      id: `job-${job.job_id}`,
      job_id: job.job_id,
      title,
      message,
      kind,
      status: job.status,
      workspace_id: job.workspace_id,
      progress: jobProgress(job),
      active,
      seen: false,
      timestamp: job.started_at || job.created_at || new Date().toISOString(),
      updated_at: job.updated_at || new Date().toISOString(),
      source,
    });
  }

  async function cancelCurrentJob() {
    const currentJob = jobFromSnapshot(state.snapshot);
    if (!currentJob || !ACTIVE_STATUSES.has(currentJob.status) || currentJob.status === "cancelling") {
      return;
    }

    if (els.cancelBtn) {
      els.cancelBtn.disabled = true;
      els.cancelBtn.textContent = "Iptal ediliyor...";
    }

    try {
      const response = await fetch(CANCEL_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: currentJob.workspace_id }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || "Iptal istegi basarisiz");
      }
      if (payload?.job) {
        emitSnapshot({ active: payload.job, latest: payload.job, has_active: true }, "cancel");
      }
    } catch (_error) {
      syncSnapshot("cancel-failed").catch(() => {});
    }
  }

  function jobStatusLabel(job) {
    if (!job) return "Hazir";
    if (job.status === "cancelling") return "Iptal ediliyor";
    if (job.status === "cancelled") return "Iptal edildi";
    if (job.status === "completed") return "Hazir";
    if (job.status === "failed") return "Kontrol gerekli";
    return "Takip aktif";
  }

  function render(job) {
    if (!els.dock) return;

    const currentJob = job;
    let status = currentJob?.status || "idle";
    const progress = jobProgress(currentJob);
    const percent = Math.round(progress * 100);
    let running = currentJob && ACTIVE_STATUSES.has(status);
    let cancelling = currentJob && status === "cancelling";

    if (cancelling && currentJob?.updated_at) {
      const updatedAt = Date.parse(currentJob.updated_at);
      if (!Number.isNaN(updatedAt) && Date.now() - updatedAt > 120000) {
        status = "cancelled";
        running = false;
        cancelling = false;
      }
    }
    const failed = currentJob && status === "failed";
    const completed = currentJob && status === "completed";
    const cancelled = currentJob && status === "cancelled";

    els.dock.dataset.state = running ? status : completed ? "completed" : failed ? "failed" : cancelled ? "cancelled" : "idle";
    els.dock.classList.toggle("is-running", running);
    els.dock.classList.toggle("is-cancelling", cancelling);
    els.dock.classList.toggle("is-complete", completed);
    els.dock.classList.toggle("is-failed", failed);
    els.dock.classList.toggle("is-cancelled", cancelled);
    els.dock.classList.toggle("is-idle", !currentJob);

    if (els.badge) {
      els.badge.textContent = running ? (cancelling ? "Iptal" : "Calisiyor") : completed ? "Tamamlandi" : failed ? "Hata" : cancelled ? "Iptal" : "Hazir";
    }

    if (els.label) {
      if (!currentJob) {
        els.label.textContent = "Arkaplan veri cekimi yok";
      } else if (running) {
        els.label.textContent = currentJob.message || (cancelling ? "Veri cekimi iptal ediliyor" : "Veri cekimi devam ediyor");
      } else if (completed) {
        els.label.textContent = currentJob.message || "Veri cekimi tamamlandi";
      } else if (failed) {
        els.label.textContent = currentJob.message || "Veri cekimi basarisiz";
      } else if (cancelled) {
        els.label.textContent = currentJob.message || "Veri cekimi iptal edildi";
      } else {
        els.label.textContent = currentJob.message || currentJob.phase || "Durum izleniyor";
      }
    }

    if (els.status) {
      const remaining = running && !cancelling ? formatRemaining(currentJob) : "";
      if (cancelling) {
        els.status.textContent = "Iptal ediliyor";
      } else {
        els.status.textContent = running
          ? remaining
            ? `${percent}% · ${remaining}`
            : `${percent}%`
          : completed
            ? "Hazir"
            : failed
              ? "Kontrol gerekli"
              : cancelled
                ? "Iptal edildi"
                : "Takip aktif";
      }
    }

    if (els.cancelBtn) {
      const allowCancel = running && !cancelling && !completed && !failed && !cancelled;
      els.cancelBtn.hidden = !allowCancel;
      els.cancelBtn.disabled = !allowCancel;
      if (allowCancel) {
        els.cancelBtn.textContent = "Iptal Et";
      }
    }

    if (els.meta) {
      els.meta.textContent = formatMeta(currentJob);
    }

    if (els.localDot) {
      els.localDot.classList.toggle("live", running);
    }

    if (els.localLabel) {
      const remaining = running && !cancelling ? formatRemaining(currentJob) : "";
      els.localLabel.textContent = running
        ? `${percent}% · ${currentJob.message || "Veri cekiliyor"}${remaining ? ` · ${remaining}` : ""}`
        : completed
          ? currentJob.message || "Veri cekimi tamamlandi"
          : failed
            ? currentJob.message || "Veri cekimi basarisiz"
            : cancelled
              ? currentJob.message || "Veri cekimi iptal edildi"
              : "Arkaplan veri cekimi izleniyor...";
    }
  }

  function formatMeta(job) {
    if (!job) {
      return "Sayfa yenilense de durum korunur.";
    }

    const parts = [];
    if (job.workspace_id) {
      parts.push(`Workspace: ${job.workspace_id}`);
    }
    if (job.status && ACTIVE_STATUSES.has(job.status)) {
      const total = Number(job.total || 0);
      const completed = Number(job.completed || 0);
      if (total > 0) {
        parts.push(`${completed}/${total}`);
      }
      const remaining = formatRemaining(job);
      if (remaining && job.status !== "cancelling") {
        parts.push(remaining);
      }
    } else if (job.result && typeof job.result === "object") {
      const cities = Number(job.result.cities || 0);
      const timeline = Number(job.result.timeline || 0);
      parts.push(`${cities} sehir, ${timeline} satir`);
    }
    if (job.updated_at) {
      parts.push(`Guncel: ${job.updated_at}`);
    }
    return parts.join(" · ") || "Sayfa yenilense de durum korunur.";
  }

  function signature(snapshot) {
    const job = jobFromSnapshot(snapshot);
    if (!job) return "idle";
    return [job.job_id, job.status, job.phase, job.completed, job.total, job.progress, job.message, job.updated_at].join("|");
  }

  function emitSnapshot(snapshot, source) {
    const currentJob = jobFromSnapshot(snapshot);
    const currentSignature = signature(snapshot);
    const previousSignature = state.lastSignature;

    state.snapshot = snapshot;
    state.lastSignature = currentSignature;
    render(currentJob);
    registerJobNotification(currentJob, source);

    window.dispatchEvent(
      new CustomEvent("b2btrend:job-state", {
        detail: {
          snapshot,
          job: currentJob,
          source,
          changed: currentSignature !== previousSignature,
        },
      })
    );

    if (currentJob) {
      const finalEvent = FINAL_STATUSES.has(currentJob.status) ? currentJob.status : null;
      if (finalEvent && currentSignature !== previousSignature) {
        window.dispatchEvent(
          new CustomEvent("b2btrend:job-final", {
            detail: { snapshot, job: currentJob, source, status: finalEvent },
          })
        );
      }
    }
  }

  async function syncSnapshot(source = "poll") {
    try {
      const response = await fetch(STATUS_URL, { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      emitSnapshot(payload, source);
    } catch (_error) {
      return;
    }
  }

  function connectSocket() {
    if (!window.WebSocket) return;
    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/status`);
    state.socket = socket;

    socket.addEventListener("message", (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (_error) {
        return;
      }

      window.dispatchEvent(new CustomEvent("b2btrend:realtime", { detail: payload }));

      if (payload?.type === "heartbeat") {
        try {
          socket.send("ping");
        } catch (_error) {
          return;
        }
        return;
      }

      if ((payload?.type === "fetch_job_update" || payload?.type === "fetch_cancel_requested" || payload?.type === "fetch_cancelled") && payload.state) {
        emitSnapshot({ active: payload.state, latest: payload.state, has_active: ACTIVE_STATUSES.has(payload.state.status) }, "socket");
        return;
      }

      if (payload?.type === "fetch_started" && payload.state) {
        emitSnapshot({ active: payload.state, latest: payload.state, has_active: true }, "socket");
        return;
      }

      if (payload?.type === "fetch_done" && payload.state) {
        emitSnapshot({ active: null, latest: payload.state, has_active: false }, "socket");
        return;
      }

      if (payload?.type === "fetch_failed" && payload.state) {
        emitSnapshot({ active: null, latest: payload.state, has_active: false }, "socket");
        return;
      }
    });

    socket.addEventListener("close", () => {
      state.socket = null;
      if (state.reconnectTimer) {
        window.clearTimeout(state.reconnectTimer);
      }
      state.reconnectTimer = window.setTimeout(connectSocket, 2000);
    });
  }

  function startPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
    }
    state.pollTimer = window.setInterval(() => {
      syncSnapshot("poll");
    }, POLL_MS);
  }

  function bindNotificationControls() {
    if (els.notificationToggle) {
      els.notificationToggle.addEventListener("click", togglePanel);
    }
    if (els.notificationClose) {
      els.notificationClose.addEventListener("click", () => setPanelOpen(false));
    }
    if (els.notificationClear) {
      els.notificationClear.addEventListener("click", clearNotificationHistory);
    }
    if (els.cancelBtn) {
      els.cancelBtn.addEventListener("click", cancelCurrentJob);
    }
    if (els.notificationList) {
      els.notificationList.addEventListener("click", (event) => {
        const cancelButton = event.target.closest("button[data-cancel-job]");
        if (!cancelButton) {
          return;
        }
        cancelCurrentJob();
      });
    }
    document.addEventListener("click", (event) => {
      if (!state.panelOpen) {
        return;
      }
      const withinPanel = event.target.closest(".notification-center");
      if (!withinPanel && !event.target.closest("#global-notification-toggle")) {
        setPanelOpen(false);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.panelOpen) {
        setPanelOpen(false);
      }
    });
  }

  function createNotificationApi() {
    window.B2BTrendNotify = {
      push: (entry) => {
        const payload = typeof entry === "string" ? { message: entry } : entry || {};
        return upsertNotification({
          id: payload.id || `note-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          title: payload.title || "Bildirim",
          message: payload.message || "",
          kind: payload.kind || "info",
          workspace_id: payload.workspace_id || "",
          source: payload.source || "toast",
          active: Boolean(payload.active),
          seen: Boolean(payload.seen),
          progress: payload.progress,
          job_id: payload.job_id || "",
          status: payload.status || "",
          timestamp: payload.timestamp || new Date().toISOString(),
          updated_at: payload.updated_at || new Date().toISOString(),
        });
      },
      getItems: () => state.notifications.slice(),
      refresh: () => renderNotifications(),
      clearHistory: () => clearNotificationHistory(),
      open: () => setPanelOpen(true),
      close: () => setPanelOpen(false),
      toggle: () => togglePanel(),
    };
  }

  function pushInitialBootNotification() {
    renderNotifications();
  }

  window.B2BTrendJobStatus = {
    refresh: () => syncSnapshot("manual"),
    getSnapshot: () => state.snapshot,
    cancel: () => cancelCurrentJob(),
  };

  createNotificationApi();
  bindNotificationControls();
  pushInitialBootNotification();
  window.setTimeout(() => syncSnapshot("boot"), 0);
  startPolling();
  connectSocket();
})();