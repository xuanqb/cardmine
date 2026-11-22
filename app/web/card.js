const cardId = Number(window.location.pathname.split("/").pop());
const statusBadge = document.getElementById("status-badge");
const statusSelect = document.getElementById("status-select");
const moduleSelect = document.getElementById("module-select");
const aliasSection = document.getElementById("alias-section");
const aliasList = document.getElementById("alias-list");
const frontInput = document.getElementById("front-input");
const backTextarea = document.getElementById("back-input");
const preview = document.getElementById("preview");
const saveButton = document.getElementById("save-button");
const saveReadyButton = document.getElementById("save-ready-button");
const toast = document.getElementById("toast");

const aiMeta = document.getElementById("ai-meta");
const aiBody = document.getElementById("ai-body");
const aiError = document.getElementById("ai-error");
const aiGenerate = document.getElementById("ai-generate");
const aiCancel = document.getElementById("ai-cancel");
const aiAdopt = document.getElementById("ai-adopt");
const aiAppend = document.getElementById("ai-append");
const aiClear = document.getElementById("ai-clear");

let card = null;
let editor = null;
let markdown = null;
let toastTimer = null;
let aiBusy = false;
let aiController = null;
let suppressPreviewUntil = 0;
let suppressEditorUntil = 0;

const editorSplit = document.querySelector(".editor-split");

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

function getBack() {
  return editor ? editor.getValue() : backTextarea.value;
}

