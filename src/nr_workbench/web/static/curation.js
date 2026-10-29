/* Curating fits: star, finalize, discard, restore, delete files.
 *
 * On a fit's page it draws the action bar from what the server says about the
 * fit, and draws it again from each answer -- the rules (a final fit is not
 * discarded, files go only after a discard and never while another fit reads
 * them) are the server's, and this page only offers what they allow. On the Fits
 * list it toggles stars and shows or hides the discarded fits.
 *
 * On a view-only server everything here is shown, and nothing is offered.
 * Every value is set as text.
 */
"use strict";

(function () {
  const N = window.NRWPage;
  const $ = N.$;
  const el = N.el;
  const WRITER = Boolean(window.NRW_WRITER);
  const api = N.client();

  function fitPath(fitId, action) {
    return "/api/experiment/fits/" + encodeURIComponent(fitId) + "/" + action;
  }

  function starLabel(starred) {
    return starred ? "★ Starred" : "☆ Star";
  }

  // -- a fit's page ----------------------------------------------------------

  const bar = $("curate");
  if (bar) {
    let said = window.NRW_CURATION;
    let asking = null;  // the question the form is showing: "finalize" or "discard"

    function status(text, kind) {
      const line = $("curate-status");
      line.className = "small mb-0 " + (kind === "error" ? "text-danger" : "text-secondary");
      line.textContent = text || "";
    }

    function badges() {
      const box = $("fit-badges");
      const state = said.curation;
      box.replaceChildren(...state.labels.map(function (label) {
        return el("span", { className: "badge text-bg-success", text: label });
      }));
      if (state.starred) box.append(el("span", { className: "badge text-bg-warning", text: "★ starred" }));
      if (state.discarded) {
        box.append(el("span", {
          className: "badge text-bg-secondary",
          text: state.deleted ? "deleted" : "discarded",
          title: state.discarded.reason || "",
        }));
      }
    }

    function button(text, id, onClick, options) {
      const b = el("button", {
        type: "button",
        className: "btn btn-sm " + ((options || {}).style || "btn-outline-secondary"),
        id: id,
        text: text,
        disabled: Boolean((options || {}).disabled),
        title: (options || {}).title || "",
      });
      b.addEventListener("click", onClick);
      return b;
    }

    function render() {
      badges();
      const state = said.curation;
      const note = $("curate-discarded");
      note.classList.toggle("d-none", !state.discarded);
      const reason = (state.discarded || {}).reason || "no reason given";
      note.textContent = !state.discarded
        ? ""
        : state.deleted
          ? "Its files were deleted" +
            (state.deleted.who ? " by " + state.deleted.who : "") +
            (state.deleted.at ? " on " + state.deleted.at : "") +
            ". It had been set aside because: " + reason + "."
          : "Set aside" +
            (state.discarded.who ? " by " + state.discarded.who : "") +
            (state.discarded.at ? " on " + state.discarded.at : "") +
            ": " + reason + ".";
      if (!WRITER) return;  // shown, not offered
      const final = state.labels.includes("final");
      const buttons = [];
      if (!state.discarded) {
        buttons.push(button(starLabel(state.starred), "curate-star", star, {
          style: state.starred ? "btn-warning" : "btn-outline-warning",
        }));
        buttons.push(button(final ? "Final" : "Finalize…", "curate-finalize",
          function () { ask("finalize"); },
          { style: "btn-outline-success", disabled: final,
            title: final ? "This is the final fit of " + (said.sample || "its sample") + "." : "" }));
        buttons.push(button("Discard…", "curate-discard", function () { ask("discard"); }, {
          disabled: final,
          title: final ? "The final fit is not set aside: finalize another first." : "",
        }));
      } else if (!state.deleted) {
        buttons.push(button("Restore", "curate-restore", restore, { style: "btn-outline-primary" }));
        buttons.push(button("Delete files…", "curate-delete", remove, {
          style: "btn-outline-danger",
          disabled: Boolean(said.delete_refusal),
          title: said.delete_refusal || "",
        }));
      }
      $("curate-buttons").replaceChildren(...buttons);
      const refusal = $("curate-refusal");
      refusal.textContent = state.discarded && !state.deleted && said.delete_refusal
        ? said.delete_refusal : "";
      refusal.classList.toggle("d-none", !refusal.textContent);
    }

    async function act(action, body, done) {
      status("…");
      try {
        said = await api("POST", fitPath(said.fit_id, action), body);
        render();
        status(done(said));
        // The ISAAC panel follows: a fit finalized now may be published.
        document.dispatchEvent(new CustomEvent("nrw:curated"));
        return true;
      } catch (error) {
        status(error.message, "error");
        return error;
      }
    }

    function star() {
      const starred = !said.curation.starred;
      act("star", { starred: starred }, function () {
        return starred ? "Starred." : "Star taken away.";
      });
    }

    function restore() {
      act("restore", {}, function () { return "Restored: it is listed again."; });
    }

    async function remove() {
      const sure = window.confirm(
        "Delete the files of " + said.fit_id + "?\n\nIts result directory goes, " +
        "its NOTES.md -- what was written about it -- included. The record that " +
        "it ran stays in the index. This cannot be undone."
      );
      if (!sure) return;
      await act("delete", { confirm: said.fit_id }, function (answer) {
        return "Deleted " + answer.removed + "/. The record that it ran stays.";
      });
    }

    /* The reason form, for finalizing or discarding: both are kept with a why. */
    function ask(what) {
      asking = what;
      const form = $("curate-form");
      form.classList.remove("d-none");
      $("curate-force").classList.add("d-none");
      const replaces = said.final && said.final !== said.fit_id
        ? " It replaces " + said.final + " as the final fit of " + said.sample +
          "; that stays in the history."
        : "";
      $("curate-question").textContent = what === "finalize"
        ? "Why is this the answer for " + (said.sample || "its sample") + "?" + replaces
        : "Why is it set aside? Its files are kept, and it can be restored.";
      $("curate-go").textContent = what === "finalize" ? "Finalize" : "Discard";
      $("curate-reason").value = "";
      $("curate-reason").focus();
    }

    async function send(force) {
      const reason = $("curate-reason").value.trim();
      if (!reason) {
        status("Give a reason: it is kept with the fit.", "error");
        return;
      }
      const body = { reason: reason };
      if (asking === "finalize") body.force = Boolean(force);
      const answered = await act(asking, body, function (answer) {
        return asking === "finalize"
          ? "Finalized." + (answer.supersedes ? " " + answer.supersedes + " is final no longer." : "")
          : "Discarded: it is no longer listed. Its files are kept.";
      });
      if (answered === true) {
        $("curate-form").classList.add("d-none");
      } else if (((answered || {}).payload || {}).needs === "force") {
        // Its inputs changed since it ran: said, and finalized only if asked again.
        $("curate-force").classList.remove("d-none");
      }
    }

    $("curate-go").addEventListener("click", function () { send(false); });
    $("curate-force").addEventListener("click", function () { send(true); });
    $("curate-cancel").addEventListener("click", function () {
      $("curate-form").classList.add("d-none");
      status("");
    });
    render();
  }

  // -- the Fits list -----------------------------------------------------------

  const table = $("fits-table");
  if (table) {
    const toggle = $("fits-show-discarded");
    if (toggle) {
      toggle.addEventListener("change", function () {
        table.querySelectorAll("tbody[data-discarded]").forEach(function (group) {
          group.classList.toggle("d-none", !toggle.checked);
        });
      });
    }
    if (WRITER) {
      table.querySelectorAll("button.fit-star").forEach(function (b) {
        b.addEventListener("click", async function () {
          const starred = b.getAttribute("aria-pressed") !== "true";
          b.disabled = true;
          try {
            const answer = await api("POST", fitPath(b.dataset.fitId, "star"), { starred: starred });
            const now = answer.curation.starred;
            b.setAttribute("aria-pressed", now ? "true" : "false");
            b.textContent = now ? "★" : "☆";
            b.title = now ? "starred: click to take the star away" : "star it";
          } catch (error) {
            b.title = error.message;
          } finally {
            b.disabled = false;
          }
        });
      });
    }
  }
})();
