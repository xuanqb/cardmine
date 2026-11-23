const state = {
  moduleId: null,
  statuses: [],
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
const mergeButton = document.getElementById("merge-button");
const mergeModal = document.getElementById("merge-modal");
const mergeCardList = document.getElementById("merge-card-list");
const mergeOptions = document.getElementById("merge-options");
const mergeWarnings = document.getElementById("merge-warnings");
const mergeCancel = document.getElementById("merge-cancel");
const mergeConfirm = document.getElementById("merge-confirm");

const selectedIds = new Set();
let mergeDetails = [];
let mergeSurvivorId = null;
let mergeBusy = false;
let deleteUnclaimedNotes = false;

const FILTER_KEY = "cardmine.cards.filters";
const SORT_VALUES = new Set(["updated_desc", "mentions_desc", "recent_desc"]);
const STATUS_VALUES = new Set(["draft", "ready", "archived"]);

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
        statuses: params.getAll("status").filter((value) => STATUS_VALUES.has(value)),
        sort: params.get("sort") || state.sort,
        q: params.get("q") || "",
        page: Number(params.get("page") || 1),
      }
    : saved;
  if (!source) return;

  if (Number.isInteger(source.moduleId) && source.moduleId > 0) {
    state.moduleId = source.moduleId;
  }
  const statuses = Array.isArray(source.statuses)
    ? source.statuses
    : typeof source.status === "string" && source.status
      ? [source.status]
      : [];
  state.statuses = [...new Set(statuses.filter((s) => STATUS_VALUES.has(s)))];
  if (SORT_VALUES.has(source.sort)) state.sort = source.sort;
  if (typeof source.q === "string") state.q = source.q;
  if (Number.isInteger(source.page) && source.page > 0) state.page = source.page;
}

function persistState() {
  const params = new URLSearchParams();
  if (state.moduleId !== null) params.set("module_id", String(state.moduleId));
  for (const status of state.statuses) params.append("status", status);
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
  document.querySelectorAll("#status-filter .chip").forEach((chip) => {
    const value = chip.dataset.status;
    chip.classList.toggle(
      "active",
      value === ""
        ? state.statuses.length === 0
        : state.statuses.includes(value)
    );
  });
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
  const wrapper = document.createElement("div");
  wrapper.className = "card-item";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "card-select";
  checkbox.checked = selectedIds.has(item.id);
  checkbox.setAttribute("aria-label", "选择该卡片用于合并");
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) {
      selectedIds.add(item.id);
    } else {
      selectedIds.delete(item.id);
    }
    wrapper.classList.toggle("selected", checkbox.checked);
    updateSelectionUI();
  });
  wrapper.classList.toggle("selected", checkbox.checked);

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

  const right = document.createElement("div");
  right.className = "card-badges";
  const origin = window.cm.originMeta(item.origin);
  const originBadge = document.createElement("span");
  originBadge.className = `badge ${origin.className}`;
  originBadge.textContent = origin.label;
  right.appendChild(originBadge);
  if (item.has_ai_answer) {
    const aiBadge = document.createElement("span");
    aiBadge.className = "badge ai";
    aiBadge.textContent = "AI";
    right.appendChild(aiBadge);
  }
  right.appendChild(badge);

  row.append(main, right);
  wrapper.append(checkbox, row);
  return wrapper;
}