function renderMarkdown(source, target) {
  if (!window.markdownit) {
    target.textContent = source;
    return;
  }
  if (!markdown) {
    markdown = window.markdownit({
      html: true,
      linkify: true,
      breaks: false,
      highlight(code, lang) {
        const escaped = markdown.utils.escapeHtml(code);
        if (lang && window.hljs && window.hljs.getLanguage(lang)) {
          try {
            const value = window.hljs.highlight(code, {
              language: lang,
              ignoreIllegals: true,
            }).value;
            return `<pre class="hljs"><code>${value}</code></pre>`;
          } catch (error) {
            return `<pre class="hljs"><code>${escaped}</code></pre>`;
          }
        }
        return `<pre class="hljs"><code>${escaped}</code></pre>`;
      },
    });
    markdown.core.ruler.push("source_line", (state) => {
      for (const token of state.tokens) {
        if (token.map && token.map.length) {
          token.attrSet("data-source-line", String(token.map[0]));
        }
      }
    });
    const fence = markdown.renderer.rules.fence;
    markdown.renderer.rules.fence = (tokens, idx, options, env, self) => {
      const html = fence(tokens, idx, options, env, self);
      const token = tokens[idx];
      if (!token.map || !token.map.length) return html;
      return html.replace(
        /^(\s*<pre)/,
        `$1 data-source-line="${token.map[0]}"`,
      );
    };
  }

  target.innerHTML = markdown.render(source);
  target.querySelectorAll("img").forEach((image) => {
    const src = image.getAttribute("src") || "";
    if (src && !/^(?:[a-z]+:|\/|#)/i.test(src)) {
      image.src = `/media/${encodeURIComponent(src)}`;
    }
  });
}

function renderPreview() {
  renderMarkdown(getBack(), preview);
}

function setBack(value) {
  if (editor) {
    editor.setValue(value);
  } else {
    backTextarea.value = value;
  }
  renderPreview();
}

function isSideBySide() {
  if (!editorSplit) return false;
  const columns = getComputedStyle(editorSplit).gridTemplateColumns || "";
  return columns.trim().split(/\s+/).filter(Boolean).length > 1;
}

function previewAnchors() {
  return preview.querySelectorAll("[data-source-line]");
}

function scrollPreviewToElement(element, keepIfVisible) {
  const box = preview.getBoundingClientRect();
  const rect = element.getBoundingClientRect();
  const visible = rect.bottom > box.top + 2 && rect.top < box.bottom - 2;
  if (keepIfVisible && visible) return;
  const top = preview.scrollTop + rect.top - box.top;
  if (Math.abs(top - preview.scrollTop) < 2) return;
  suppressPreviewUntil = performance.now() + 120;
  preview.scrollTop = top;
}

function syncPreviewToLine(line, keepIfVisible) {
  const anchors = previewAnchors();
  let target = anchors[0] || null;
  for (const element of anchors) {
    const value = Number(element.getAttribute("data-source-line"));
    if (!Number.isFinite(value)) continue;
    if (value > line) break;
    target = element;
  }
  if (target) scrollPreviewToElement(target, keepIfVisible);
}

function syncEditorToPreview() {
  if (!editor) return;
  const boxTop = preview.getBoundingClientRect().top;
  let target = null;
  for (const element of previewAnchors()) {
    if (element.getBoundingClientRect().bottom > boxTop + 2) {
      target = element;
      break;
    }
  }
  if (!target) return;
  const line = Number(target.getAttribute("data-source-line"));
  if (!Number.isFinite(line)) return;
  suppressEditorUntil = performance.now() + 120;
  editor.scrollTo(null, editor.heightAtLine(line, "local"));
}

function bindScrollSync() {
  if (!editor) return;
  editor.on("scroll", () => {
    if (!isSideBySide() || performance.now() < suppressEditorUntil) return;
    const top = editor.getScrollInfo().top;
    syncPreviewToLine(editor.lineAtHeight(top, "local"), false);
  });
  editor.on("cursorActivity", () => {
    if (!isSideBySide() || performance.now() < suppressEditorUntil) return;
    syncPreviewToLine(editor.getCursor().line, true);
  });
  preview.addEventListener("scroll", () => {
    if (!isSideBySide() || performance.now() < suppressPreviewUntil) return;
    syncEditorToPreview();
  });
}

function renderAI() {
  const hasAnswer = Boolean((card.ai_back_md || "").trim());
  const status = card.ai_status || "none";

  aiError.hidden = true;
  aiError.textContent = "";
  aiBody.hidden = !hasAnswer;
  if (hasAnswer) {
    renderMarkdown(card.ai_back_md, aiBody);
  } else {
    aiBody.replaceChildren();
  }

  aiAdopt.hidden = !hasAnswer;
  aiAppend.hidden = !hasAnswer;
  aiClear.hidden = !hasAnswer;
  aiGenerate.disabled = aiBusy;
  aiGenerate.textContent = hasAnswer ? "重新生成" : "生成 AI 答案";
  aiCancel.hidden = !aiBusy;
  aiCancel.disabled = !aiBusy;

  if (aiBusy) {
    aiMeta.textContent = "生成中，可随时取消…";
    return;
  }
  if (status === "failed") {
    aiMeta.textContent = "生成失败";
    aiError.hidden = false;
    aiError.textContent = card.ai_error || "未知错误";
    return;
  }
  if (status === "running") {
    aiMeta.textContent = "上次生成未完成，可重新生成";
    return;
  }
  aiMeta.textContent = hasAnswer
    ? `生成于 ${window.cm.formatTime(card.ai_generated_at)}`
    : "未生成";
}

const schedulePreview = window.cm.debounce(renderPreview, 180);

function renderInfo() {
  const meta = window.cm.statusMeta(card.status);
  statusBadge.className = `badge ${meta.className}`;
  statusBadge.textContent = meta.label;
  statusSelect.value = card.status;

  document.getElementById("fact-mentions").textContent = `${card.mention_count} 次`;
  document.getElementById("fact-last").textContent = window.cm.formatTime(
    card.last_mention_at
  );
  document.getElementById("fact-created").textContent = window.cm.formatTime(
    card.created_at
  );
  document.getElementById("fact-updated").textContent = window.cm.formatTime(
    card.updated_at
  );
  document.getElementById("front-view").textContent = card.front_md;
}

function renderVariants() {
  const list = document.getElementById("variant-list");
  list.replaceChildren();
  if (!card.variants.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "暂无问法变体";
    list.appendChild(empty);
    return;
  }
  for (const variant of card.variants) {
    const item = document.createElement("li");
    const text = document.createElement("p");
    text.className = "variant-text";
    text.textContent = variant.raw_text;
    const meta = document.createElement("p");
    meta.className = "variant-meta";
    meta.textContent = [
      variant.section || "未分章节",
      variant.post_title || "未知来源",
    ].join(" · ");
    item.append(text, meta);
    list.appendChild(item);
  }
}

function renderSources() {
  const list = document.getElementById("source-list");
  list.replaceChildren();
  if (!card.sources.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "暂无来源";
    list.appendChild(empty);
    return;
  }
  for (const source of card.sources) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = source.id ? `/posts/${source.id}` : source.url || "#";
    link.textContent = source.title || source.url || "未命名面经";
    const meta = document.createElement("span");
    meta.className = "variant-meta";
    meta.textContent = ` ${window.cm.formatTime(source.fetched_at)}`;
    item.append(link, meta);
    list.appendChild(item);
  }
}

