const root = document.getElementById("app-root");
const seedWorkspaces = JSON.parse(root.dataset.workspaces || "[]");
const defaultWorkspaceId = root.dataset.defaultWorkspace || "";
const THEME_KEY = "b2btrend-theme";
const LAST_WORKSPACE_KEY = "b2btrend-last-workspace";

function resolveWorkspaceId(candidate, availableWorkspaces = seedWorkspaces) {
  if (candidate && availableWorkspaces.some((item) => item.id === candidate)) {
    return candidate;
  }

  const stored = localStorage.getItem(LAST_WORKSPACE_KEY);
  if (stored && availableWorkspaces.some((item) => item.id === stored)) {
    return stored;
  }

  return defaultWorkspaceId || (availableWorkspaces[0] ? availableWorkspaces[0].id : "");
}

function workspaceOptionLabel(item) {
  const modeLabel = item.use_topic_mode ? "Topic" : "Keyword";
  const keywordCount = Object.keys(item.country_keywords || {}).length;
  return `${item.name} · ${item.keyword} · ${modeLabel} · ${keywordCount} kural`;
}

const state = {
  workspaces: seedWorkspaces,
  selectedWorkspaceId: resolveWorkspaceId(defaultWorkspaceId),
  range: "all",
  selectedCountry: "",
  selectedCity: "",
  countryOptions: [],
  activeTab: "tab-map",
  activeSubTab: "overview",
  lastCompletedJobId: "",
};

const el = {
  workspaceSelect: document.getElementById("workspace-select"),
  rangeSelect: document.getElementById("range-select"),
  fetchBtn: document.getElementById("fetch-btn"),
  clearCacheBtn: document.getElementById("clear-cache-btn"),
  refreshBtn: document.getElementById("refresh-btn"),
  metricsRow: document.getElementById("metrics-row"),
  toast: document.getElementById("toast"),
  wsIndicator: document.getElementById("ws-indicator"),
  wsLabel: document.getElementById("ws-label"),
  newWorkspaceBtn: document.getElementById("new-workspace-btn"),
  wsDialog: document.getElementById("workspace-dialog"),
  wsForm: document.getElementById("workspace-form"),
  wsCancel: document.getElementById("workspace-cancel"),
  themeButtons: document.querySelectorAll(".theme-btn"),
  wsTopicMode: document.getElementById("ws-topic-mode"),
  wsCountryKeywords: document.getElementById("ws-country-keywords"),
  mapCountrySelect: document.getElementById("map-country-select"),
  mapModeSelect: document.getElementById("map-mode-select"),
  countryAnalysisSelect: document.getElementById("country-analysis-select"),
  cityCountrySelect: document.getElementById("city-country-select"),
  citySelect: document.getElementById("city-select"),
  hourlyCountrySelect: document.getElementById("hourly-country-select"),
  compareInput: document.getElementById("compare-input"),
  rawSearchInput: document.getElementById("raw-search-input"),
  relatedRefreshBtn: document.getElementById("related-refresh-btn"),
  hourlyRefreshBtn: document.getElementById("hourly-refresh-btn"),
  rankingRefreshBtn: document.getElementById("ranking-refresh-btn"),
  rawRefreshBtn: document.getElementById("raw-refresh-btn"),
  downloadCityBtn: document.getElementById("download-city-btn"),
  downloadTimelineBtn: document.getElementById("download-timeline-btn"),
  countryMetrics: document.getElementById("country-metrics"),
  cityMetrics: document.getElementById("city-metrics"),
  hourlyBestHours: document.getElementById("hourly-best-hours"),
};

function notify(text, kind = "info", persist = true) {
  if (persist && window.B2BTrendNotify?.push) {
    window.B2BTrendNotify.push({ title: kind === "error" ? "Hata" : "Bildirim", message: text, kind });
  }
  el.toast.textContent = text;
  el.toast.classList.add("show");
  setTimeout(() => el.toast.classList.remove("show"), 2200);
}

function setFetchButtonState(isBusy, label) {
  if (!el.fetchBtn) return;
  el.fetchBtn.disabled = Boolean(isBusy);
  el.fetchBtn.textContent = label || (isBusy ? "Cekiliyor..." : "Veri Cek");
}

