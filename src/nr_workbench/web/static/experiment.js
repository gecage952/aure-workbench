/* The Experiment page: every run, organized into samples, and apply.
 *
 * Everything shown comes from /api/experiment and nothing here decides
 * anything: the catalog validates edits, apply decides what may be copied.
 *
 * Every value that came from a file or a person is set with textContent. Run
 * titles are typed by an operator at the instrument, and will later come from
 * a remote source; a title containing markup must render as text. The one
 * exception is the sample.md preview, which the server renders with raw HTML
 * disabled -- exactly as the sample page renders sample.md.
 */
"use strict";

(function () {
  const initial = window.NRW_EXPERIMENT;
  const POLL_MS = 10000;
  const MAX_QUICK = 8;

  const STATE_BADGE = {
    complete: "text-bg-success",
    unconfirmed: "text-bg-warning",
    arriving: "text-bg-info",
    settling: "text-bg-info",
    awaiting: "text-bg-secondary",
    quarantined: "text-bg-danger",
    "clock-skew": "text-bg-danger",
    "not listed": "text-bg-light",
  };
  /* sample.md is written by Apply, never by saving a sample: say when. */
  const MD_STATE = {
    create: ["created when you apply", "text-bg-info"],
    unchanged: ["in step", "text-bg-success"],
    upgrade: ["updated when you apply", "text-bg-info"],
    drifted: ["edited by hand", "text-bg-warning"],
    untracked: ["not written by nrw", "text-bg-warning"],
  };
  const SYMBOL = {
    copy: "+", restore: "+", record: "=", "move-out": "−", deferred: "…",
    "not-managed": "?",
  };

  const state = {
    rows: new Map(),
    samples: [],
    payload: initial,
    cursor: initial.cursor,
    catalogVersion: initial.catalog.version,
    filter: "all",
    search: "",
    selected: new Set(),
    editing: null,
    // The open sample's id, rev and the values the editor was filled with.
    editorBase: null,
    // The experiment's own context, and the rev and values its panel was
    // filled with.
    experiment: initial.experiment || {},
    contextBase: null,
    writable: Boolean(initial.writable),
    confirmed: new Set(),
    plan: null,
  };

  const $ = window.NRWPage.$;
  const el = window.NRWPage.el;
  const api = window.NRWPage.client();

  function message(text, kind) {
    window.NRWPage.show($("expt-message"), text, kind);
  }

  async function failed(error, what) {
    if (error.status === 409) {
      message(error.message + " The page has been reloaded.", "warning");
      await reload();
    } else {
      message(what + ": " + error.message, "danger");
    }
  }

  /* ---------------------------------------------------------------- */
  /* Loading and polling                                                */
  /* ---------------------------------------------------------------- */

  function load(payload) {
    state.payload = payload;
    state.rows = new Map(payload.runs.map(function (row) {
      return [row.key, row];
    }));
    state.samples = payload.samples;
    state.experiment = payload.experiment || {};
    state.cursor = payload.cursor;
    // Only a load sets this, never a save's answer: the catalog a save wrote
    // can hold what someone else saved since the last poll, so the poll after
    // a save always reloads, and shows it.
    state.catalogVersion = payload.catalog.version;
    state.writable = Boolean(payload.writable);
    for (const key of Array.from(state.selected)) {
      if (!state.rows.has(key)) state.selected.delete(key);
    }
    renderAll();
  }

  async function reload() {
    load(await api("GET", "/api/experiment"));
    // Keep what is being typed. The page reloads after a conflict, and when
    // the catalog was saved elsewhere -- another tab, the command line, a
    // `git pull` -- and refilling the editor then wiped an unsaved edit, or
    // left stale values in it for the next save to write back.
    if (state.editing) openSample(state.editing, true);
  }

  async function poll() {
    try {
      const changes = await api(
        "GET", "/api/experiment/changes?since=" + encodeURIComponent(state.cursor)
      );
      if (changes.resync || changes.catalog_version !== state.catalogVersion) {
        await reload();
        return;
      }
      changes.runs.forEach(function (row) {
        state.rows.set(row.key, row);
      });
      changes.removed.forEach(function (key) {
        state.rows.delete(key);
        state.selected.delete(key);
      });
      state.cursor = changes.cursor;
      renderScan(changes.scan);
      renderProblems(changes.problems);
      if (changes.runs.length || changes.removed.length) renderRuns();
      firstLook(changes.scan);
    } catch (error) {
      renderScan(null, error.message);
    }
  }

  /* The server lists the data source in the background, so the very first
   * page can arrive before that listing has finished. Ask again every second
   * until it has, rather than showing an empty table for a whole poll. */
  let firstLookTimer = null;
  function firstLook(scan) {
    clearTimeout(firstLookTimer);
    if (scan && scan.scanned_at === null) firstLookTimer = setTimeout(poll, 1000);
  }

  /* ---------------------------------------------------------------- */
  /* Header                                                             */
  /* ---------------------------------------------------------------- */

  function renderHeader() {
    const payload = state.payload;
    $("expt-ipts").textContent = payload.ipts || "(no IPTS in nrw.toml)";
    const source = payload.source || {};
    $("expt-source").textContent =
      (source.kind || "") + "  " + (source.path || source.location || "");
    // Nothing can be watched until someone says where the data is.
    $("expt-setup").classList.toggle("d-none", !payload.needs_setup);
    const banner = $("expt-readonly");
    if (state.writable) {
      banner.classList.add("d-none");
    } else {
      banner.classList.remove("d-none");
      banner.textContent = payload.read_only_reason || "View only.";
    }
    renderScan(payload.scan);
  }

  function renderScan(scan, error) {
    const node = $("expt-scan");
    if (error) {
      node.textContent = "cannot reach the server: " + error;
      return;
    }
    if (!scan || scan.scanned_at === null) {
      node.textContent = "first look at the data source in progress…";
      return;
    }
    let text = "checked " + Math.round(scan.age) + " s ago";
    if (scan.stuck) text = "the data source is not answering";
    else if (scan.scanning) text += " · checking now";
    node.textContent = text;
  }

  function renderProblems(problems) {
    window.NRWPage.renderProblems($("expt-problems"), problems);
  }

  /* ---------------------------------------------------------------- */
  /* Runs                                                               */
  /* ---------------------------------------------------------------- */

  function visibleRows() {
    const needle = state.search.trim().toLowerCase();
    return Array.from(state.rows.values())
      .filter(function (row) {
        if (state.filter === "unassigned" && row.sample) return false;
        if (state.filter === "arriving" &&
            ["arriving", "settling", "awaiting"].indexOf(row.state) === -1) return false;
        if (state.filter === "attention" &&
            ["unconfirmed", "quarantined", "clock-skew", "not listed"].indexOf(row.state) === -1) {
          return false;
        }
        if (!needle) return true;
        return [String(row.run), row.title || "", row.sample || ""].some(function (text) {
          return text.toLowerCase().indexOf(needle) !== -1;
        });
      })
      .sort(function (a, b) {
        return a.run - b.run;
      });
  }

  function segmentsText(row) {
    const combined = (row.artifacts || []).some(function (a) {
      return a.role === "combined";
    });
    if (!row.segments.length) return combined ? "combined only" : "—";
    const listed = row.segments.join(",");
    const text = row.n_segments ? listed + " of " + row.n_segments : listed;
    return combined ? text + " + combined" : text;
  }

  function renderRuns() {
    const tbody = $("expt-runs").querySelector("tbody");
    tbody.replaceChildren();
    const rows = visibleRows();
    rows.forEach(function (row) {
      const box = el("input", {
        type: "checkbox",
        checked: state.selected.has(row.key),
        "aria-label": "select run " + row.run,
      });
      box.addEventListener("change", function () {
        if (box.checked) state.selected.add(row.key);
        else state.selected.delete(row.key);
        renderBulk();
        scheduleQuickLook();
      });
      const badge = el("span", {
        className: "badge " + (STATE_BADGE[row.state] || "text-bg-light"),
        text: row.state,
        title: row.reason,
      });
      const thetas = row.thetas.filter(function (t) {
        return t !== null;
      }).map(function (t) {
        return t.toFixed(2);
      }).join(" / ");
      const tr = el("tr", { className: row.include ? "" : "text-secondary" }, [
        el("td", {}, [box]),
        el("td", { className: "mono", text: String(row.run) }),
        el("td", {}, [badge]),
        el("td", { className: "small expt-title", text: row.title || "", title: row.reason }),
        el("td", { className: "small mono", text: segmentsText(row) }),
        el("td", { className: "small mono", text: thetas || "—" }),
        el("td", { className: "mono", text: row.sample || "—" }),
        el("td", { className: "small", text: row.measurement }),
        el("td", { className: "small", text: row.condition }),
        el("td", { className: "small" }, [row.include ? null : el("span", {
          className: "badge text-bg-secondary", text: "excluded",
        })]),
      ]);
      tbody.append(tr);
    });
    const empty = $("expt-empty");
    empty.classList.toggle("d-none", rows.length > 0);
    empty.textContent = state.rows.size
      ? "No runs match."
      : "No runs yet. They appear here as the reduction writes them.";
    $("expt-select-all").checked =
      rows.length > 0 && rows.every(function (row) {
        return state.selected.has(row.key);
      });
  }

  function renderBulk() {
    const bar = $("expt-bulk");
    const count = state.selected.size;
    bar.classList.toggle("d-none", !(state.writable && count));
    $("expt-selected").textContent = count + " selected";
    const select = $("expt-sample");
    const current = select.value;
    select.replaceChildren(el("option", { value: "", text: "to sample…" }));
    state.samples.filter(function (card) {
      return card.managed;
    }).forEach(function (card) {
      select.append(el("option", { value: card.id, text: card.id }));
    });
    select.append(el("option", { value: "__new__", text: "new sample…" }));
    if (current) select.value = current;
    $("expt-new-sample").classList.toggle("d-none", select.value !== "__new__");
    // Rebuilt with the types now in use, keeping whatever was chosen. It
    // starts at "unchanged": a type never set is steady anyway, and choosing
    // steady here would reset runs already recorded as tNR.
    const slot = $("expt-type-slot");
    const field = slot.querySelector(".expt-type-field");
    const chosen = field ? field.typeValue() : "";
    slot.replaceChildren(typeField(chosen, "measurement type", "type (unchanged)"));
  }

  async function editRuns(fields, what) {
    const changes = Array.from(state.selected).map(function (key) {
      const row = state.rows.get(key);
      return { run: row.run, base_rev: row.rev, fields: fields };
    });
    try {
      const result = await api("PUT", "/api/experiment/runs", { changes: changes });
      result.runs.forEach(function (row) {
        state.rows.set(row.key, row);
      });
      state.samples = result.samples;
      state.selected.clear();
      renderRuns();
      renderBulk();
      renderSamples();
      message(what + " " + changes.length + " run(s).", "success");
    } catch (error) {
      await failed(error, what);
    }
  }

  function assign() {
    let sample = $("expt-sample").value;
    if (sample === "__new__") sample = $("expt-new-sample").value.trim();
    if (!sample) {
      message("Choose a sample, or type a new sample id.", "warning");
      return;
    }
    const fields = { sample_id: sample };
    const type = $("expt-type-slot").querySelector(".expt-type-field").typeValue();
    const condition = $("expt-condition").value.trim();
    if (type) fields.measurement = type;
    if (condition) fields.condition = condition;
    editRuns(fields, "Assigned");
  }

  /* ---------------------------------------------------------------- */
  /* Quick look                                                         */
  /* ---------------------------------------------------------------- */

  let quick = null;
  let quickTimer = null;
  let quickRequest = 0;

  function scheduleQuickLook() {
    clearTimeout(quickTimer);
    quickTimer = setTimeout(quickLook, 300);
  }

  async function quickLook() {
    const runs = Array.from(state.selected).map(function (key) {
      return state.rows.get(key);
    }).filter(function (row) {
      return row && row.segments.length;
    }).slice(0, MAX_QUICK);
    const hint = $("quick-hint");
    if (!runs.length) {
      hint.textContent = "Select runs to plot them.";
      if (quick) quick.setCurves([]);
      return;
    }
    const ticket = ++quickRequest;
    hint.textContent = "reading " + runs.length + " run(s) from the data source…";
    const curves = [];
    const problems = [];
    for (const row of runs) {
      try {
        const result = await api("GET", "/api/experiment/runs/" + row.run + "/curves");
        result.curves.forEach(function (curve) {
          curves.push(curve);
        });
        result.problems.forEach(function (p) {
          problems.push(p.message);
        });
      } catch (error) {
        problems.push("run " + row.run + ": " + error.message);
      }
    }
    if (ticket !== quickRequest) return;
    hint.textContent = problems.length
      ? problems.join("; ")
      : state.selected.size > MAX_QUICK
        ? "showing the first " + MAX_QUICK + " selected runs"
        : "";
    if (typeof NRW === "undefined" || typeof Plotly === "undefined") return;
    if (!quick) quick = NRW.reflPanel("plot-quick", curves, { toggleId: "rq4-toggle" });
    else quick.setCurves(curves);
  }

  /* ---------------------------------------------------------------- */
  /* What both editors share                                            */
  /* ---------------------------------------------------------------- */

  /* Fill each box (field name to element id) from the server's values, except
   * one typed in since it was last filled: its value differs from what it was
   * filled with, in *shown*, and a reload -- after a conflict, or a save made
   * elsewhere -- must not overwrite it. Says whether any box was kept. */
  function fillKeepingTyped(fields, fresh, shown, disabled) {
    let typed = false;
    Object.entries(fields).forEach(function ([name, id]) {
      if (shown && $(id).value !== shown[name]) typed = true;
      else $(id).value = fresh[name];
      $(id).disabled = disabled;
    });
    return typed;
  }

  /* The boxes whose value differs from the record's: all a save sends. */
  function changedFields(fields, record) {
    const changed = {};
    Object.entries(fields).forEach(function ([name, id]) {
      const value = $(id).value;
      if ((record[name] || "") !== value) changed[name] = value;
    });
    return changed;
  }

  function savedElsewhere(what) {
    return what + " was saved somewhere else while you were editing. What you " +
      "typed is kept; saving puts your version of the fields you changed over " +
      "the saved ones.";
  }

  /* ---------------------------------------------------------------- */
  /* The experiment                                                     */
  /* ---------------------------------------------------------------- */

  const CONTEXT_FIELDS = {
    summary: "c-summary", hypotheses: "c-hypotheses", notes: "c-notes",
  };

  function renderContext() {
    const record = state.experiment;
    const base = state.contextBase;
    const values = {};
    Object.keys(CONTEXT_FIELDS).forEach(function (name) {
      values[name] = record[name] || "";
    });
    const typed = fillKeepingTyped(
      CONTEXT_FIELDS, values, base ? base.values : null, !state.writable
    );
    $("expt-context-save").disabled = !state.writable;
    if (typed && base.rev !== (record.rev || 0)) {
      $("expt-context-status").textContent = savedElsewhere("The experiment");
    } else if (!base) {
      $("expt-context-status").textContent = "";
    }
    // Written means something is in it: a record saved empty is not written.
    const texts = Object.values(values).filter(Boolean);
    $("expt-context-state").textContent = texts.length
      ? texts[0].split("\n")[0]
      : "not written yet";
    // Open on a new experiment, to be filled in; folded away once it is.
    if (!base && !texts.length && state.writable) $("expt-context").open = true;
    state.contextBase = { rev: record.rev || 0, values: values };
  }

  async function saveContext(event) {
    event.preventDefault();
    const record = state.experiment;
    const fields = changedFields(CONTEXT_FIELDS, record);
    if (!Object.keys(fields).length) {
      $("expt-context-status").textContent = "Nothing changed.";
      return;
    }
    try {
      const result = await api(
        "PUT", "/api/experiment/context", { base_rev: record.rev || 0, fields: fields }
      );
      state.experiment = result.experiment;
      // Refilled from what the server now holds, which is what was typed.
      state.contextBase = null;
      renderContext();
      $("expt-context-status").textContent = "Saved.";
    } catch (error) {
      if (error.status === 400) $("expt-context-status").textContent = error.message;
      else await failed(error, "Saving the experiment");
    }
  }

  /* ---------------------------------------------------------------- */
  /* Samples                                                            */
  /* ---------------------------------------------------------------- */

  function renderSamples() {
    const list = $("expt-samples");
    list.replaceChildren();
    if (!state.samples.length) {
      list.append(el("p", {
        className: "p-3 mb-0 text-secondary",
        text: "No samples yet. Select runs and assign them to a new sample.",
      }));
    }
    state.samples.forEach(function (card) {
      const detail = card.managed
        ? card.runs.length + " run(s)" +
          (card.excluded.length ? ", " + card.excluded.length + " excluded" : "")
        : "on disk, not in the catalog";
      const item = el("button", {
        type: "button",
        className: "list-group-item list-group-item-action" +
          (state.editing === card.id ? " active" : ""),
      }, [
        el("div", { className: "d-flex gap-2" }, [
          el("span", { className: "mono", text: card.id }),
          el("span", { className: "text-truncate", text: card.title || "" }),
        ]),
        el("div", { className: "small opacity-75", text: detail }),
      ]);
      item.addEventListener("click", function () {
        openSample(card.id);
      });
      list.append(item);
    });
  }

  const FIELDS = {
    title: "f-title",
    description: "f-description",
    details: "f-details",
    mounting: "f-mounting",
    measurement_conditions: "f-conditions",
    fits_to_perform: "f-fits",
  };

  function card(id) {
    return state.samples.find(function (c) {
      return c.id === id;
    });
  }

  const NEW_TYPE = "__new__";

  /* The types to offer: the built-in ones first, then every other type this
   * experiment already uses -- the types in use are the list. */
  function typeChoices(extra) {
    const builtIn = state.payload.measurement_types || [];
    const others = new Set();
    state.rows.forEach(function (row) {
      if (row.measurement) others.add(row.measurement);
    });
    if (extra) others.add(extra);
    builtIn.forEach(function (type) {
      others.delete(type);
    });
    return builtIn.concat(Array.from(others).sort());
  }

  /* A type chooser, with "Add a type…" opening a box to type a new one. */
  function typeField(selected, label, unchanged) {
    const select = el("select", { className: "form-select form-select-sm", "aria-label": label });
    if (unchanged) select.append(el("option", { value: "", text: unchanged }));
    typeChoices(selected).forEach(function (type) {
      select.append(el("option", { value: type, text: type }));
    });
    select.append(el("option", { value: NEW_TYPE, text: "Add a type…" }));
    select.value = selected || "";
    const other = el("input", {
      className: "form-control form-control-sm d-none",
      placeholder: "new type",
      "aria-label": "new " + label,
    });
    select.addEventListener("change", function () {
      other.classList.toggle("d-none", select.value !== NEW_TYPE);
      if (select.value === NEW_TYPE) other.focus();
    });
    const field = el("span", { className: "d-flex gap-1 expt-type-field" }, [select, other]);
    field.typeValue = function () {
      return select.value === NEW_TYPE ? other.value.trim() : select.value;
    };
    field.disable = function (off) {
      select.disabled = off;
      other.disabled = off;
    };
    return field;
  }

  /* The runs of one sample, in run order. */
  function sampleRuns(id) {
    return Array.from(state.rows.values()).filter(function (row) {
      return row.sample === id;
    }).sort(function (a, b) {
      return a.run - b.run;
    });
  }

  /* What one run's row in the editor shows, as the server has it. */
  function runValues(row) {
    return {
      measurement: row.measurement || "",
      condition: row.condition || "",
      note: row.note || "",
      include: row.include !== false,
    };
  }

  /* What is in each run's row of the editor now, by run key. */
  function typedRuns() {
    const out = {};
    document.querySelectorAll("#f-measurements .expt-measurement").forEach(function (item) {
      out[item.dataset.key] = {
        measurement: item.querySelector(".expt-type-field").typeValue(),
        condition: item.querySelector("[data-field=condition]").value,
        note: item.querySelector("[data-field=note]").value,
        include: item.querySelector("[data-field=include]").checked,
      };
    });
    return out;
  }

  /* The sample's measurements, one per run: its type and condition, which
   * sample.md's table writes, and the notes on it, which go under
   * Measurement conditions with the run named. What was typed and not saved
   * is kept, as the fields above are. Returns what each run showed fresh. */
  function renderMeasurements(id, base, same, editable) {
    const typed = same ? typedRuns() : {};
    const box = $("f-measurements");
    box.replaceChildren();
    const rows = sampleRuns(id);
    if (!rows.length) {
      box.append(el("p", {
        className: "text-secondary mb-0",
        text: "No runs yet: select runs above and assign them to this sample.",
      }));
    }
    const fresh = {};
    let kept = false;
    rows.forEach(function (row) {
      fresh[row.key] = runValues(row);
      const shown = (base && base.runs && base.runs[row.key]) || null;
      const mine = typed[row.key] || {};
      function value(name) {
        if (same && shown && mine[name] !== undefined && mine[name] !== shown[name]) {
          kept = true;
          return mine[name];
        }
        return fresh[row.key][name];
      }
      const type = typeField(value("measurement"), "type of run " + row.run);
      const condition = el("input", {
        className: "form-control form-control-sm mb-1",
        placeholder: "condition",
        "aria-label": "condition of run " + row.run,
        "data-field": "condition",
      });
      condition.value = value("condition");
      const note = el("textarea", {
        className: "form-control form-control-sm",
        rows: 1,
        placeholder: "notes on this measurement",
        "aria-label": "notes on run " + row.run,
        "data-field": "note",
      });
      note.value = value("note");
      // Good (used) by default. A bad run slid off is excluded: left out of
      // sample.md's table, and moved out of data/steady by the next apply.
      const good = el("input", {
        className: "form-check-input",
        type: "checkbox",
        role: "switch",
        id: "f-good-" + row.run,
        "aria-label": "run " + row.run + " is good",
        "data-field": "include",
      });
      good.checked = value("include");
      const said = el("label", {
        className: "form-check-label small",
        htmlFor: "f-good-" + row.run,
      });
      function describe() {
        said.textContent = good.checked ? "good" : "bad: not used";
        item.classList.toggle("expt-measurement-bad", !good.checked);
      }
      good.addEventListener("change", describe);
      type.disable(!editable);
      condition.disabled = !editable;
      note.disabled = !editable;
      good.disabled = !editable;
      // The condition on a line of its own: in the rail's width it is the
      // field that needs the room ("-0.5 mA/cm2 after 2 h").
      const item = el("div", { className: "expt-measurement" }, [
        el("div", { className: "d-flex gap-2 align-items-center mb-1" }, [
          el("span", { className: "mono", text: String(row.run) }),
          type,
          el("div", { className: "form-check form-switch mb-0 ms-auto" }, [good, said]),
        ]),
        condition,
        note,
      ]);
      item.dataset.key = row.key;
      describe();
      box.append(item);
    });
    return { fresh: fresh, kept: kept };
  }

  /* What the editor showed when it was filled: a field that differs from it
   * has been typed in, and is not the server's to replace. */
  function shownValues(current) {
    const values = {};
    Object.entries(FIELDS).forEach(function ([name]) {
      values[name] = current && current.managed ? current[name] || "" : "";
      if (name === "mounting" && !values[name]) values[name] = "unknown";
    });
    return values;
  }

  async function openSample(id, keepTyping) {
    const base = state.editorBase;
    const same = Boolean(keepTyping && base && base.id === id);
    state.editing = id;
    renderSamples();
    const current = card(id);
    const fresh = shownValues(current);
    $("expt-editor").classList.remove("d-none");
    $("expt-editor-title").textContent = "Sample " + id;
    let typed = fillKeepingTyped(
      FIELDS, fresh, same ? base.values : null,
      !state.writable || !(current && current.managed)
    );
    $("expt-save").disabled = !state.writable || !(current && current.managed);
    const measured = renderMeasurements(
      id, base, same, state.writable && Boolean(current && current.managed)
    );
    typed = typed || measured.kept;
    const runMoved = same && sampleRuns(id).some(function (row) {
      const was = base.runs && base.runs[row.key];
      return was && was.rev !== row.rev;
    });
    const moved = same && typed && current &&
      ((current.rev || 0) !== base.rev || runMoved);
    $("expt-form-status").textContent = moved
      ? savedElsewhere("This sample")
      : current && !current.managed
        ? "Adopt this sample to edit it here."
        : same
          ? $("expt-form-status").textContent
          : "";
    const runs = {};
    sampleRuns(id).forEach(function (row) {
      runs[row.key] = Object.assign({ rev: row.rev }, measured.fresh[row.key]);
    });
    state.editorBase = {
      id: id, rev: current ? current.rev || 0 : 0, values: fresh, runs: runs,
    };
    $("expt-adopt-plan").classList.add("d-none");
    // models.js shows this sample's specs; it listens rather than being called,
    // so the two files share nothing but this event.
    document.dispatchEvent(new CustomEvent("nrw:sample-opened", { detail: { id: id } }));
    await refreshPreview(id);
  }

  async function refreshPreview(id) {
    const badge = $("expt-md-state");
    try {
      const preview = await api(
        "GET", "/api/experiment/samples/" + encodeURIComponent(id) + "/preview"
      );
      if (state.editing !== id) return;
      const [label, cls] = MD_STATE[preview.state] || [preview.state || "", "text-bg-light"];
      badge.className = "badge ms-auto " + cls;
      badge.textContent = "sample.md " + label;
      /* Rendered by the server with raw HTML disabled, as on the sample page. */
      $("expt-preview").innerHTML = preview.html || "";
      const adoptable = state.writable &&
        (preview.state === "drifted" || preview.state === "untracked" || !preview.managed);
      const button = $("expt-adopt");
      button.classList.toggle("d-none", !adoptable);
      button.textContent = preview.managed ? "Pull hand edits" : "Adopt";
    } catch (error) {
      badge.className = "badge ms-auto text-bg-light";
      badge.textContent = "";
      $("expt-preview").textContent = error.message;
    }
  }

  async function saveSample(event) {
    event.preventDefault();
    const id = state.editing;
    const current = card(id);
    if (!id || !current) return;
    const fields = changedFields(FIELDS, current);
    // Each run's type, condition and notes, sent only where they changed.
    const runChanges = [];
    Object.entries(typedRuns()).forEach(function ([key, values]) {
      const row = state.rows.get(key);
      if (!row) return;
      const changed = {};
      if (values.measurement && values.measurement !== (row.measurement || "")) {
        changed.measurement = values.measurement;
      }
      if (values.condition !== (row.condition || "")) changed.condition = values.condition;
      if (values.note !== (row.note || "")) changed.note = values.note;
      if (values.include !== (row.include !== false)) changed.include = values.include;
      if (Object.keys(changed).length) {
        runChanges.push({ run: row.run, base_rev: row.rev, fields: changed });
      }
    });
    if (!Object.keys(fields).length && !runChanges.length) {
      $("expt-form-status").textContent = "Nothing changed.";
      return;
    }
    try {
      if (runChanges.length) {
        const saved = await api("PUT", "/api/experiment/runs", { changes: runChanges });
        saved.runs.forEach(function (row) {
          state.rows.set(row.key, row);
        });
        state.samples = saved.samples;
        renderRuns();
        renderBulk();  // a type added here is a choice there now
      }
      if (Object.keys(fields).length) {
        const result = await api(
          "PUT", "/api/experiment/samples/" + encodeURIComponent(id),
          { base_rev: current.rev || 0, fields: fields }
        );
        state.samples = result.samples;
      }
      // Refilled from what the server now holds, which is what was typed.
      await openSample(id);
      $("expt-form-status").textContent = "Saved. Apply writes it into sample.md.";
    } catch (error) {
      if (error.status === 400) $("expt-form-status").textContent = error.message;
      else await failed(error, "Saving");
    }
  }

  async function showAdoptPlan() {
    const id = state.editing;
    const box = $("expt-adopt-plan");
    box.classList.remove("d-none");
    box.replaceChildren(el("p", { className: "text-secondary", text: "Reading sample.md…" }));
    let plan;
    try {
      plan = await api("GET", "/api/experiment/samples/" + encodeURIComponent(id) + "/adopt");
    } catch (error) {
      box.replaceChildren(el("p", { text: error.message }));
      return;
    }
    const items = [];
    plan.runs.forEach(function (run) {
      const described = Object.entries(run).filter(function ([k]) {
        return k !== "run";
      }).map(function ([k, v]) {
        return k + "=" + v;
      }).join(", ");
      items.push(el("li", { text: "run " + run.run + ": " + described }));
    });
    Object.keys(plan.context).forEach(function (name) {
      items.push(el("li", { text: "context: " + name.replace(/_/g, " ") }));
    });
    const leftovers = plan.leftovers.map(function (text) {
      return el("li", { className: "text-warning-emphasis", text: "not kept: " + text });
    });
    const problems = plan.problems.map(function (p) {
      return el("li", { className: "text-danger", text: p.message });
    });
    const keep = el("button", {
      type: "button", className: "btn btn-sm btn-primary", text: "Take into the catalog",
      disabled: plan.problems.length > 0,
    });
    const rewrite = el("button", {
      type: "button", className: "btn btn-sm btn-outline-primary",
      text: "…and rewrite sample.md", disabled: !plan.rewrite_ready,
      title: plan.rewrite_ready ? "sample.md is backed up first"
        : "sample.md has text the catalog cannot hold",
    });
    async function run(rewriteFile) {
      try {
        await api("POST", "/api/experiment/samples/" + encodeURIComponent(id) + "/adopt",
          { rewrite: rewriteFile, plan_id: plan.plan_id });
        message("Taken into the catalog" + (rewriteFile ? "; sample.md rewritten." : "."),
          "success");
        await reload();
      } catch (error) {
        await failed(error, "Adopting");
      }
    }
    keep.addEventListener("click", function () {
      run(false);
    });
    rewrite.addEventListener("click", function () {
      run(true);
    });
    /* replaceChildren turns a null argument into the text "null", so the
     * optional paragraph is filtered out rather than passed as null. */
    box.replaceChildren(...[
      el("ul", { className: "mb-2" }, items.concat(leftovers, problems)),
      plan.undocumented.length ? el("p", {
        className: "text-secondary",
        text: "In the data but not in the table (not assigned): " +
          plan.undocumented.join(", "),
      }) : null,
      plan.kept.length ? el("p", {
        className: "text-secondary",
        text: "In the catalog but not in this file's table (kept as the catalog has them): " +
          plan.kept.join(", "),
      }) : null,
      el("div", { className: "d-flex gap-2" }, [keep, rewrite]),
    ].filter(Boolean));
  }

  /* ---------------------------------------------------------------- */
  /* Apply                                                              */
  /* ---------------------------------------------------------------- */

  async function review() {
    const confirm = Array.from(state.confirmed).join(",");
    try {
      state.plan = await api(
        "GET", "/api/experiment/apply" + (confirm ? "?confirm=" + confirm : "")
      );
      renderPlan();
    } catch (error) {
      await failed(error, "Reviewing");
    }
  }

  function renderPlan(report) {
    const plan = state.plan;
    const box = $("expt-plan");
    box.replaceChildren();
    plan.problems.forEach(function (p) {
      box.append(el("p", { className: "text-danger", text: p.message }));
    });
    if (!plan.samples.length) {
      box.append(el("p", {
        className: "text-secondary mb-0", text: "The catalog assigns no runs to any sample yet.",
      }));
    }
    plan.samples.forEach(function (sample) {
      box.append(el("h3", { className: "h6 mt-2 mb-1" }, [
        el("span", { className: "mono", text: sample.sample }),
        sample.creates ? el("span", { className: "badge text-bg-info ms-2", text: "new" }) : null,
      ]));
      sample.problems.forEach(function (p) {
        box.append(el("p", { className: "text-danger mb-1", text: p.message }));
      });
      const list = el("ul", { className: "list-unstyled mb-1 expt-plan-list" });
      let unchanged = 0;
      const unconfirmed = new Set();
      sample.files.forEach(function (file) {
        if (file.action === "unchanged") {
          unchanged += 1;
          return;
        }
        const row = state.rows.get(file.run + ":steady");
        if (file.action === "deferred" && row && row.state === "unconfirmed") {
          unconfirmed.add(file.run);
        }
        const attention = !(file.action in SYMBOL);
        list.append(el("li", { className: attention ? "text-warning-emphasis" : "" }, [
          el("span", { className: "mono", text: (SYMBOL[file.action] || "!") + " " }),
          el("span", { text: file.action + " " }),
          el("span", { className: "mono", text: file.name }),
          file.detail ? el("span", { className: "text-secondary", text: " — " + file.detail }) : null,
        ]));
      });
      box.append(list);
      if (unchanged) {
        box.append(el("p", {
          className: "text-secondary mb-1",
          text: unchanged + " file(s) already copied and unchanged.",
        }));
      }
      unconfirmed.forEach(function (run) {
        const id = "confirm-" + run;
        const check = el("input", {
          type: "checkbox", className: "form-check-input", id: id,
          checked: state.confirmed.has(run), disabled: !state.writable,
        });
        check.addEventListener("change", function () {
          if (check.checked) state.confirmed.add(run);
          else state.confirmed.delete(run);
          review();
        });
        box.append(el("div", { className: "form-check" }, [
          check,
          el("label", {
            className: "form-check-label", htmlFor: id,
            text: "Use run " + run + " as it is: it settled, but nothing shows the measurement ended.",
          }),
        ]));
      });
      if (sample.sample_md) {
        box.append(el("p", {
          className: ["drifted", "untracked"].indexOf(sample.sample_md) !== -1
            ? "text-warning-emphasis mb-1" : "mb-1",
          text: "sample.md " + sample.sample_md_detail,
        }));
      }
    });
    if (report) {
      box.append(el("p", {
        className: "mt-2 mb-1", text: "Done: " + report.done.length + " file action(s).",
      }));
      report.failed.forEach(function (f) {
        box.append(el("p", { className: "text-danger mb-1", text: f.name + ": " + f.detail }));
      });
    }
    if (state.writable && plan.writes) {
      const button = el("button", {
        type: "button", className: "btn btn-sm btn-primary mt-2", text: "Apply",
      });
      button.addEventListener("click", applyPlan);
      box.append(button);
    } else if (!plan.writes && plan.samples.length) {
      box.append(el("p", { className: "text-secondary mt-2 mb-0", text: "Everything is in step." }));
    }
  }

  async function applyPlan() {
    try {
      const report = await api("POST", "/api/experiment/apply", {
        plan_id: state.plan.plan_id,
        confirmed: Array.from(state.confirmed),
      });
      state.confirmed.clear();
      await reload();
      state.plan = await api("GET", "/api/experiment/apply");
      renderPlan(report);
      message("Applied.", "success");
    } catch (error) {
      if (error.status === 409) {
        message(error.message, "warning");
        await review();
      } else {
        message("Applying: " + error.message, "danger");
      }
    }
  }

  /* ---------------------------------------------------------------- */
  /* Wiring                                                             */
  /* ---------------------------------------------------------------- */

  function renderAll() {
    renderHeader();
    renderProblems(state.payload.problems);
    renderRuns();
    renderBulk();
    renderContext();
    renderSamples();
  }

  document.querySelectorAll("#expt-filters button").forEach(function (button) {
    button.addEventListener("click", function () {
      state.filter = button.dataset.filter;
      document.querySelectorAll("#expt-filters button").forEach(function (other) {
        other.classList.toggle("active", other === button);
      });
      renderRuns();
    });
  });
  $("expt-search").addEventListener("input", function (event) {
    state.search = event.target.value;
    renderRuns();
  });
  $("expt-select-all").addEventListener("change", function (event) {
    visibleRows().forEach(function (row) {
      if (event.target.checked) state.selected.add(row.key);
      else state.selected.delete(row.key);
    });
    renderRuns();
    renderBulk();
    scheduleQuickLook();
  });
  $("expt-sample").addEventListener("change", renderBulk);
  $("expt-assign").addEventListener("click", assign);
  $("expt-unassign").addEventListener("click", function () {
    editRuns({ sample_id: null }, "Unassigned");
  });
  $("expt-exclude").addEventListener("click", function () {
    editRuns({ include: false }, "Excluded");
  });
  $("expt-include").addEventListener("click", function () {
    editRuns({ include: true }, "Included");
  });
  $("expt-form").addEventListener("submit", saveSample);
  $("expt-context-form").addEventListener("submit", saveContext);
  $("expt-adopt").addEventListener("click", showAdoptPlan);
  $("expt-review").addEventListener("click", review);

  load(initial);
  firstLook(initial.scan);
  setInterval(poll, POLL_MS);
})();
