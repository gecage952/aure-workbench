/* The Settings page: the IPTS, where the data is, and how new runs are noticed.
 *
 * Everything here is nrw.toml's. The page shows what the file says, sends back
 * only what the person changed, and says which revision of the file it was
 * shown, so an edit made by hand in between is never written over. The server
 * decides what is allowed; this page only explains its answer.
 */
"use strict";

(function () {
  const N = window.NRWPage;
  const $ = N.$;
  const el = N.el;
  const api = N.client();
  let shown = window.NRW_SETTINGS;

  function message(text, kind) {
    N.show($("set-message"), text, kind);
  }

  function writable() {
    return Boolean(shown.writable);
  }

  function isSet(value) {
    return value !== null && value !== undefined;
  }

  /* What a setting is in effect: its value, else nrw's default. */
  function effective(name) {
    return isSet(shown.values[name]) ? shown.values[name] : shown.defaults[name];
  }

  function normalizedIpts(text) {
    const trimmed = text.trim();
    return /^\d+$/.test(trimmed) ? "IPTS-" + trimmed : trimmed.toUpperCase();
  }

  function currentIpts() {
    return normalizedIpts($("s-ipts").value) || shown.effective.ipts || "";
  }

  function checked(name) {
    const input = document.querySelector('input[name="' + name + '"]:checked');
    return input ? input.value : null;
  }

  /* ---------------------------------------------------------------- */
  /* Showing                                                            */
  /* ---------------------------------------------------------------- */

  function renderOptions(container, options, name, chosen) {
    container.replaceChildren();
    options.forEach(function (option) {
      const id = name + "-" + option.kind;
      container.append(
        el("div", { className: "form-check" }, [
          el("input", {
            className: "form-check-input",
            type: "radio",
            name: name,
            id: id,
            value: option.kind,
            checked: option.kind === chosen,
            disabled: !option.available || !writable(),
          }),
          el("label", { className: "form-check-label", htmlFor: id }, [
            option.label,
            option.available ? null : el("span", {
              className: "badge text-bg-light border ms-2", text: "coming",
            }),
          ]),
          el("div", { className: "small text-secondary", text: option.detail }),
        ])
      );
    });
  }

  function renderDefaultPath() {
    const template = shown.defaults["source.location"];
    const ipts = currentIpts();
    $("s-default-path").textContent = ipts
      ? template.replace("{ipts}", ipts)
      : template + "  -- needs the IPTS";
  }

  function renderIptsHint() {
    const hint = $("s-ipts-hint");
    hint.replaceChildren();
    if ($("s-ipts").value.trim()) return;
    if (shown.suggested_ipts) {
      const use = el("button", {
        type: "button",
        className: "btn btn-link btn-sm p-0 align-baseline",
        text: "use it",
        disabled: !writable(),
      });
      use.addEventListener("click", function () {
        $("s-ipts").value = shown.suggested_ipts;
        renderIptsHint();
        renderDefaultPath();
      });
      hint.append("This project's folder names " + shown.suggested_ipts + ": ", use);
    } else {
      hint.textContent = "Needed to find the data at nrw's default location.";
    }
  }

  function render() {
    const values = shown.values;
    $("s-ipts").value = values.ipts || "";
    $("s-label").value = values.label || "";
    renderOptions($("s-sources"), shown.options.source, "s-source", effective("source.kind"));
    renderOptions($("s-feeds"), shown.options.feed, "s-feed", effective("feed.kind"));
    // Shown, not saved: the project's own files are where it is kept.
    renderOptions($("s-catalogs"), shown.options.catalog, "s-catalog",
      shown.effective.catalog_kind);
    const custom = isSet(values["source.location"]);
    $("s-default").checked = !custom;
    $("s-custom").checked = custom;
    $("s-location").value = custom ? values["source.location"] : "";
    [["s-settle", "source.settle_seconds"], ["s-poll", "feed.poll_seconds"]].forEach(
      function ([id, name]) {
        const input = $(id);
        input.value = isSet(values[name]) ? values[name] : "";
        input.placeholder = shown.defaults[name] + " (nrw's default)";
        input.min = shown.ranges[name][0];
        input.max = shown.ranges[name][1];
      }
    );
    renderIptsHint();
    renderDefaultPath();

    const banner = $("set-readonly");
    if (writable()) {
      banner.classList.add("d-none");
    } else {
      banner.classList.remove("d-none");
      banner.textContent = shown.read_only_reason || (
        shown.editable
          ? "View only."
          : "nrw.toml cannot be edited from here; the notes below say why."
      );
    }
    ["s-ipts", "s-label", "s-default", "s-custom", "s-location", "s-settle",
      "s-poll", "s-check", "s-save"].forEach(function (id) {
      $(id).disabled = !writable();
    });
    N.renderProblems($("set-problems"), shown.problems);
  }

  /* ---------------------------------------------------------------- */
  /* What the person changed                                            */
  /* ---------------------------------------------------------------- */

  function number(id, name, out) {
    const text = $(id).value.trim();
    const current = shown.values[name];
    if (text === "") {
      if (isSet(current)) out[name] = null;
      return;
    }
    const value = Number(text);
    // Something that is not a number is sent as typed, for the server to
    // refuse with its reason.
    if (value !== current) out[name] = Number.isFinite(value) ? value : text;
  }

  function changes() {
    const out = {};
    const values = shown.values;
    const ipts = $("s-ipts").value.trim();
    if (ipts !== (values.ipts || "")) out.ipts = ipts;
    const label = $("s-label").value.trim();
    if (label !== (values.label || "")) out.label = label;
    const source = checked("s-source");
    if (source && source !== effective("source.kind")) out["source.kind"] = source;
    if ($("s-default").checked) {
      if (isSet(values["source.location"])) out["source.location"] = null;
    } else {
      const location = $("s-location").value.trim();
      if (location !== values["source.location"]) out["source.location"] = location;
    }
    number("s-settle", "source.settle_seconds", out);
    const feed = checked("s-feed");
    if (feed && feed !== effective("feed.kind")) out["feed.kind"] = feed;
    number("s-poll", "feed.poll_seconds", out);
    return out;
  }

  /* ---------------------------------------------------------------- */
  /* Saving                                                             */
  /* ---------------------------------------------------------------- */

  async function reload() {
    shown = await api("GET", "/api/experiment/settings");
    render();
  }

  function saved(result) {
    const parts = [result.changed ? "Saved." : "Nothing needed changing."];
    if (result.backup) parts.push("The previous nrw.toml is kept at " + result.backup + ".");
    parts.push(...(result.notes || []), ...(result.warnings || []));
    // A note or a warning is worth reading, so it stays on the page.
    const quiet = !(result.notes || []).length && !(result.warnings || []).length;
    message(parts.join(" "), quiet ? "success" : "info");
    $("s-diff").textContent = result.diff || "";
    $("s-diff-box").classList.toggle("d-none", !result.diff);
    const status = $("s-status");
    status.replaceChildren();
    // What the server reads now, as it says -- not what this page expects.
    if (!shown.effective.needs_setup && shown.watching) {
      status.append(
        "The Experiment page now watches " + shown.watching + ". ",
        el("a", { href: "/experiment", text: "Open it" })
      );
    }
  }

  async function save(event, confirmed) {
    if (event) event.preventDefault();
    confirmed = confirmed || [];
    const change = changes();
    const status = $("s-status");
    if (!Object.keys(change).length) {
      status.textContent = "Nothing has changed.";
      return;
    }
    $("s-save").disabled = true;
    status.textContent = "Saving…";
    $("s-lines").classList.add("d-none");
    try {
      const response = await api("PUT", "/api/experiment/settings", {
        revision: shown.revision,
        changes: change,
        confirmed: confirmed,
      });
      shown = response.settings;
      render();
      status.textContent = "";
      saved(response.result);
    } catch (error) {
      status.textContent = "Not saved.";
      const payload = error.payload || {};
      if (error.status === 409 && payload.needs && !confirmed.includes(payload.needs)) {
        // The server names what to confirm; the page asks, and sends the same
        // change again with that confirmation. Asked once each, never looped.
        if (window.confirm(error.message + "\n\nGo ahead anyway?")) {
          await save(null, confirmed.concat([payload.needs]));
        }
      } else if (error.status === 409 && payload.lines) {
        message(error.message, "warning");
        $("s-lines").textContent = payload.lines;
        $("s-lines").classList.remove("d-none");
      } else if (payload.kind === "TomlConflictError") {
        // nrw.toml changed since the page loaded it: show what it says now,
        // and only then say so.
        try {
          await reload();
          message(error.message + " The settings were reloaded; make the change again.", "warning");
        } catch (_) {
          message(error.message + " Reload the page to see what nrw.toml says now.", "warning");
        }
      } else {
        message(error.message, error.status === 409 ? "warning" : "danger");
      }
    } finally {
      $("s-save").disabled = !writable();
    }
  }

  /* ---------------------------------------------------------------- */
  /* Checking a folder before choosing it                               */
  /* ---------------------------------------------------------------- */

  function renderCheck(found) {
    const parts = [el("div", { className: "mono", text: found.path })];
    if (!found.reachable) {
      const why = (found.problems[0] || {}).message || "It cannot be listed.";
      parts.push(el("div", { className: "text-danger", text: why }));
    } else if (!found.runs) {
      parts.push(el("div", { text: "Reachable, and no reduced runs in it yet." }));
    } else {
      parts.push(el("div", {
        text: found.runs + " run(s), " + found.first + " to " + found.last + ".",
      }));
      if (found.experiments.length) {
        // Said, not judged: whether a header's IPTS is this project's is the
        // server's call, made against the IPTS being checked, and it comes
        // back as a problem on the run below.
        parts.push(el("div", {
          className: "text-secondary",
          text: "The newest files' headers name " + found.experiments.join(", ") + ".",
        }));
      }
      const list = el("ul", { className: "mb-0" });
      found.newest.slice().reverse().forEach(function (run) {
        const segments = run.segments.join(",") +
          (run.n_segments ? " of " + run.n_segments : "");
        list.append(el("li", {}, [
          el("span", { className: "mono", text: String(run.run) }),
          "  " + (run.title || "") + "  segments " + segments,
        ].concat((run.problems || []).map(function (text) {
          return el("div", { className: "small text-warning-emphasis", text: text });
        }))));
      });
      parts.push(el("div", { className: "text-secondary mt-1", text: "Newest:" }), list);
    }
    if (found.reachable && found.unrecognized) {
      parts.push(el("div", {
        className: "text-secondary",
        text: found.unrecognized + " data file(s) whose names nrw does not recognise.",
      }));
    }
    (found.reachable ? found.problems : found.problems.slice(1)).forEach(function (p) {
      parts.push(el("div", { className: "text-warning-emphasis", text: p.message }));
    });
    (found.warnings || []).forEach(function (text) {
      parts.push(el("div", { className: "text-warning-emphasis", text: text }));
    });
    $("s-check-result").replaceChildren(...parts);
  }

  async function check() {
    const status = $("s-check-status");
    $("s-check-result").replaceChildren();
    status.textContent = "checking…";
    $("s-check").disabled = true;
    const body = { location: $("s-default").checked ? null : $("s-location").value.trim() };
    // Whatever the save would change about the source, the check reads too.
    const ipts = $("s-ipts").value.trim();
    if (ipts !== (shown.values.ipts || "")) body.ipts = ipts;
    const kind = checked("s-source");
    if (kind && kind !== effective("source.kind")) body.kind = kind;
    try {
      const found = await api("POST", "/api/experiment/settings/check", body);
      status.textContent = "";
      renderCheck(found);
    } catch (error) {
      status.textContent = error.message;
    } finally {
      $("s-check").disabled = !writable();
    }
  }

  /* ---------------------------------------------------------------- */
  /* Wiring                                                             */
  /* ---------------------------------------------------------------- */

  $("set-form").addEventListener("submit", save);
  $("s-check").addEventListener("click", check);
  $("s-ipts").addEventListener("input", function () {
    renderIptsHint();
    renderDefaultPath();
  });
  $("s-location").addEventListener("input", function () {
    $("s-custom").checked = true;
  });

  render();
})();
