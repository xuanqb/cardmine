const { fetchJSON, debounce, formatTime } = window.cm;

const list = document.getElementById("post-list");
const toast = document.getElementById("toast");
const fetchButton = document.getElementById("fetch-run");
const progressBox = document.getElementById("fetch-progress");
const summaryBox = document.getElementById("fetch-summary");

const state = { page: 1, pageSize: 20, total: 0, q: "" };
let toastTimer = null;
let busy = false;

const AI_STATUS = {
  done: { label: "已切题", className: "ready" },
  pending: { label: "待处理", className: "draft" },
  failed: { label: "失败", className: "archived" },
};

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

function renderPosts(data) {
  state.total = data.total;
  list.textContent = "";

  if (!data.items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有面经，点击「立即抓取」开始采集。";
    list.append(empty);
  }

  for (const post of data.items) {
    const row = document.createElement("a");
    row.className = "post-row";
    row.href = `/posts/${post.id}`;

    const main = document.createElement("div");
    main.className = "post-main";
    const title = document.createElement("h3");
    title.textContent = post.title;
    const meta = document.createElement("p");
    meta.className = "post-meta";
    meta.textContent = [
      post.feed_name,
      post.author || "匿名",
      formatTime(post.fetched_at),
      `${post.mentions} 个问题`,
    ].join(" · ");
    main.append(title, meta);

    const status = AI_STATUS[post.ai_status] || {
      label: post.ai_status,
      className: "",
    };
    const badge = document.createElement("span");
    badge.className = `badge ${status.className}`;
    badge.textContent = status.label;

    row.append(main, badge);
    list.append(row);
  }

  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-info").textContent =
    `第 ${state.page} / ${pages} 页 · 共 ${state.total} 篇`;
  document.getElementById("prev").disabled = state.page <= 1;
  document.getElementById("next").disabled = state.page >= pages;
}

async function loadPosts() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
  });
  const feedId = document.getElementById("feed-filter").value;
  const aiStatus = document.getElementById("ai-filter").value;
  if (feedId) params.set("feed_id", feedId);
  if (aiStatus) params.set("ai_status", aiStatus);
  if (state.q) params.set("q", state.q);

  try {
    const data = await fetchJSON(`/api/posts?${params.toString()}`);
    renderPosts(data);
  } catch (error) {
    showToast(error.message || "读取面经失败", true);
  }
}

async function loadFeeds() {
  try {
    const data = await fetchJSON("/api/feeds");
    const select = document.getElementById("feed-filter");
    for (const feed of data.items) {
      const option = document.createElement("option");
      option.value = feed.id;
      option.textContent = `${feed.name || feed.url}（${feed.posts}）`;
      select.append(option);
    }
  } catch (error) {
    // 来源加载失败不影响列表
  }
}

function renderProgress(job) {
  progressBox.hidden = false;
  document.getElementById("fetch-phase").textContent = job.phase || "运行中";
  const total = job.total || 0;
  const done = job.processed || 0;
  document.getElementById("fetch-count").textContent = total
    ? `${done} / ${total}`
    : "";
  document.getElementById("fetch-fill").style.width = total
    ? `${Math.min(100, Math.round((done / total) * 100))}%`
    : "0%";
}

function renderSummary(report) {
  summaryBox.hidden = false;
  const parts = [
    `抓取 ${report.items} 条`,
    `新增 ${report.new_posts} 篇`,
    `问题 ${report.mentions} 个`,
    `新卡 ${report.created}`,
    `归并 ${report.attached}`,
    `待确认 ${report.candidates}`,
    `失败 ${report.ai_failed}`,
  ];
  if (report.push) {
    parts.push(`推送 +${report.push.added} / 更新 ${report.push.updated}`);
  }
  if (report.errors && report.errors.length) {
    parts.push(`错误 ${report.errors.length} 条`);
  }
  summaryBox.textContent = parts.join(" · ");
  if (report.errors && report.errors.length) {
    const detail = document.createElement("span");
    detail.className = "hint-error";
    detail.textContent = `（${report.errors[0]}）`;
    summaryBox.append(detail);
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
          reject(new Error(job.error || "采集失败"));
        } else {
          renderProgress(job);
        }
      } catch (error) {
        clearInterval(timer);
        reject(error);
      }
    }, 800);
  });
}

async function runFetch() {
  if (busy) return;
  busy = true;
  fetchButton.disabled = true;
  summaryBox.hidden = true;
  renderProgress({ phase: "排队中", processed: 0, total: 0 });

  try {
    const { job_id } = await fetchJSON("/api/fetch/run", { method: "POST" });
    const job = await pollJob(job_id);
    progressBox.hidden = true;
    renderSummary(job.report || {});
    showToast("抓取完成");
    state.page = 1;
    loadPosts();
    loadFeeds();
  } catch (error) {
    progressBox.hidden = true;
    showToast(error.message || "抓取失败", true);
  } finally {
    busy = false;
    fetchButton.disabled = false;
  }
}

document.getElementById("search").addEventListener(
  "input",
  debounce((event) => {
    state.q = event.target.value.trim();
    state.page = 1;
    loadPosts();
  }, 300),
);
for (const id of ["feed-filter", "ai-filter"]) {
  document.getElementById(id).addEventListener("change", () => {
    state.page = 1;
    loadPosts();
  });
}
document.getElementById("prev").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadPosts();
  }
});
document.getElementById("next").addEventListener("click", () => {
  if (state.page * state.pageSize < state.total) {
    state.page += 1;
    loadPosts();
  }
});
fetchButton.addEventListener("click", runFetch);

loadFeeds();
loadPosts();