function metricCard(label, value) {
  return `<article class="metric"><p>${label}</p><h3>${value}</h3></article>`;
}

function renderMetrics(metrics = {}) {
  el.metricsRow.innerHTML = [
    metricCard("Ulke", metrics.countries ?? 0),
    metricCard("Sehir", metrics.cities ?? 0),
    metricCard("Ortalama", metrics.avg_score ?? 0),
    metricCard("Best", `${metrics.best_country || "-"} (${metrics.best_country_score || 0})`),
  ].join("");
}

function clearDashboardPanels() {
  [
    "chart-world",
    "chart-drill",
    "chart-world-city",
    "chart-country-overview",
    "chart-country-change",
    "chart-country-stl",
    "chart-country-vol",
    "chart-country-forecast",
    "chart-country-corr",
    "chart-city-trend",
    "chart-hourly-avg",
    "chart-hourly-heatmap",
    "chart-ranking-rising",
    "chart-ranking-falling",
    "chart-ranking-compare",
  ].forEach((targetId) => plot(targetId, null));

  renderTable("#drill-table", [], ["city", "score", "geo_code"]);
  renderTable("#related-queries-top", [], ["query", "value"]);
  renderTable("#related-queries-rising", [], ["query", "value"]);
  renderTable("#related-topics-top", [], ["topic_title", "value"]);
  renderTable("#related-topics-rising", [], ["topic_title", "value"]);
  renderTable("#city-ranking-table", [], ["rank", "city", "score", "geo_code"]);
  renderTable("#raw-city-table", [], ["country", "city", "geo_code", "score"]);
  renderTable("#raw-timeline-table", [], ["country", "city", "date", "score"]);

  if (el.countryMetrics) el.countryMetrics.innerHTML = "";
  if (el.cityMetrics) el.cityMetrics.innerHTML = "";
  if (el.hourlyBestHours) el.hourlyBestHours.textContent = "";
}

function setSelectOptions(selectEl, items, getValue, getLabel, selected) {
  if (!selectEl) return;
  selectEl.innerHTML = "";
  items.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = getValue(item);
    opt.textContent = getLabel(item);
    selectEl.appendChild(opt);
  });
  if (selected) {
    selectEl.value = selected;
  }
}

function renderTable(tbodyId, rows, cols) {
  const tbody = document.querySelector(`${tbodyId} tbody`) || document.querySelector(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    cols.forEach((col) => {
      const td = document.createElement("td");
      td.textContent = row[col] ?? "";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function plot(targetId, fig) {
  const target = document.getElementById(targetId);
  if (!target) return;
  if (!fig || !fig.data) {
    target.innerHTML = "<p class='inline-note'>Veri yok</p>";
    return;
  }

  const currentTheme = document.body.getAttribute("data-theme") || "system";
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const darkMode = currentTheme === "dark" || (currentTheme === "system" && prefersDark);

  const rootStyles = getComputedStyle(document.documentElement);
  const textColor = rootStyles.getPropertyValue("--ink").trim() || (darkMode ? "#edf2fb" : "#101329");
  const lineColor = rootStyles.getPropertyValue("--line").trim() || (darkMode ? "#2d3748" : "#d7e6e2");
  const paperColor = rootStyles.getPropertyValue("--paper").trim() || (darkMode ? "#1a1f26" : "#ffffff");

  const layout = Object.assign({}, fig.layout || {}, {
    plot_bgcolor: paperColor,
    paper_bgcolor: paperColor,
    font: Object.assign({}, (fig.layout || {}).font || {}, { color: textColor }),
    xaxis: Object.assign({}, (fig.layout || {}).xaxis || {}, {
      gridcolor: lineColor,
      zerolinecolor: lineColor,
      color: textColor,
    }),
    yaxis: Object.assign({}, (fig.layout || {}).yaxis || {}, {
      gridcolor: lineColor,
      zerolinecolor: lineColor,
      color: textColor,
    }),
    legend: Object.assign({}, (fig.layout || {}).legend || {}, {
      font: Object.assign({}, ((fig.layout || {}).legend || {}).font || {}, { color: textColor }),
    }),
  });

  Plotly.react(target, fig.data, layout, {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d"],
  });
}

function toQuery(params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && `${v}` !== "") q.set(k, v);
  });
  return q.toString();
}

function parseCountryKeywordsText(rawText) {
  const out = {};
  (rawText || "").split(/\r?\n/).forEach((line) => {
    const val = line.trim();
    if (!val || !val.includes(":")) return;
    const [country, ...rest] = val.split(":");
    const cc = (country || "").trim().toUpperCase();
    const keyword = rest.join(":").trim();
    if (cc && keyword) out[cc] = keyword;
  });
  return out;
}

function applyTheme(theme) {
  document.body.setAttribute("data-theme", theme);

  if (el.themeButtons && el.themeButtons.length > 0) {
    el.themeButtons.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.theme === theme);
    });
  }
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "system";
  applyTheme(saved);
}

