const root = document.getElementById("workspace-root");
const grid = document.getElementById("workspace-grid");
const toast = document.getElementById("toast");
const newBtn = document.getElementById("ws-new-btn");
const dialog = document.getElementById("ws-dialog");
const form = document.getElementById("ws-form");
const cancelBtn = document.getElementById("ws-cancel");
const formTitle = document.getElementById("ws-form-title");
const themeButtons = document.querySelectorAll(".theme-btn");

const inputId = document.getElementById("ws-id");
const inputName = document.getElementById("ws-name");
const inputKeyword = document.getElementById("ws-keyword");
const inputCountries = document.getElementById("ws-countries");
const inputTopicMode = document.getElementById("ws-topic-mode");
const inputCountryKeywords = document.getElementById("ws-country-keywords");

const THEME_KEY = "b2btrend-theme";
const LAST_WORKSPACE_KEY = "b2btrend-last-workspace";

const state = {
  workspaces: JSON.parse(root.dataset.workspaces || "[]"),
};

function notify(text, kind = "info", persist = true) {
  if (persist && window.B2BTrendNotify?.push) {
    window.B2BTrendNotify.push({ title: kind === "error" ? "Hata" : "Bildirim", message: text, kind });
  }
  toast.textContent = text;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function countriesText(countries) {
  return (countries || []).slice(0, 6).join(", ") + ((countries || []).length > 6 ? " ..." : "");
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

function formatCountryKeywords(map) {
  const obj = map || {};
  return Object.keys(obj)
    .sort()
    .map((k) => `${k}:${obj[k]}`)
    .join("\n");
}

function workspaceCardLabel(ws) {
  const modeLabel = ws.use_topic_mode ? "Google Topic ID modu" : "Keyword modu";
  const keywordCount = Object.keys(ws.country_keywords || {}).length;
  return `${modeLabel} · ${keywordCount} ulke kural`;
}

function applyTheme(theme) {
  document.body.setAttribute("data-theme", theme);
  if (themeButtons && themeButtons.length > 0) {
    themeButtons.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.theme === theme);
    });
  }
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "system";
  applyTheme(saved);
}

function toggleCountryKeywordsInput() {
  const isTopicMode = Boolean(inputTopicMode?.checked);
  if (inputCountryKeywords) {
    inputCountryKeywords.disabled = isTopicMode;
    if (isTopicMode) {
      inputCountryKeywords.placeholder = "Topic modu acik: ulkeye ozel keyword gerekmez";
    } else {
      inputCountryKeywords.placeholder = "TR:tavuk\nUS:chicken\nDE:huhn";
    }
  }
}

function card(ws) {
  const avg = Number(ws.stats?.avg_score || 0).toFixed(1);
  return `
    <article class="ws-card" data-workspace-id="${ws.id}">
      <header class="ws-head">
        <div>
          <h3>${ws.name} ${ws.is_default ? "<span class='ws-badge'>Default</span>" : ""}</h3>
          <p>${ws.keyword}</p>
        </div>
        <div class="ws-menu-wrap">
          <button class="ws-menu-btn" data-action="menu" data-id="${ws.id}" title="Menu">⋯</button>
          <div class="ws-menu" id="menu-${ws.id}">
            <button data-action="default" data-id="${ws.id}">Varsayilan Yap</button>
            <button data-action="edit" data-id="${ws.id}">Duzenle</button>
            <button data-action="delete" data-id="${ws.id}">Sil</button>
          </div>
        </div>
      </header>
      <p class="ws-meta">Ulkeler: ${countriesText(ws.countries)}</p>
      <p class="ws-meta">${workspaceCardLabel(ws)}</p>
      <div class="ws-kpis">
        <div class="ws-kpi"><p>Ulke</p><b>${ws.stats?.countries_count || 0}</b></div>
        <div class="ws-kpi"><p>Sehir</p><b>${ws.stats?.cities_count || 0}</b></div>
        <div class="ws-kpi"><p>Ortalama</p><b>${avg}</b></div>
      </div>
      <div class="ws-actions">
        <a class="btn btn-primary" href="/dashboard?ws=${ws.id}">Sec ve Ac</a>
      </div>
    </article>
  `;
}

function render() {
  if (!state.workspaces.length) {
    grid.innerHTML = "<p class='inline-note'>Workspace yok, yeni olusturabilirsin.</p>";
    return;
  }
  grid.innerHTML = state.workspaces.map(card).join("");
}

async function getWorkspaces() {
  const res = await fetch("/api/workspaces");
  const payload = await res.json();
  state.workspaces = payload.items || [];
  render();
}

