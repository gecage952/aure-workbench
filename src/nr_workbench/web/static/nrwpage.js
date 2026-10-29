/* What the Experiment and Settings pages share.
 *
 * Every value that came from a file or a person is set with textContent,
 * never innerHTML: a run title typed at the instrument, or a folder name,
 * containing markup must render as text.
 */
"use strict";

window.NRWPage = (function () {
  function $(id) {
    return document.getElementById(id);
  }

  /* createElement with text and attributes; never innerHTML. */
  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(function ([key, value]) {
      if (value === undefined || value === null || value === false) return;
      if (key === "text") node.textContent = value;
      else if (key === "className") node.className = value;
      else if (key in node && key !== "title") node[key] = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    (children || []).forEach(function (child) {
      if (child !== null && child !== undefined) {
        node.append(typeof child === "string" ? document.createTextNode(child) : child);
      }
    });
    return node;
  }

  /* A JSON client for the page's API. An error keeps the server's JSON on
   * `error.payload`, so a page can show the TOML to add by hand, or ask for
   * the confirmation it needs. */
  function client() {
    return async function api(method, path, body) {
      const init = { method: method, headers: {} };
      if (body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      const response = await fetch(path, init);
      let payload = null;
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
      if (!response.ok) {
        const error = new Error((payload && payload.error) || "HTTP " + response.status);
        error.status = response.status;
        error.payload = payload || {};
        throw error;
      }
      return payload;
    };
  }

  /* Show a message in an alert box; a success fades after a few seconds. */
  function show(box, text, kind) {
    box.className = "alert py-2 alert-" + (kind || "info");
    box.textContent = text;
    if (kind === "success") {
      setTimeout(function () {
        box.classList.add("d-none");
      }, 4000);
    }
  }

  function renderProblems(box, problems) {
    const list = box.querySelector("ul");
    list.replaceChildren();
    (problems || []).forEach(function (problem) {
      list.append(el("li", { text: problem.message }));
    });
    box.classList.toggle("d-none", !(problems || []).length);
  }

  return { $: $, el: el, client: client, show: show, renderProblems: renderProblems };
})();