function toggleCountryKeywordInput() {
  const isTopicMode = Boolean(el.wsTopicMode?.checked);
  if (el.wsCountryKeywords) {
    el.wsCountryKeywords.disabled = isTopicMode;
    if (isTopicMode) {
      el.wsCountryKeywords.placeholder = "Topic modu acik oldugu icin ulkeye ozel keyword kapatildi";
    } else {
      el.wsCountryKeywords.placeholder = "TR:tavuk\nUS:chicken\nDE:huhn";
    }
  }
}

function readUrlState() {
  const p = new URLSearchParams(window.location.search);
  const tab = p.get("tab");
  const ws = p.get("ws");
  const country = p.get("country");
  const subtab = p.get("subtab");
  if (tab) state.activeTab = tab;
  if (ws) state.selectedWorkspaceId = ws;
  if (country) state.selectedCountry = country;
  if (subtab) state.activeSubTab = subtab;
}

function writeUrlState() {
  const p = new URLSearchParams(window.location.search);
  p.set("tab", state.activeTab);
  if (state.selectedWorkspaceId) p.set("ws", state.selectedWorkspaceId);
  if (state.selectedCountry) p.set("country", state.selectedCountry);
  if (state.activeSubTab && state.activeSubTab !== "overview") p.set("subtab", state.activeSubTab);
  const newUrl = `${window.location.pathname}?${p.toString()}`;
  window.history.replaceState({}, "", newUrl);
}

async function getJSON(path, params = {}) {
  const res = await fetch(`${path}?${toQuery(params)}`);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `Request failed: ${path}`);
  }
  return res.json();
}

async function loadWorkspaces() {
  const payload = await getJSON("/api/workspaces");
  state.workspaces = payload.items || [];
  if (state.selectedWorkspaceId && !state.workspaces.some((item) => item.id === state.selectedWorkspaceId)) {
    state.selectedWorkspaceId = resolveWorkspaceId(state.selectedWorkspaceId, state.workspaces);
  }
  if (!state.selectedWorkspaceId && state.workspaces[0]) {
    state.selectedWorkspaceId = state.workspaces[0].id;
  }

  if (state.selectedWorkspaceId) {
    localStorage.setItem(LAST_WORKSPACE_KEY, state.selectedWorkspaceId);
  }

  setSelectOptions(
    el.workspaceSelect,
    state.workspaces,
    (x) => x.id,
    workspaceOptionLabel,
    state.selectedWorkspaceId
  );
}

function syncCountrySelectors(countries) {
  setSelectOptions(el.mapCountrySelect, countries, (x) => x.country, (x) => `${x.country} - ${x.country_name}`, state.selectedCountry);
  setSelectOptions(el.countryAnalysisSelect, countries, (x) => x.country, (x) => `${x.country} - ${x.country_name}`, state.selectedCountry);
  setSelectOptions(el.cityCountrySelect, countries, (x) => x.country, (x) => `${x.country} - ${x.country_name}`, state.selectedCountry);
  setSelectOptions(el.hourlyCountrySelect, countries, (x) => x.country, (x) => `${x.country} - ${x.country_name}`, state.selectedCountry);
}

