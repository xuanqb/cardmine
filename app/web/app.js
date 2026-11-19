const form = document.getElementById("settings-form");
const toast = document.getElementById("toast");
const saveButton = document.getElementById("save-button");
const healthBox = document.getElementById("health");

const ankiBox = document.querySelector(".sync-state");
const ankiAlert = document.getElementById("anki-alert");
const ankiProgress = document.getElementById("anki-progress");
const ankiReport = document.getElementById("anki-report");
const jobButtons = [
  document.getElementById("anki-takeover"),
  document.getElementById("anki-push"),
  document.getElementById("anki-sync"),
];

const RESULT_LABELS = {
  scanned_decks: "扫描牌组",
  scanned_notes: "扫描笔记",
  matched: "匹配成功",
  unmatched_local: "本地未匹配",
  unmatched_anki: "Anki 未匹配",
  ready: "待推送总数",
  changed: "内容变化",
  added: "新增卡片",
  updated: "更新卡片",
  failed: "失败",
  media_uploaded: "上传媒体",
  sync: "同步结果",
};

let toastTimer = null;
let busy = false;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data.detail) {
        message =
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail);
      }
    } catch (error) {
      // 保留 HTTP 状态码提示
    }
    throw new Error(message);
  }
  return response.json();
}

function setHealth(ok, text) {
  healthBox.classList.toggle("ok", ok);
  healthBox.classList.toggle("error", !ok);
  document.getElementById("health-text").textContent = text;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatSize(bytes) {
  if (!bytes) return "0 B";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setHealth(true, "运行正常");
    document.getElementById("app-version").textContent = data.app_version;
    document.getElementById("db-status").textContent =
      data.db === "ok" ? "正常（WAL）" : data.db;
    document.getElementById("schema-version").textContent =
      `v${data.schema_version}`;
    document.getElementById("anki-url").textContent = data.anki_url;
  } catch (error) {
    setHealth(false, "服务不可用");
    document.getElementById("db-status").textContent = "未知";
  }
}

async function loadSettings() {
  const data = await fetchJSON("/api/settings");

  for (const [key, value] of Object.entries(data)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
    } else {
      input.value = value === null || value === undefined ? "" : value;
    }
  }
}

function setBusy(value) {
  busy = value;
  for (const button of jobButtons) {
    button.disabled = value;
  }
}

function hideAnkiNotice() {
  ankiAlert.hidden = true;
  ankiAlert.textContent = "";
  ankiReport.hidden = true;
  ankiReport.textContent = "";
}

function renderAnkiStatus(data) {
  const connected = Boolean(data.connected);
  ankiBox.classList.toggle("ok", connected);
  ankiBox.classList.toggle("error", !connected);
  document.getElementById("anki-state").textContent = connected
    ? `已连接 · v${data.version} · ${data.fields.join("/")}`
    : "未连接";

  const cards = data.cards || {};
  document.getElementById("anki-pending").textContent =
    `${cards.pending_push ?? 0} 张`;
  document.getElementById("anki-matched").textContent =
    `${cards.matched ?? 0} 张`;
  document.getElementById("anki-last-push").textContent = formatTime(
    data.last_push_at,
  );
  const backup = (data.backups || [])[0];
  document.getElementById("anki-backup").textContent = backup
    ? `${backup.name} · ${formatSize(backup.size)}`
    : "暂无";

  if (connected) {
    ankiAlert.hidden = true;
    ankiAlert.textContent = "";
  } else {
    ankiAlert.hidden = false;
    ankiAlert.textContent = data.error || "无法连接 Anki 容器";
  }

  const running = data.running_job;
  if (running && !busy) {
    ankiProgress.hidden = false;
    document.getElementById("anki-phase").textContent = running.phase || "运行中";
  }
}

async function loadAnkiStatus() {
  try {
    renderAnkiStatus(await fetchJSON("/api/anki/status"));
  } catch (error) {
    ankiBox.classList.remove("ok");
    ankiBox.classList.add("error");
    document.getElementById("anki-state").textContent = "状态读取失败";
    ankiAlert.hidden = false;
    ankiAlert.textContent = error.message;
  }
}

