# tzmrit-display

Linux/Windows driver and system monitor for HONGTAI USB LCD panels
(`33c3:7791`/`7792`). No kernel driver — the panel enumerates as USB CDC-ACM
(`/dev/ttyACM*`) and this Python package pushes JPEG frames at it. Console
script: `tzmrit-display`.

## Delegation

Delegate behavior-relevant code to the right agent instead of touching it
yourself — the principle, the lane and the hardware hazards are in
`.claude/rules/tzmrit-display-rules.md` (auto-loaded every turn).

| Task | Agent |
|---|---|
| Implement / refactor / debug / test code under `tzmrit_display/` or `tests/` | `tzmrit-display-worker` (default) |
| Cross-cutting release audit (version, deps, changes) | `tzmrit-display-release-checker` |
| Linux release chain (sdist/wheel, systemd, udev) | `tzmrit-display-release-linux` |
| Windows release chain (PyInstaller, NSIS, `.bat`) | `tzmrit-display-release-windows` |

The three release auditors are read-only and report; the worker fixes what they
find. Run the two platform auditors in parallel for a full pre-release check.

The agents carry their knowledge via `briefing.skills` (see `.claude/agents/`);
the main agent delegates rather than loading those skills. Architecture, hardware
invariants and repo conventions live in skill `tzmrit-display-core`; the two release
pipelines in `tzmrit-display-packaging`; the karr command surface in
`kanban-issues-karr-cli` (all under `.claude/skills/`).

## Verify

```bash
./.venv/bin/pytest        # testpaths=tests; no device needed
```

`preview` and `image` subcommands render to a PNG without hardware. No physical
panel is attached here, and the firmware fails silently — a change that passes
tests is not proven on hardware.

## Coordination & release

Work is tracked on the repo's `karr` board (`karr board`). **Release = creating
the version git tag** (setuptools-scm derives the version from it) — forbidden
without the maintainer's explicit go-ahead. GitHub issues (`Getty/tzmrit-display`)
are never touched on the agent's own initiative.
