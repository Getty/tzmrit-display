# Windows support

Verified on real hardware: Windows 11, panel `33c3:7792`
(D215-NOR-FL7707N-9.16inch-hor, firmware 3.2) on `COM3`. Windows binds the
panel with its built-in `usbser.sys` — no driver install needed. The dashboard
ran at a steady 1 fps with ~70 KB frames, sessions view included.

The panel protocol itself is platform independent: it is a serial port and
JPEG frames. `panel.py`, `render.py` and `theme.py` contain no OS assumptions,
and pyserial reports the same `vid`/`pid`/`manufacturer` fields on Windows
(the device is just called `COM3` instead of `/dev/ttyACM0`).

## Installing

Three ways, pick one:

1. **The installer.** `tzmrit-display-setup-<version>.exe` — no Python, no git
   required. Installs per-user (no admin prompt), registers an uninstaller in
   *Apps & Features*, and offers autostart at logon with or without the Claude
   sessions view. Build it yourself with `packaging\build.bat` (needs Python
   3.10+ and NSIS, `winget install NSIS.NSIS`).

2. **From a checkout:** run `install.bat`. It creates `.venv`, installs the
   package, checks that the panel answers, and puts a shortcut in the Startup
   folder (`pythonw.exe`, so no console window). `uninstall.bat` reverses all
   of it and blanks the panel.

3. **Manually:**

   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e .
   .venv\Scripts\tzmrit-display info
   ```

   The fonts ship inside the package (`tzmrit_display/fonts/`), so a regular
   `pip install .` works too; `-e` is used here only so a live checkout picks
   up edits.

If the port is busy, the vendor application is probably still running — it
holds the COM port exclusively.

## What was adapted

| Concern | Linux | Windows |
|---|---|---|
| Load average | `os.getloadavg()` | does not exist — the LOAD tile is dropped, DISK takes the slot |
| CPU temperature | `psutil.sensors_temperatures()` | not available in psutil — TEMP tile dropped |
| Process liveness | exact `procStart` compare via `/proc/<pid>/stat` | `procStart` compare as .NET ticks (see below) |
| Root filesystem | `/` | `Path.home().anchor` → `C:\` |
| Autostart | systemd user service | Startup-folder shortcut (installer or `install.bat`) |

With no temperature sensor and no load average, the metric set becomes
CPU / RAM / NET↑ / NET↓ / DISK — five columns instead of six. Both layouts
handle that; the column width is computed, not hard coded.

## `procStart` on Windows

On Linux the field holds clock ticks from `/proc/<pid>/stat`. On Windows it
holds **.NET `DateTime` ticks** — 100 ns units since 0001-01-01, in local
time. Verified against a live session: the value matches
`psutil.Process(pid).create_time()` to the microsecond.

`_is_live()` therefore compares the stored value against psutil's start time
(2 s tolerance, UTC reading accepted as well), which restores the protection
against a recycled PID being shown as a live session — the same guarantee the
exact `/proc` compare gives on Linux. A value that does not parse as an
integer falls back to the weak check (process exists and looks like Claude).

## Verified on first run

* `tzmrit-display info` prints model, geometry, firmware over `COM3`.
* Rendering and fonts work; the metric set degrades to five tiles as designed.
* `--claude` finds running sessions in `%USERPROFILE%\.claude\sessions\`,
  including status and memory (child processes — MCP servers — included).
* Continuous `run`: 1.01 fps sustained, ~70 KB per frame.
* The PyInstaller build (`packaging\tzmrit-display.spec`) finds the fonts
  because the frozen layout mirrors the checkout layout
  (`_internal\tzmrit_display\fonts`).

`run --http PORT` serves the same rendered frame over HTTP (open
`http://<host>:PORT/`, or fetch `/frame.png`); `--no-panel` makes it a headless
web dashboard with no device attached. It uses only the stdlib `http.server`, so
the frozen build gains no dependency and it should behave the same as on Linux —
but this has not been exercised on a Windows machine here. The default bind is
`0.0.0.0`; use `--http-host 127.0.0.1` to keep it local.

Worth one look on a new panel model: the image orientation. The rotation is
derived from the device's own `angle` field (`to_wire()`), same code path as
on Linux — but angle=90 hardware has never been seen.

## Known gaps

* The keepalive requirement is unchanged: without a running process the panel
  blanks. Log off and the image goes with it — that is what autostart at
  logon is for.
* Sleep and hibernate are untested. The device may need to be re-enumerated
  after resume; the process may need a restart then.
* A frozen `tzmrit-displayw.exe` has no console: errors are invisible. If the
  panel stays dark, run `tzmrit-display.exe run -v` (the console twin) once to
  see why.
