const form = document.getElementById("import-form");
const fileInput = document.getElementById("file");
const uploadButton = document.getElementById("upload-button");
const progressWrap = document.getElementById("progress-wrap");
const phaseText = document.getElementById("phase");
const progressText = document.getElementById("progress-text");
const progressFill = document.getElementById("progress-fill");
const errorBox = document.getElementById("error");
const reportBox = document.getElementById("report");

let pollTimer = null;

function setProgress(phase, processed, total) {
  progressWrap.hidden = false;
  phaseText.textContent = phase || "处理中";
  if (total > 0) {
    const percent = Math.min(100, Math.round((processed / total) * 100));
    progressFill.style.width = `${percent}%`;
    progressText.textContent = `${processed} / ${total}`;
  } else {
    progressFill.style.width = "0%";
    progressText.textContent = "";
  }
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function renderList(id, title, items) {
  const box = document.getElementById(id);
  if (!items || items.length === 0) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const shown = items.slice(0, 20);
  const more = items.length > shown.length ? ` 等 ${items.length} 项` : "";
  box.innerHTML = "";
  const heading = document.createElement("p");
  heading.className = "report-detail-title";
  heading.textContent = `${title}${more}`;
  const list = document.createElement("ul");
  for (const item of shown) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  box.append(heading, list);
}

function renderReport(report) {
  reportBox.hidden = false;
  setText("r-total", report.cards_total);
  setText("r-new", report.cards_new);
  setText("r-filled", report.cards_filled);
  setText("r-skipped", report.cards_skipped);
  setText("r-modules", report.modules);
  setText("r-media", report.media);
  setText("r-elapsed", `${(report.elapsed_ms / 1000).toFixed(1)} s`);

  renderList("missing", "缺失媒体", report.media_missing);

  const skipped = Object.entries(report.skipped_models || {}).map(
    ([name, count]) => `${name}（${count} 张）`
  );
  renderList("skipped-models", "跳过的模型", skipped);

  renderList("errors", "错误", report.errors);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJob(jobId) {
  const response = await fetch(`/api/import/${jobId}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const job = await response.json();

  setProgress(job.phase, job.processed, job.total);

  if (job.status === "done") {
    stopPolling();
    uploadButton.disabled = false;
    uploadButton.textContent = "开始导入";
    progressFill.style.width = "100%";
    phaseText.textContent = "导入完成";
    renderReport(job.report);
  } else if (job.status === "failed") {
    stopPolling();
    uploadButton.disabled = false;
    uploadButton.textContent = "开始导入";
    errorBox.hidden = false;
    errorBox.textContent = job.error || "导入失败";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  errorBox.hidden = true;
  reportBox.hidden = true;
  uploadButton.disabled = true;
  uploadButton.textContent = "上传中…";
  setProgress("上传安装包", 0, 0);

  const body = new FormData();
  body.append("file", file);

  try {
    const response = await fetch("/api/import/apkg", { method: "POST", body });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${response.status}`);
    }
    const data = await response.json();
    uploadButton.textContent = "导入中…";
    stopPolling();
    pollTimer = setInterval(() => {
      pollJob(data.job_id).catch((error) => {
        stopPolling();
        uploadButton.disabled = false;
        uploadButton.textContent = "开始导入";
        errorBox.hidden = false;
        errorBox.textContent = error.message;
      });
    }, 800);
    pollJob(data.job_id);
  } catch (error) {
    uploadButton.disabled = false;
    uploadButton.textContent = "开始导入";
    errorBox.hidden = false;
    errorBox.textContent = error.message || "上传失败";
  }
});

const mdForm = document.getElementById("md-form");
const mdFile = document.getElementById("md-file");
const mdModule = document.getElementById("md-module");
const mdDryRun = document.getElementById("md-dry-run");
const mdOverwrite = document.getElementById("md-overwrite");
const mdButton = document.getElementById("md-upload-button");
const mdError = document.getElementById("md-error");
const mdReport = document.getElementById("md-report");

