const { fetchJSON, debounce, formatTime } = window.cm;

const list = document.getElementById("post-list");
const toast = document.getElementById("toast");
const fetchButton = document.getElementById("fetch-run");
const progressBox = document.getElementById("fetch-progress");
const summaryBox = document.getElementById("fetch-summary");

const state = {
  page: 1,
  pageSize: 20,
  total: 0,
  q: "",
  groups: [],
  feeds: [],
  editingFeedId: null,
};
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

function fillGroupSelect(select, selected) {
  select.textContent = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "未分组";
  select.append(none);
  for (const group of state.groups) {
    const option = document.createElement("option");
    option.value = String(group.id);
    option.textContent = group.name;
    select.append(option);
  }
  select.value = selected ? String(selected) : "";
}

async function loadGroups() {
  try {
    state.groups = await fetchJSON("/api/modules");
  } catch (error) {
    state.groups = [];
  }
  fillGroupSelect(document.getElementById("feed-group"), null);
}

function renderFeedList(feeds) {
  const container = document.getElementById("feed-list");
  container.textContent = "";
  if (!feeds.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有来源，用上面的表单添加一个 RSS 地址。";
    container.append(empty);
    return;
  }

  for (const feed of feeds) {
    const row = document.createElement("div");
    row.className = "feed-row";

    const info = document.createElement("div");
    info.className = "feed-info";
    const title = document.createElement("h3");
    title.textContent = feed.name || feed.url;
    const url = document.createElement("p");
    url.className = "feed-url";
    url.textContent = feed.url;
    const stats = document.createElement("p");
    stats.className = "feed-stats";
    stats.textContent = [
      feed.group_name ? `组：${feed.group_name}` : "未分组",
      `${feed.posts} 篇`,
      `${feed.mentions} 个问题`,
      feed.last_fetched_at ? `最近抓取 ${formatTime(feed.last_fetched_at)}` : "尚未抓取",
    ].join(" · ");
    info.append(title, url, stats);

    const actions = document.createElement("div");
    actions.className = "feed-actions";

    const groupSelect = document.createElement("select");
    groupSelect.className = "feed-group";
    groupSelect.dataset.feedId = feed.id;
    groupSelect.setAttribute("aria-label", "目标组");
    fillGroupSelect(groupSelect, feed.group_id);

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "check";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.className = "feed-enabled";
    enabled.dataset.feedId = feed.id;
    enabled.checked = feed.enabled;
    enabledLabel.append(enabled, "启用");

    const edit = document.createElement("button");
    edit.className = "ghost feed-edit";
    edit.dataset.feedId = feed.id;
    edit.textContent = "编辑";

    const remove = document.createElement("button");
    remove.className = "ghost danger feed-delete";
    remove.dataset.feedId = feed.id;
    remove.textContent = "删除";

    actions.append(groupSelect, enabledLabel, edit, remove);
    row.append(info, actions);
    container.append(row);
  }
}

async function loadFeeds() {
  try {
    const data = await fetchJSON("/api/feeds");
    state.feeds = data.items;
    const select = document.getElementById("feed-filter");
    const previous = select.value;
    select.textContent = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "全部来源";
    select.append(all);
    for (const feed of data.items) {
      const option = document.createElement("option");
      option.value = feed.id;
      option.textContent = `${feed.group_name || "未分组"} · ${
        feed.name || feed.url
      }（${feed.posts}）`;
      select.append(option);
    }
    if (previous && select.querySelector(`option[value="${previous}"]`)) {
      select.value = previous;
    }
    renderFeedList(data.items);
  } catch (error) {
    // 来源加载失败不影响列表
  }
}

