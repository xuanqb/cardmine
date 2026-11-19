const { fetchJSON, formatTime, flattenModules } = window.cm;

const toast = document.getElementById("toast");
const moduleSelect = document.getElementById("module-select");
const statusSelect = document.getElementById("status-select");
const exportHint = document.getElementById("export-hint");
const buttons = [
  document.getElementById("export-apkg"),
  document.getElementById("export-csv"),
  document.getElementById("backup-db"),
  document.getElementById("backup-media"),
];

let toastTimer = null;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 3000);
}

function setBusy(value) {
  for (const button of buttons) button.disabled = value;
}

async function loadModules() {
  try {
    const tree = await fetchJSON("/api/modules");
    for (const node of flattenModules(tree)) {
      const option = document.createElement("option");
      option.value = String(node.id);
      option.textContent = `${"\u3000".repeat(node.depth)}${node.name}（${node.card_count}）`;
      moduleSelect.append(option);
    }
  } catch (error) {
    showToast("读取模块失败", true);
  }
}

function query() {
  const params = new URLSearchParams();
  if (moduleSelect.value) params.set("module_id", moduleSelect.value);
  if (statusSelect.value) params.set("status", statusSelect.value);
  const text = params.toString();
  return text ? `?${text}` : "";
}

function filenameFrom(response, fallback) {
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  if (!match) return fallback;
  try {
    return decodeURIComponent(match[1]);
  } catch (error) {
    return match[1];
  }
}

async function download(url, fallback, onDone) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data.detail) message = String(data.detail);
    } catch (error) {
      // 保留状态码
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  const filename = filenameFrom(response, fallback);
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(href), 3000);

  if (onDone) onDone(response.headers);
}

async function run(task) {
  setBusy(true);
  exportHint.hidden = true;
  try {
    await task();
  } catch (error) {
    showToast(error.message || "操作失败", true);
  } finally {
    setBusy(false);
  }
}

document.getElementById("export-apkg").addEventListener("click", () =>
  run(async () => {
    await download(`/api/export/apkg${query()}`, "cardmine.apkg", (headers) => {
      const missing = Number(headers.get("X-Cardmine-Missing-Media") || 0);
      if (missing > 0) {
        exportHint.hidden = false;
        exportHint.textContent = `导出完成，但有 ${missing} 个媒体文件缺失，Anki 中对应图片会无法显示。`;
        showToast(`导出完成，缺失 ${missing} 个媒体`, true);
      } else {
        showToast("apkg 导出完成");
      }
    });
  }),
);

document.getElementById("export-csv").addEventListener("click", () =>
  run(async () => {
    await download(`/api/export/csv${query()}`, "cardmine.csv", (headers) => {
      const cards = headers.get("X-Cardmine-Cards");
      showToast(cards ? `CSV 导出完成：${cards} 张` : "CSV 导出完成");
    });
  }),
);

document.getElementById("backup-db").addEventListener("click", () =>
  run(async () => {
    await download("/api/export/backup/db", "cardmine-backup.db");
    showToast("数据库备份已下载");
  }),
);

document.getElementById("backup-media").addEventListener("click", () =>
  run(async () => {
    await download("/api/export/backup/media", "cardmine-media.csv");
    showToast("媒体清单已下载");
  }),
);

async function loadSyncLogs() {
  const tbody = document.getElementById("sync-log-rows");
  try {
    const data = await fetchJSON("/api/anki/logs?status=failed&limit=10");
    document.getElementById("sync-log-note").textContent = `共 ${data.total} 条失败`;
    tbody.textContent = "";
    if (!data.items.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "muted";
      td.textContent = "暂无失败记录";
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const item of data.items) {
      const tr = document.createElement("tr");
      const front = document.createElement("td");
      front.textContent =
        item.front_md.length > 60 ? `${item.front_md.slice(0, 60)}…` : item.front_md;
      const deck = document.createElement("td");
      deck.textContent = item.pushed_deck || "—";
      const time = document.createElement("td");
      time.textContent = formatTime(item.pushed_at);
      const error = document.createElement("td");
      error.className = "text-danger";
      error.textContent = item.last_error;
      tr.append(front, deck, time, error);
      tbody.append(tr);
    }
  } catch (error) {
    document.getElementById("sync-log-note").textContent = "读取失败";
  }
}

async function loadAnki() {
  try {
    const data = await fetchJSON("/api/stats/dashboard");
    const anki = data.anki;
    const box = document.getElementById("anki-state");
    const error = document.getElementById("anki-error");
    box.classList.toggle("ok", anki.connected);
    box.classList.toggle("error", !anki.connected);
    document.getElementById("anki-text").textContent = anki.connected
      ? `已连接 · v${anki.version}`
      : "未连接";
    document.getElementById("anki-url").textContent = anki.url;
    document.getElementById("anki-pending").textContent = `${data.cards.pending_push} 张`;
    document.getElementById("anki-failed").textContent = `${data.sync.failed} 张`;
    document.getElementById("anki-last-push").textContent = formatTime(data.sync.last_push_at);
    if (anki.connected) {
      error.hidden = true;
      error.textContent = "";
    } else {
      error.hidden = false;
      error.textContent = anki.error || "无法连接 Anki 容器";
    }
  } catch (error) {
    showToast("Anki 状态读取失败", true);
  }
}

loadModules();
loadAnki();
loadSyncLogs();
setInterval(() => {
  loadAnki();
  loadSyncLogs();
}, 30000);
