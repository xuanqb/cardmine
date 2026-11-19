const cardId = Number(window.location.pathname.split("/").pop());
const statusBadge = document.getElementById("status-badge");
const statusSelect = document.getElementById("status-select");
const moduleSelect = document.getElementById("module-select");
const frontInput = document.getElementById("front-input");
const backTextarea = document.getElementById("back-input");
const preview = document.getElementById("preview");
const saveButton = document.getElementById("save-button");
const toast = document.getElementById("toast");

let card = null;
let editor = null;
let markdown = null;
let toastTimer = null;

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

function renderPreview() {
  const source = getBack();
  if (!window.markdownit) {
    preview.textContent = source;
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

  preview.innerHTML = markdown.render(source);
  preview.querySelectorAll("img").forEach((image) => {
    const src = image.getAttribute("src") || "";
    if (src && !/^(?:[a-z]+:|\/|#)/i.test(src)) {
      image.src = `/media/${encodeURIComponent(src)}`;
    }
  });
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
    link.href = source.url || "#";
    link.target = "_blank";
    link.rel = "noreferrer";
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

    saveButton.addEventListener("click", save);
    frontInput.addEventListener("input", () => {
      document.getElementById("front-view").textContent = frontInput.value;
    });
  } catch (error) {
    showToast(error.message || "卡片加载失败", true);
  }
}

init();
