/* Publishing a final fit to ISAAC, from its page: export, validate, push.
 *
 * Shown for the final fit of its sample, the only one published -- and, for a
 * fit pushed while it was final, its pushes stay shown after that. The server
 * says whether this one is, which tools are installed, where the portal and
 * its key are set (never the key), the records the export wrote and each push
 * so far. Each step runs as a job, one at a time with the page's fits, and the
 * panel follows it. Push asks first, naming what leaves and where.
 *
 * On a view-only server the panel is shown, and nothing is offered. Every
 * value is set as text.
 */
"use strict";

(function () {
  const N = window.NRWPage;
  const $ = N.$;
  const el = N.el;
  const api = N.client();
  const panel = $("isaac");
  const said = window.NRW_CURATION;
  if (!panel || !said) return;
  const path = "/api/experiment/fits/" + encodeURIComponent(said.fit_id) + "/isaac";
  const POLL_MS = 1000;
  let shown = null;
  let running = false;

  function line(text, kind) {
    return el("li", { className: kind || "", text: text });
  }

  function setup() {
    const items = [];
    Object.entries(shown.tools).forEach(function ([name, installed]) {
      if (!installed) items.push(line(name + " is not installed: " + shown.install, "text-danger"));
    });
    items.push(shown.portal.host
      ? line("Portal: " + shown.portal.host + " (ISAAC_URL, in " + shown.portal.from + ")")
      : line("ISAAC_URL is not set: add it to the project's .env or ~/.nrw.",
        "text-warning-emphasis"));
    items.push(shown.key.set
      ? line("Key: ISAAC_KEY is set, in " + shown.key.from + ".")
      : line("ISAAC_KEY is not set: add it to the project's .env or ~/.nrw. It is " +
        "never shown here.", "text-warning-emphasis"));
    items.push(shown.records.length
      ? line(shown.records.length + " record(s) exported, " + shown.exported_at + ".")
      : line("Not exported yet."));
    $("isaac-setup").replaceChildren(...items);
  }

  function published() {
    const pushes = shown.published;
    $("isaac-published").textContent = pushes.length
      ? pushes.map(function (push) {
        const made = (push.records || []).length;
        const said = "Pushed " + push.at + " by " + push.who + " to " + push.portal + ": ";
        // Recorded before it ran: one that never reported back may still have
        // made records, and pushing again would make them twice.
        if (push.complete === undefined) {
          return said + "its outcome was never recorded, so it may have made " +
            "records. Check the portal before pushing again.";
        }
        return said + made + " record(s) confirmed" +
          (push.complete === false
            ? ", and it may have made more: check the portal before pushing again"
            : "") + ".";
      }).join(" ")
      : "";
  }

  function button(text, id, onClick, disabled, title) {
    const b = el("button", {
      type: "button",
      className: "btn btn-sm btn-outline-primary",
      id: id,
      text: text,
      disabled: disabled,
      title: title || "",
    });
    b.addEventListener("click", onClick);
    return b;
  }

  function render() {
    const pushed = shown.published.length > 0;
    panel.classList.toggle("d-none", !shown.final && !pushed);
    if (!shown.final && !pushed) return;
    published();
    if (!shown.final) {
      // Pushed while it was final: that stays said, on its own page, and
      // nothing more is offered.
      $("isaac-setup").replaceChildren(line("No longer the final fit of " +
        (said.sample || "its sample") + ", so it is not published again."));
      $("isaac-buttons").replaceChildren();
      return;
    }
    setup();
    if (!shown.writable) return;  // shown, not offered
    const tools = Object.values(shown.tools).every(Boolean);
    const sendable = tools && shown.records.length && shown.portal.host && shown.key.set;
    const why = !tools ? "install the tools first"
      : !shown.records.length ? "export first"
        : !shown.key.set || !shown.portal.host ? "set ISAAC_URL and ISAAC_KEY" : "";
    $("isaac-buttons").replaceChildren(
      button("Export", "isaac-export", function () { start("export"); }, running || !tools),
      button("Validate with the server", "isaac-validate", validate,
        running || !sendable, why),
      button("Push…", "isaac-push", push, running || !sendable, why)
    );
  }

  async function load() {
    try {
      shown = await api("GET", path);
      render();
    } catch (error) {
      $("isaac-status").textContent = error.message;
    }
  }

  /* Validating sends the records, and the key, to the portal: asked first. */
  function validate() {
    const sure = window.confirm(
      "Send " + shown.records.length + " record(s) of " + said.fit_id + " to " +
      shown.portal.host + " to validate?\n\nThe portal checks them and keeps " +
      "nothing. Your key goes with them."
    );
    if (sure) start("validate");
  }

  function push() {
    const again = shown.published.length
      ? "\n\nIt was pushed before, on " + shown.published[shown.published.length - 1].at +
        ": this adds new records, and replaces none."
      : "";
    const sure = window.confirm(
      "Publish " + shown.records.length + " record(s) of " + said.fit_id + " to " +
      shown.portal.host + "?\n\nThis shares the data, the fitted model and the " +
      "notes outside this project -- sample.md, this fit's NOTES.md, the reason " +
      "it was finalized, and any report that cites it -- and the portal keeps " +
      "them." + again
    );
    if (sure) start("push", { confirm: said.fit_id });
  }

  async function start(step, body) {
    const status = $("isaac-status");
    status.textContent = "…";
    try {
      const answer = await api("POST", path + "/" + step, body || {});
      running = true;
      render();
      status.textContent = answer.job.label + ": running.";
      follow(answer.job.id);
    } catch (error) {
      status.textContent = error.message;
    }
  }

  /* What the job prints, until it ends; then the panel as it now stands. */
  async function follow(jobId) {
    const log = $("isaac-log");
    log.textContent = "";
    log.classList.remove("d-none");
    let offset = 0;
    for (;;) {
      let payload;
      try {
        payload = await api("GET", "/api/experiment/jobs/current?offset=" + offset);
      } catch (error) {
        $("isaac-status").textContent = "Following the job: " + error.message;
        break;
      }
      if (!payload.job || payload.job.id !== jobId) break;
      if (payload.log) log.textContent += payload.log;
      offset = payload.offset;
      if (payload.job.status !== "running" && !payload.log) {
        $("isaac-status").textContent = payload.job.label + ": " +
          (payload.job.status === "ok" ? "done." : payload.job.status + "; its output says why.");
        break;
      }
      await new Promise(function (done) { setTimeout(done, payload.log ? 0 : POLL_MS); });
    }
    running = false;
    load();
  }

  document.addEventListener("nrw:curated", load);
  load();
})();
