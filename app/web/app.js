const form = document.getElementById("settings-form");
const toast = document.getElementById("toast");
const saveButton = document.getElementById("save-button");
const healthBox = document.getElementById("health");
const settingsTabs = Array.from(document.querySelectorAll(".settings-tab"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));
const conflictTabCount = document.getElementById("conflict-tab-count");

const ankiBox = document.querySelector(".sync-state");
const ankiAlert = document.getElementById("anki-alert");
const ankiProgress = document.getElementById("anki-progress");
const ankiReport = document.getElementById("anki-report");
const auditResult = document.getElementById("audit-result");
const jobButtons = [
  document.getElementById("anki-takeover"),
  document.getElementById("anki-push"),
  document.getElementById("anki-pull"),
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
  pulled: "回写卡片",
  conflicts: "新冲突",
  skipped_conflicts: "跳过冲突卡",
  linked: "已关联卡片",
  checked: "检查卡片",
  skipped: "跳过",
  failed: "失败",
  media_uploaded: "上传媒体",
  sync: "同步结果",
};

const conflictList = document.getElementById("conflict-list");
const resolvedBox = document.getElementById("conflict-resolved");
const resolvedList = document.getElementById("conflict-resolved-list");

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

function activateSettingsTab(name) {
  for (const tab of settingsTabs) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of tabPanels) {
    const active = panel.id === `tab-${name}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  }
}

for (const tab of settingsTabs) {
  tab.addEventListener("click", () => {
    activateSettingsTab(tab.dataset.tab);
    history.replaceState(null, "", `#${tab.dataset.tab}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

const initialTab = location.hash.slice(1);
activateSettingsTab(
  settingsTabs.some((tab) => tab.dataset.tab === initialTab)
    ? initialTab
    : "general",
);

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
    } else if (key === "candidate_years" && !value) {
      input.value = "";
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
  document.getElementById("anki-conflicts").textContent =
    `${data.conflicts ?? 0} 条`;
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
    "Anki 有、系统未关联",
    result.orphan_total || 0,
    orphans.length,
  );

  if (orphans.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "没有发现未关联的 note。";
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
    const row = document.createElement("div");
    row.className = "audit-row";

    const check = document.createElement("label");
    check.className = "audit-check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(item.note_id);
    box.addEventListener("change", syncSelection);
    boxes.push(box);
    check.append(box);

    const main = document.createElement("span");
    main.className = "audit-main";
    const front = document.createElement("span");
    front.className = "audit-front";
    front.textContent = item.front || "（空正面）";
    const meta = document.createElement("span");
    meta.className = "audit-meta";
    meta.textContent = `note ${item.note_id} · ${item.deck || "未知牌组"} · ${item.model}`;

    const relation = document.createElement("span");
    relation.className = "audit-relation";
    if (item.card_id) {
      relation.append(document.createTextNode("已并入 "));
      const link = document.createElement("a");
      link.href = `/cards/${item.card_id}`;
      link.textContent = `卡片 #${item.card_id}`;
      relation.append(link);
      const cardFront = (item.card_front || "").slice(0, 80);
      if (cardFront) {
        relation.append(document.createTextNode(`「${cardFront}」`));
      }
      if (item.merge_action_id) {
        relation.append(
          document.createTextNode(` · 合并记录 #${item.merge_action_id}`),
        );
      } else if (item.relation === "front") {
        relation.append(document.createTextNode(" · 正面相同"));
      }
    } else if (item.relation === "merge") {
      relation.textContent = `来自合并记录 #${item.merge_action_id}，存活卡已不存在`;
    } else {
      relation.textContent = "未找到关联卡片，请确认内容后再删除";
    }

    main.append(front, meta, relation);
    row.append(check, main);
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
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent =
    "带「已并入」的 note 内容已包含在对应卡片中，可先点开确认再删除。";
  section.append(note);
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
    `未关联 ${result.orphan_total} 条（其中合并遗留 ${result.merged_total || 0} 条）· ` +
    `失效 ${result.missing_total} 条`;
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

const mediaResult = document.getElementById("media-result");
const mediaScan = document.getElementById("media-scan");
const mediaCleanup = document.getElementById("media-cleanup");

function renderMediaOrphans(result) {
  mediaResult.replaceChildren();
  mediaResult.hidden = false;

  const summary = document.createElement("p");
  summary.className = "audit-summary";
  if (!result.count) {
    summary.textContent = "没有发现无引用媒体。";
    mediaCleanup.hidden = true;
    mediaResult.append(summary);
    return;
  }

  summary.textContent = `发现 ${result.count} 个无引用文件，共 ${formatSize(result.size)}`;
  mediaResult.append(summary);

  const list = document.createElement("div");
  list.className = "audit-list";
  for (const item of result.items || []) {
    const row = document.createElement("div");
    row.className = "audit-row";
    const name = document.createElement("span");
    name.className = "audit-front";
    name.textContent = item.filename;
    const size = document.createElement("span");
    size.className = "audit-meta";
    size.textContent = formatSize(item.size);
    row.append(name, size);
    list.append(row);
  }
  mediaResult.append(list);

  if (result.truncated) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = `仅显示前 ${(result.items || []).length} 个，删除时会处理全部。`;
    mediaResult.append(hint);
  }

  mediaCleanup.hidden = false;
  mediaCleanup.textContent = `删除 ${result.count} 个文件`;
}

