---
name: tzmrit-display-core
description: Architecture, invariants and repo conventions for tzmrit-display — the Linux/Windows driver and system monitor for HONGTAI USB LCD panels (33c3:7791/7792). Use when implementing, refactoring, debugging or testing any module under tzmrit_display/.
---

# tzmrit-display — core

A single Python package (`tzmrit_display`) that drives HONGTAI USB CDC-ACM LCD
panels and paints a system monitor onto them. There is no kernel driver: the
panel enumerates as `/dev/ttyACM*` on its own (Linux) or a COM port (Windows),
and this code is the "driver software" — it opens the port and pushes JPEG
frames. Console script: `tzmrit-display` (`tzmrit_display.cli:main`). Python
≥3.10; deps `pyserial`, `pillow`, `psutil`.

## The pipeline

```
sources.SystemSource ─┐
claude_sessions ──────┼─► render.DashboardRenderer ─► PIL RGB image
theme (T) ────────────┘                                   │
                                             panel.Panel.to_wire() → JPEG frame
                                                          │
                                             /dev/ttyACM* (CDC-ACM serial)
```

- `cli.py` — argparse subcommands: `run` (live loop), `preview` (write PNG),
  `image FILE` (push a still), `clear`, `brightness LEVEL`, `info`. `run`/`preview`
  take `--claude` for the split layout. The live loop pushes a frame every
  `--interval` seconds and must keep sending keepalives.
- `sources.py` — `SystemSource` samples CPU/RAM/temp/load/disk/net into
  `Metric` objects, each holding a bounded `deque` history (`HISTORY = 90`) for
  the sparklines. Thresholds drive `Metric.status` (`ok`/`warn`/`crit`).
- `claude_sessions.py` — reads `~/.claude/sessions/<pid>.json` files Claude Code
  writes; no API, no service. Only local sessions are visible.
- `render.py` — layout engine for the 1920×462 strip. `render()` = six metric
  columns; `render_split()` = four metrics + running Claude sessions.
- `theme.py` — colors, fonts, geometry.
- `panel.py` — the wire protocol and serial transport.

## Hardware invariants — verified against real firmware, do not "simplify"

These come from a capture against `D215-NOR-FL7707N-9.16inch-hor` firmware 3.2,
not a spec. Full protocol writeup: `docs/protocol.md`. Break one and the screen
silently stays black — the firmware never complains.

1. **Geometry is queried, never assumed.** The device reports `1920×462`
   (advertised as 480; it is not) with `angle=270` — geometry *before* rotation.
   Render in viewer orientation; `to_wire()` does the rotation. Related panels
   (e.g. `33c3:7791`, 480×480) must keep working because the size comes from the
   device, so never hardcode `1920`/`462` outside `theme`/`panel` derivation.
2. **JPEG must be 4:2:0 chroma subsampling.** The firmware's decoder drops a
   4:4:4 frame without a word. When encoding through PIL, keep the subsampling
   that `to_wire()` sets.
3. **Keepalives (opcode `0x11`) are mandatory.** Without them the firmware
   leaves live mode and blanks, so even a static image needs a running process.
   Don't turn the live loop into a fire-and-forget single push.
4. **Two frame formats share the port.** Control frames
   (`55 AA | len_le16 | opcode | payload | ck_le16`, `len = payload+7`) and
   image frames (`len_le32 | JPEG | ck_le16`, firmware > 2.8). Checksum is the
   little-endian 16-bit sum of every preceding byte. The test expectations are
   captured bytes — a change that alters `control_frame`/`image_frame`/`checksum`
   output is wrong unless the capture changed.

## Rendering conventions — meaning, not decoration

- **Color always means something.** The six metric columns deliberately share
  one accent color; identity comes from position + label. `warn`/`crit` are the
  only status colors and appear only when a threshold is crossed, always with a
  marker shape, never alone. Don't add per-column colors. The palette in
  `theme.py` is contrast/colorblind-verified (OKLab dE + WCAG against the
  surface) — changing a status color means re-checking those numbers.
- **No locale-dependent formatting.** Weekday/month names are spelled out in a
  constant, not via `strftime("%A")`, so the panel reads the same everywhere.
  Keep new date/number formatting locale-independent.
- **Supersampling is intentional.** PIL has no antialiased lines; rendering at
  `--scale` and downsampling is what keeps sparklines from going ragged.

## Portability

Runs on Linux and Windows (verified on hardware). Anything platform-specific is
**probed, not assumed**: `HAS_LOADAVG = hasattr(os, "getloadavg")`,
`ROOT_PATH = Path.home().anchor or "/"`, temp/load absent on Windows so the
metric set adapts rather than leaving a gap. New platform-specific code follows
the same rule — probe capability, degrade gracefully. Windows notes:
`docs/windows.md`.

## Repo conventions

- Surgical, style-matching changes. Module docstrings carry the *why* — read the
  relevant one before editing; keep it accurate if behavior changes.
- Version is derived from the git tag by `setuptools-scm` (`v0.2.0` → `0.2.0`;
  between tags `0.2.1.devN+g<hash>`). There is no version string to bump by hand;
  releasing = creating the tag. `fallback_version` covers git-less tarballs.
- `__init__.__version__` is a static fallback string, not the source of truth.

## Verification

```bash
./.venv/bin/pytest        # or: python -m pytest — testpaths=tests in pyproject
```

Tests are plain pytest with `monkeypatch`/`tmp_path`; no fixture harness. The
protocol tests assert exact captured bytes and the theme/render tests assert
status thresholds and history bounds — treat a red one as a claim about real
hardware or real intent before changing code to make it green.
