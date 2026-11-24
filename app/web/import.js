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
