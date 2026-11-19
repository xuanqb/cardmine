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