function renderAliases() {
  const items = card.aliases || [];
  aliasSection.hidden = items.length === 0;
  aliasList.replaceChildren();
  for (const alias of items) {
    const item = document.createElement("li");
    const text = document.createElement("p");
    text.className = "variant-text";
    text.textContent = alias.front_md;
    const meta = document.createElement("p");
    meta.className = "variant-meta";
    meta.textContent = [
      alias.module_path || "未分类",
      alias.merge_action_id ? `合并记录 #${alias.merge_action_id}` : "合并来源",
    ].join(" · ");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost alias-remove";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      const ok = window.confirm(
        `删除这条来源问题？\n\n${alias.front_md}\n\n` +
          "删除后不再用于后续去重，已挂接的卡片不受影响。"
      );
      if (!ok) return;
      try {
        card = await window.cm.fetchJSON(
          `/api/cards/${cardId}/aliases/${alias.id}`,
          { method: "DELETE" }
        );
        renderAliases();
        showToast("已删除来源问题");
      } catch (error) {
        showToast(error.message || "删除失败", true);
      }
    });
    item.append(text, meta, remove);
    aliasList.appendChild(item);
  }
}

function renderConflicts() {
  const items = card.conflicts || [];
  const badge = document.getElementById("conflict-badge");
  const section = document.getElementById("conflict-section");
  const list = document.getElementById("conflict-card-list");
  badge.hidden = items.length === 0;
  section.hidden = items.length === 0;
  list.replaceChildren();
  for (const item of items) {
    const entry = document.createElement("li");
    const head = document.createElement("p");
    head.className = "variant-text";
    head.textContent = item.field === "front" ? "正面冲突" : "背面冲突";
    entry.append(head);
    if (item.note) {
      const note = document.createElement("p");
      note.className = "variant-meta";
      note.textContent = item.note;
      entry.append(note);
    }
    const compare = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "查看两侧内容";
    const body = document.createElement("div");
    body.className = "conflict-compare";
    for (const [label, value] of [
      ["系统", item.system_value],
      ["Anki", item.anki_value],
    ]) {
      const block = document.createElement("div");
      const title = document.createElement("span");
      title.className = "conflict-side-label";
      title.textContent = label;
      const pre = document.createElement("pre");
      pre.textContent = value || "（空）";
      block.append(title, pre);
      body.append(block);
    }
    compare.append(summary, body);
    entry.append(compare);
    list.append(entry);
  }
}

function renderModuleOptions(tree) {
  moduleSelect.replaceChildren();
  for (const node of window.cm.flattenModules(tree)) {
    const option = document.createElement("option");
    option.value = String(node.id);
    option.textContent = node.path;
    moduleSelect.appendChild(option);
  }
  if (card.module_id) moduleSelect.value = String(card.module_id);
}

function applySaved(updated) {
  card = updated;
  renderInfo();
  renderVariants();
  renderAliases();
  renderConflicts();
  renderSources();
  renderAI();
  if (card.module_id) moduleSelect.value = String(card.module_id);
}

