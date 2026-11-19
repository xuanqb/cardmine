const { fetchJSON, debounce, formatTime, statusMeta } = window.cm;

const listEl = document.getElementById("candidate-list");
const toast = document.getElementById("toast");
const scanButton = document.getElementById("scan-run");
const scanProgress = document.getElementById("scan-progress");
const scanSummary = document.getElementById("scan-summary");
const actionList = document.getElementById("action-list");
const batchButtons = {
  merge: document.getElementById("batch-merge"),
  keep: document.getElementById("batch-keep"),
  ignore: document.getElementById("batch-ignore"),
};

const state = { page: 1, pageSize: 10, total: 0, q: "", status: "pending" };
const selected = new Set();
const ACTION_LABEL = { merge: "合并", keep: "保持独立", ignore: "忽略" };

let toastTimer = null;
let busy = false;
let scanning = false;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 3000);
}

function excerpt(markdown, max = 180) {
  const text = (markdown || "")
    .replace(/```[\s\S]*?```/g, " [代码] ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/[#>*_`~|[\]()!-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "（背面为空）";
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function badge(text, className) {
  const span = document.createElement("span");
  span.className = `badge ${className || ""}`.trim();
  span.textContent = text;
  return span;
}

function cardPane(item, side) {
  const isLeft = side === "left";
  const pane = document.createElement("div");
  pane.className = "compare-pane";

  const head = document.createElement("div");
  head.className = "compare-head";
  const label = document.createElement("span");
  label.className = "compare-label";
  label.textContent = isLeft
    ? "左侧 · 保留"
    : item.mention_id
      ? "右侧 · 新卡"
      : "右侧 · 候选卡";
  const meta = statusMeta(isLeft ? item.left_status : item.right_status);
  head.append(label, badge(meta.label, meta.className));

  const module = document.createElement("p");
  module.className = "compare-module";
  module.textContent = (isLeft ? item.left_module : item.right_module) || "未分类";

  const front = document.createElement("a");
  front.className = "compare-front";
  front.href = `/cards/${isLeft ? item.left_kp_id : item.right_kp_id}`;
  front.textContent = isLeft ? item.left_front : item.right_front;

  const back = document.createElement("p");
  back.className = "compare-back";
  back.textContent = excerpt(isLeft ? item.left_back : item.right_back);

  pane.append(head, module, front, back);
  return pane;
}

function candidateActions(item) {
  const actions = document.createElement("div");
  actions.className = "candidate-actions";

  const merge = document.createElement("button");
  merge.type = "button";
  merge.textContent = item.mention_id ? "合并为变体" : "合并两张卡";
  merge.addEventListener("click", () => resolveOne(item.id, "merge"));

  const keep = document.createElement("button");
  keep.type = "button";
  keep.className = "ghost";
  keep.textContent = "保持独立";
  keep.addEventListener("click", () => resolveOne(item.id, "keep"));

  const ignore = document.createElement("button");
  ignore.type = "button";
  ignore.className = "ghost";
  ignore.textContent = item.mention_id ? "忽略该问题" : "忽略";
  ignore.addEventListener("click", () => resolveOne(item.id, "ignore"));

  actions.append(merge, keep, ignore);
  return actions;
}

function renderCandidate(item) {
  const card = document.createElement("article");
  card.className = "candidate";
  card.dataset.id = String(item.id);

  const head = document.createElement("div");
  head.className = "candidate-head";

  const selectLabel = document.createElement("label");
  selectLabel.className = "candidate-select";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = selected.has(item.id);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) selected.add(item.id);
    else selected.delete(item.id);
    updateBatchButtons();
  });
  const idText = document.createElement("span");
  idText.textContent = `#${item.id}`;
  selectLabel.append(checkbox, idText);

  const score = document.createElement("span");
  score.className = "candidate-score";
  score.textContent = `相似度 ${(item.score * 100).toFixed(1)}%`;

  head.append(selectLabel, score);

  if (item.status !== "pending") {
    head.append(badge(item.status === "kept" ? "保持独立" : "已忽略", "archived"));
  }

  const compare = document.createElement("div");
  compare.className = "compare-grid";
  compare.append(cardPane(item, "left"), cardPane(item, "right"));

  const reason = document.createElement("p");
  reason.className = "candidate-reason";
  reason.textContent = item.reason || "—";

  card.append(head, compare);

  if (item.mention_text) {
    const source = document.createElement("p");
    source.className = "candidate-source";
    const section = item.mention_section ? `${item.mention_section} · ` : "";
    source.textContent = `${section}「${item.mention_text}」`;
    if (item.post_id) {
      const link = document.createElement("a");
      link.href = `/posts/${item.post_id}`;
      link.textContent = `来自《${item.post_title || "面经"}》`;
      source.append(" ", link);
    }
    card.append(source);
  }

  card.append(reason);
  if (item.status === "pending") {
    card.append(candidateActions(item));
  }
  return card;
}