function renderSamples(samples) {
  const container = document.getElementById("md-samples");
  container.replaceChildren();
  if (!samples || !samples.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const title = document.createElement("p");
  title.className = "report-detail-title";
  title.textContent = `题目样例（前 ${samples.length} 条）`;
  container.append(title);
  samples.forEach((sample, index) => {
    const box = document.createElement("div");
    box.className = "md-sample";
    const head = document.createElement("p");
    head.className = "md-sample-title";
    head.textContent = `${index + 1}. ${sample.front}（答案 ${sample.back_length} 字${sample.has_images ? "，含图片" : ""}）`;
    const pre = document.createElement("pre");
    pre.textContent = sample.back_excerpt || "（空答案）";
    box.append(head, pre);
    container.append(box);
  });
}

function renderMarkdownReport(report) {
  mdReport.hidden = false;
  document.getElementById("md-report-title").textContent = report.dry_run
    ? "预览结果（未写入数据库）"
    : "导入报告";
  setText("md-r-total", report.cards_total);
  setText("md-r-new", report.cards_new);
  setText("md-r-filled", report.cards_filled);
  setText("md-r-overwritten", report.cards_overwritten);
  setText("md-r-skipped", report.cards_skipped);
  setText("md-r-empty", report.skipped_empty_front);
  setText("md-r-modules", report.modules);
  setText("md-r-media", report.media);
  setText("md-r-elapsed", `${(report.elapsed_ms / 1000).toFixed(1)} s`);

  renderSamples(report.samples);
  renderList("md-missing", "缺失图片", report.media_missing);
  renderList("md-conflicts", "同名图片冲突（保留旧文件）", report.media_conflicts);
  renderList("md-errors", "错误", report.errors);
}

function syncMdButtonLabel() {
  mdButton.textContent = mdDryRun.checked ? "解析预览" : "导入题库";
}

mdDryRun.addEventListener("change", syncMdButtonLabel);

mdForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = mdFile.files[0];
  if (!file) return;

  mdError.hidden = true;
  mdReport.hidden = true;
  mdButton.disabled = true;
  mdButton.textContent = mdDryRun.checked ? "解析中…" : "导入中…";

  const body = new FormData();
  body.append("file", file);
  body.append("module", mdModule.value.trim() || "前端");
  body.append("dry_run", mdDryRun.checked ? "true" : "false");
  body.append("overwrite", mdOverwrite.checked ? "true" : "false");

  try {
    const response = await fetch("/api/import/markdown", { method: "POST", body });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderMarkdownReport(data);
  } catch (error) {
    mdError.hidden = false;
    mdError.textContent = error.message || "导入失败";
  } finally {
    mdButton.disabled = false;
    syncMdButtonLabel();
  }
});

const textForm = document.getElementById("text-form");
const textTitle = document.getElementById("text-title");
const textGroup = document.getElementById("text-group");
const textBody = document.getElementById("text-body");
const textSubmit = document.getElementById("text-submit");
const textError = document.getElementById("text-error");
const textProgressWrap = document.getElementById("text-progress-wrap");
const textPhase = document.getElementById("text-phase");
const textProgressText = document.getElementById("text-progress-text");
const textProgressFill = document.getElementById("text-progress-fill");
const textReport = document.getElementById("text-report");
const textItems = document.getElementById("text-r-items");

const ACTION_LABELS = {
  created: "新建",
  attached: "关联",
  candidate: "待确认",
  failed: "失败",
};

let textPollTimer = null;

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
    } catch {
      // 保留状态码
    }
    throw new Error(message);
  }
  return response.json();
}

async function loadTextGroups() {
  try {
    const roots = await fetchJSON("/api/modules");
    textGroup.replaceChildren();
    if (!roots.length) {
      const option = document.createElement("option");
      option.textContent = "请先在卡片页创建分组";
      option.value = "";
      textGroup.append(option);
      textSubmit.disabled = true;
      return;
    }
    for (const root of roots) {
      const option = document.createElement("option");
      option.value = String(root.id);
      option.textContent = root.name;
      textGroup.append(option);
    }
  } catch (error) {
    textError.hidden = false;
    textError.textContent = `读取分组失败：${error.message}`;
  }
}