async function loadOverview() {
  const payload = await getJSON("/api/dashboard/overview", {
    workspace_id: state.selectedWorkspaceId,
    range: state.range,
    country: state.selectedCountry,
  });

  if (!payload.has_data) {
    renderMetrics({});
    clearDashboardPanels();
    const activeJob = window.B2BTrendJobStatus?.getSnapshot?.()?.active || null;
    const jobRunning = activeJob && (activeJob.status === "queued" || activeJob.status === "running" || activeJob.status === "cancelling");
    if (!jobRunning) {
      notify(payload.message || "Veri yok", "info", false);
    }
    return;
  }

  state.selectedCountry = payload.selected_country;
  state.countryOptions = payload.country_options || [];
  renderMetrics(payload.metrics);
  syncCountrySelectors(payload.country_options || []);

  plot("chart-world", payload.charts.world);
  plot("chart-drill", payload.charts.drill_city);
  plot("chart-world-city", payload.charts.world_city);
  renderTable("#drill-table", payload.charts.drill_table || [], ["city", "score", "geo_code"]);
  bindWorldMapClick();
  writeUrlState();

  await Promise.all([
    loadCountryAnalysis(),
    loadCityAnalysis(),
    loadHourly(),
    loadRanking(),
    loadRaw(),
    loadRelated(),
  ]);
}

function bindWorldMapClick() {
  const worldEl = document.getElementById("chart-world");
  if (!worldEl || typeof worldEl.on !== "function") return;

  if (typeof worldEl.removeAllListeners === "function") {
    worldEl.removeAllListeners("plotly_click");
  }

  worldEl.on("plotly_click", async (evt) => {
    const location = evt?.points?.[0]?.location;
    if (!location) return;

    const found = state.countryOptions.find((x) => String(x.iso3 || "").toUpperCase() === String(location).toUpperCase());
    if (!found) return;

    state.selectedCountry = found.country;
    syncCountrySelectors(state.countryOptions);
    await loadOverview();
  });
}

async function loadCountryAnalysis() {
  if (!state.selectedCountry) return;
  const payload = await getJSON("/api/dashboard/country", {
    workspace_id: state.selectedWorkspaceId,
    country: state.selectedCountry,
    range: state.range,
  });

  if (!payload.has_data) return;

  const s = payload.stats || {};
  const sig = s.signal || {};
  const scores = s.scores || {};
  const strength = s.strength || {};

  el.countryMetrics.innerHTML = [
    metricCard("Trend", sig.label || "-"),
    metricCard("7d Avg", scores.avg_7d ?? 0),
    metricCard("30d Avg", scores.avg_30d ?? 0),
    metricCard("Growth", `${scores.growth_rate ?? 0}%`),
    metricCard("Strength", `${strength.score ?? 0} / 100`),
  ].join("");

  plot("chart-country-overview", payload.charts.overview);
  plot("chart-country-change", payload.charts.change_points);
  plot("chart-country-stl", payload.charts.stl);
  plot("chart-country-vol", payload.charts.volatility);
  plot("chart-country-forecast", payload.charts.forecast);
  plot("chart-country-corr", payload.charts.correlation);
}

async function loadCityAnalysis() {
  if (!state.selectedCountry) return;
  const payload = await getJSON("/api/dashboard/city", {
    workspace_id: state.selectedWorkspaceId,
    country: state.selectedCountry,
    city: state.selectedCity,
    range: state.range,
  });

  if (!payload.has_data) return;

  state.selectedCity = payload.city || "";
  setSelectOptions(el.citySelect, payload.city_options || [], (x) => x, (x) => x, state.selectedCity);

  const stats = payload.stats || {};
  const sig = stats.signal || {};
  const score = stats.scores || {};
  const st = stats.strength || {};

  el.cityMetrics.innerHTML = [
    metricCard("Trend", sig.label || "-"),
    metricCard("Slope", sig.slope ?? 0),
    metricCard("Volatility", sig.volatility ?? 0),
    metricCard("Growth", `${score.growth_rate ?? 0}%`),
    metricCard("Strength", `${st.score ?? 0} / 100`),
  ].join("");

  plot("chart-city-trend", payload.charts.city_trend);
  renderTable("#city-ranking-table", payload.ranking || [], ["rank", "city", "score", "geo_code"]);
}

async function loadHourly() {
  if (!state.selectedCountry) return;
  const payload = await getJSON("/api/dashboard/hourly", {
    workspace_id: state.selectedWorkspaceId,
    country: state.selectedCountry,
  });

  if (!payload.has_data) {
    el.hourlyBestHours.textContent = payload.message || "Saatlik veri yok";
    return;
  }

  el.hourlyBestHours.textContent = payload.best_hours_text || "";
  plot("chart-hourly-avg", payload.charts.avg_hour);
  plot("chart-hourly-heatmap", payload.charts.heatmap);
}

