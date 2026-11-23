window.cm = (() => {
  const STATUS = {
    draft: { label: "草稿", className: "draft" },
    ready: { label: "已整理", className: "ready" },
    archived: { label: "已归档", className: "archived" },
  };

  const ORIGIN = {
    crawl: { label: "RSS 抓取", className: "origin-crawl" },
    apkg: { label: "apkg 导入", className: "origin-apkg" },
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
