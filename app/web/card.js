const cardId = Number(window.location.pathname.split("/").pop());
const statusBadge = document.getElementById("status-badge");
const statusSelect = document.getElementById("status-select");
const moduleSelect = document.getElementById("module-select");
const frontInput = document.getElementById("front-input");
const backTextarea = document.getElementById("back-input");
const preview = document.getElementById("preview");
const saveButton = document.getElementById("save-button");
const toast = document.getElementById("toast");

const aiMeta = document.getElementById("ai-meta");
const aiBody = document.getElementById("ai-body");
const aiError = document.getElementById("ai-error");
const aiGenerate = document.getElementById("ai-generate");
const aiAdopt = document.getElementById("ai-adopt");
const aiAppend = document.getElementById("ai-append");
const aiClear = document.getElementById("ai-clear");

let card = null;
let editor = null;
let markdown = null;
let toastTimer = null;
let aiBusy = false;

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

  if (aiBusy) {
    aiMeta.textContent = "生成中，通常需要几十秒…";
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
  renderSources();
  renderAI();
  if (card.module_id) moduleSelect.value = String(card.module_id);
}

async function save() {
  saveButton.disabled = true;
  const payload = {
    front_md: frontInput.value,
    back_md: getBack(),
    status: statusSelect.value,
  };
  if (moduleSelect.value) payload.module_id = Number(moduleSelect.value);

  try {
    const updated = await window.cm.fetchJSON(`/api/cards/${cardId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applySaved(updated);
    showToast("已保存");
  } catch (error) {
    showToast(error.message || "保存失败", true);
  } finally {
    saveButton.disabled = false;
  }
}

async function generateAI() {
  if (aiBusy) return;
  aiBusy = true;
  renderAI();
  try {
    card = await window.cm.fetchJSON(`/api/cards/${cardId}/ai-answer`, {
      method: "POST",
    });
    showToast("AI 答案已生成");
  } catch (error) {
    try {
      card = await window.cm.fetchJSON(`/api/cards/${cardId}`);
    } catch (ignored) {
      /* 保留原卡片状态 */
    }
    showToast(error.message || "生成失败", true);
  } finally {
    aiBusy = false;
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
    } else {
      backTextarea.value = card.back_md;
      backTextarea.addEventListener("input", schedulePreview);
    }

    renderInfo();
    renderVariants();
    renderSources();
    renderModuleOptions(modules);
    renderPreview();
    renderAI();

    saveButton.addEventListener("click", save);
    aiGenerate.addEventListener("click", generateAI);
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
