"""Regression tests for the cooperative stop channel and `run` takeover.

The windowed exe has no window or tray, so shutdown goes through per-user
runtime files (tzmrit_display.runtime): `run` records its PID, `stop` and a
taking-over `run` write a stop-request sentinel the run loop polls, and the
honoring instance exits like on Ctrl+C with rc 0. No processes are killed.

Fakes are shared with test_reconnect; a fake "other instance" is simulated by
a pre-seeded pid file plus a patched runtime.pid_alive that "exits" (removes
its files) once it sees a stop request addressed to it.
"""

import os
import signal

from test_reconnect import FakeSerial, _image_frames, _is_image, _patch_env

import tzmrit_display.panel as panel
from tzmrit_display import cli, runtime


def _patch_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "STOP_WAIT", 0.5)


def test_stop_request_exits_run_gracefully(monkeypatch, tmp_path):
    """The run loop honors a stop request: graceful rc 0, sentinel consumed,
    pid claim released, and --blank-on-exit behaves exactly like Ctrl+C."""
    handlers = {}
    serials = []

    def on_write(fake, data):
        if not _is_image(data):
            return
        images = len(_image_frames(fake.writes))
        if images == 1:
            # the running instance has claimed the pid file
            assert (tmp_path / runtime.PID_FILE).read_text() == str(os.getpid())
        elif images == 2:
            runtime.request_stop(os.getpid())  # what `stop`/takeover write
        elif images >= 30:
            # escape hatch: only reached if the sentinel is being ignored
            handlers[signal.SIGINT](signal.SIGINT, None)

    def make_serial(port, *a, **kw):
        fake = FakeSerial(port, on_write)
        serials.append(fake)
        return fake

    _patch_env(monkeypatch, tmp_path, handlers, lambda: "COM7", make_serial)

    rc = cli.main(["run", "--interval", "0.01", "--blank-on-exit"])

    assert rc == 0
    writes = serials[0].writes
    assert len(_image_frames(writes)) == 2, "run loop ignored the stop request"
    assert not (tmp_path / runtime.STOP_FILE).exists(), "request was not acknowledged"
    assert not (tmp_path / runtime.PID_FILE).exists(), "pid claim was not released"
    # --blank-on-exit ran, same as on Ctrl+C: the session ends with a clear
    assert writes[-2:] == [panel.CLEAR_SEQUENCE, panel.CLEAR_TAIL]


def test_takeover_requests_old_instance_and_proceeds(monkeypatch, tmp_path):
    """A second `run` asks the recorded instance to exit, waits for it, then
    claims the panel itself."""
    handlers = {}
    serials = []
    state = {"requested": False, "old_exited": False}
    (tmp_path / runtime.PID_FILE).write_text("4242")  # the "old" dashboard

    def fake_pid_alive(pid):
        if pid != 4242:
            return False
        stop_file = tmp_path / runtime.STOP_FILE
        if stop_file.exists() and stop_file.read_text().strip() == "4242":
            # the old instance sees its stop request: ack + release + exit
            state["requested"] = True
            state["old_exited"] = True
            stop_file.unlink()
            (tmp_path / runtime.PID_FILE).unlink(missing_ok=True)
        return not state["old_exited"]

    def on_write(fake, data):
        if _is_image(data):
            handlers[signal.SIGINT](signal.SIGINT, None)  # frame arrived; stop

    def make_serial(port, *a, **kw):
        fake = FakeSerial(port, on_write)
        serials.append(fake)
        return fake

    _patch_env(monkeypatch, tmp_path, handlers, lambda: "COM7", make_serial)
    monkeypatch.setattr(runtime, "pid_alive", fake_pid_alive)

    rc = cli.main(["run", "--interval", "0.01"])

    assert rc == 0
    assert state["requested"], "the old instance was never asked to exit"
    assert len(serials) == 1
    assert len(_image_frames(serials[0].writes)) >= 1
    assert not (tmp_path / runtime.PID_FILE).exists()


def test_stale_pid_file_is_tolerated(monkeypatch, tmp_path):
    """A pid file left by a crashed instance neither blocks the start nor
    triggers a stop request into the void."""
    handlers = {}
    serials = []
    requests = []
    (tmp_path / runtime.PID_FILE).write_text("999999")

    def on_write(fake, data):
        if _is_image(data):
            handlers[signal.SIGINT](signal.SIGINT, None)

    def make_serial(port, *a, **kw):
        fake = FakeSerial(port, on_write)
        serials.append(fake)
        return fake

    _patch_env(monkeypatch, tmp_path, handlers, lambda: "COM7", make_serial)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime, "request_stop", lambda *a, **kw: requests.append(a))

    rc = cli.main(["run", "--interval", "0.01"])

    assert rc == 0
    assert requests == [], "stop was requested from a dead instance"
    assert len(_image_frames(serials[0].writes)) >= 1
    assert not (tmp_path / runtime.PID_FILE).exists()


def test_stop_with_no_instance(monkeypatch, tmp_path, capsys):
    _patch_runtime(monkeypatch, tmp_path)

    rc = cli.main(["stop"])

    assert rc == 1
    assert "No dashboard running." in capsys.readouterr().out


def test_stop_with_stale_pid_file(monkeypatch, tmp_path, capsys):
    (tmp_path / runtime.PID_FILE).write_text("999999")
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)

    rc = cli.main(["stop"])

    assert rc == 1
    assert "No dashboard running." in capsys.readouterr().out
    assert not (tmp_path / runtime.PID_FILE).exists(), "stale pid file kept"


def test_stop_running_instance_reports_stopped(monkeypatch, tmp_path, capsys):
    """`stop` targets the recorded pid and confirms once the process is gone."""
    state = {"old_exited": False}
    (tmp_path / runtime.PID_FILE).write_text("4242")

    def fake_pid_alive(pid):
        if pid != 4242:
            return False
        stop_file = tmp_path / runtime.STOP_FILE
        if stop_file.exists() and stop_file.read_text().strip() == "4242":
            state["old_exited"] = True
            stop_file.unlink()
            (tmp_path / runtime.PID_FILE).unlink(missing_ok=True)
        return not state["old_exited"]

    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "pid_alive", fake_pid_alive)

    rc = cli.main(["stop"])

    assert rc == 0
    assert "Dashboard stopped." in capsys.readouterr().out
