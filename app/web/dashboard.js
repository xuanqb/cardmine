const { fetchJSON, formatTime } = window.cm;

const toast = document.getElementById("toast");
const alertList = document.getElementById("alert-list");
const feedRows = document.getElementById("feed-rows");
const logRows = document.getElementById("log-rows");
const fetchButton = document.getElementById("fetch-run");
const fetchHint = document.getElementById("fetch-hint");

let toastTimer = null;
let busy = false;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2800);
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function row(...cells) {
  const tr = document.createElement("tr");
  tr.append(...cells);
  return tr;
}

function badge(text, className) {
  const span = document.createElement("span");
  span.className = `badge ${className || ""}`.trim();
  span.textContent = text;
  return span;
}

function renderAlerts(alerts) {
  alertList.textContent = "";
  if (!alerts.length) {
    const li = document.createElement("li");
    li.className = "alert-item empty";
    li.textContent = "暂无告警";
    alertList.append(li);
    return;
  }
  for (const alert of alerts) {
    const li = document.createElement("li");
    li.className = `alert-item ${alert.level || "info"}`;
    if (alert.items && alert.items.length) {
      const details = document.createElement("details");
      details.className = "alert-details";
      const summary = document.createElement("summary");
      summary.append(alert.text);
      const expand = document.createElement("span");
      expand.className = "alert-expand";
      expand.textContent = "查看列表";
      summary.append(expand);
      const list = document.createElement("ul");
      list.className = "alert-detail-list";
      for (const item of alert.items) {
        const entry = document.createElement("li");
        if (typeof item === "string") {
          entry.textContent = item;
          list.appendChild(entry);
          continue;
        }
        const name = document.createElement("code");
        name.textContent = item.name;
        entry.appendChild(name);
        if (item.card_count) {
          const meta = document.createElement("div");
          meta.className = "media-ref-meta";
          meta.textContent = `被 ${item.card_count} 张卡片引用`;
          entry.appendChild(meta);
        }
        if (item.cards && item.cards.length) {
          const cards = document.createElement("ul");
          cards.className = "media-card-list";
          for (const card of item.cards) {
            const row = document.createElement("li");
            const link = document.createElement("a");
            link.href = `/cards/${card.id}`;
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = card.front
              ? `#${card.id} ${card.front}`
              : `#${card.id}`;
            row.appendChild(link);
            const meta = document.createElement("span");
            meta.className = "media-card-meta";
            const parts = [];
            if (card.module) parts.push(card.module.replaceAll("/", " / "));
            if (card.where && card.where.length) {
              parts.push(
                card.where
                  .map((where) => (where === "front" ? "正面" : "背面"))
                  .join("、"),
              );
            }
            if (parts.length) {
              meta.textContent = ` · ${parts.join(" · ")}`;
              row.appendChild(meta);
            }
            cards.appendChild(row);
          }
          entry.appendChild(cards);
          if (item.card_count > item.cards.length) {
            const more = document.createElement("div");
            more.className = "media-ref-meta";
            more.textContent = `另有 ${
              item.card_count - item.cards.length
            } 张卡片引用`;
            entry.appendChild(more);
          }
        }
        list.appendChild(entry);
      }
      const hint = document.createElement("p");
      hint.className = "alert-detail-hint";
      hint.textContent =
        "点击卡片可查看引用位置；补齐媒体文件或重新导入包含该媒体的 apkg 后刷新。";
      details.append(summary, hint, list);
      li.append(details);
    } else {
      li.textContent = alert.text;
    }
    alertList.append(li);
  }
}

function renderFeeds(data) {
  const feeds = data.feeds || [];
  feedRows.textContent = "";
  if (!feeds.length) {
    feedRows.append(row(cell("还没有 RSS 源", "muted"), cell("—"), cell("—"), cell("—"), cell("—")));
  }
  for (const feed of feeds) {
    const latest = feed.latest;
    let result = "暂无记录";
    let resultClass = "muted";
    let status;
    if (latest) {
      if (latest.error) {
        result = `失败：${latest.error}`;
        resultClass = "text-danger";
      } else {
        result = `${latest.items} 条 / 新增 ${latest.new_posts}`;
        resultClass = "";
      }
    }
    if (latest && latest.all_new) {
      const flag = document.createElement("span");
      flag.className = "badge draft";
      flag.textContent = "可能漏抓";
      const wrap = document.createElement("span");
      wrap.className = "cell-inline";
      wrap.append(result, flag);
      result = wrap;
    }
    if (feed.stale) status = badge("已超期", "draft");
    else if (data.running) status = badge("采集中", "topic-tech");
    else status = badge("正常", "ready");

    const resultCell = document.createElement("td");
    if (resultClass) resultCell.className = resultClass;
    if (typeof result === "string") resultCell.textContent = result;
    else resultCell.append(result);

    feedRows.append(
      row(
        cell(feed.name || feed.url),
        cell(formatTime(feed.last_fetched_at)),
        cell(`${feed.interval_minutes} 分钟`),
        resultCell,
        (() => {
          const td = document.createElement("td");
          td.append(status);
          return td;
        })(),
      ),
    );
  }

  const last = data.last_success_at;
  document.getElementById("fetch-summary").textContent = data.running
    ? "采集任务运行中…"
    : last
      ? `最近成功：${formatTime(last)}`
      : "尚未成功抓取";
}

