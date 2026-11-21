const form = document.getElementById("settings-form");
const toast = document.getElementById("toast");
const saveButton = document.getElementById("save-button");
const healthBox = document.getElementById("health");

const ankiBox = document.querySelector(".sync-state");
const ankiAlert = document.getElementById("anki-alert");
const ankiProgress = document.getElementById("anki-progress");
const ankiReport = document.getElementById("anki-report");
const auditResult = document.getElementById("audit-result");
const jobButtons = [
  document.getElementById("anki-takeover"),
  document.getElementById("anki-push"),
  document.getElementById("anki-sync"),
  document.getElementById("anki-audit"),
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

function auditSection(title, total, shown) {
  const section = document.createElement("div");
  section.className = "audit-section";

  const head = document.createElement("div");
  head.className = "audit-head";
  const heading = document.createElement("h3");
  heading.textContent = total > 0 ? `${title}（${total}）` : title;
  head.append(heading);

  const hint = document.createElement("p");
  hint.className = "hint";
  if (total > shown) {
    hint.textContent = `共 ${total} 条，仅显示前 ${shown} 条。`;
  }
  section.append(head);
  if (hint.textContent) section.append(hint);
  return section;
}

async function deleteAuditNotes(noteIds) {
  if (noteIds.length === 0) return;
  const ok = window.confirm(
    `将永久删除 ${noteIds.length} 条 Anki note，删除后无法恢复。确认继续？`,
  );
  if (!ok) return;

  try {
    await fetchJSON("/api/anki/notes/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note_ids: noteIds }),
    });
    showToast(`已删除 ${noteIds.length} 条 note`);
    startJob("audit", renderAudit);
  } catch (error) {
    showToast(error.message || "删除失败", true);
  }
}

function renderOrphans(result) {
  const orphans = result.orphans || [];
  const section = auditSection(
    "Anki 有、系统无",
    result.orphan_total || 0,
    orphans.length,
  );

  if (orphans.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "没有发现孤儿 note。";
    section.append(empty);
    return section;
  }

  const actions = document.createElement("div");
  actions.className = "audit-actions";

  const selectLabel = document.createElement("label");
  selectLabel.className = "checkbox";
  const selectAll = document.createElement("input");
  selectAll.type = "checkbox";
  const selectText = document.createElement("span");
  selectText.textContent = "全选";
  selectLabel.append(selectAll, selectText);

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "danger";
  deleteButton.textContent = "删除所选";
  deleteButton.disabled = true;
  actions.append(selectLabel, deleteButton);
  section.querySelector(".audit-head").append(actions);

  const list = document.createElement("div");
  list.className = "audit-list";
  const boxes = [];

  function syncSelection() {
    const selected = boxes.filter((box) => box.checked);
    deleteButton.disabled = selected.length === 0;
    deleteButton.textContent = selected.length
      ? `删除所选（${selected.length}）`
      : "删除所选";
    selectAll.checked = selected.length === boxes.length;
    selectAll.indeterminate =
      selected.length > 0 && selected.length < boxes.length;
  }

  for (const item of orphans) {
    const row = document.createElement("label");
    row.className = "audit-row";

    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(item.note_id);
    box.addEventListener("change", syncSelection);
    boxes.push(box);

    const main = document.createElement("span");
    main.className = "audit-main";
    const front = document.createElement("span");
    front.className = "audit-front";
    front.textContent = item.front || "（空正面）";
    const meta = document.createElement("span");
    meta.className = "audit-meta";
    meta.textContent = `note ${item.note_id} · ${item.deck || "未知牌组"} · ${item.model}`;
    main.append(front, meta);
    row.append(box, main);
    list.append(row);
  }

  selectAll.addEventListener("change", () => {
    for (const box of boxes) box.checked = selectAll.checked;
    syncSelection();
  });
  deleteButton.addEventListener("click", () => {
    const ids = boxes
      .filter((box) => box.checked)
      .map((box) => Number(box.value));
    deleteAuditNotes(ids);
  });

  section.append(list);
  return section;
}

function renderMissing(result) {
  const missing = result.missing || [];
  const section = auditSection(
    "系统有、Anki 无",
    result.missing_total || 0,
    missing.length,
  );

  if (missing.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "没有发现失效关联。";
    section.append(empty);
    return section;
  }

  const list = document.createElement("div");
  list.className = "audit-list";
  for (const item of missing) {
    const row = document.createElement("a");
    row.className = "audit-row";
    row.href = `/cards/${item.kp_id}`;

    const main = document.createElement("span");
    main.className = "audit-main";
    const front = document.createElement("span");
    front.className = "audit-front";
    front.textContent = item.front || "（空正面）";
    const meta = document.createElement("span");
    meta.className = "audit-meta";
    const parts = [
      `卡片 #${item.kp_id}`,
      `note ${item.note_id}`,
      item.module_path ? item.module_path.replaceAll("/", " / ") : "未分类",
      item.status,
    ];
    meta.textContent = parts.join(" · ");
    main.append(front, meta);
    row.append(main);
    list.append(row);
  }
  section.append(list);
  return section;
}

function renderAudit(result) {
  auditResult.replaceChildren();
  auditResult.hidden = false;
  if (!result) return;

  const summary = document.createElement("p");
  summary.className = "audit-summary";
  summary.textContent =
    `扫描 ${result.scanned_notes} 条 Anki note · 系统关联 ${result.system_notes} 条 · ` +
    `孤儿 ${result.orphan_total} 条 · 失效 ${result.missing_total} 条`;
  auditResult.append(summary);

  const decks = result.decks || [];
  const scope = document.createElement("p");
  scope.className = "hint";
  const deckText = decks.length
    ? `${decks.slice(0, 4).join("、")}${decks.length > 4 ? ` 等 ${decks.length} 个牌组` : ""}`
    : "未配置模块牌组";
  scope.textContent = `范围：${deckText} · 模型 ${result.model}`;
  auditResult.append(scope);

  auditResult.append(renderOrphans(result), renderMissing(result));

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
    auditResult.append(detail);
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

async function startJob(action, render = renderReport) {
  if (busy) return;
  setBusy(true);
  hideAnkiNotice();
  if (action === "audit") {
    auditResult.hidden = true;
    auditResult.replaceChildren();
  }
  renderProgress({ phase: "排队中", processed: 0, total: 0 });

  try {
    const { job_id } = await fetchJSON(`/api/anki/${action}`, {
      method: "POST",
    });
    const job = await pollJob(job_id);
    ankiProgress.hidden = true;
    render(job.result);
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
document
  .getElementById("anki-audit")
  .addEventListener("click", () => startJob("audit", renderAudit));

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