async function loadRanking() {
  const payload = await getJSON("/api/dashboard/ranking", {
    workspace_id: state.selectedWorkspaceId,
    compare: (el.compareInput.value || "").trim(),
    range: state.range,
  });
  if (!payload.has_data) return;

  plot("chart-ranking-rising", payload.charts.rising);
  plot("chart-ranking-falling", payload.charts.falling);
  plot("chart-ranking-compare", payload.charts.compare);
}

async function loadRaw() {
  const payload = await getJSON("/api/dashboard/raw", {
    workspace_id: state.selectedWorkspaceId,
    search: (el.rawSearchInput.value || "").trim(),
    range: state.range,
  });
  if (!payload.has_data) return;

  renderTable("#raw-city-table", payload.city || [], ["country", "city", "geo_code", "score"]);
  renderTable("#raw-timeline-table", payload.timeline || [], ["country", "city", "date", "score"]);
}

async function loadRelated() {
  if (!state.selectedCountry) return;
  const payload = await getJSON("/api/dashboard/related", {
    workspace_id: state.selectedWorkspaceId,
    country: state.selectedCountry,
  });

  renderTable("#related-queries-top", payload.queries.top || [], ["query", "value"]);
  renderTable("#related-queries-rising", payload.queries.rising || [], ["query", "value"]);
  renderTable("#related-topics-top", payload.topics.top || [], ["topic_title", "value"]);
  renderTable("#related-topics-rising", payload.topics.rising || [], ["topic_title", "value"]);
}

async function fetchDataset() {
  if (!state.selectedWorkspaceId) {
    notify("Workspace secin", "warning", false);
    return;
  }

  setFetchButtonState(true, "Arkaplanda calisiyor...");

  try {
    const res = await fetch("/api/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: state.selectedWorkspaceId }),
    });
    const payload = await res.json();
    if (!res.ok) {
      if (res.status === 409 && payload.job) {
        const activeWs = payload.job.workspace_id || "bilinmiyor";
        const activeStatus = payload.job.status || "running";
        if (activeWs === state.selectedWorkspaceId) {
          notify(`Bu workspace icin veri cekimi zaten calisiyor (${activeStatus})`);
        } else {
          notify(`Su anda baska bir workspace icin cekim calisiyor: ${activeWs} (${activeStatus})`);
        }
        return;
      }
      throw new Error(payload.detail || "Fetch error");
    }
    notify("Veri cekimi arkaplanda baslatildi");
  } catch (err) {
    notify(`Hata: ${err.message}`);
    setFetchButtonState(false, "Veri Cek");
  }
}

async function clearCache() {
  const res = await fetch("/api/cache/clear", { method: "POST" });
  const payload = await res.json();
  if (res.ok) {
    notify(`Cache temizlendi: ${payload.deleted}`);
  } else {
    notify("Cache temizleme hatasi");
  }
}

function handleRealtimeEvent(event) {
  const payload = event.detail || {};
  if (!payload.type) return;

  if (payload.type === "workspace_created" || payload.type === "workspace_updated" || payload.type === "workspace_deleted") {
    loadWorkspaces().catch(() => {});
  }

  if (payload.type === "cache_cleared") {
    notify("Cache temizlendi");
  }
}