function renderCandidates(data) {
  state.total = data.total;
  listEl.textContent = "";

  if (!data.items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent =
      state.status === "pending"
        ? "暂无待确认重复。抓取到相似问法或完成全库扫描后会出现在这里。"
        : "没有符合条件的记录。";
    listEl.append(empty);
  }

  for (const item of data.items) {
    listEl.append(renderCandidate(item));
  }

  document.getElementById("review-count").textContent = `${data.total} 条`;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-info").textContent =
    `第 ${state.page} / ${pages} 页 · 共 ${state.total} 条`;
  document.getElementById("prev").disabled = state.page <= 1;
  document.getElementById("next").disabled = state.page >= pages;
  updateBatchButtons();
}

function updateBatchButtons() {
  const disabled = busy || selected.size === 0;
  for (const button of Object.values(batchButtons)) {
    button.disabled = disabled;
  }
}

async function loadCandidates() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
    status: state.status,
  });
  if (state.q) params.set("q", state.q);
  try {
    const data = await fetchJSON(`/api/dup-candidates?${params.toString()}`);
    renderCandidates(data);
  } catch (error) {
    showToast(error.message || "读取审核队列失败", true);
  }
}

async function resolveOne(candidateId, action) {
  if (busy) return;
  busy = true;
  updateBatchButtons();
  try {
    await fetchJSON(`/api/dup-candidates/${candidateId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    selected.delete(candidateId);
    showToast(`已${ACTION_LABEL[action] || "处理"}`);
    await Promise.all([loadCandidates(), loadActions()]);
  } catch (error) {
    showToast(error.message || "操作失败", true);
  } finally {
    busy = false;
    updateBatchButtons();
  }
}

async function resolveBatch(action) {
  if (busy || !selected.size) return;
  busy = true;
  updateBatchButtons();
  const ids = [...selected];
  try {
    const result = await fetchJSON("/api/dup-candidates/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, action }),
    });
    selected.clear();
    const errors = result.errors || [];
    const message = `已处理 ${result.resolved.length} 条${
      errors.length ? `，失败 ${errors.length} 条：${errors[0].error}` : ""
    }`;
    showToast(message, errors.length > 0);
    await Promise.all([loadCandidates(), loadActions()]);
  } catch (error) {
    showToast(error.message || "批量操作失败", true);
  } finally {
    busy = false;
    updateBatchButtons();
  }
}

function renderScanProgress(job) {
  scanProgress.hidden = false;
  document.getElementById("scan-phase").textContent = job.phase || "运行中";
  const total = job.total || 0;
  const done = job.processed || 0;
  document.getElementById("scan-count").textContent = total
    ? `${done} / ${total}`
    : "";
  document.getElementById("scan-fill").style.width = total
    ? `${Math.min(100, Math.round((done / total) * 100))}%`
    : "0%";
}

function renderScanSummary(report) {
  scanSummary.hidden = false;
  const parts = [
    `扫描 ${report.knowledge_points || 0} 张卡`,
    `${report.pairs || 0} 对比对`,
    `超阈值 ${report.above_threshold || 0} 对`,
    `AI 复核 ${report.reviewed || 0} 对`,
    `新增候选 ${report.candidates || 0} 条`,
    `${((report.elapsed_ms || 0) / 1000).toFixed(1)} 秒`,
  ];
  scanSummary.textContent = parts.join(" · ");
  if (report.errors && report.errors.length) {
    const detail = document.createElement("span");
    detail.className = "hint-error";
    detail.textContent = `（${report.errors[0]}）`;
    scanSummary.append(detail);
  }
}

function pollScan(jobId) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const job = await fetchJSON(`/api/dedupe/jobs/${jobId}`);
        if (job.status === "done") {
          clearInterval(timer);
          resolve(job);
        } else if (job.status === "failed") {
          clearInterval(timer);
          reject(new Error(job.error || "扫描失败"));
        } else {
          renderScanProgress(job);
        }
      } catch (error) {
        clearInterval(timer);
        reject(error);
      }
    }, 1000);
  });
}

async function runScan() {
  if (scanning) return;
  scanning = true;
  scanButton.disabled = true;
  scanSummary.hidden = true;
  renderScanProgress({ phase: "排队中", processed: 0, total: 0 });

  try {
    const { job_id } = await fetchJSON("/api/dedupe/scan", { method: "POST" });
    const job = await pollScan(job_id);
    scanProgress.hidden = true;
    renderScanSummary(job.report || {});
    showToast("全库扫描完成");
    state.page = 1;
    await Promise.all([loadCandidates(), loadActions()]);
  } catch (error) {
    scanProgress.hidden = true;
    showToast(error.message || "扫描失败", true);
  } finally {
    scanning = false;
    scanButton.disabled = false;
  }
}

async function undoAction(actionId) {
  try {
    await fetchJSON(`/api/review/actions/${actionId}/undo`, { method: "POST" });
    showToast("已撤销");
    await Promise.all([loadCandidates(), loadActions()]);
  } catch (error) {
    showToast(error.message || "撤销失败", true);
  }
}

function renderActions(items) {
  actionList.textContent = "";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "还没有审核操作。";
    actionList.append(empty);
    return;
  }

  for (const item of items) {
    const li = document.createElement("li");
    li.className = "action-item";

    const main = document.createElement("div");
    main.className = "action-main";

    const title = document.createElement("p");
    title.className = "action-title";
    title.textContent = `${ACTION_LABEL[item.action] || item.action}：${
      item.left_front || "—"
    }`;

    const meta = document.createElement("p");
    meta.className = "action-meta";
    const parts = [];
    if (item.right_front) parts.push(`另一张：${item.right_front}`);
    if (item.mention_text) parts.push(`问法：${item.mention_text}`);
    parts.push(formatTime(item.created_at));
    meta.textContent = parts.join(" · ");

    main.append(title, meta);
    li.append(main);

    if (item.undone_at) {
      li.append(badge("已撤销", "archived"));
    } else {
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "ghost";
      undo.textContent = "撤销";
      undo.addEventListener("click", () => undoAction(item.id));
      li.append(undo);
    }

    actionList.append(li);
  }
}

async function loadActions() {
  try {
    const data = await fetchJSON("/api/review/actions?limit=20");
    renderActions(data.items || []);
  } catch (error) {
    // 操作记录加载失败不影响主流程
  }
}

document.getElementById("search").addEventListener(
  "input",
  debounce((event) => {
    state.q = event.target.value.trim();
    state.page = 1;
    selected.clear();
    loadCandidates();
  }, 300),
);

document.getElementById("status-filter").addEventListener("change", (event) => {
  state.status = event.target.value;
  state.page = 1;
  selected.clear();
  loadCandidates();
});

document.getElementById("select-all").addEventListener("click", () => {
  const boxes = listEl.querySelectorAll(".candidate-select input");
  const shouldSelect = boxes.length > 0 && selected.size < boxes.length;
  selected.clear();
  boxes.forEach((box) => {
    box.checked = shouldSelect;
    if (shouldSelect) {
      const id = Number(box.closest(".candidate").dataset.id);
      if (id) selected.add(id);
    }
  });
  updateBatchButtons();
});

for (const [action, button] of Object.entries(batchButtons)) {
  button.addEventListener("click", () => resolveBatch(action));
}

document.getElementById("prev").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadCandidates();
  }
});
document.getElementById("next").addEventListener("click", () => {
  if (state.page * state.pageSize < state.total) {
    state.page += 1;
    loadCandidates();
  }
});

scanButton.addEventListener("click", runScan);

loadCandidates();
loadActions();