async function patchFeed(feedId, patch) {
  return fetchJSON(`/api/feeds/${feedId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

function resetFeedEditor() {
  state.editingFeedId = null;
  document.getElementById("feed-form").reset();
  document.getElementById("feed-interval").value = 60;
  document.getElementById("feed-submit").textContent = "添加来源";
  document.getElementById("feed-cancel").hidden = true;
}

function editFeed(feedId) {
  const feed = state.feeds.find((item) => String(item.id) === String(feedId));
  if (!feed) return;
  state.editingFeedId = feed.id;
  document.getElementById("feed-url").value = feed.url;
  document.getElementById("feed-name").value = feed.name || "";
  document.getElementById("feed-interval").value = feed.interval_minutes;
  fillGroupSelect(document.getElementById("feed-group"), feed.group_id);
  document.getElementById("feed-submit").textContent = "保存修改";
  document.getElementById("feed-cancel").hidden = false;
  feedPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function submitFeed(event) {
  event.preventDefault();
  const url = document.getElementById("feed-url").value.trim();
  const name = document.getElementById("feed-name").value.trim();
  const groupId = document.getElementById("feed-group").value;
  const interval = Number(document.getElementById("feed-interval").value) || 60;
  const body = {
    url,
    name: name || null,
    interval_minutes: interval,
    group_id: groupId ? Number(groupId) : null,
  };
  try {
    if (state.editingFeedId) {
      await patchFeed(state.editingFeedId, body);
      showToast("已保存来源");
    } else {
      await fetchJSON("/api/feeds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      showToast("已添加来源");
    }
    resetFeedEditor();
    await loadFeeds();
  } catch (error) {
    const fallback = state.editingFeedId ? "保存来源失败" : "添加来源失败";
    showToast(error.message || fallback, true);
  }
}

async function createGroup() {
  const name = window.prompt("新组名称（会作为顶层模块）");
  if (!name || !name.trim()) return;
  try {
    await fetchJSON("/api/modules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    showToast("已新建组");
    await loadGroups();
    await loadFeeds();
  } catch (error) {
    showToast(error.message || "新建组失败", true);
  }
}

async function changeFeed(event) {
  const target = event.target;
  const feedId = target.dataset.feedId;
  if (!feedId) return;
  try {
    if (target.classList.contains("feed-group")) {
      await patchFeed(feedId, {
        group_id: target.value ? Number(target.value) : null,
      });
      showToast("已更新目标组");
      await loadFeeds();
    } else if (target.classList.contains("feed-enabled")) {
      await patchFeed(feedId, { enabled: target.checked });
      showToast(target.checked ? "已启用来源" : "已停用来源");
    }
  } catch (error) {
    showToast(error.message || "更新来源失败", true);
    await loadFeeds();
  }
}

async function deleteFeed(feedId) {
  if (!window.confirm("删除该来源？已有面经的来源不能删除，可先停用。")) return;
  try {
    await fetchJSON(`/api/feeds/${feedId}`, { method: "DELETE" });
    if (String(state.editingFeedId) === String(feedId)) resetFeedEditor();
    showToast("已删除来源");
    await loadFeeds();
    loadPosts();
  } catch (error) {
    showToast(error.message || "删除来源失败", true);
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

const feedPanel = document.getElementById("feed-panel");
document.getElementById("feed-manage").addEventListener("click", async () => {
  feedPanel.hidden = !feedPanel.hidden;
  if (!feedPanel.hidden) await loadFeeds();
});
document.getElementById("feed-close").addEventListener("click", () => {
  feedPanel.hidden = true;
});
document.getElementById("group-create").addEventListener("click", createGroup);
document.getElementById("feed-form").addEventListener("submit", submitFeed);
document.getElementById("feed-cancel").addEventListener("click", resetFeedEditor);
document.getElementById("feed-list").addEventListener("change", changeFeed);
document.getElementById("feed-list").addEventListener("click", (event) => {
  const edit = event.target.closest(".feed-edit");
  if (edit) {
    editFeed(edit.dataset.feedId);
    return;
  }
  const remove = event.target.closest(".feed-delete");
  if (remove) deleteFeed(remove.dataset.feedId);
});

loadGroups();
loadFeeds();
loadPosts();