async function save(statusOverride) {
  const nextStatus = statusOverride || statusSelect.value;
  if (nextStatus === "ready" && !getBack().trim()) {
    showToast("背面为空，先补充内容再整理", true);
    return;
  }
  saveButton.disabled = true;
  saveReadyButton.disabled = true;
  const payload = {
    front_md: frontInput.value,
    back_md: getBack(),
    status: nextStatus,
  };
  if (moduleSelect.value) payload.module_id = Number(moduleSelect.value);

  try {
    const updated = await window.cm.fetchJSON(`/api/cards/${cardId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applySaved(updated);
    showToast("已保存" + (nextStatus === "ready" ? "并整理" : ""));
  } catch (error) {
    showToast(error.message || "保存失败", true);
  } finally {
    saveButton.disabled = false;
    saveReadyButton.disabled = false;
  }
}

async function generateAI() {
  if (aiBusy) return;
  aiBusy = true;
  const controller = new AbortController();
  aiController = controller;
  renderAI();
  try {
    card = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-answer`, {
      method: "POST",
      signal: controller.signal,
    });
    showToast("AI 答案已生成");
  } catch (error) {
    if (error.name !== "AbortError") {
      try {
        card = await window.cm.fetchJSON(`/api/cards/${cardId}`);
      } catch (ignored) {
        /* 保留原卡片状态 */
      }
      showToast(error.message || "生成失败", true);
    }
  } finally {
    if (aiController === controller) aiController = null;
    aiBusy = false;
    renderAI();
  }
}

async function cancelAI() {
  if (!aiBusy) return;
  aiCancel.disabled = true;
  const controller = aiController;
  try {
    await window.cm.fetchJSON(`/api/cards/${cardId}/ai-answer/cancel`, {
      method: "POST",
    });
    showToast("已取消生成");
  } catch (error) {
    showToast(error.message || "取消失败", true);
  } finally {
    if (controller) controller.abort();
    try {
      card = await window.cm.fetchJSON(`/api/cards/${cardId}`);
    } catch (ignored) {
      /* 保留原卡片状态 */
    }
    renderAI();
  }
}

async function clearAI() {
  aiClear.disabled = true;
  try {
    card = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-answer`, {
      method: "DELETE",
    });
    renderAI();
    showToast("已清空 AI 答案");
  } catch (error) {
    showToast(error.message || "清空失败", true);
  } finally {
    aiClear.disabled = false;
  }
}

function adoptAI() {
  if (!(card.ai_back_md || "").trim()) return;
  setBack(card.ai_back_md);
  showToast("已填入编辑器，记得保存");
}

function appendAI() {
  const answer = (card.ai_back_md || "").trim();
  if (!answer) return;
  const current = getBack().trim();
  setBack(current ? `${current}\n\n${answer}` : answer);
  showToast("已追加到末尾，记得保存");
}

async function init() {
  try {
    const [detail, modules] = await Promise.all([
      window.cm.fetchJSON(`/api/cards/${cardId}`),
      window.cm.fetchJSON("/api/modules"),
    ]);
    card = detail;

    frontInput.value = card.front_md;
    document.title = `cardmine · ${card.front_md.slice(0, 20)}`;

    if (window.CodeMirror) {
      editor = window.CodeMirror.fromTextArea(backTextarea, {
        mode: "markdown",
        lineWrapping: true,
        lineNumbers: true,
        indentUnit: 2,
        extraKeys: { Enter: "newlineAndIndentContinueMarkdownList" },
      });
      editor.setValue(card.back_md);
      editor.on("change", schedulePreview);
      bindScrollSync();
    } else {
      backTextarea.value = card.back_md;
      backTextarea.addEventListener("input", schedulePreview);
    }

    renderInfo();
    renderVariants();
    renderAliases();
    renderConflicts();
    renderSources();
    renderModuleOptions(modules);
    renderPreview();
    renderAI();

    saveButton.addEventListener("click", () => save());
    saveReadyButton.addEventListener("click", () => save("ready"));
    aiGenerate.addEventListener("click", generateAI);
    aiCancel.addEventListener("click", cancelAI);
    aiAdopt.addEventListener("click", adoptAI);
    aiAppend.addEventListener("click", appendAI);
    aiClear.addEventListener("click", clearAI);
    frontInput.addEventListener("input", () => {
      document.getElementById("front-view").textContent = frontInput.value;
    });
  } catch (error) {
    showToast(error.message || "卡片加载失败", true);
  }
}

init();
