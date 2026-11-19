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

function setActiveTree(button) {
  document
    .querySelectorAll(".tree-item.active")
    .forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
}

function buildTree(nodes, depth = 0) {
  const fragment = document.createDocumentFragment();
  for (const node of nodes) {
    const button = document.createElement("button");
    button.className = "tree-item";
    button.style.paddingLeft = `${10 + depth * 14}px`;

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
    fragment.appendChild(button);

    if (node.children && node.children.length) {
      fragment.appendChild(buildTree(node.children, depth + 1));
    }
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

searchInput.addEventListener(
  "input",
  window.cm.debounce(() => {
    state.q = searchInput.value.trim();
    state.page = 1;
    loadCards();
  })
);

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

loadModules();
loadCards();
