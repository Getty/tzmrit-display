# display-panel House Rules

Apply to every task in this repository unless explicitly overridden. Bias: caution
over speed on non-trivial work; use judgment on trivial tasks. Loaded automatically
at launch (same priority as `CLAUDE.md`). Subagents get their discipline from the
skills force-loaded via `briefing.skills` — this file is for the orchestrating agent.

## Engineering discipline

1. **Think before coding** — State assumptions. When uncertain, ask rather than
   guess. Present alternatives when ambiguous. Push back when a simpler approach
   exists. Stop when confused; name what's unclear.
2. **Simplicity first** — Minimum code that solves the problem. Nothing speculative.
3. **Surgical changes** — Touch only what you must. Don't "improve" adjacent code,
   comments, or formatting. Match existing style.
4. **Read before you write** — Before new code, read the module docstring, exports
   and immediate callers. The docstrings in `display_panel/*.py` carry the *why*;
   "looks orthogonal" is dangerous around the wire protocol and the render pipeline.
5. **Surface conflicts, don't average them** — Contradicting patterns: pick one,
   explain why, flag the other. Don't blend.
6. **Tests verify intent** — A test that can't fail when the logic changes is wrong.
   Reproduce a bug before fixing it; leave a regression test behind.
7. **Fail loud** — "Done" is wrong if anything was skipped silently. "Tests pass" is
   wrong if any were skipped. Surface uncertainty, don't hide it.
8. **A red test is a claim before it is a failure** — The protocol tests assert bytes
   captured from real firmware; the render tests assert real intent. Before turning
   one green, say what it claims and whether your fix keeps that claim. If the claim
   is wrong, fix the claim and say so.

## Delegation

This rule depends on whether the Agent/Task tool is available to you.

- **You can spawn subagents** (orchestrating main agent): Do NOT touch
  behavior-relevant code yourself — delegate to `display-panel-worker`. Your lane:
  coordinate, inspect, plan, review diffs, run tests, manage git, edit non-behavioral
  docs. When in doubt, delegate. Why: only the `display-panel-*` agents get their
  skills force-loaded via `briefing.skills`; you get no briefing and would touch the
  wire protocol / render engine with too little context.

  | Task | Agent |
  |---|---|
  | Implement / refactor / debug / test code | `display-panel-worker` (default) |
  | Cross-cutting pre-release audit (version, deps, changes) | `display-panel-release-checker` |
  | Linux release chain (build, systemd, udev) | `display-panel-release-linux` |
  | Windows release chain (PyInstaller, NSIS, `.bat`) | `display-panel-release-windows` |

  The three release auditors are read-only and report; they never edit, tag or
  release — the worker fixes what they find. Run the two platform auditors in
  parallel for a full pre-release check.

- **You cannot spawn subagents** (you ARE a `display-panel-*` agent): The delegation
  lock does not apply to you — implement, refactor, debug, and test per these rules.

Behavior-relevant = anything under `display_panel/` and `tests/`: the wire protocol,
the render pipeline, metric sources, session detection, the CLI, error handling,
performance. Pure prose docs (`README.md`, `docs/*.md`) and changelog notes are not.

## Coordination — karr board (always in scope)

Ticket coordination is the orchestrating agent's job, so `karr` is always in scope —
don't invoke the `kanban-issues-karr-cli` skill first, just use it. Git-native kanban;
state lives in `refs/karr/*`; this repo has its own board. Day-to-day:

- `karr list --compact` / `karr board` — open work · `karr show ID` — detail
- `karr create "Title" --priority high --tags a,b --body '…'` — new ticket
- `karr move ID in-progress --claim NAME` — start · `karr handoff ID --claim NAME --note "…"` — to review
- mutating commands auto-sync. Full surface: skill `kanban-issues-karr-cli`.

**Serialize board mutations when fanning out.** Keep implementation parallel if you
like, but collect the results and then loop `karr move`/`handoff`/`sync`
sequentially — N landing at once is a resource event, not a cheap command.

## Release — never without permission

`pytest`, `python -m build`, `preview`/`image` are fine anytime. Creating the version
**git tag**, `twine`/PyPI upload, and any release CI trigger are STRICTLY forbidden
without the maintainer's explicit go-ahead — even if a plan lists "release" next.
Releasing here IS tagging: the version is derived from the tag by setuptools-scm, so
the tag is the release. Stop and ask.

## Public issues — never act without instruction

Two trackers, two universes. **karr** is the agent work board — internal, churned
freely. **GitHub** (`Getty/tzmrit-display`) carries real humans' bug reports, written
under the maintainer's account. **Never act on a GitHub issue on your own
initiative — not even to read it.** No listing, viewing, commenting, closing, or
creating unless the user explicitly says to handle a specific issue.

## Hardware hazards — why this file is worth loading

No physical panel is attached in this environment, and the firmware fails **silently**:
a 4:4:4 JPEG, a missing keepalive, hardcoded geometry, or an altered checksum all end
in a black screen with no error, not an exception. So a change to `panel.py`/`render.py`
that "runs fine" against `pytest` and `preview` (which need no device) is *not* proven
on hardware — say exactly what you verified and what you did not. The protocol tests
guard the byte-level contract; never edit `control_frame`/`image_frame`/`checksum`
output to make a test pass unless a new real-hardware capture backs it.

## Python & architecture specifics — reference, don't restate

Architecture, hardware invariants, rendering rules, portability and repo conventions
live in skill `display-panel-core` (force-loaded for `display-panel-*` agents). Do not
duplicate that content here.
