const cardId = Number(window.location.pathname.split("/").pop());
const statusBadge = document.getElementById("status-badge");
const originBadge = document.getElementById("origin-badge");
const flagField = document.getElementById("flag-field");
const flagBadge = document.getElementById("flag-badge");
const flagClear = document.getElementById("flag-clear");
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
const aiStatus = document.getElementById("ai-status");
const aiDrawer = document.getElementById("ai-drawer");
const aiDrawerBackdrop = document.getElementById("ai-drawer-backdrop");
const aiDrawerClose = document.getElementById("ai-drawer-close");
const aiOpen = document.getElementById("ai-open");
const aiError = document.getElementById("ai-error");
const aiGenerate = document.getElementById("ai-generate");
const aiThreadSelect = document.getElementById("ai-thread-select");
const aiThreadNew = document.getElementById("ai-thread-new");
const aiThreadDelete = document.getElementById("ai-thread-delete");
const aiThreadStale = document.getElementById("ai-thread-stale");
const aiMessages = document.getElementById("ai-messages");
const aiComposer = document.getElementById("ai-composer");
const aiInput = document.getElementById("ai-input");
const aiSend = document.getElementById("ai-send");
const aiStop = document.getElementById("ai-stop");

let card = null;
let editor = null;
let markdown = null;
let toastTimer = null;
let aiBusy = false;
let aiThreads = [];
let activeThreadId = null;
let aiStreamController = null;
let aiMessagesPinned = true;
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

  if (window.DOMPurify) {
    target.innerHTML = window.DOMPurify.sanitize(markdown.render(source), {
      ADD_TAGS: ["font"],
      ADD_ATTR: ["data-source-line"],
      FORBID_TAGS: [
        "style",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "option",
      ],
      FORBID_ATTR: ["srcdoc"],
    });
  } else {
    target.innerHTML = window
      .markdownit({ html: false, linkify: true })
      .render(source);
  }
  target.querySelectorAll("img").forEach((image) => {
    const src = image.getAttribute("src") || "";
    if (!src || /^(?:[a-z]+:|\/|#)/i.test(src)) return;
    const name = src.split(/[?#]/, 1)[0];
    image.src = `/media/${encodeURIComponent(name)}`;
    image.addEventListener(
      "error",
      () => {
        const placeholder = document.createElement("span");
        placeholder.className = "media-missing";
        placeholder.textContent = `图片缺失：${name} `;
        const upload = document.createElement("button");
        upload.type = "button";
        upload.className = "link-button";
        upload.textContent = "上传";
        upload.addEventListener("click", () => pickMedia(name, source, target));
        placeholder.append(upload);
        image.replaceWith(placeholder);
      },
      { once: true },
    );
  });
}

function pickMedia(name, source, target) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*,audio/*";
  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      await window.cm.fetchJSON(
        `/api/media/upload?filename=${encodeURIComponent(name)}`,
        { method: "POST", body: form },
      );
      renderMarkdown(source, target);
    } catch (error) {
      window.cm.toast(error.message || "上传失败", "error");
    }
  });
  input.click();
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

function activeThread() {
  return aiThreads.find((thread) => thread.id === activeThreadId) || null;
}

function openAIDrawer() {
  aiDrawer.hidden = false;
  aiDrawerBackdrop.hidden = false;
  document.body.classList.add("drawer-open");
  scrollChat(true);
  aiInput.focus();
}

function closeAIDrawer() {
  aiDrawer.hidden = true;
  aiDrawerBackdrop.hidden = true;
  document.body.classList.remove("drawer-open", "drawer-instant");
}

function renderAI() {
  const hasAnswer = Boolean((card.ai_back_md || "").trim());

  const stale = hasAnswer && Boolean(card.ai_stale);
  aiMeta.classList.toggle("stale", stale);
  aiMeta.textContent = hasAnswer
    ? `当前答案生成于 ${window.cm.formatTime(card.ai_generated_at)}` +
      (stale ? " · 正面已修改，答案可能已过时" : "")
    : "未生成当前答案";

  const thread = activeThread();
  aiThreadStale.hidden = !(thread && thread.stale);
  aiThreadDelete.hidden = !thread;
  aiGenerate.disabled = aiBusy;
  aiThreadNew.disabled = aiBusy;
  aiThreadDelete.disabled = aiBusy;
  aiThreadSelect.disabled = aiBusy || !aiThreads.length;
  aiInput.disabled = aiBusy;
  aiSend.disabled = aiBusy;
  aiStop.hidden = !aiBusy;
  aiStatus.textContent = aiBusy
    ? "生成中…"
    : thread
      ? `${thread.message_count} 条消息`
      : "选择或新建一个对话";
}

function renderThreadOptions() {
  aiThreadSelect.replaceChildren();
  if (!aiThreads.length) {
    aiThreadSelect.append(new Option("暂无对话", ""));
    aiThreadSelect.disabled = true;
    return;
  }
  for (const thread of aiThreads) {
    const label = thread.stale ? `${thread.title} · 旧题面` : thread.title;
    aiThreadSelect.append(new Option(label, String(thread.id)));
  }
  aiThreadSelect.value = activeThreadId ? String(activeThreadId) : "";
  aiThreadSelect.disabled = aiBusy;
}

function scrollChat(force) {
  if (force || aiMessagesPinned) {
    aiMessages.scrollTop = aiMessages.scrollHeight;
  }
}

function messageElement(message) {
  const article = document.createElement("article");
  article.className = `ai-message ${message.role}`;

  const head = document.createElement("div");
  head.className = "ai-message-head";
  const role = document.createElement("span");
  role.textContent = message.role === "user" ? "我" : "AI";
  head.append(role);
  if (message.status === "interrupted") {
    const badge = document.createElement("span");
    badge.className = "ai-message-badge";
    badge.textContent = "未完成";
    head.append(badge);
  }
  const isCurrent =
    message.role === "assistant" && card.ai_answer_message_id === message.id;
  if (isCurrent) {
    const badge = document.createElement("span");
    badge.className = "ai-message-badge current";
    badge.textContent = "当前答案";
    head.append(badge);
  }
  article.append(head);

  const body = document.createElement("div");
  body.className = "markdown-body ai-message-body";
  if (message.role === "assistant") {
    renderMarkdown(message.content || "", body);
  } else {
    body.textContent = message.content || "";
  }
  article.append(body);

  if (message.role === "assistant" && message.status === "done") {
    const actions = document.createElement("div");
    actions.className = "ai-message-actions";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost link-button";
    button.textContent = isCurrent ? "已是当前答案" : "设为当前答案";
    button.disabled = isCurrent;
    button.addEventListener("click", () =>
      setCurrentAnswer(message.id, button)
    );
    actions.append(button);
    if (isCurrent) {
      const adopt = document.createElement("button");
      adopt.type = "button";
      adopt.className = "ghost link-button";
      adopt.textContent = "覆盖背面";
      adopt.addEventListener("click", adoptAI);

      const append = document.createElement("button");
      append.type = "button";
      append.className = "ghost link-button";
      append.textContent = "追加到背面";
      append.addEventListener("click", appendAI);

      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "ghost link-button";
      clear.textContent = "清空答案";
      clear.addEventListener("click", () => clearAI(clear));

      actions.append(adopt, append, clear);
    }
    article.append(actions);
  }
  return article;
}

function renderChat(messages) {
  aiMessages.replaceChildren();
  aiMessagesPinned = true;
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "ai-empty";
    empty.textContent = "还没有消息。直接输入问题，或点「生成答案」。";
    aiMessages.append(empty);
    return;
  }
  for (const message of messages) {
    aiMessages.append(messageElement(message));
  }
  scrollChat(true);
}

async function loadThreads(preferredId) {
  try {
    const data = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-threads`);
    aiThreads = data.threads || [];
  } catch (error) {
    showToast(error.message || "对话列表加载失败", true);
    return;
  }
  if (!aiThreads.length) {
    activeThreadId = null;
    renderThreadOptions();
    renderChat([]);
    renderAI();
    return;
  }
  const next =
    aiThreads.find((thread) => thread.id === preferredId) ||
    aiThreads.find((thread) => thread.id === activeThreadId) ||
    aiThreads[0];
  await openThread(next.id);
}

async function openThread(threadId) {
  activeThreadId = Number(threadId);
  renderThreadOptions();
  try {
    const data = await window.cm.fetchJSON(
      `/api/ai-threads/${activeThreadId}/messages`
    );
    renderChat(data.messages || []);
  } catch (error) {
    showToast(error.message || "对话加载失败", true);
    renderChat([]);
  }
  renderAI();
}

async function createThread(title) {
  const data = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title || "" }),
  });
  aiThreads.unshift(data.thread);
  activeThreadId = data.thread.id;
  renderThreadOptions();
  renderChat([]);
  renderAI();
  return data.thread;
}

async function setCurrentAnswer(messageId, button) {
  button.disabled = true;
  try {
    card = await window.cm.fetchJSON(
      `/api/ai-threads/${activeThreadId}/messages/${messageId}/answer`,
      { method: "POST" }
    );
    if ((card.ai_back_md || "").trim()) {
      setBack(card.ai_back_md);
    }
    await openThread(activeThreadId);
    showToast("已设为当前答案并填入背面，记得保存");
  } catch (error) {
    button.disabled = false;
    showToast(error.message || "设置失败", true);
  }
}

const schedulePreview = window.cm.debounce(renderPreview, 180);

function renderInfo() {
  const meta = window.cm.statusMeta(card.status);
  statusBadge.className = `badge ${meta.className}`;
  statusBadge.textContent = meta.label;
  statusSelect.value = card.status;

  const origin = window.cm.originMeta(card.origin);
  originBadge.className = `badge ${origin.className}`;
  originBadge.textContent = origin.label;

  const flagValue = Number(card.anki_flag) || 0;
  if (card.flag_clear_pending) {
    flagField.hidden = false;
    flagBadge.className = "badge flag-pending";
    flagBadge.textContent = "待清除同步";
    flagClear.hidden = true;
  } else if (flagValue) {
    const flag = window.cm.flagMeta(flagValue);
    flagField.hidden = false;
    flagBadge.className = `badge ${flag.className}`;
    flagBadge.textContent = flag.label;
    flagClear.hidden = false;
  } else {
    flagField.hidden = true;
  }
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

function parseSSE(block) {
  let name = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  try {
    return { name, data: JSON.parse(dataLines.join("\n")) };
  } catch (error) {
    return null;
  }
}

function createStreamingBubble() {
  const article = document.createElement("article");
  article.className = "ai-message assistant";
  const head = document.createElement("div");
  head.className = "ai-message-head";
  const role = document.createElement("span");
  role.textContent = "AI";
  const badge = document.createElement("span");
  badge.className = "ai-message-badge";
  badge.textContent = "生成中";
  head.append(role, badge);
  const body = document.createElement("div");
  body.className = "markdown-body ai-message-body";
  body.textContent = "…";
  article.append(head, body);
  aiMessages.append(article);
  scrollChat(true);
  return body;
}

async function sendMessageText(text) {
  const content = (text || "").trim();
  if (aiBusy || !content) return;

  aiBusy = true;
  aiError.hidden = true;
  aiError.textContent = "";
  aiInput.value = "";
  renderAI();

  let threadId = activeThreadId;
  let frame = 0;
  let stopped = false;
  try {
    if (!threadId) {
      const thread = await createThread("");
      threadId = thread.id;
    }

    aiMessages.append(
      messageElement({ id: 0, role: "user", content, status: "done" })
    );
    scrollChat(true);
    const bubble = createStreamingBubble();

    const controller = new AbortController();
    aiStreamController = controller;
    let accumulated = "";
    let streamError = "";
    const paint = () => {
      frame = 0;
      renderMarkdown(accumulated, bubble);
      scrollChat(false);
    };
    const handle = (event) => {
      if (!event) return;
      if (event.name === "delta") {
        accumulated += event.data.text || "";
        if (!frame) frame = requestAnimationFrame(paint);
      } else if (event.name === "error") {
        streamError = event.data.message || "生成失败";
      }
    };

    const response = await fetch(`/api/ai-threads/${threadId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        if (data.detail) message = data.detail;
      } catch (error) {
        /* 保留状态码提示 */
      }
      throw new Error(message);
    }
    if (!response.body) throw new Error("浏览器不支持流式响应");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        handle(parseSSE(block));
      }
    }
    if (buffer.trim()) handle(parseSSE(buffer));
    if (frame) {
      cancelAnimationFrame(frame);
      frame = 0;
    }
    paint();
    if (streamError) throw new Error(streamError);
  } catch (error) {
    if (error.name === "AbortError") {
      stopped = true;
      showToast("已停止生成");
    } else {
      aiError.hidden = false;
      aiError.textContent = error.message || "生成失败";
    }
  } finally {
    if (frame) cancelAnimationFrame(frame);
    aiStreamController = null;
    aiBusy = false;
    try {
      card = await window.cm.fetchJSON(`/api/cards/${cardId}`);
    } catch (error) {
      /* 保留当前卡片状态 */
    }
    if (stopped) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    await loadThreads(threadId);
  }
}

