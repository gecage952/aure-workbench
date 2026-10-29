/* The Experiment page's models and fits.
 *
 * The Models panel shows the specs of the sample open in the editor, writes a
 * new one from its notes and data (as `nrw model new --from-notes` does), and
 * starts a fit of one. The Fit panel follows the job: what it prints, Cancel,
 * and the fit it recorded. One job runs at a time, for the whole project, so
 * the Fit panel shows it whichever sample is open.
 *
 * experiment.js says which sample is open with an "nrw:sample-opened" event;
 * the two files share nothing else. Every value is set as text.
 */
"use strict";

(function () {
  const page = window.NRWPage;
  const $ = page.$;
  const el = page.el;
  const WRITER = Boolean(window.NRW_EXPERIMENT && window.NRW_EXPERIMENT.writable);
  const api = page.client();
  const POLL_MS = 1000;
  // The most of a job's output kept on the page; the whole of it is in
  // .nrw/jobs/<id>.log.
  const LOG_KEEP = 400000;

  let shown = null;  // the sample whose models are shown
  let listed = null;  // what the last listing of it said
  let fitting = null;  // the name of the model the fit form is for
  // The project's nrw.toml [fit], as the last listing gave it.
  let fitDefaults = { method: "dream", settings: {}, problem: null };
  // offset null: not read yet, so the first read is the log's last part.
  const job = { id: null, offset: null, running: false, timer: null, ended: null };

  function modelsPath(id) {
    return "/api/experiment/samples/" + encodeURIComponent(id) + "/models";
  }

  /* Why a new model cannot be written yet, or "" when it can. */
  function blocked(payload) {
    if (!payload.writable) return "This page is view-only.";
    if (!payload.exists) {
      return "Apply first: it creates samples/" + payload.sample +
        "/ and copies in the data a spec is built from.";
    }
    if (!payload.has_data) {
      return "samples/" + payload.sample + "/data/ holds no data yet: apply the " +
        "runs assigned to this sample first.";
    }
    return "";
  }

  /* New model and a quick fit are jobs, and one job runs at a time: while one
   * runs, they wait, and the panel says so rather than refusing a click. */
  function writeButtons() {
    if (!listed) return;
    const why = blocked(listed);
    const waiting = !why && job.running
      ? "A job is running (below), and one runs at a time: New model and a " +
        "quick fit can start once it ends."
      : "";
    $("expt-model-create").disabled = Boolean(why || waiting);
    const quick = $("expt-model-quick");
    quick.disabled = Boolean(why || waiting) || !listed.aure;
    quick.title = why || waiting ||
      (listed.aure ? "" : "AuRE is not installed where nrw serve runs.");
    $("expt-model-wait").textContent = waiting;
    $("expt-model-wait").classList.toggle("d-none", !waiting);
  }

  function render(payload) {
    if (payload.fit) fitDefaults = payload.fit;
    listed = payload;
    $("expt-models").classList.remove("d-none");
    $("expt-models-title").textContent = "Models of " + payload.sample;
    const list = $("expt-models-list");
    list.replaceChildren();
    if (!payload.models.length) {
      list.append(el("p", { className: "text-secondary mb-1", text: "No models yet." }));
    }
    const canFit = Boolean(payload.writable);
    payload.models.forEach(function (model) {
      const fit = el("button", {
        type: "button",
        className: "btn btn-sm btn-outline-primary py-0 ms-auto expt-model-fit",
        text: "Fit…",
        "aria-label": "fit " + model.name,
        disabled: !canFit,
      });
      fit.addEventListener("click", function () {
        openFit(model);
      });
      // A proposal of AuRE's nobody edited can be asked for again -- after the
      // notes changed, say; the new one replaces it.
      let again = null;
      if (model.proposed) {
        again = el("button", {
          type: "button",
          className: "btn btn-sm btn-outline-secondary py-0 expt-model-again",
          text: "Quick fit again",
          "aria-label": "quick fit " + model.name + " again with AuRE",
          disabled: !canFit || !payload.aure,
          title: payload.aure ? "" : "AuRE is not installed where nrw serve runs.",
        });
        again.addEventListener("click", function () {
          // The run it was proposed from, not whichever is chosen in the list.
          quickFit(model.name, again, model.run);
        });
      }
      list.append(el("div", { className: "d-flex gap-2 align-items-baseline mb-1 expt-model" }, [
        el("span", { className: "mono", text: model.name }),
        el("span", { className: "text-secondary", text: model.spec }),
        // nrw's stub, not the sample: written when no language model answered.
        model.placeholder
          ? el("span", {
            className: "badge text-bg-warning expt-model-placeholder",
            text: "placeholder stack",
            title: "air on a film on Si, not this sample: edit the stack before fitting",
          })
          : null,
        model.script
          ? el("span", { className: "badge text-bg-light", text: "script generated" })
          : null,
        el("span", { className: "ms-auto d-flex gap-2" }, [again, fit]),
      ]));
    });
    const why = blocked(payload);
    if (why) list.append(el("p", { className: "text-secondary mb-0 mt-1", text: why }));
    if (payload.sample_md_pending) {
      list.append(el("p", {
        className: "text-warning-emphasis mb-0 mt-1 expt-md-pending",
        text: "sample.md does not have the edits saved in the sample editor yet. A new model " +
          "and a quick fit are written from sample.md, so Apply first.",
      }));
    }
    $("expt-model-name").disabled = Boolean(why);
    // AuRE fits one measurement: which, when there is a choice.
    const runs = $("expt-model-run");
    const chosen = runs.value;
    runs.replaceChildren();
    (payload.runs || []).forEach(function (run) {
      runs.append(el("option", { value: String(run), text: "run " + run }));
    });
    if (chosen && (payload.runs || []).map(String).includes(chosen)) runs.value = chosen;
    runs.classList.toggle("d-none", (payload.runs || []).length < 2);
    writeButtons();
  }

  async function load(id) {
    try {
      const payload = await api("GET", modelsPath(id));
      if (shown === id) render(payload);
    } catch (error) {
      if (shown === id) $("expt-model-status").textContent = error.message;
    }
  }

  document.addEventListener("nrw:sample-opened", function (event) {
    const id = event.detail.id;
    if (id !== shown) {
      // What was said about another sample is not about this one.
      $("expt-model-status").textContent = "";
      $("expt-fit-form").classList.add("d-none");
      fitting = null;
      listed = null;
      shown = id;
    }
    load(id);
  });

  $("expt-model-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    const id = shown;
    const name = $("expt-model-name").value.trim();
    if (!id) return;
    if (!name) {
      $("expt-model-status").textContent = "Give the model a name.";
      return;
    }
    $("expt-model-create").disabled = true;
    try {
      const started = await api("POST", modelsPath(id), { name: name });
      $("expt-model-name").value = "";
      $("expt-model-status").textContent =
        "Writing " + name + ".yaml from the notes and the data; the Fit panel " +
        "follows it.";
      follow(started.job);
    } catch (error) {
      $("expt-model-status").textContent = error.message;
    } finally {
      load(id);  // the buttons as the sample now stands
    }
  });

  /* A quick fit of *name* with AuRE: a new model, or a new proposal for one
   * AuRE proposed before. */
  async function quickFit(name, button, run) {
    const id = shown;
    if (!id) return;
    const body = { name: name };
    const runs = $("expt-model-run");
    if (run) body.run = run;
    else if (!runs.classList.contains("d-none") && runs.value) body.run = Number(runs.value);
    button.disabled = true;
    try {
      const started = await api("POST", modelsPath(id) + "/quick-fit", body);
      $("expt-model-status").textContent =
        "AuRE is proposing a stack for " + name + "; the Fit panel follows it.";
      follow(started.job);
      return true;
    } catch (error) {
      $("expt-model-status").textContent = error.message;
      return false;
    } finally {
      load(id);  // the buttons as the sample now stands
    }
  }

  $("expt-model-quick").addEventListener("click", async function () {
    const name = $("expt-model-name").value.trim();
    if (!name) {
      $("expt-model-status").textContent = "Give the model a name.";
      return;
    }
    if (await quickFit(name, $("expt-model-quick"))) $("expt-model-name").value = "";
  });

  // -- starting a fit --------------------------------------------------------

  function openFit(model) {
    fitting = model.name;
    $("expt-fit-title").textContent = "Fit " + model.name;
    // A fit of the stub stack fits a sample nobody measured: said first, and on
    // the button, but not refused -- trying the machinery on it is legitimate.
    $("expt-fit-placeholder").classList.toggle("d-none", !model.placeholder);
    $("expt-fit-run").textContent = model.placeholder ? "Fit the placeholder anyway" : "Run fit";
    // The project's default fitter, said so beside its name.
    const select = $("expt-fit-method");
    Array.from(select.options).forEach(function (option) {
      if (!option.dataset.label) option.dataset.label = option.textContent;
      option.textContent = option.dataset.label +
        (option.value === fitDefaults.method ? " (default)" : "");
    });
    select.value = fitDefaults.method;
    const problem = $("expt-fit-problem");
    problem.textContent = fitDefaults.problem ? "nrw.toml: " + fitDefaults.problem : "";
    problem.classList.toggle("d-none", !fitDefaults.problem);
    showMethodSettings();
    $("expt-fit-form").classList.remove("d-none");
    select.focus();
  }

  const FORM_SETTINGS = [["expt-fit-samples", "samples"], ["expt-fit-burn", "burn"],
    ["expt-fit-steps", "steps"]];
  // What each box started at, for sending only what was changed.
  let startedAt = {};

  /* The boxes the chosen fitter takes, each filled with what the fit would use
   * -- nrw.toml's value, else bumps' default -- and a line saying which. */
  function showMethodSettings() {
    const method = $("expt-fit-method").value;
    const takes = (fitDefaults.takes || {})[method] || [];
    const configured = (fitDefaults.settings || {})[method] || {};
    const bumps = (fitDefaults.bumps || {})[method] || {};
    const said = [];
    startedAt = {};
    FORM_SETTINGS.forEach(function ([id, key]) {
      const box = $(id);
      const taken = takes.includes(key);
      box.closest(".expt-fit-setting").classList.toggle("d-none", !taken);
      let value = "";
      if (taken && key in configured) {
        value = String(configured[key]);
        said.push(key + " " + value + " from nrw.toml");
      } else if (taken && bumps[key]) {
        value = String(bumps[key]);
        said.push(key + " " + value + ", bumps' default");
      }
      box.value = value;
      // DREAM's steps default to what its samples need: no one number.
      box.placeholder = taken && !value ? "from samples" : "";
      startedAt[key] = value;
    });
    $("expt-fit-defaults").textContent = said.length ? said.join("; ") + "." : "";
  }

  $("expt-fit-method").addEventListener("change", showMethodSettings);
  $("expt-fit-close").addEventListener("click", function () {
    $("expt-fit-form").classList.add("d-none");
    fitting = null;
  });

  $("expt-fit-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!shown || !fitting) return;
    const method = $("expt-fit-method").value;
    const body = { method: method };
    // What was not changed is left to `nrw fit run`, which takes it from the
    // same place: so a fit from here records what one from the terminal would.
    FORM_SETTINGS.forEach(function ([id, key]) {
      const box = $(id);
      const text = box.value.trim();
      if (box.closest(".expt-fit-setting").classList.contains("d-none")) return;
      if (text === "" || text === startedAt[key]) return;
      body[key] = Number(text);
    });
    const note = $("expt-fit-note").value.trim();
    if (note) body.note = note;
    $("expt-fit-run").disabled = true;
    try {
      const started = await api(
        "POST", modelsPath(shown) + "/" + encodeURIComponent(fitting) + "/fit", body
      );
      $("expt-fit-form").classList.add("d-none");
      $("expt-fit-note").value = "";
      follow(started.job);
    } catch (error) {
      $("expt-model-status").textContent = error.message;
    } finally {
      $("expt-fit-run").disabled = false;
    }
  });

  // -- following the job -----------------------------------------------------

  const ENDINGS = {
    ok: "Finished.",
    failed: "Failed: the last lines of its output say why.",
    cancelled: "Cancelled. What the fit had written is left as it was; `nrw check` " +
      "lists it as an interrupted run.",
    detached: "nrw serve was stopped while this ran, so how it ended is not " +
      "known here. A fit that finished is in Fits.",
  };

  function follow(current) {
    if (!current) return;
    if (current.id !== job.id) {
      job.id = current.id;
      job.offset = null;
      job.ended = null;
      $("expt-job-log").textContent = "";
      $("expt-job-result").replaceChildren();
    }
    $("expt-job-panel").classList.remove("d-none");
    showJob(current);
    schedule(0);
  }

  function showJob(current) {
    job.running = current.status === "running";
    writeButtons();
    $("expt-job-title").textContent =
      current.label.charAt(0).toUpperCase() + current.label.slice(1) +
      " (" + current.sample + ")";
    $("expt-job-state").textContent = job.running
      ? "step " + current.step + " of " + current.steps.length
      : current.status;
    $("expt-job-cancel").classList.toggle("d-none", !job.running || !WRITER);
    $("expt-job-close").classList.toggle("d-none", job.running);
    if (job.running || job.ended === current.status) return;
    job.ended = current.status;
    const result = $("expt-job-result");
    const last = current.steps[current.steps.length - 1] || [];
    const fit = last[0] === "fit" && last[1] === "run";
    // Only a fit leaves an interrupted run for `nrw check` to list.
    result.replaceChildren(current.status === "cancelled" && !fit
      ? "Cancelled. What it had written so far is left as it was."
      : ENDINGS[current.status] || current.status);
    if (current.fit_id) result.append(" Recorded as ", fitLink(current.fit_id), ".");
    result.append(" ", el("a", { href: "/fits", text: "All fits" }), ".");
    if (shown === current.sample) load(shown);  // its script is generated now
  }

  function fitLink(fitId) {
    return el("a", { href: "/f/" + encodeURIComponent(fitId), className: "mono", text: fitId });
  }

  function append(text) {
    if (!text) return;
    const log = $("expt-job-log");
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    let all = log.textContent + text;
    if (all.length > LOG_KEEP) all = all.slice(all.length - LOG_KEEP);
    log.textContent = all;
    if (atEnd) log.scrollTop = log.scrollHeight;
  }

  function schedule(delay) {
    clearTimeout(job.timer);
    job.timer = setTimeout(poll, delay);
  }

  async function poll() {
    try {
      const payload = await api(
        "GET",
        "/api/experiment/jobs/current" + (job.offset === null ? "" : "?offset=" + job.offset)
      );
      if (!payload.job || payload.job.id !== job.id) {
        follow(payload.job);
        return;
      }
      append(payload.log);
      job.offset = payload.offset;
      showJob(payload.job);
      // On while it runs, and until an ended job's output has all been read.
      if (job.running || payload.log) schedule(payload.log ? 0 : POLL_MS);
    } catch (error) {
      $("expt-job-result").textContent = "Following the job: " + error.message;
      schedule(POLL_MS * 5);
    }
  }

  $("expt-job-cancel").addEventListener("click", async function () {
    if (!job.id) return;
    $("expt-job-cancel").disabled = true;
    try {
      await api("POST", "/api/experiment/jobs/" + encodeURIComponent(job.id) + "/cancel", {});
      schedule(0);
    } catch (error) {
      $("expt-job-result").textContent = error.message;
    } finally {
      $("expt-job-cancel").disabled = false;
    }
  });

  $("expt-job-close").addEventListener("click", function () {
    $("expt-job-panel").classList.add("d-none");
  });

  // A job started before this page was opened -- or before nrw serve was. One
  // that has ended is not shown again: the fits it made are in Fits.
  api("GET", "/api/experiment/jobs/current").then(function (payload) {
    const current = payload.job;
    if (current && (current.status === "running" || current.status === "detached")) {
      follow(current);
    }
  }).catch(function () {});
})();
