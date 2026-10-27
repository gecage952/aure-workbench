/* The Settings page's Language model: which one New model and AuRE use.
 *
 * Saved in the project's .env, not nrw.toml: settings.js and this file share
 * nothing but the page. The server says what applies and where each part comes
 * from; this page shows that, sends back the choice with the revision of .env
 * it was shown, and never sees a key. Every control starts disabled, and only
 * the server's answer enables it. Every value is set as text.
 */
"use strict";

(function () {
  const N = window.NRWPage;
  const $ = N.$;
  const el = N.el;
  const api = N.client();
  const PATH = "/api/experiment/settings/llm";
  const CONTROLS = ["llm-claude", "llm-outside", "llm-model", "llm-save", "llm-check"];
  let shown = null;

  function writable() {
    return Boolean(shown && shown.writable);
  }

  function isClaude(provider) {
    return Boolean(shown && provider === shown.claude_code.id);
  }

  /* "Claude, through the Claude Code CLI, the CLI's default model (from .env)". */
  function describe(part) {
    if (!part.provider) {
      return part.key
        ? "no provider is named; a key is set, so AuRE guesses one from it"
        : "nothing is set up";
    }
    const claude = isClaude(part.provider);
    let text = claude ? "Claude, through the Claude Code CLI" : part.provider;
    if (part.model) text += ", model " + part.model;
    else if (claude) text += ", the CLI's default model";
    text += " (from " + part.provider_from;
    if (part.model_from && part.model_from !== part.provider_from) {
      text += "; the model from " + part.model_from;
    }
    return text + ")";
  }

  /* Why Claude could not answer a job from here, or "" when it could. Said,
   * not enforced: Claude Code may be installed after the choice is saved. */
  function claudeProblem() {
    if (!shown.aure) return "AuRE is not installed where nrw serve runs.";
    if (!shown.claude_code.supported) {
      return "The installed AuRE predates this provider: upgrade nr-workbench where " +
        "nrw serve runs (docs/install.md, Upgrading), which brings the AuRE it needs.";
    }
    if (!shown.claude_code.cli) {
      return "claude is not on nrw serve's PATH. Install Claude Code, or set " +
        "AURE_CLAUDE_BIN to the binary.";
    }
    return "";
  }

  function toggle(id, text) {
    $(id).textContent = text;
    $(id).classList.toggle("d-none", !text);
  }

  function render() {
    $("llm-now").textContent =
      "New model and quick fits use: " + describe(shown.effective) + ".";
    toggle("llm-environment", shown.environment.length
      ? "nrw serve was started with " + shown.environment.join(" and ") +
        " set in its environment, which wins over this project's .env: a " +
        "choice saved here reaches no job until nrw serve is started without it."
      : "");
    toggle("llm-problem", shown.problem ? shown.problem + " It cannot be changed from here." : "");
    toggle("llm-claude-problem", claudeProblem());
    toggle("llm-claude-binary", shown.claude_code.binary_from
      ? "Which claude runs is set by AURE_CLAUDE_BIN, in " + shown.claude_code.binary_from + "."
      : "");

    $("llm-outside-detail").textContent = describe(shown.outside) + ".";
    const other = shown.choice === "other";
    $("llm-other-row").classList.toggle("d-none", !other);
    $("llm-other-detail").textContent = other
      ? "LLM_PROVIDER=" + shown.project.LLM_PROVIDER + ", written by hand. " +
        "Choosing Claude replaces it; choosing As set outside removes it."
      : "";

    $("llm-claude").value = shown.claude_code.id;
    $("llm-claude").checked = isClaude(shown.choice);
    $("llm-other").checked = other;
    $("llm-outside").checked = shown.choice === "outside";
    $("llm-model").maxLength = shown.model_max;
    $("llm-model").value = isClaude(shown.choice) ? shown.project.LLM_MODEL || "" : "";

    const off = !writable();
    CONTROLS.forEach(function (id) {
      $(id).disabled = off;
    });
    // What .env cannot be changed to; Check still asks what a job would use.
    if (shown.problem) {
      ["llm-claude", "llm-outside", "llm-model", "llm-save"].forEach(function (id) {
        $(id).disabled = true;
      });
    }
    if (off) {
      $("llm-status").textContent = shown.read_only_reason || "View only.";
    }
  }

  async function load() {
    try {
      shown = await api("GET", PATH);
      render();
      return true;
    } catch (error) {
      // Nothing is enabled: a choice made without the section's answer would
      // be sent against a revision nobody read.
      $("llm-now").textContent = "What is set could not be read: " + error.message;
      return false;
    }
  }

  $("llm-model").addEventListener("input", function () {
    $("llm-claude").checked = true;
  });

  $("llm-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!shown) return;
    const choice = document.querySelector('input[name="llm-choice"]:checked');
    const status = $("llm-status");
    if (!choice || choice.value === "other") {
      status.textContent = "Nothing has changed.";
      return;
    }
    const body = { revision: shown.revision, provider: null };
    if (choice === $("llm-claude")) {
      body.provider = shown.claude_code.id;
      body.model = $("llm-model").value.trim();
    }
    $("llm-save").disabled = true;
    status.textContent = "Saving…";
    try {
      const saved = await api("PUT", PATH, body);
      shown = saved.llm;
      render();
      status.textContent = saved.changes.length
        ? "Saved in .env: " + saved.changes.join("; ") + "."
        : "Nothing needed changing.";
    } catch (error) {
      if ((error.payload || {}).kind === "EnvFileConflict" && await load()) {
        // Shown as .env is now, and only then said.
        status.textContent = ".env was changed meanwhile; the section was " +
          "reloaded. Make the change again.";
      } else {
        status.textContent = error.message;
        $("llm-save").disabled = !writable() || Boolean(shown.problem);
      }
    }
  });

  $("llm-check").addEventListener("click", async function () {
    const status = $("llm-status");
    const result = $("llm-result");
    result.replaceChildren();
    status.textContent = "Asking the model saved here…";
    $("llm-check").disabled = true;
    try {
      const probe = (await api("POST", PATH + "/check", {})).probe;
      status.textContent = "";
      const kinds = { ok: "text-success", warn: "text-warning-emphasis" };
      const parts = [el("div", {
        className: (kinds[probe.status] || "text-danger") + " llm-probe",
        text: (probe.status === "ok" ? "It answered" : probe.detail) +
          (probe.seconds ? ", in " + probe.seconds + " s" : "") + ".",
      })];
      if (probe.model) parts.push(el("div", { className: "mono", text: probe.model }));
      if (probe.reply && probe.status !== "ok") {
        parts.push(el("div", { className: "text-secondary", text: "It said: " + probe.reply }));
      }
      result.replaceChildren(...parts);
    } catch (error) {
      status.textContent = error.message;
    } finally {
      $("llm-check").disabled = !writable();
    }
  });

  load();
})();