async function generateAnswer() {
  if (aiBusy) return;
  const title = ((card.front_md || "").trim().split("\n")[0] || "").slice(0, 40);
  aiBusy = true;
  renderAI();
  try {
    await createThread(title || "生成答案");
  } catch (error) {
    aiBusy = false;
    renderAI();
    showToast(error.message || "创建对话失败", true);
    return;
  }
  aiBusy = false;
  await sendMessageText("请为这道题生成一版答案。");
}

async function newThread() {
  if (aiBusy) return;
  try {
    await createThread("");
    aiInput.focus();
  } catch (error) {
    showToast(error.message || "创建对话失败", true);
  }
}

async function deleteThread() {
  if (aiBusy || !activeThreadId) return;
  if (!window.confirm("删除这个对话？当前答案不会受影响。")) return;
  try {
    await window.cm.fetchJSON(`/api/ai-threads/${activeThreadId}`, {
      method: "DELETE",
    });
    aiThreads = aiThreads.filter((thread) => thread.id !== activeThreadId);
    activeThreadId = null;
    await loadThreads();
    showToast("已删除对话");
  } catch (error) {
    showToast(error.message || "删除失败", true);
  }
}

function stopAI() {
  if (aiStreamController) aiStreamController.abort();
}

async function clearAI(button) {
  if (button) button.disabled = true;
  try {
    card = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-answer`, {
      method: "DELETE",
    });
    renderAI();
    if (activeThreadId) await openThread(activeThreadId);
    showToast("已清空 AI 答案");
  } catch (error) {
    showToast(error.message || "清空失败", true);
    if (button) button.disabled = false;
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
    await loadThreads();

    saveButton.addEventListener("click", () => save());
    saveReadyButton.addEventListener("click", () => save("ready"));
    flagClear.addEventListener("click", async () => {
      if (!card || card.flag_clear_pending) return;
      flagClear.disabled = true;
      try {
        card = await window.cm.fetchJSON(`/api/cards/${card.id}/flag/clear`, {
          method: "POST",
        });
        renderInfo();
        showToast("已登记清除，下次推送同步到 Anki");
      } catch (error) {
        showToast(error.message || "登记失败", true);
      } finally {
        flagClear.disabled = false;
      }
    });
    aiOpen.addEventListener("click", openAIDrawer);
    aiDrawerClose.addEventListener("click", closeAIDrawer);
    aiDrawerBackdrop.addEventListener("click", closeAIDrawer);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !aiDrawer.hidden) closeAIDrawer();
    });
    aiGenerate.addEventListener("click", generateAnswer);
    aiThreadNew.addEventListener("click", newThread);
    aiThreadDelete.addEventListener("click", deleteThread);
    aiThreadSelect.addEventListener("change", () => {
      if (!aiThreadSelect.value) return;
      aiError.hidden = true;
      aiError.textContent = "";
      openThread(Number(aiThreadSelect.value));
    });
    aiComposer.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessageText(aiInput.value);
    });
    aiInput.addEventListener("keydown", (event) => {
      if (
        event.key === "Enter" &&
        event.metaKey &&
        !event.isComposing &&
        !aiSend.disabled
      ) {
        event.preventDefault();
        aiComposer.requestSubmit();
      }
    });
    aiStop.addEventListener("click", stopAI);
    aiMessages.addEventListener("scroll", () => {
      aiMessagesPinned =
        aiMessages.scrollHeight -
          aiMessages.scrollTop -
          aiMessages.clientHeight <
        40;
    });
    frontInput.addEventListener("input", () => {
      document.getElementById("front-view").textContent = frontInput.value;
    });
  } catch (error) {
    showToast(error.message || "卡片加载失败", true);
  }
}

init();