function renderLogs(data) {
  const logs = data.recent || [];
  logRows.textContent = "";
  if (!logs.length) {
    logRows.append(row(cell("暂无抓取记录", "muted"), cell("—"), cell("—"), cell("—"), cell("—")));
    return;
  }
  for (const log of logs) {
    let result = "成功";
    let className = "";
    if (log.error) {
      result = `失败：${log.error}`;
      className = "text-danger";
    } else if (log.all_new) {
      result = "疑似整体滚动";
      className = "text-warning";
    } else if (!log.finished_at) {
      result = "进行中";
      className = "muted";
    }
    logRows.append(
      row(
        cell(formatTime(log.started_at)),
        cell(log.feed_name),
        cell(`${log.items} / ${log.new_posts}`),
        cell(formatDuration(log.elapsed_ms)),
        cell(result, className),
      ),
    );
  }
}

function renderStats(data) {
  document.getElementById("stat-draft").textContent = data.cards.draft;
  document.getElementById("stat-dup").textContent = data.review.pending_candidates;
  document.getElementById("stat-sync-failed").textContent = data.sync.failed;
  document.getElementById("stat-ai-failed").textContent = data.ai.failed_posts;
  document.getElementById("stat-media-missing").textContent = data.media.missing_count;

  const anki = data.anki;
  document.getElementById("stat-anki").textContent = anki.connected ? "已连接" : "未连接";
  document.getElementById("stat-anki-note").textContent = anki.connected
    ? `v${anki.version} · ${anki.model}`
    : anki.error || "无法连接";

  document.getElementById("cards-total").textContent = `${data.cards.total} 张`;
  document.getElementById("cards-ready").textContent = `${data.cards.ready} 张`;
  document.getElementById("cards-archived").textContent = `${data.cards.archived} 张`;
  document.getElementById("cards-matched").textContent = `${data.cards.matched} 张`;

  document.getElementById("anki-pending").textContent = `${data.cards.pending_push} 张`;
  document.getElementById("anki-last-push").textContent = formatTime(data.sync.last_push_at);
  document.getElementById("anki-url").textContent = anki.url;

  const ankiBox = document.getElementById("anki-state");
  const ankiError = document.getElementById("anki-error");
  ankiBox.classList.toggle("ok", anki.connected);
  ankiBox.classList.toggle("error", !anki.connected);
  document.getElementById("anki-text").textContent = anki.connected
    ? `已连接 · v${anki.version}`
    : "未连接";
  if (anki.connected) {
    ankiError.hidden = true;
    ankiError.textContent = "";
  } else {
    ankiError.hidden = false;
    ankiError.textContent = anki.error || "无法连接 Anki 容器";
  }

  if (data.ai.failed_posts) {
    document.getElementById("stat-ai-failed").title = (data.ai.recent || [])
      .map((item) => item.title)
      .join("\n");
  }
}

function renderMediaSample(media) {
  const missing = document.getElementById("stat-media-missing");
  const names = media.missing || media.sample || [];
  if (media.missing_count && names.length) {
    missing.title = `缺失媒体（${media.missing_count}）：\n${names.join("\n")}`;
  }
}

async function load() {
  try {
    const data = await fetchJSON("/api/stats/dashboard");
    renderAlerts(data.alerts || []);
    renderStats(data);
    renderFeeds(data.fetch);
    renderLogs(data.fetch);
    renderMediaSample(data.media);
  } catch (error) {
    renderAlerts([{ level: "danger", text: `仪表盘数据加载失败：${error.message}` }]);
    showToast("仪表盘数据加载失败", true);
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error("bad status");
    document.getElementById("health").className = "status ok";
    document.getElementById("health-text").textContent = "运行正常";
  } catch (error) {
    document.getElementById("health").className = "status error";
    document.getElementById("health-text").textContent = "服务不可用";
  }
}

function pollJob(jobId) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const job = await fetchJSON(`/api/fetch/jobs/${jobId}`);
        if (job.status === "done") {
          clearInterval(timer);
          resolve(job);
        } else if (job.status === "failed") {
          clearInterval(timer);
          reject(new Error(job.error || "抓取失败"));
        } else {
          fetchHint.hidden = false;
          fetchHint.textContent = `${job.phase || "运行中"}${
            job.total ? ` ${job.processed}/${job.total}` : ""
          }`;
        }
      } catch (error) {
        clearInterval(timer);
        reject(error);
      }
    }, 900);
  });
}

fetchButton.addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  fetchButton.disabled = true;
  fetchHint.hidden = false;
  fetchHint.textContent = "排队中…";
  try {
    const { job_id } = await fetchJSON("/api/fetch/run", { method: "POST" });
    const job = await pollJob(job_id);
    const report = job.report || {};
    showToast(
      `抓取完成：新增 ${report.new_posts || 0} 篇，提取 ${report.mentions || 0} 条问题`,
    );
    await load();
  } catch (error) {
    showToast(error.message || "抓取失败", true);
  } finally {
    busy = false;
    fetchButton.disabled = false;
    fetchHint.hidden = true;
  }
});

document.getElementById("refresh").addEventListener("click", load);

loadHealth();
load();
setInterval(loadHealth, 30000);
setInterval(() => {
  if (!busy) load();
}, 30000);
