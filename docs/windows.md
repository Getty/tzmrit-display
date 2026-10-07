# Windows support

Verified on real hardware: Windows 11, panel `33c3:7792`
(D215-NOR-FL7707N-9.16inch-hor, firmware 3.2) on `COM3`. Windows binds the
panel with its built-in `usbser.sys` — no driver install needed. The dashboard
ran at a steady 1 fps with ~70 KB frames, sessions view included. The board
with three remote hosts (`--hosts`) was run on the same machine from the
autostart task.

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
| Process liveness | exact `procStart` compare via `/proc/<pid>/stat` | `procStart` compare as FILETIME or .NET ticks (see below) |
| Root filesystem | `/` | `Path.home().anchor` → `C:\` |
| Autostart | systemd user service | logon task (`service install`), or the Startup-folder shortcut of the installer / `install.bat` |
| Drives on the board | real block devices and network mounts | every drive letter with a medium; letters on the same share merged |

With no temperature sensor and no load average, the metric set becomes
CPU / RAM / NET↑ / NET↓ / DISK — five columns instead of six. Both layouts
handle that; the column width is computed, not hard coded.

## `procStart` on Windows

On Linux the field holds clock ticks from `/proc/<pid>/stat`. On Windows it
holds a 100 ns count, and the epoch depends on the Claude Code version:

* **FILETIME** — since 1601-01-01 UTC. Measured with Claude Code 2.1.29x:
  `procStart` 134358218750129363 for a process started at epoch 1791348275.
* **.NET `DateTime` ticks** — since 0001-01-01, in local time. What older
  versions wrote; verified against a live session to the microsecond.

A dashboard that knows only the second reading shows no session at all on a
current Claude Code, so `_is_live()` accepts either: it compares the stored
value against psutil's start time in all three readings (FILETIME, local
ticks, UTC ticks; 2 s tolerance), which restores the protection
against a recycled PID being shown as a live session — the same guarantee the
exact `/proc` compare gives on Linux. A value that does not parse as an
integer falls back to the weak check (process exists and looks like Claude).

## Autostart: a logon task, not a service

`tzmrit-display service install` registers a Task Scheduler task named
*TZMRIT Display* for the current user: trigger at logon, no time limit,
restart one minute after a failure. A requested stop exits 0 and is therefore
not restarted. No admin rights are involved.

It is deliberately not a service in the Services console. A service runs in
session 0, outside the user's logon session, and there the board loses most of
what it shows:

* **Mapped network drives** belong to the logon session. The DISKS card would
  list the local disks only.
* **`%USERPROFILE%\.claude`** — sessions and the account's credentials — and
  the **ssh configuration and keys** belong to the user. A key can itself live
  on a mapped drive.

The task runs in the user's own session with the user's unelevated token, so
all of that is there.

**The ssh client is pinned.** The task does not inherit the PATH of the shell
it was installed from, and which `ssh` comes first decides whether a host works
at all. Measured here: a key on a network drive (`IdentityFile A:/.ssh/id_rsa`)
is accepted by Git's `ssh` and refused by Windows' own OpenSSH —

```
Permissions for 'A:/.ssh/id_rsa' are too open.
Load key "A:/.ssh/id_rsa": bad permissions
```

— so the same host connected from Git Bash and stayed `offline` under the
task. `service install` therefore resolves `ssh` in the shell it runs in and
registers that path (`--ssh`). Install from the shell in which
`ssh -o BatchMode=yes <host> true` works. `--ssh PROGRAM` overrides it.

When a host stays offline, the reason is in the log
(`%LOCALAPPDATA%\tzmrit-display\dashboard.log`): the dashboard records the
last line `ssh` wrote before it gave up, once per distinct failure.

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
the frozen build gains no dependency. Alongside the panel it was run on Windows
with `--http-host 127.0.0.1`; `--no-panel` has not been exercised there. The
default bind is `0.0.0.0`; use `--http-host 127.0.0.1` to keep it local.

Worth one look on a new panel model: the image orientation. The rotation is
derived from the device's own `angle` field (`to_wire()`), same code path as
on Linux — but angle=90 hardware has never been seen.

## Known gaps

* The keepalive requirement is unchanged: without a running process the panel
  blanks. Log off and the image goes with it — that is what autostart at
  logon is for.
* Sleep and hibernate are untested. The device may need to be re-enumerated
  after resume; the process may need a restart then.
* A frozen `tzmrit-displayw.exe` has no console: errors are invisible unless
  it was started with `--log-file` (the autostart task does that). If the
  panel stays dark, run `tzmrit-display.exe run -v` (the console twin) once to
  see why.
* The task has been started by hand and by `service install`; an actual logoff
  and logon has not been observed yet.
