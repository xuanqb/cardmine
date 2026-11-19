const { fetchJSON, formatTime } = window.cm;

const postId = Number(window.location.pathname.split("/").pop());
const toast = document.getElementById("toast");
const mentionList = document.getElementById("mention-list");
const cleanBody = document.getElementById("clean-body");

const TOPIC_LABELS = {
  tech: "技术",
  algo: "算法",
  nontech: "非技术",
};

let toastTimer = null;
let markdown = null;

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

function renderMarkdown(source) {
  if (!window.markdownit) {
    cleanBody.textContent = source;
    return;
  }
  if (!markdown) {
    markdown = window.markdownit({
      html: false,
      linkify: true,
      breaks: true,
      highlight(code, lang) {
        const escaped = markdown.utils.escapeHtml(code);
        if (lang && window.hljs && window.hljs.getLanguage(lang)) {
          try {
            return `<pre class="hljs"><code>${window.hljs.highlight(code, {
              language: lang,
              ignoreIllegals: true,
            }).value}</code></pre>`;
          } catch (error) {
            return `<pre class="hljs"><code>${escaped}</code></pre>`;
          }
        }
        return `<pre class="hljs"><code>${escaped}</code></pre>`;
      },
    });
  }
  cleanBody.innerHTML = markdown.render(source || "");
  cleanBody.querySelectorAll("img").forEach((image) => {
    const src = image.getAttribute("src") || "";
    if (src && !/^(?:[a-z]+:|\/|#)/i.test(src)) {
      image.src = `/media/${encodeURIComponent(src)}`;
    }
  });
}

function renderMeta(post) {
  document.title = `cardmine · ${post.title.slice(0, 20)}`;
  document.getElementById("post-title").textContent = post.title;
  const meta = [
    post.feed_name,
    post.author || "匿名",
    formatTime(post.fetched_at),
    `${post.mentions.length} 个问题`,
  ];
  document.getElementById("post-meta").textContent = meta.join(" · ");
}

function topicLabel(mention) {
  if (mention.topic === "nontech") {
    return mention.subtype === "personal" ? "个人题" : "模板题";
  }
  return TOPIC_LABELS[mention.topic] || mention.topic;
}

function renderMentions(mentions) {
  mentionList.replaceChildren();
  if (!mentions.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这篇面经还没有抽取到问题。";
    mentionList.append(empty);
    return;
  }

  for (const mention of mentions) {
    const card = document.createElement("article");
    card.className = "mention-card";

    const head = document.createElement("div");
    head.className = "mention-head";

    const title = document.createElement(mention.kp_id ? "a" : "span");
    title.className = "mention-title";
    title.textContent = mention.raw_text;
    if (mention.kp_id) {
      title.href = `/cards/${mention.kp_id}`;
    }

    const badges = document.createElement("div");
    badges.className = "mention-badges";
    const topic = document.createElement("span");
    topic.className = `badge topic-${mention.topic}`;
    topic.textContent = topicLabel(mention);
    badges.append(topic);
    if (mention.section) {
      const section = document.createElement("span");
      section.className = "badge";
      section.textContent = mention.section;
      badges.append(section);
    }
    if (mention.status === "pending") {
      const pending = document.createElement("span");
      pending.className = "badge draft";
      pending.textContent = "待确认";
      badges.append(pending);
    }

    head.append(title, badges);
    card.append(head);

    if (mention.context_md) {
      const context = document.createElement("p");
      context.className = "mention-context";
      context.textContent = mention.context_md;
      card.append(context);
    }
    mentionList.append(card);
  }
}

async function init() {
  try {
    const post = await fetchJSON(`/api/posts/${postId}`);
    renderMeta(post);
    renderMentions(post.mentions);
    renderMarkdown(post.clean_md);
  } catch (error) {
    showToast(error.message || "面经加载失败", true);
  }
}

init();