async function scanMediaOrphans() {
  mediaScan.disabled = true;
  try {
    const result = await fetchJSON("/api/media/orphans");
    renderMediaOrphans(result);
  } catch (error) {
    showToast(error.message || "扫描失败", true);
  } finally {
    mediaScan.disabled = false;
  }
}

async function cleanupMedia() {
  if (mediaCleanup.hidden) return;
  const ok = window.confirm(
    `${mediaCleanup.textContent}，删除后无法恢复。确认继续？`,
  );
  if (!ok) return;

  mediaCleanup.disabled = true;
  try {
    const result = await fetchJSON("/api/media/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    const failed = result.failed || [];
    showToast(
      `已删除 ${result.deleted} 个文件，释放 ${formatSize(result.size)}` +
        (failed.length ? `，${failed.length} 个失败` : ""),
      failed.length > 0,
    );
    await scanMediaOrphans();
  } catch (error) {
    showToast(error.message || "清理失败", true);
  } finally {
    mediaCleanup.disabled = false;
  }
}

function conflictSide(title, text) {
  const side = document.createElement("div");
  side.className = "conflict-side";
  const label = document.createElement("span");
  label.className = "conflict-side-label";
  label.textContent = title;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const preview = (text || "").replace(/\s+/g, " ").trim().slice(0, 120);
  summary.textContent = preview || "（空）";
  const body = document.createElement("pre");
  body.textContent = text || "（空）";
  details.append(summary, body);
  side.append(label, details);
  return side;
}

async function resolveConflictItem(id, choice) {
  const hint =
    choice === "system"
      ? "将用系统当前内容覆盖 Anki 该字段，Anki 原文会保留在冲突记录中。确认继续？"
      : "将采用 Anki 内容回写系统，可在解决后撤销。确认继续？";
  if (!window.confirm(hint)) return;
  try {
    await fetchJSON(`/api/anki/conflicts/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ choice }),
    });
    showToast(choice === "system" ? "已用系统内容覆盖 Anki" : "已采用 Anki 内容");
    loadConflicts();
    loadAnkiStatus();
  } catch (error) {
    showToast(error.message || "处理失败", true);
  }
}

async function undoConflictItem(id) {
  if (!window.confirm("撤销后会恢复系统原背面，并重新打开该冲突。确认继续？")) {
    return;
  }
  try {
    await fetchJSON(`/api/anki/conflicts/${id}/undo`, { method: "POST" });
    showToast("已撤销「用 Anki」");
    loadConflicts();
    loadAnkiStatus();
  } catch (error) {
    showToast(error.message || "撤销失败", true);
  }
}

function renderConflictItem(item, resolved) {
  const row = document.createElement("div");
  row.className = "conflict-row";

  const head = document.createElement("div");
  head.className = "conflict-head";
  const title = document.createElement("a");
  title.href = `/cards/${item.kp_id}`;
  title.className = "conflict-title";
  title.textContent =
    `#${item.kp_id} · ${item.field_label}` +
    ` · ${item.front_preview || "（空正面）"}`;
  const meta = document.createElement("span");
  meta.className = "conflict-meta";
  const parts = [];
  if (item.module_path) parts.push(item.module_path.replaceAll("/", " / "));
  if (resolved && item.resolution) {
    parts.push(item.resolution === "anki" ? "已采用 Anki" : "已用系统");
    parts.push(formatTime(item.resolved_at));
  } else {
    parts.push(formatTime(item.created_at));
  }
  meta.textContent = parts.join(" · ");
  head.append(title, meta);
  row.append(head);

  if (item.note) {
    const note = document.createElement("p");
    note.className = "conflict-note";
    note.textContent = item.note;
    row.append(note);
  }

  const diff = document.createElement("div");
  diff.className = "conflict-diff";
  diff.append(
    conflictSide("系统", item.system_value),
    conflictSide("Anki", item.anki_value),
  );
  row.append(diff);

  const actions = document.createElement("div");
  actions.className = "conflict-actions";
  if (!resolved) {
    const useSystem = document.createElement("button");
    useSystem.type = "button";
    useSystem.textContent = "用系统";
    useSystem.addEventListener("click", () =>
      resolveConflictItem(item.id, "system"),
    );
    actions.append(useSystem);

    if (item.field === "back") {
      const useAnki = document.createElement("button");
      useAnki.type = "button";
      useAnki.className = "ghost";
      useAnki.textContent = "用 Anki";
      useAnki.addEventListener("click", () =>
        resolveConflictItem(item.id, "anki"),
      );
      actions.append(useAnki);
    }
  } else if (item.resolution === "anki") {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "ghost";
    undo.textContent = "撤销";
    undo.addEventListener("click", () => undoConflictItem(item.id));
    actions.append(undo);
  }
  if (actions.childElementCount) row.append(actions);
  return row;
}

function emptyHint(text) {
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = text;
  return hint;
}

async function loadConflicts() {
  try {
    const pending = await fetchJSON(
      "/api/anki/conflicts?status=pending&limit=200",
    );
    const pendingCount = pending.total ?? pending.items.length;
    conflictTabCount.textContent = pendingCount;
    conflictTabCount.hidden = pendingCount === 0;
    conflictList.replaceChildren();
    if (!pending.items.length) {
      conflictList.append(emptyHint("没有待处理冲突。"));
    } else {
      for (const item of pending.items) {
        conflictList.append(renderConflictItem(item, false));
      }
    }
  } catch (error) {
    conflictList.replaceChildren(emptyHint(error.message || "读取冲突失败"));
  }

  try {
    const resolved = await fetchJSON(
      "/api/anki/conflicts?status=resolved&limit=10",
    );
    resolvedList.replaceChildren();
    resolvedBox.hidden = resolved.items.length === 0;
    for (const item of resolved.items) {
      resolvedList.append(renderConflictItem(item, true));
    }
  } catch (error) {
    resolvedBox.hidden = true;
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
    loadConflicts();
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
  .getElementById("anki-pull")
  .addEventListener("click", () => startJob("pull"));
document
  .getElementById("anki-sync")
  .addEventListener("click", () => startJob("sync"));
document
  .getElementById("anki-audit")
  .addEventListener("click", () => startJob("audit", renderAudit));
mediaScan.addEventListener("click", scanMediaOrphans);
mediaCleanup.addEventListener("click", cleanupMedia);

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
loadConflicts();
setInterval(loadHealth, 30000);
setInterval(() => {
  if (!busy) {
    loadAnkiStatus();
    loadConflicts();
  }
}, 30000);
