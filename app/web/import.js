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
const textPreviewButton = document.getElementById("text-preview");
const textError = document.getElementById("text-error");
const textPreviewBox = document.getElementById("text-preview-box");
const textMentions = document.getElementById("text-mentions");
const textCount = document.getElementById("text-count");
const textAll = document.getElementById("text-all");
const textCommit = document.getElementById("text-commit");
const textCommitHint = document.getElementById("text-commit-hint");
const textReport = document.getElementById("text-report");

const TOPIC_LABELS = { tech: "技术", algo: "算法", nontech: "非技术" };
let previewItems = [];
let previewSnapshot = "";

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
      textPreviewButton.disabled = true;
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

function selectedTextItems() {
  return Array.from(
    textMentions.querySelectorAll('input[type="checkbox"]:checked'),
  ).map((box) => previewItems[Number(box.dataset.index)]);
}

function syncCommitHint() {
  const count = selectedTextItems().length;
  textCommit.disabled = count === 0;
  textCommitHint.textContent = count ? `将导入 ${count} 题` : "至少选择一题";
}

function renderTextPreview(items) {
  textMentions.replaceChildren();
  items.forEach((item, index) => {
    const row = document.createElement("label");
    row.className = "preview-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.dataset.index = String(index);
    box.addEventListener("change", () => {
      textAll.checked = selectedTextItems().length === previewItems.length;
      syncCommitHint();
    });

    const body = document.createElement("div");
    body.className = "preview-item-body";
    const question = document.createElement("p");
    question.className = "preview-question";
    question.textContent = item.question;
    const meta = document.createElement("p");
    meta.className = "preview-meta";
    meta.textContent = [
      item.module || "未指定模块",
      item.section || "",
      TOPIC_LABELS[item.topic] || item.topic,
      item.subtype === "personal" ? "个人经历" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    body.append(question, meta);
    row.append(box, body);
    textMentions.append(row);
  });
  textCount.textContent = `共 ${items.length} 题`;
  textAll.checked = true;
  syncCommitHint();
}

textAll.addEventListener("change", () => {
  for (const box of textMentions.querySelectorAll('input[type="checkbox"]')) {
    box.checked = textAll.checked;
  }
  syncCommitHint();
});

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

  textPreviewButton.disabled = true;
  textPreviewButton.textContent = "解析中…";
  try {
    const data = await fetchJSON("/api/import/text/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: textTitle.value.trim() || null,
        text,
        group_id: groupId,
      }),
    });
    previewItems = data.mentions || [];
    if (!previewItems.length) {
      textPreviewBox.hidden = true;
      textError.hidden = false;
      textError.textContent = "没有识别到面试题，试试补充上下文或换一段文本";
      return;
    }
    renderTextPreview(previewItems);
    previewSnapshot = `${groupId}\n${text}`;
    textPreviewBox.hidden = false;
  } catch (error) {
    textPreviewBox.hidden = true;
    textError.hidden = false;
    textError.textContent = error.message;
  } finally {
    textPreviewButton.disabled = false;
    textPreviewButton.textContent = "解析预览";
  }
});

textCommit.addEventListener("click", async () => {
  const items = selectedTextItems();
  if (!items.length) return;
  const groupId = Number(textGroup.value);
  const text = textBody.value.trim();
  if (previewSnapshot !== `${groupId}\n${text}`) {
    textError.hidden = false;
    textError.textContent = "正文或分组已修改，请重新解析预览";
    return;
  }
  textError.hidden = true;
  textCommit.disabled = true;
  textCommit.textContent = "导入中…";
  try {
    const report = await fetchJSON("/api/import/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: textTitle.value.trim() || null,
        text,
        group_id: groupId,
        mentions: items,
      }),
    });
    textReport.hidden = false;
    setText("text-r-mentions", report.mentions);
    setText("text-r-attached", report.attached);
    setText("text-r-created", report.created);
    setText("text-r-candidates", report.candidates);
    setText("text-r-embeddings", report.embeddings);
    renderList("text-r-errors", "错误", report.errors);
    textPreviewBox.hidden = true;
    previewItems = [];
    previewSnapshot = "";
    textBody.value = "";
    textBody.focus();
  } catch (error) {
    textError.hidden = false;
    textError.textContent = error.message;
  } finally {
    textCommit.disabled = false;
    textCommit.textContent = "确认导入";
    syncCommitHint();
  }
});

loadTextGroups();
