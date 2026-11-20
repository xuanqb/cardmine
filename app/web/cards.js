const state = {
  moduleId: null,
  status: "",
  sort: "updated_desc",
  q: "",
  page: 1,
  pageSize: 20,
  total: 0,
};

const treeBox = document.getElementById("module-tree");
const listBox = document.getElementById("card-list");
const searchInput = document.getElementById("search");
const statusFilter = document.getElementById("status-filter");
const sortSelect = document.getElementById("sort");
const prevButton = document.getElementById("prev");
const nextButton = document.getElementById("next");
const pageInfo = document.getElementById("page-info");
const treeAll = document.getElementById("tree-all");

const FILTER_KEY = "cardmine.cards.filters";
const SORT_VALUES = new Set(["updated_desc", "mentions_desc", "recent_desc"]);
const STATUS_VALUES = new Set(["", "draft", "ready", "archived"]);

function restoreState() {
  const params = new URLSearchParams(window.location.search);
  const hasQuery = [...params.keys()].length > 0;
  let saved = null;
  if (!hasQuery) {
    try {
      saved = JSON.parse(sessionStorage.getItem(FILTER_KEY) || "null");
    } catch {
      saved = null;
    }
  }

  const source = hasQuery
    ? {
        moduleId: params.has("module_id") ? Number(params.get("module_id")) : null,
        status: params.get("status") || "",
        sort: params.get("sort") || state.sort,
        q: params.get("q") || "",
        page: Number(params.get("page") || 1),
      }
    : saved;
  if (!source) return;

  if (Number.isInteger(source.moduleId) && source.moduleId > 0) {
    state.moduleId = source.moduleId;
  }
  if (STATUS_VALUES.has(source.status)) state.status = source.status;
  if (SORT_VALUES.has(source.sort)) state.sort = source.sort;
  if (typeof source.q === "string") state.q = source.q;
  if (Number.isInteger(source.page) && source.page > 0) state.page = source.page;
}

function persistState() {
  const params = new URLSearchParams();
  if (state.moduleId !== null) params.set("module_id", String(state.moduleId));
  if (state.status) params.set("status", state.status);
  if (state.q) params.set("q", state.q);
  params.set("sort", state.sort);
  params.set("page", String(state.page));
  window.history.replaceState(null, "", `/cards?${params.toString()}`);
  try {
    sessionStorage.setItem(FILTER_KEY, JSON.stringify(state));
  } catch {
    /* 隐私模式下忽略 */
  }
}

function syncControls() {
  searchInput.value = state.q;
  statusFilter.value = state.status;
  sortSelect.value = state.sort;
}

function setActiveTreeById(moduleId) {
  treeAll.classList.toggle("active", moduleId === null);
  document.querySelectorAll(".tree-item[data-module-id]").forEach((item) => {
    item.classList.toggle("active", Number(item.dataset.moduleId) === moduleId);
  });
}

function setActiveTree(button) {
  document
    .querySelectorAll(".tree-item.active")
    .forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
}

function buildTree(nodes, depth = 0) {
  const fragment = document.createDocumentFragment();
  for (const node of nodes) {
    const wrapper = document.createElement("div");
    wrapper.className = "tree-node";

    const row = document.createElement("div");
    row.className = "tree-row";
    row.style.paddingLeft = `${depth * 14}px`;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "tree-item";
    button.dataset.moduleId = String(node.id);

    const name = document.createElement("span");
    name.textContent = node.name;
    const count = document.createElement("span");
    count.className = "tree-count";
    count.textContent = node.card_count;

    button.append(name, count);
    button.addEventListener("click", () => {
      state.moduleId = node.id;
      state.page = 1;
      setActiveTree(button);
      loadCards();
    });

    const children = node.children || [];
    if (children.length) {
      const childBox = document.createElement("div");
      childBox.className = "tree-children";
      childBox.appendChild(buildTree(children, depth + 1));
      wrapper.appendChild(row);
      wrapper.appendChild(childBox);

      toggle.textContent = "▾";
      toggle.setAttribute("aria-expanded", "true");
      toggle.setAttribute("aria-label", `折叠 ${node.name}`);
      toggle.addEventListener("click", () => {
        const collapsed = wrapper.classList.toggle("collapsed");
        toggle.textContent = collapsed ? "▸" : "▾";
        toggle.setAttribute("aria-expanded", String(!collapsed));
        toggle.setAttribute(
          "aria-label",
          `${collapsed ? "展开" : "折叠"} ${node.name}`,
        );
      });
      row.append(toggle, button);
    } else {
      toggle.classList.add("tree-toggle-empty");
      toggle.tabIndex = -1;
      toggle.setAttribute("aria-hidden", "true");
      row.append(toggle, button);
      wrapper.appendChild(row);
    }

    fragment.appendChild(wrapper);
  }
  return fragment;
}

