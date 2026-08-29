"""Regression tests: `run` must survive a vanished or absent panel.

On Windows, unplugging the panel mid-run makes pyserial raise SerialException
from write() ("WriteFile failed (PermissionError(13, ...))"). Before the
reconnect loop this escaped cli.main() uncaught and surfaced as the
PyInstaller windowed unhandled-exception dialog. These tests drive the real
cmd_run against a fake serial port - no hardware, no real COM ports.
"""

import json
import signal
import time

import serial

import tzmrit_display.panel as panel
from tzmrit_display import cli

INFO = {"data": {"width": 1920, "height": 462, "angle": 270, "version": "Ver3.2",
                 "model": "D215-NOR-FL7707N-9.16inch-hor", "uid": "t",
                 "brightness": 100}}
REPLY = b"\x55\xaa\x00\x00\x06" + json.dumps(INFO).encode() + b"\x00\x00"


def _is_image(data):
    """Anything that is not a control frame or the clear sequence is a frame."""
    return data[:2] != panel.MAGIC and data not in (panel.CLEAR_SEQUENCE,
                                                    panel.CLEAR_TAIL)


def _image_frames(writes):
    return [w for w in writes if _is_image(w)]


class FakeSerial:
    """Answers the 0x06 handshake; the on_write hook scripts the scenario."""

    def __init__(self, port, on_write):
        self.port = port
        self.on_write = on_write
        self.alive = True
        self.writes = []
        self._pending = b""

    def write(self, data):
        if not self.alive:
            # what serialwin32.write() raises for a vanished port
            raise serial.SerialException(
                "WriteFile failed (PermissionError(13, 'Access is denied.', None, 5))")
        self.writes.append(bytes(data))
        self.on_write(self, bytes(data))
        return len(data)

    def flush(self):
        if not self.alive:
            raise serial.SerialException("ClearCommError failed")

    def read(self, n):
        buf, self._pending = self._pending, b""
        return buf

    def reset_input_buffer(self):
        self._pending = REPLY

    def close(self):
        pass


def _patch_env(monkeypatch, handlers, fake_find_port, make_serial):
    """Wire the fakes into both namespaces that resolve them.

    cli imports find_port by name and Panel() resolves its default port via
    panel.find_port, so both bindings must point at the fake. Sleeps are
    capped (not removed) to keep ordering semantics while the test runs fast.
    """
    monkeypatch.setattr(signal, "signal", lambda s, h: handlers.__setitem__(s, h))
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(min(s, 0.003)))
    monkeypatch.setattr(cli, "RECONNECT_POLL", 0.02)
    monkeypatch.setattr(cli, "find_port", fake_find_port)
    monkeypatch.setattr(panel, "find_port", fake_find_port)
    monkeypatch.setattr(panel.serial, "Serial", make_serial)


def test_unplug_midrun_reconnects_with_cold_start(monkeypatch):
    """(i) + (ii): the unplug is contained, the wait loop polls, and the new
    port goes through the full cold-start sequence before frames resume."""
    handlers = {}
    serials = []
    state = {"phase": "up1", "polls": 0}

    def on_write(fake, data):
        if not _is_image(data):
            return
        if fake is serials[0] and len(_image_frames(fake.writes)) == 2:
            fake.alive = False  # the unplug: every later write raises
            state["phase"] = "down"
        elif len(serials) == 2 and fake is serials[1]:
            handlers[signal.SIGINT](signal.SIGINT, None)  # frame arrived; stop

    def fake_find_port():
        if state["phase"] == "up1":
            return "COM7"
        if state["phase"] == "up2":
            return "COM8"
        state["polls"] += 1
        assert state["polls"] < 50, "reconnect poll loop runs away"
        if state["polls"] >= 3:
            state["phase"] = "up2"  # the panel re-enumerated, new port name
            return "COM8"
        return None

    def make_serial(port, *a, **kw):
        fake = FakeSerial(port, on_write)
        serials.append(fake)
        return fake

    _patch_env(monkeypatch, handlers, fake_find_port, make_serial)

    rc = cli.main(["run", "--interval", "0.01"])

    assert rc == 0, "unplug must not escape cmd_run (backstop rc=1 means it did)"
    assert len(serials) == 2, "expected a second connection after the unplug"
    assert state["polls"] >= 3, "the wait loop never polled for the port"
    assert serials[1].port == "COM8", "reconnect must use the rediscovered port"
    # Cold start on the new port: link reset, geometry re-queried, live mode
    # re-entered - then frames flow again.
    second = serials[1].writes
    opcodes = [w[4] for w in second if w[:2] == panel.MAGIC]
    assert panel.CLEAR_SEQUENCE in second
    assert panel.Opcode.GET_DEVICE_INFO in opcodes
    assert panel.Opcode.KEEPALIVE in opcodes
    assert len(_image_frames(second)) >= 1


def test_absent_at_start_waits_then_connects(monkeypatch):
    """(iii): no panel at logon means wait, not exit."""
    handlers = {}
    serials = []
    state = {"calls": 0}

    def fake_find_port():
        state["calls"] += 1
        assert state["calls"] < 50, "startup wait loop runs away"
        return "COM5" if state["calls"] > 3 else None

    def on_write(fake, data):
        if _is_image(data):
            handlers[signal.SIGINT](signal.SIGINT, None)  # frame arrived; stop

    def make_serial(port, *a, **kw):
        fake = FakeSerial(port, on_write)
        serials.append(fake)
        return fake

    _patch_env(monkeypatch, handlers, fake_find_port, make_serial)

    rc = cli.main(["run", "--interval", "0.01"])

    assert rc == 0
    assert state["calls"] > 3, "run exited instead of waiting for the panel"
    assert len(serials) == 1
    assert len(_image_frames(serials[0].writes)) >= 1


def test_oneshot_serial_failure_is_reported_not_raised(monkeypatch, capsys):
    """The main() backstop: one-shot commands turn SerialException into a
    clean message and rc=1 instead of a traceback / windowed dialog."""
    monkeypatch.setattr(panel, "find_port", lambda: "COM9")

    def raise_open(*a, **kw):
        raise serial.SerialException(
            "could not open port 'COM9': PermissionError(13, 'Access is denied.')")

    monkeypatch.setattr(panel.serial, "Serial", raise_open)

    rc = cli.main(["brightness", "50"])

    assert rc == 1
    assert "Error:" in capsys.readouterr().err
