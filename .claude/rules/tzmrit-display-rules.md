# tzmrit-display House Rules

Apply to every task here unless explicitly overridden. Bias: caution over speed on non-trivial work.
Subagents get their conventions from the skills force-loaded via `briefing.skills` — not restated
here.

## Engineering discipline

1. **Think before coding** — State assumptions; when uncertain, ask rather than guess, offer
   alternatives if ambiguous, push back on simpler approaches. Stop when confused; say what's
   unclear.
2. **Simplicity first** — Minimum code that solves the problem. Nothing speculative.
3. **Surgical changes** — Touch only what you must. Don't "improve" adjacent code, comments, or
   formatting. Match existing style.
4. **Read before you write** — Before new code, read the docstring, exports and callers; docstrings
   carry the *why*. "Looks orthogonal" is dangerous around the wire protocol and the renderer.
5. **Surface conflicts, don't average them** — Contradicting patterns: pick one, explain why, flag
   the other; don't blend.
6. **Tests verify intent** — A test that can't fail when the logic changes is wrong. Reproduce a bug
   before fixing it; leave a regression test behind.
7. **Fail loud** — "Done" is wrong if anything was skipped silently. "Tests pass" is wrong if any
   were skipped. Surface uncertainty, don't hide it.
8. **A red test is a claim before it is a failure** — Before turning a red test green, say what it
   claims and whether your fix keeps that claim. If the claim is wrong, fix the claim and say so.

## Delegation

This rule depends on whether the Agent/Task tool is available to you.

- **You can spawn subagents** (orchestrating main agent): Do NOT touch behavior-relevant code —
  delegate to `tzmrit-display-worker`. Your lane: coordinate, inspect, plan, review diffs, run
  tests, edit non-behavioral docs. When in doubt, delegate. Why: only the `tzmrit-display-*` agents
  get their skills force-loaded via `briefing.skills`; you get none and would touch the wire
  protocol blind.

| Task | Agent |
|---|---|
| Implement / refactor / debug / test code | `tzmrit-display-worker` (default) |
| Cross-cutting pre-release audit (version, deps, changes) | `tzmrit-display-release-manager` |
| Linux release chain (build, systemd, udev) | `tzmrit-display-release-linux` |
| Windows release chain (PyInstaller, NSIS, `.bat`) | `tzmrit-display-release-windows` |

All three release auditors are read-only: they report, never edit, tag or release — the worker fixes
what they find. Run the two platform auditors in parallel for a full pre-release check.

- **You cannot spawn subagents** (you ARE a `tzmrit-display-*` agent): The delegation lock does not
  apply to you — implement, refactor, debug, and test per these rules.

Behavior-relevant = anything under `tzmrit_display/` and `tests/`: wire protocol, render pipeline,
metric sources, session detection, CLI, error handling, performance. Prose docs are not.

**Only `tzmrit-display-release-manager` commits.** A worker leaves a commit-ready tree and hands its
card to `review`; then dispatch `tzmrit-display-release-manager` to commit and close the card.

## Coordination — karr board (always in scope)

Ticket coordination is the orchestrating agent's job: `karr` is always in scope — don't invoke the
`kanban-issues-karr-coordination` skill first, just use it. State lives in `refs/karr/*`; one board.

- `karr list --compact` / `karr board` — open work · `karr show ID` — detail
- `karr create "Title" --priority high --tags a,b --body '…'` — new · `karr move ID in-progress
  --claim NAME` — start · `karr handoff ID --claim NAME --note "…"` — to review
- mutating commands auto-sync. Full surface: skill `kanban-issues-karr-coordination`.

**Serialize board mutations when fanning out.** Keep implementation parallel, collect results, then
loop `karr move`/`handoff`/`sync` sequentially — N at once is a resource event, not a cheap command.

## Release — never without permission

`pytest`, `python -m build`, `preview`/`image` are fine anytime. Creating the version **git tag**,
`twine`/PyPI upload, and any release CI trigger are STRICTLY forbidden without the maintainer's
explicit go-ahead — even if a plan lists "release" next. Stop and ask.

## Public issues — never act without instruction

**GitHub** (`Getty/tzmrit-display`) is the public tracker — real humans' bug reports, under the
maintainer's account; **karr** is our internal board. **Never act on a GitHub issue on your own —
not even to read it.** No listing, viewing, commenting, closing/creating unless told to handle one.

## Hardware hazards — locks here, mechanisms in `tzmrit-display-core`

- **No panel; firmware fails silently** — black screen, no error. A `panel.py`/`render.py` change
  that passes `pytest`/`preview` is *not* proven on hardware — say what you verified and what not.
- **Never edit `control_frame`/`image_frame`/`checksum` output to make a test pass** unless a new
  real-hardware capture backs it — the protocol tests guard the byte-level contract.

## Python & architecture specifics — reference, don't restate

Architecture, invariants, rendering rules and portability live in skill `tzmrit-display-core`; the
release pipelines in `tzmrit-display-packaging`. Do not duplicate that here.