function renderCardRow(item) {
  const row = document.createElement("a");
  row.className = "card-row";
  row.href = `/cards/${item.id}`;

  const main = document.createElement("div");
  main.className = "card-main";

  const front = document.createElement("div");
  front.className = "card-front";
  front.textContent = item.front_md.replace(/\s+/g, " ").slice(0, 160);

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const parts = [
    item.module_path ? item.module_path.replaceAll("/", " / ") : "未分类",
    `出现 ${item.mention_count} 次`,
    `更新于 ${window.cm.formatTime(item.updated_at)}`,
  ];
  meta.textContent = parts.join(" · ");

  main.append(front, meta);

  const status = window.cm.statusMeta(item.status);
  const badge = document.createElement("span");
  badge.className = `badge ${status.className}`;
  badge.textContent = status.label;

  row.append(main, badge);
  return row;
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  pageInfo.textContent = `第 ${state.page} / ${pages} 页 · 共 ${state.total} 张`;
  prevButton.disabled = state.page <= 1;
  nextButton.disabled = state.page >= pages;
}

async function loadCards() {
  persistState();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    sort: state.sort,
  });
  if (state.moduleId !== null) params.set("module_id", String(state.moduleId));
  if (state.status) params.set("status", state.status);
  if (state.q) params.set("q", state.q);

  listBox.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "empty-state";
  loading.textContent = "加载中…";
  listBox.appendChild(loading);

  try {
    const data = await window.cm.fetchJSON(`/api/cards?${params.toString()}`);
    state.total = data.total;
    listBox.replaceChildren();
    if (data.items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "没有符合条件的卡片";
      listBox.appendChild(empty);
    } else {
      for (const item of data.items) {
        listBox.appendChild(renderCardRow(item));
      }
    }
    renderPager();
  } catch (error) {
    listBox.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "empty-state";
    failed.textContent = error.message;
    listBox.appendChild(failed);
  }
}

async function loadModules() {
  try {
    const tree = await window.cm.fetchJSON("/api/modules");
    treeBox.replaceChildren(buildTree(tree));
    if (
      state.moduleId !== null &&
      !treeBox.querySelector(`[data-module-id="${state.moduleId}"]`)
    ) {
      state.moduleId = null;
      persistState();
    }
    setActiveTreeById(state.moduleId);
  } catch (error) {
    treeBox.textContent = "目录加载失败";
  }
}

treeAll.addEventListener("click", () => {
  state.moduleId = null;
  state.page = 1;
  setActiveTree(treeAll);
  loadCards();
});

const reloadAfterSearch = window.cm.debounce(() => loadCards(), 300);

searchInput.addEventListener("input", () => {
  state.q = searchInput.value.trim();
  state.page = 1;
  persistState();
  reloadAfterSearch();
});

statusFilter.addEventListener("change", () => {
  state.status = statusFilter.value;
  state.page = 1;
  loadCards();
});

sortSelect.addEventListener("change", () => {
  state.sort = sortSelect.value;
  state.page = 1;
  loadCards();
});

prevButton.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadCards();
  }
});

nextButton.addEventListener("click", () => {
  const pages = Math.ceil(state.total / state.pageSize);
  if (state.page < pages) {
    state.page += 1;
    loadCards();
  }
});

restoreState();
syncControls();
loadModules();
loadCards();