function openCreate() {
  formTitle.textContent = "Yeni Workspace";
  inputId.value = "";
  inputName.value = "";
  inputKeyword.value = "/m/02vqb5x";
  inputCountries.value = "TR,US,DE";
  inputTopicMode.checked = false;
  inputCountryKeywords.value = "";
  toggleCountryKeywordsInput();
  dialog.showModal();
}

function openEdit(ws) {
  formTitle.textContent = "Workspace Duzenle";
  inputId.value = ws.id;
  inputName.value = ws.name || "";
  inputKeyword.value = ws.keyword || "/m/02vqb5x";
  inputCountries.value = (ws.countries || []).join(",");
  inputTopicMode.checked = Boolean(ws.use_topic_mode);
  inputCountryKeywords.value = formatCountryKeywords(ws.country_keywords || {});
  toggleCountryKeywordsInput();
  dialog.showModal();
}

async function saveForm(event) {
  event.preventDefault();
  const id = inputId.value.trim();
  const name = inputName.value.trim();
  const keyword = inputKeyword.value.trim();
  const countries = inputCountries.value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
  const useTopicMode = Boolean(inputTopicMode?.checked);
  const countryKeywords = parseCountryKeywordsText(inputCountryKeywords?.value || "");

  if (!name || !keyword || !countries.length) {
    notify("Tum alanlar zorunlu");
    return;
  }

  const payload = { name, keyword, countries, use_topic_mode: useTopicMode, country_keywords: countryKeywords };
  let savedWorkspaceId = id;

  if (!id) {
    const res = await fetch("/api/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const responsePayload = await res.json();
    if (!res.ok) {
      notify("Olusturma hatasi");
      return;
    }
    savedWorkspaceId = responsePayload?.item?.id || savedWorkspaceId;
    notify("Workspace olusturuldu");
  } else {
    const res = await fetch(`/api/workspaces/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await res.json();
    if (!res.ok) {
      notify("Guncelleme hatasi");
      return;
    }
    notify("Workspace guncellendi");
  }

  dialog.close();
  if (savedWorkspaceId) {
    localStorage.setItem(LAST_WORKSPACE_KEY, savedWorkspaceId);
  }
  await getWorkspaces();
}

async function setDefault(id) {
  const res = await fetch(`/api/workspaces/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_default: true }),
  });
  if (res.ok) {
    notify("Varsayilan workspace guncellendi");
    await getWorkspaces();
  } else {
    notify("Varsayilan ayarlanamadi");
  }
}

async function removeWorkspace(id) {
  const ok = window.confirm("Bu workspace silinsin mi?");
  if (!ok) return;
  const res = await fetch(`/api/workspaces/${id}`, { method: "DELETE" });
  if (res.ok) {
    notify("Workspace silindi");
    await getWorkspaces();
  } else {
    notify("Silme hatasi");
  }
}

function handleRealtimeEvent(event) {
  const payload = event.detail || {};
  if (payload.type === "workspace_created" || payload.type === "workspace_updated" || payload.type === "workspace_deleted") {
    getWorkspaces().catch(() => {});
  }
}

function handleJobFinal(event) {
  const job = event.detail?.job;
  if (!job) return;
  getWorkspaces().catch(() => {});
  if (job.status === "completed" && event.detail?.source !== "bootstrap") {
    notify(`Arkaplan veri cekimi tamamlandi: ${job.workspace_id}`, "info", false);
  }
}

grid.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;

  const id = btn.dataset.id;
  const action = btn.dataset.action;
  const ws = state.workspaces.find((x) => x.id === id);

  if (action === "menu") {
    document.querySelectorAll(".ws-menu").forEach((m) => {
      if (m.id !== `menu-${id}`) m.classList.remove("open");
    });
    const menu = document.getElementById(`menu-${id}`);
    if (menu) menu.classList.toggle("open");
    return;
  }

  if (action === "edit" && ws) openEdit(ws);
  if (action === "default") await setDefault(id);
  if (action === "delete") await removeWorkspace(id);
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".ws-menu-wrap")) {
    document.querySelectorAll(".ws-menu").forEach((m) => m.classList.remove("open"));
  }
});

newBtn.addEventListener("click", openCreate);
cancelBtn.addEventListener("click", () => dialog.close());
inputTopicMode?.addEventListener("change", toggleCountryKeywordsInput);
if (themeButtons && themeButtons.length > 0) {
  themeButtons.forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const mode = e.currentTarget.dataset.theme || "system";
      localStorage.setItem(THEME_KEY, mode);
      applyTheme(mode);
    });
  });
}
form.addEventListener("submit", saveForm);
window.addEventListener("b2btrend:realtime", handleRealtimeEvent);
window.addEventListener("b2btrend:job-final", handleJobFinal);

(async function bootstrap() {
  initTheme();
  render();
  await getWorkspaces();
})();