async function handleJobSnapshot(snapshot, source = "event") {
  const job = snapshot?.active || snapshot?.latest || null;
  if (!job) {
    if (state.selectedWorkspaceId) {
      setFetchButtonState(false, "Veri Cek");
    }
    return;
  }

  const percent = Math.round((Number(job.progress || 0) || 0) * 100);
  const isCurrentWorkspace = job.workspace_id === state.selectedWorkspaceId;
  const isRunning = job.status === "queued" || job.status === "running" || job.status === "cancelling";

  if (isRunning) {
    setFetchButtonState(true, isCurrentWorkspace ? (job.status === "cancelling" ? "Iptal ediliyor..." : `Cekiliyor... ${percent}%`) : "Baska bir cekim calisiyor");
  } else {
    setFetchButtonState(false, "Veri Cek");
  }

  const isFinal = job.status === "completed" || job.status === "failed" || job.status === "cancelled";

  if (isFinal && job.job_id !== state.lastCompletedJobId) {
    state.lastCompletedJobId = job.job_id;
    if (isCurrentWorkspace) {
      if (source !== "bootstrap" && job.status === "completed") {
        notify(`Veri guncellendi: ${job.result?.cities ?? 0} sehir`, "info", false);
      } else if (source !== "bootstrap" && job.status === "cancelled") {
        notify(job.message || "Veri cekimi iptal edildi", "warning", false);
      } else if (source !== "bootstrap" && job.status === "failed") {
        notify(job.message || job.error || "Veri cekimi basarisiz", "error", false);
      }
      if (job.status === "completed") {
        await loadWorkspaces();
        await loadOverview();
      }
    } else if (source === "event") {
      await loadWorkspaces();
    }
  }

  if (job.status === "cancelled") {
    setFetchButtonState(false, "Veri Cek");
  }
}

async function createWorkspaceFromDialog(event) {
  event.preventDefault();

  const name = document.getElementById("ws-name").value.trim();
  const keyword = document.getElementById("ws-keyword").value.trim();
  const countries = (document.getElementById("ws-countries").value || "")
    .split(",")
    .map((x) => x.trim().toUpperCase())
    .filter(Boolean);
  const useTopicMode = Boolean(el.wsTopicMode?.checked);
  const countryKeywords = parseCountryKeywordsText(el.wsCountryKeywords?.value || "");

  if (!name || !keyword || countries.length === 0) {
    notify("Tum alanlari doldurun", "warning", false);
    return;
  }

  const res = await fetch("/api/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, keyword, countries, use_topic_mode: useTopicMode, country_keywords: countryKeywords }),
  });

  const payload = await res.json();
  if (!res.ok) {
    notify("Workspace olusturulamadi");
    return;
  }

  state.selectedWorkspaceId = payload.item.id;
  localStorage.setItem(LAST_WORKSPACE_KEY, state.selectedWorkspaceId);
  el.wsDialog.close();
  await loadWorkspaces();
  await loadOverview();
  notify("Workspace olusturuldu");
}

function resizeVisibleCharts() {
  document.querySelectorAll('.tab-panel.is-active .plot-box').forEach((chartEl) => {
    if (window.Plotly && chartEl && chartEl.offsetWidth > 0 && chartEl.offsetHeight > 0) {
      try {
        Plotly.Plots.resize(chartEl);
      } catch (err) {
        // ignore charts that are not initialized yet
      }
    }
  });
}

function activateTab(id) {
  state.activeTab = id;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === id);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === id);
  });
  if (id === "tab-country") {
    activateSubTab(state.activeSubTab);
  }
  writeUrlState();
  // resize Plotly charts after activating a tab
  setTimeout(resizeVisibleCharts, 50);
}

function activateSubTab(id) {
  state.activeSubTab = id;
  document.querySelectorAll(".sub-tab-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.subtab === id);
  });
  document.querySelectorAll(".group-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.subtab === id);
  });
  setTimeout(resizeVisibleCharts, 50);
}