function updateSelectionUI() {
  mergeButton.disabled = selectedIds.size < 2;
  mergeButton.textContent = selectedIds.size
    ? `合并所选（${selectedIds.size}）`
    : "合并所选";
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
  for (const status of state.statuses) params.append("status", status);
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

function renderMergeChoices() {
  mergeCardList.replaceChildren();
  for (const card of mergeDetails) {
    const label = document.createElement("label");
    label.className = "merge-choice";

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "merge-survivor";
    radio.value = String(card.id);
    radio.checked = card.id === mergeSurvivorId;
    radio.addEventListener("change", () => {
      mergeSurvivorId = card.id;
    });

    const body = document.createElement("div");
    body.className = "merge-choice-body";

    const front = document.createElement("div");
    front.className = "merge-choice-front";
    front.textContent = card.front_md.replace(/\s+/g, " ").slice(0, 140);

    const meta = document.createElement("div");
    meta.className = "merge-choice-meta";
    const status = window.cm.statusMeta(card.status);
    meta.textContent = [
      card.module_path ? card.module_path.replaceAll("/", " / ") : "未分类",
      status.label,
      card.anki_note_id ? `Anki note ${card.anki_note_id}` : "无 Anki note",
    ].join(" · ");

    body.append(front, meta);
    label.append(radio, body);
    mergeCardList.appendChild(label);
  }
}

function renderMergeOptions() {
  mergeOptions.replaceChildren();
  deleteUnclaimedNotes = false;
  if (!mergeDetails.some((card) => card.anki_note_id)) return;

  const label = document.createElement("label");
  label.className = "merge-option";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.addEventListener("change", () => {
    deleteUnclaimedNotes = checkbox.checked;
  });

  const text = document.createElement("span");
  text.textContent = "同时在 Anki 中删除未被接管的 note（不可逆）";

  label.append(checkbox, text);
  mergeOptions.appendChild(label);
}

function renderMergeWarnings() {
  const modules = new Set(mergeDetails.map((card) => card.module_path || ""));
  const notes = mergeDetails.filter((card) => card.anki_note_id);
  const warnings = [
    "合并后保留卡会回到草稿状态，请确认背面或手动生成 AI 答案后再同步。",
  ];
  if (modules.size > 1) {
    warnings.push("所选卡片来自多个模块，合并后保留卡仍留在原模块。");
  }
  if (notes.length > 0) {
    warnings.push(
      `共 ${notes.length} 张卡已关联 Anki note，合并后最多接管 1 个；其余 note 默认不会在 Anki 中自动删除，请手动处理。`,
    );
  }

  mergeWarnings.replaceChildren();
  for (const text of warnings) {
    const p = document.createElement("p");
    p.textContent = text;
    mergeWarnings.appendChild(p);
  }
}

async function openMergeModal() {
  if (mergeBusy || selectedIds.size < 2) return;
  mergeButton.disabled = true;
  mergeButton.textContent = "加载所选卡片…";

  try {
    mergeDetails = await Promise.all(
      [...selectedIds].map((id) => window.cm.fetchJSON(`/api/cards/${id}`)),
    );
  } catch (error) {
    updateSelectionUI();
    const failure = document.createElement("p");
    failure.className = "merge-error";
    failure.textContent = error.message || "加载所选卡片失败";
    listBox.prepend(failure);
    return;
  }

  mergeDetails.sort(
    (a, b) =>
      String(a.created_at || "").localeCompare(String(b.created_at || "")) ||
      a.id - b.id,
  );
  mergeSurvivorId = mergeDetails[0].id;
  renderMergeChoices();
  renderMergeOptions();
  renderMergeWarnings();
  mergeModal.hidden = false;
  updateSelectionUI();
}

function closeMergeModal() {
  if (mergeBusy) return;
  mergeModal.hidden = true;
  mergeDetails = [];
  mergeSurvivorId = null;
  deleteUnclaimedNotes = false;
  mergeOptions.replaceChildren();
  updateSelectionUI();
}

async function confirmMerge() {
  if (mergeBusy || mergeSurvivorId === null) return;
  const sourceIds = mergeDetails
    .map((card) => card.id)
    .filter((id) => id !== mergeSurvivorId);
  if (sourceIds.length === 0) return;

  if (
    deleteUnclaimedNotes &&
    !window.confirm(
      "将在 Anki 中永久删除未被接管的 note，删除后无法恢复。确认继续合并？",
    )
  ) {
    return;
  }

  mergeBusy = true;
  mergeConfirm.disabled = true;
  mergeConfirm.textContent = "合并中…";
  try {
    const result = await window.cm.fetchJSON("/api/cards/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        survivor_id: mergeSurvivorId,
        source_ids: sourceIds,
        delete_unclaimed_notes: deleteUnclaimedNotes,
      }),
    });
    const errors = result.note_delete_errors || [];
    if (errors.length) {
      window.alert(
        `合并已完成，但以下 Anki note 删除失败：\n${errors.join(
          "\n",
        )}\n请在 Anki 中手动删除。`,
      );
    }
    mergeModal.hidden = true;
    selectedIds.clear();
    window.location.href = `/cards/${result.card.id}`;
  } catch (error) {
    mergeWarnings.replaceChildren();
    const failure = document.createElement("p");
    failure.className = "merge-error";
    failure.textContent = error.message || "合并失败";
    mergeWarnings.appendChild(failure);
  } finally {
    mergeBusy = false;
    mergeConfirm.disabled = false;
    mergeConfirm.textContent = "确认合并";
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

statusFilter.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  const value = chip.dataset.status;
  if (!value) {
    state.statuses = [];
  } else {
    const selected = new Set(state.statuses);
    if (selected.has(value)) {
      selected.delete(value);
    } else {
      selected.add(value);
    }
    state.statuses = [...STATUS_VALUES].filter((item) => selected.has(item));
  }
  syncControls();
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

mergeButton.addEventListener("click", openMergeModal);
mergeCancel.addEventListener("click", closeMergeModal);
mergeConfirm.addEventListener("click", confirmMerge);
mergeModal.addEventListener("click", (event) => {
  if (event.target === mergeModal) closeMergeModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !mergeModal.hidden) closeMergeModal();
});

restoreState();
syncControls();
updateSelectionUI();
loadModules();
loadCards();
