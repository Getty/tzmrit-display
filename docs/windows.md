# Windows support — status and checklist

The panel protocol itself is platform independent: it is a serial port and
JPEG frames. `panel.py`, `render.py` and `theme.py` contain no OS assumptions,
and pyserial reports the same `vid`/`pid`/`manufacturer` fields on Windows
(the device is just called `COM3` instead of `/dev/ttyACM0`).

The platform specific parts have been made portable, but **nothing below has
run on real Windows hardware yet**. This file is the checklist for that first
run.

## What was adapted

| Concern | Linux | Windows |
|---|---|---|
| Load average | `os.getloadavg()` | does not exist — the LOAD tile is dropped, DISK takes the slot |
| CPU temperature | `psutil.sensors_temperatures()` | not available in psutil — TEMP tile dropped |
| Process liveness | exact `procStart` compare via `/proc/<pid>/stat` | falls back to `psutil` (process exists and looks like Claude) |
| Root filesystem | `/` | `Path.home().anchor` → `C:\` |
| Autostart | systemd user service | Task Scheduler (see below) |

With no temperature sensor and no load average, the metric set becomes
CPU / RAM / NET↑ / NET↓ / DISK — five columns instead of six. Both layouts
handle that; the column width is computed, not hard coded.

## To verify on the first run

1. **Does the port open at all?**
   `display-panel info` should print model, geometry and firmware. Windows
   binds CDC-ACM devices with the built-in `usbser.sys`; no driver install
   should be needed. If the port is busy, the vendor application is probably
   still running — it holds the port exclusively.

2. **Is the image oriented correctly?**
   `display-panel preview -o test.png` then `display-panel image test.png`.
   The rotation is derived from the device's own `angle` field, so it should
   behave identically — but this is worth one look.

3. **What does `procStart` look like?** ← *the one real unknown*
   ```
   type %USERPROFILE%\.claude\sessions\*.json
   ```
   On Linux this field holds clock ticks from `/proc`. On Windows the format
   is unknown. Right now `_is_live()` ignores the value off Linux and only
   checks that the process exists and looks like Claude. If the Windows value
   turns out to be comparable (for example a filetime that matches
   `psutil.Process(pid).create_time()`), tighten the check — that restores
   protection against a recycled PID being shown as a live session.

4. **Does `--claude` find the sessions?**
   `display-panel run --claude`. If the list stays empty although sessions are
   running, check that `%USERPROFILE%\.claude\sessions\` is the right path and
   that the JSON carries `pid`, `name`, `cwd` and `status`.

5. **Memory including MCP servers**
   `psutil.Process(pid).children(recursive=True)` works on Windows, but the
   process tree may look different (npm wrappers, `cmd.exe` shims). Compare
   the reported figure against Task Manager once.

## Autostart via Task Scheduler

There is no systemd. A scheduled task at logon does the same job:

```powershell
$py = "$env:USERPROFILE\dev\display\.venv\Scripts\pythonw.exe"
$action  = New-ScheduledTaskAction -Execute $py `
           -Argument "-m display_panel run --claude" `
           -WorkingDirectory "$env:USERPROFILE\dev\display"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "display-panel" -Action $action -Trigger $trigger
```

`pythonw.exe` rather than `python.exe` keeps a console window from appearing.

## Known gaps

* The keepalive requirement is unchanged: without a running process the panel
  blanks. A scheduled task that exits will take the image with it.
* Sleep and hibernate are untested. The device may need to be re-enumerated
  after resume; `Restart=on-failure` has no Task Scheduler equivalent unless
  the task is configured to restart on failure.
