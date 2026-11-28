window.cm = (() => {
  const STATUS = {
    draft: { label: "草稿", className: "draft" },
    ready: { label: "已整理", className: "ready" },
    archived: { label: "已归档", className: "archived" },
  };

  const ORIGIN = {
    crawl: { label: "RSS 抓取", className: "origin-crawl" },
    apkg: { label: "apkg 导入", className: "origin-apkg" },
    manual: { label: "手动导入", className: "origin-manual" },
  };

  const FLAGS = {
    1: { label: "红旗", className: "flag-red" },
    2: { label: "橙旗", className: "flag-orange" },
    3: { label: "绿旗", className: "flag-green" },
    4: { label: "蓝旗", className: "flag-blue" },
    5: { label: "粉旗", className: "flag-pink" },
    6: { label: "青旗", className: "flag-teal" },
    7: { label: "紫旗", className: "flag-purple" },
  };

  async function fetchJSON(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        if (data.detail) {
          message =
            typeof data.detail === "string"
              ? data.detail
              : JSON.stringify(data.detail);
        }
      } catch (error) {
        // 保留 HTTP 状态码提示
      }
      throw new Error(message);
    }
    return response.json();
  }

  function debounce(fn, wait = 250) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function flattenModules(tree, depth = 0, out = []) {
    for (const node of tree) {
      out.push({ ...node, depth });
      flattenModules(node.children || [], depth + 1, out);
    }
    return out;
  }

  function statusMeta(status) {
    return STATUS[status] || { label: status, className: "" };
  }

  function originMeta(origin) {
    return ORIGIN[origin] || { label: origin || "未知", className: "" };
  }

  function flagMeta(flag) {
    return FLAGS[Number(flag)] || { label: `旗帜 ${flag}`, className: "" };
  }

  function initShell() {
    const toggle = document.getElementById("sidebar-toggle");
    const close = document.getElementById("sidebar-close");
    const backdrop = document.getElementById("sidebar-backdrop");

    const openSidebar = () => {
      document.body.classList.add("sidebar-open");
      if (backdrop) backdrop.hidden = false;
      if (toggle) toggle.setAttribute("aria-expanded", "true");
    };

    const closeSidebar = () => {
      document.body.classList.remove("sidebar-open");
      if (backdrop) backdrop.hidden = true;
      if (toggle) toggle.setAttribute("aria-expanded", "false");
    };

    if (toggle) toggle.addEventListener("click", openSidebar);
    if (close) close.addEventListener("click", closeSidebar);
    if (backdrop) backdrop.addEventListener("click", closeSidebar);

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        document.body.classList.contains("sidebar-open")
      ) {
        closeSidebar();
      }
    });

    document.querySelectorAll(".file-dropzone").forEach((zone) => {
      const input = zone.querySelector('input[type="file"]');
      const nameEl = zone.querySelector(".file-dropzone-name");
      if (!input || !nameEl) return;

      const defaultLabel = nameEl.textContent;
      const updateName = () => {
        const file = input.files && input.files[0];
        if (file) {
          nameEl.textContent = file.name;
          zone.classList.add("has-file");
        } else {
          nameEl.textContent = defaultLabel;
          zone.classList.remove("has-file");
        }
      };

      input.addEventListener("change", updateName);
      zone.addEventListener("dragover", (event) => {
        event.preventDefault();
        zone.classList.add("drag-over");
      });
      zone.addEventListener("dragleave", () => {
        zone.classList.remove("drag-over");
      });
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("drag-over");
        const files = event.dataTransfer && event.dataTransfer.files;
        if (!files || !files.length) return;
        const dt = new DataTransfer();
        dt.items.add(files[0]);
        input.files = dt.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initShell);
  } else {
    initShell();
  }

  return {
    fetchJSON,
    debounce,
    formatTime,
    flattenModules,
    statusMeta,
    originMeta,
    flagMeta,
  };
})();
