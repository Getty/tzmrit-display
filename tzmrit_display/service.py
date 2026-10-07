"""Autostart of the dashboard on Windows: `tzmrit-display service ...`.

"Service" is what it is to the user - it starts by itself, is restarted when
it dies, and is controlled with install/start/stop/status. Underneath it is a
Task Scheduler task that runs at logon **in the user's own session**, not a
service in the Services console, and that is deliberate. A real service runs
in session 0, where none of what the dashboard reads exists: drive letters
mapped to network shares belong to the logon session, `~/.claude` and the ssh
keys belong to the user (and a key may itself live on a mapped drive), and an
ssh agent is not reachable either. The board would come up with the local
disks only and every remote host offline.

The task needs no admin rights: it is registered for the current user alone.
It is restarted after a crash but not after `stop` - the dashboard exits 0 on
a requested shutdown, and only a failure triggers the restart.

Hosts live in a text file (`hosts_file()`), one per line, which the running
dashboard re-reads when it changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import runtime

TASK_NAME = "TZMRIT Display"
LOG_NAME = "dashboard.log"
# Startup-folder shortcuts written by install.bat and the installer. The task
# replaces them; left in place they would start a second dashboard at logon
# that immediately takes the panel over from the first.
_STARTUP_SHORTCUTS = ("TZMRIT Display.lnk", "tzmrit-display.lnk")

HOSTS_TEMPLATE = """\
# Machines whose Claude Code sessions the panel shows, read over ssh.
# One per line, anything `ssh` accepts; `label=target` chooses the name on the
# panel. The running dashboard picks up changes to this file by itself.
#
#   atlas
#   alex=alex@lab.example.org
"""


def hosts_file() -> Path:
    return runtime.config_dir() / "hosts.txt"


def log_file() -> Path:
    return runtime.runtime_dir() / LOG_NAME


def write_hosts(names, path: Path | None = None) -> Path:
    """Write the hosts file: the given hosts, or the commented template if
    there is no file yet. An existing file is only replaced when hosts are
    given - a plain reinstall must not empty the user's list."""
    path = path or hosts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if names:
        path.write_text(HOSTS_TEMPLATE + "\n" + "\n".join(names) + "\n",
                        encoding="utf-8")
    elif not path.exists():
        path.write_text(HOSTS_TEMPLATE, encoding="utf-8")
    return path


def launcher() -> tuple[str, list[str]]:
    """The windowless executable and the arguments that reach our CLI.

    A console build would flash a window at every logon and keep it open, so
    the `w` twin is used wherever it exists: `tzmrit-displayw.exe` beside the
    frozen build, `pythonw.exe` beside the interpreter of a checkout.
    """
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        twin = exe.with_name("tzmrit-displayw.exe")
        return str(twin if twin.exists() else exe), []
    twin = exe.with_name("pythonw.exe")
    return str(twin if twin.exists() else exe), ["-m", "tzmrit_display"]


def task_xml(command: str, arguments: str, workdir: str, user: str) -> str:
    """The task definition. XML because `schtasks /SC ONLOGON` can express
    neither the restart on failure nor "no time limit" - by default a task is
    killed after three days and does not start on battery."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Drives the HONGTAI USB panel (tzmrit-display dashboard).</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _current_user() -> str:
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def _require_windows() -> bool:
    if os.name == "nt":
        return True
    print("`service` manages the Windows autostart task. On Linux use the "
          "systemd user unit in systemd/tzmrit-display.service.", file=sys.stderr)
    return False


def installed() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def _remove_startup_shortcuts() -> list[str]:
    startup = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    removed = []
    for name in _STARTUP_SHORTCUTS:
        try:
            (startup / name).unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


def install(run_args: list[str], hosts=()) -> int:
    """Register the task with these `run` arguments and start it now."""
    if not _require_windows():
        return 1
    hosts_path = write_hosts(list(hosts))
    command, prefix = launcher()
    argv = prefix + ["--log-file", str(log_file()), "run",
                     "--hosts-file", str(hosts_path)] + list(run_args)
    xml = task_xml(command, subprocess.list2cmdline(argv),
                   str(Path(command).parent), _current_user())
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16",
                                     delete=False) as fh:
        fh.write(xml)
    try:
        made = _schtasks("/Create", "/TN", TASK_NAME, "/XML", fh.name, "/F")
    finally:
        os.unlink(fh.name)
    if made.returncode != 0:
        print(f"Could not register the task: {(made.stderr or made.stdout).strip()}",
              file=sys.stderr)
        return 1
    for name in _remove_startup_shortcuts():
        print(f"Removed the Startup shortcut '{name}' - the task replaces it.")
    print(f"Installed task '{TASK_NAME}': starts at logon, restarts after a crash.")
    print(f"  hosts  {hosts_path}")
    print(f"  log    {log_file()}")
    print(f"  runs   {command} {subprocess.list2cmdline(argv)}")
    return start()


def start() -> int:
    """Start the task now. A dashboard started by hand hands the panel over."""
    if not _require_windows():
        return 1
    ran = _schtasks("/Run", "/TN", TASK_NAME)
    if ran.returncode != 0:
        print(f"Could not start: {(ran.stderr or ran.stdout).strip()}", file=sys.stderr)
        return 1
    print("Dashboard started.")
    return 0


def uninstall() -> int:
    """Remove the task. The hosts file stays - it is the user's."""
    if not _require_windows():
        return 1
    if not installed():
        print("Not installed.")
        return 1
    gone = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if gone.returncode != 0:
        print(f"Could not remove the task: {(gone.stderr or gone.stdout).strip()}",
              file=sys.stderr)
        return 1
    print(f"Removed task '{TASK_NAME}'. The hosts file was kept: {hosts_file()}")
    return 0


def status() -> int:
    """rc 0 installed and running, 1 not installed, 3 installed but stopped."""
    if not _require_windows():
        return 1
    if not installed():
        print("Not installed. `tzmrit-display service install` sets it up.")
        return 1
    pid = runtime.read_instance()
    print(f"Task      '{TASK_NAME}' installed (starts at logon)")
    print(f"Dashboard {'running, pid %d' % pid if pid else 'not running'}")
    print(f"Hosts     {hosts_file()}")
    from .remote_hosts import read_hosts_file
    for name in read_hosts_file(hosts_file()):
        print(f"            {name}")
    print(f"Log       {log_file()}")
    return 0 if pid else 3
