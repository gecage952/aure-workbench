// Refuse the commands an unattended agent must not run.
//
// Written by `nrw init`. This is OpenCode 1's half of a limit that Claude Code
// gets from a PreToolUse hook: `tool.execute.before` runs before the command
// does, so the refusal does not depend on the model agreeing to it.
//
// OpenCode 2 refuses to load this file and reads nrw-guard-opencode2.js, the
// same guard in the form it takes. Each version warns about the other's file
// in its log; that warning is expected.
//
// **No refusal logic lives here.** Every decision is `nrw agent guard`, which
// splits the raw command line on `;&|()` and newlines before tokenising and
// scans every segment -- six real shapes got past an earlier version that
// looked only at the first few tokens (`if true; then nrw promote...`,
// `A=1 nrw promote abc`, `python -m nr_workbench.cli promote abc`, ...).
// Reimplementing any part of that in JavaScript would mean two answers to the
// same question, and the one here would be the stale one.
//
// The deny list in opencode.json is the second, weaker mechanism: it matches
// the command as written, so `A=1 nrw promote x` slips past it.
//
// Note `opencode --pure` skips external plugins, and therefore this file.

import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { join } from "node:path"

/** Exit code `nrw agent guard` uses to block a command. */
const BLOCK = 2

/**
 * Locate `nrw`.
 *
 * The project-local shim is tried first and exists precisely for this case: a
 * session started from an editor inherits none of the environment that put
 * `nrw` on PATH, and searching for the binary has cost ten tool calls before.
 */
function nrwCommand(directory) {
  const shim = join(directory, ".nrw", "bin", "nrw")
  return existsSync(shim) ? shim : "nrw"
}

export const NrwGuard = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash" && input.tool !== "shell") return

      const command = output?.args?.command
      if (typeof command !== "string" || command.trim() === "") return

      const verdict = spawnSync(
        nrwCommand(directory),
        ["agent", "guard", "--command", command],
        { encoding: "utf-8" },
      )

      // A guard that cannot run must not block everything. An unreachable
      // `nrw` is our problem, and failing closed on it would make the project
      // unusable rather than safe -- the same call `nrw agent guard` itself
      // makes on an unreadable payload.
      if (verdict.error || verdict.status === null) return

      if (verdict.status === BLOCK) {
        throw new Error(
          (verdict.stderr || "Refused by nr-workbench.").trim(),
        )
      }
    },
  }
}