function renderProgress(job) {
  ankiProgress.hidden = false;
  document.getElementById("anki-phase").textContent = job.phase || "运行中";
  const total = job.total || 0;
  const done = job.processed || 0;
  document.getElementById("anki-count").textContent = total
    ? `${done} / ${total}`
    : "";
  document.getElementById("anki-fill").style.width = total
    ? `${Math.min(100, Math.round((done / total) * 100))}%`
    : "0%";
}

function renderReport(result) {
  if (!result) return;
  const grid = document.createElement("dl");
  grid.className = "report-grid";

  for (const [key, label] of Object.entries(RESULT_LABELS)) {
    if (!(key in result)) continue;
    let value = result[key];
    if (key === "backup") {
      value = value ? value.skipped || value.path : "未执行";
    } else if (key === "sync") {
      value = value || "—";
    }
    const cell = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    cell.append(dt, dd);
    grid.append(cell);
  }

  ankiReport.hidden = false;
  ankiReport.textContent = "";
  const title = document.createElement("h3");
  title.textContent = "任务结果";
  ankiReport.append(title, grid);

  const errors = result.errors || [];
  if (errors.length) {
    const detail = document.createElement("div");
    detail.className = "report-detail";
    const head = document.createElement("p");
    head.className = "report-detail-title";
    head.textContent = `错误 ${errors.length} 条`;
    const list = document.createElement("ul");
    for (const item of errors.slice(0, 20)) {
      const li = document.createElement("li");
      li.textContent = item;
      list.append(li);
    }
    detail.append(head, list);
    ankiReport.append(detail);
  }
}

function pollJob(jobId) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const job = await fetchJSON(`/api/anki/jobs/${jobId}`);
        if (job.status === "done") {
          clearInterval(timer);
          resolve(job);
        } else if (job.status === "failed") {
          clearInterval(timer);
          reject(new Error(job.error || "任务失败"));
        } else {
          renderProgress(job);
        }
      } catch (error) {
        clearInterval(timer);
        reject(error);
      }
    }, 700);
  });
}

async function startJob(action) {
  if (busy) return;
  setBusy(true);
  hideAnkiNotice();
  renderProgress({ phase: "排队中", processed: 0, total: 0 });

  try {
    const { job_id } = await fetchJSON(`/api/anki/${action}`, {
      method: "POST",
    });
    const job = await pollJob(job_id);
    ankiProgress.hidden = true;
    renderReport(job.result);
    showToast("任务完成");
  } catch (error) {
    ankiProgress.hidden = true;
    ankiAlert.hidden = false;
    ankiAlert.textContent = error.message;
    showToast(error.message || "任务失败", true);
  } finally {
    setBusy(false);
    loadAnkiStatus();
  }
}

document.getElementById("anki-check").addEventListener("click", loadAnkiStatus);
document
  .getElementById("anki-takeover")
  .addEventListener("click", () => startJob("takeover"));
document
  .getElementById("anki-push")
  .addEventListener("click", () => startJob("push"));
document
  .getElementById("anki-sync")
  .addEventListener("click", () => startJob("sync"));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveButton.disabled = true;

  const payload = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    if (element.type === "checkbox") {
      payload[element.name] = element.checked;
    } else if (element.type === "number") {
      payload[element.name] = Number(element.value);
    } else {
      payload[element.name] = element.value;
    }
  }

  try {
    await fetchJSON("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showToast("设置已保存");
    loadAnkiStatus();
  } catch (error) {
    showToast(error.message || "保存失败", true);
  } finally {
    saveButton.disabled = false;
  }
});

loadHealth();
loadSettings().catch(() => showToast("读取设置失败", true));
loadAnkiStatus();
setInterval(loadHealth, 30000);
setInterval(() => {
  if (!busy) loadAnkiStatus();
}, 30000);