function setTextProgress(phase, processed, total) {
  textProgressWrap.hidden = false;
  textPhase.textContent = phase || "处理中";
  if (total > 0) {
    const percent = Math.min(100, Math.round((processed / total) * 100));
    textProgressFill.style.width = `${percent}%`;
    textProgressText.textContent = `${processed} / ${total}`;
  } else {
    textProgressFill.style.width = "0%";
    textProgressText.textContent = "";
  }
}

function renderTextItems(items) {
  textItems.replaceChildren();
  if (!items || !items.length) {
    textItems.hidden = true;
    return;
  }
  textItems.hidden = false;
  for (const item of items) {
    const row = document.createElement("li");
    row.className = "result-item";

    const tag = document.createElement("span");
    tag.className = `result-tag ${item.action || "failed"}`;
    tag.textContent = ACTION_LABELS[item.action] || item.action || "失败";

    const question = document.createElement("span");
    question.className = "result-question";
    question.textContent = item.question;

    row.append(tag, question);

    if (item.action === "attached" && item.kp_front && item.kp_front !== item.question) {
      const note = document.createElement("span");
      note.className = "result-note";
      note.textContent = `→ ${item.kp_front}`;
      row.append(note);
    }
    textItems.append(row);
  }
}

function renderTextReport(report) {
  textReport.hidden = false;
  setText("text-r-mentions", report.mentions);
  setText("text-r-attached", report.attached);
  setText("text-r-created", report.created);
  setText("text-r-candidates", report.candidates);
  setText("text-r-embeddings", report.embeddings);
  setText("text-r-elapsed", `${(report.elapsed_ms / 1000).toFixed(1)} s`);
  renderList("text-r-errors", "错误", report.errors);
  renderTextItems(report.items);
}

function stopTextPolling() {
  if (textPollTimer) {
    clearInterval(textPollTimer);
    textPollTimer = null;
  }
}

function resetTextSubmit() {
  textSubmit.disabled = false;
  textSubmit.textContent = "提交导入";
  textGroup.disabled = false;
  textTitle.disabled = false;
}

async function pollTextJob(jobId) {
  const job = await fetchJSON(`/api/import/${jobId}`);
  if (job.status === "queued" || job.status === "running") {
    setTextProgress(job.phase, job.processed, job.total);
    return;
  }
  stopTextPolling();
  resetTextSubmit();
  if (job.status === "done") {
    textProgressFill.style.width = "100%";
    textPhase.textContent = "导入完成";
    renderTextReport(job.report);
    textBody.value = "";
    textBody.focus();
    if (!job.report.mentions) {
      textError.hidden = false;
      textError.textContent = "没有识别到面试题，换个文本或补充上下文再试";
    }
  } else {
    textError.hidden = false;
    textError.textContent = job.error || "导入失败";
  }
}

textForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const groupId = Number(textGroup.value);
  const text = textBody.value.trim();
  textError.hidden = true;
  textReport.hidden = true;
  if (!groupId) {
    textError.hidden = false;
    textError.textContent = "请先选择目标组";
    return;
  }
  if (!text) {
    textError.hidden = false;
    textError.textContent = "请粘贴面经正文";
    return;
  }

  stopTextPolling();
  textSubmit.disabled = true;
  textSubmit.textContent = "提交中…";
  textGroup.disabled = true;
  textTitle.disabled = true;
  setTextProgress("排队中", 0, 0);

  try {
    const data = await fetchJSON("/api/import/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: textTitle.value.trim() || null,
        text,
        group_id: groupId,
      }),
    });
    textSubmit.textContent = "处理中…";
    const fail = (error) => {
      stopTextPolling();
      resetTextSubmit();
      textError.hidden = false;
      textError.textContent = error.message || "导入失败";
    };
    textPollTimer = setInterval(() => {
      pollTextJob(data.job_id).catch(fail);
    }, 800);
    pollTextJob(data.job_id).catch(fail);
  } catch (error) {
    resetTextSubmit();
    textError.hidden = false;
    textError.textContent = error.message || "提交失败";
  }
});

loadTextGroups();
