# tzmrit-display House Rules

The shared house rules come from skilletor — bundle `getty-house@getty`: engineering
discipline, the delegation lock, the publish gate, karr coordination. They load as
`.claude/rules/.local.*.md`; if they are missing, run `skilletor sync` and publish nothing
until they load. This file adds what is true only here, and wins where it differs.

## What this repo is

Linux/Windows driver and system monitor for HONGTAI USB LCD panels (`33c3:7791`/`7792`), a
Python package. Read first: the docstring, exports and callers — docstrings carry the *why*;
"looks orthogonal" is dangerous around the wire protocol and the renderer.

## Agent team

| Task | Agent |
|---|---|
| Implement / refactor / debug / test code | `tzmrit-display-worker` (default) |
| Cross-cutting pre-release audit (version, deps, changes) | `tzmrit-display-release-manager` |
| Linux release chain (build, systemd, udev) | `tzmrit-display-release-linux` |
| Windows release chain (PyInstaller, NSIS, `.bat`) | `tzmrit-display-release-windows` |
| Read-only research, audit, gap analysis | `tzmrit-display-worker` ("read-only, change nothing") |

All three release auditors are read-only: they report, never edit, tag or release — the worker
fixes what they find. Run the two platform auditors in parallel for a full pre-release check.

Behavior-relevant = anything under `tzmrit_display/` and `tests/`: wire protocol, render
pipeline, metric sources, session detection, CLI, error handling, performance. Prose docs are
not.

## Release

`pytest`, `python -m build`, `preview`/`image` are fine anytime. Creating the version git tag,
`twine`/PyPI upload and any release CI trigger need the maintainer's explicit go-ahead.

## Hardware hazards — locks here, mechanisms in `tzmrit-display-core`

- **No panel; firmware fails silently** — black screen, no error. A `panel.py`/`render.py`
  change that passes `pytest`/`preview` is *not* proven on hardware — say what you verified
  and what not.
- **Never edit `control_frame`/`image_frame`/`checksum` output to make a test pass** unless a
  new real-hardware capture backs it — the protocol tests guard the byte-level contract.

## Python & architecture specifics — reference, don't restate

Architecture, invariants, rendering rules and portability live in skill `tzmrit-display-core`;
the release pipelines in `tzmrit-display-packaging`.