function bindEvents() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  document.querySelectorAll(".sub-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateSubTab(btn.dataset.subtab));
  });

  el.workspaceSelect.addEventListener("change", async (e) => {
    state.selectedWorkspaceId = e.target.value;
    localStorage.setItem(LAST_WORKSPACE_KEY, state.selectedWorkspaceId);
    state.selectedCountry = "";
    state.selectedCity = "";
    writeUrlState();
    await loadOverview();
  });

  el.rangeSelect.addEventListener("change", async (e) => {
    state.range = e.target.value;
    await loadOverview();
  });

  el.mapCountrySelect.addEventListener("change", async (e) => {
    state.selectedCountry = e.target.value;
    writeUrlState();
    await loadOverview();
  });

  el.countryAnalysisSelect.addEventListener("change", async (e) => {
    state.selectedCountry = e.target.value;
    writeUrlState();
    await Promise.all([loadCountryAnalysis(), loadRelated(), loadCityAnalysis(), loadHourly()]);
  });

  el.cityCountrySelect.addEventListener("change", async (e) => {
    state.selectedCountry = e.target.value;
    state.selectedCity = "";
    writeUrlState();
    await loadCityAnalysis();
  });

  el.citySelect.addEventListener("change", async (e) => {
    state.selectedCity = e.target.value;
    await loadCityAnalysis();
  });

  el.hourlyCountrySelect.addEventListener("change", async (e) => {
    state.selectedCountry = e.target.value;
    await loadHourly();
  });

  el.mapModeSelect.addEventListener("change", (e) => {
    const worldCity = document.getElementById("chart-world-city");
    const splitCharts = document.querySelector("#tab-map .grid-2");
    if (e.target.value === "world-city") {
      worldCity.classList.remove("hidden");
      splitCharts.classList.add("hidden");
    } else {
      worldCity.classList.add("hidden");
      splitCharts.classList.remove("hidden");
    }
    setTimeout(resizeVisibleCharts, 50);
  });

  el.fetchBtn.addEventListener("click", fetchDataset);
  el.clearCacheBtn.addEventListener("click", clearCache);
  el.refreshBtn.addEventListener("click", loadOverview);

  el.relatedRefreshBtn.addEventListener("click", loadRelated);
  el.hourlyRefreshBtn.addEventListener("click", loadHourly);
  el.rankingRefreshBtn.addEventListener("click", loadRanking);
  el.rawRefreshBtn.addEventListener("click", loadRaw);

  el.downloadCityBtn.addEventListener("click", () => {
    const q = toQuery({ workspace_id: state.selectedWorkspaceId, dataset: "city", range: state.range });
    window.open(`/api/export/csv?${q}`, "_blank");
  });

  window.addEventListener("resize", () => {
    setTimeout(resizeVisibleCharts, 20);
  });

  el.downloadTimelineBtn.addEventListener("click", () => {
    const q = toQuery({ workspace_id: state.selectedWorkspaceId, dataset: "timeline", range: state.range });
    window.open(`/api/export/csv?${q}`, "_blank");
  });

  el.newWorkspaceBtn.addEventListener("click", () => {
    document.getElementById("ws-id").value = "";
    document.getElementById("ws-name").value = "";
    document.getElementById("ws-keyword").value = "/m/02vqb5x";
    document.getElementById("ws-countries").value = "TR,US,DE";
    if (el.wsTopicMode) el.wsTopicMode.checked = false;
    if (el.wsCountryKeywords) el.wsCountryKeywords.value = "";
    toggleCountryKeywordInput();
    el.wsDialog.showModal();
  });
  el.wsCancel.addEventListener("click", () => el.wsDialog.close());
  el.wsTopicMode?.addEventListener("change", toggleCountryKeywordInput);

  if (el.themeButtons && el.themeButtons.length > 0) {
    el.themeButtons.forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const mode = e.currentTarget.dataset.theme || "system";
        localStorage.setItem(THEME_KEY, mode);
        applyTheme(mode);
      });
    });
  }

  el.wsForm.addEventListener("submit", createWorkspaceFromDialog);
}

(async function bootstrap() {
  try {
    initTheme();
    toggleCountryKeywordInput();
    readUrlState();
    const hasWorkspaceQuery = new URLSearchParams(window.location.search).has("ws");
    if (hasWorkspaceQuery) {
      state.selectedWorkspaceId = resolveWorkspaceId(state.selectedWorkspaceId);
    } else {
      state.selectedWorkspaceId = resolveWorkspaceId(localStorage.getItem(LAST_WORKSPACE_KEY) || "");
    }
    bindEvents();
    window.addEventListener("b2btrend:realtime", handleRealtimeEvent);
    window.addEventListener("b2btrend:job-state", (event) => {
      handleJobSnapshot(event.detail?.snapshot || {}, event.detail?.source || "event").catch(() => {});
    });
    const initialSnapshot = window.B2BTrendJobStatus?.getSnapshot?.();
    if (initialSnapshot) {
      await handleJobSnapshot(initialSnapshot, "bootstrap");
    }
    await loadWorkspaces();
    await loadOverview();
    activateTab(state.activeTab || "tab-map");
  } catch (err) {
    notify(`Baslatma hatasi: ${err.message}`);
  }
})();
