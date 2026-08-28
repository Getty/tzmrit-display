"""Wire protocol for HONGTAI USB panels (VID 0x33C3).

The panel enumerates as USB CDC-ACM, so the in-tree `cdc_acm` driver exposes
it at /dev/ttyACM* on its own. There is no kernel driver to write - the
Windows "driver software" is an application that opens the COM port and pushes
JPEG frames at it. That is exactly what this module does.

Two frame formats share the port:

    Control frame  55 AA | len_lo len_hi | opcode | payload | ck_lo ck_hi
                   len = len(payload) + 7
                   ck  = sum of every preceding byte & 0xFFFF

    Image frame    len_le32 | JPEG | ck_lo ck_hi          (firmware > 2.8)
                   ck = sum of length prefix and JPEG & 0xFFFF

Verified against D215-NOR-FL7707N-9.16inch-hor, firmware 3.2:

  * The device reports 1920x462 with angle=270, i.e. the geometry BEFORE
    rotation. Rendering happens in viewer orientation; `to_wire()` rotates.
  * The firmware's JPEG decoder requires 4:2:0 chroma subsampling. A 4:4:4
    image is dropped without a word - the screen just stays black.
  * Without keepalives (opcode 0x11) the firmware drops out of live mode and
    blanks. A still image therefore needs a running process.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from dataclasses import dataclass, field

import serial
from PIL import Image

log = logging.getLogger(__name__)

MAGIC = b"\x55\xaa"
USB_VID = 0x33C3
KNOWN_PIDS = {0x7791, 0x7792}

# Aborts a partially transmitted frame and blanks the panel.
CLEAR_SEQUENCE = b"\xff\xd9\xff\xd9"
CLEAR_TAIL = b"\x00\x00\x00\x00"

KEEPALIVE_INTERVAL = 1.5
BAUD = 2_000_000  # ignored by CDC-ACM, but matches the vendor app


class Opcode:
    RESTART = 0x01
    SET_BRIGHTNESS = 0x03
    GET_DEVICE_INFO = 0x06
    KEEPALIVE = 0x11
    SET_REGION = 0x20
    CLOSE = 0x21


ERROR_CODES = {
    "01": "operation failed",
    "02": "out of memory",
    "03": "internal storage full",
    "04": "SD card full",
    "05": "file does not exist",
    "06": "file open failed",
    "07": "file write failed",
}


class PanelError(RuntimeError):
    """The device reported an error, or the link misbehaved."""


def checksum(data: bytes) -> bytes:
    return (sum(data) & 0xFFFF).to_bytes(2, "little")


def control_frame(opcode: int, payload: bytes = b"") -> bytes:
    body = MAGIC + (len(payload) + 7).to_bytes(2, "little") + bytes([opcode]) + payload
    return body + checksum(body)


def image_frame(jpeg: bytes) -> bytes:
    body = len(jpeg).to_bytes(4, "little") + jpeg
    return body + checksum(body)


def parse_reply(raw: bytes) -> dict:
    """Decode a reply: payload sits between the 5-byte head and 2-byte checksum."""
    if len(raw) < 8:
        raise PanelError(f"short reply ({len(raw)} bytes): {raw.hex()}")
    payload = raw[5:-2]
    try:
        return json.loads(payload.decode("utf8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        code = payload.hex()
        raise PanelError(f"device error {code}: {ERROR_CODES.get(code, 'unknown')}") from None


@dataclass
class PanelInfo:
    """What the panel reports about itself via opcode 0x06."""

    width: int = 1920
    height: int = 462
    angle: int = 0
    version: str = ""
    model: str = ""
    uid: str = ""
    brightness: int = 100
    raw: dict = field(default_factory=dict)

    @property
    def version_number(self) -> float:
        try:
            return float(str(self.version).replace("Ver", "").strip())
        except ValueError:
            return 0.0

    @property
    def uses_length_header(self) -> bool:
        """Firmware after 2.8 expects the length + checksum image envelope."""
        return self.version_number > 2.8

    @property
    def rotated(self) -> bool:
        """At 90/270 degrees the panel reports pre-rotation geometry."""
        return self.angle in (90, 270)

    @property
    def max_frame_kb(self) -> int:
        """Per-frame JPEG budget; the firmware drops anything larger."""
        longest = max(self.width, self.height)
        if "10.26" in self.model:
            return 350
        if "6.67" in self.model or longest >= 1024:
            return 260
        if "9.16" in self.model:
            return 120 if self.uses_length_header else 90
        return 80 if self.uses_length_header else 50


def find_port() -> str | None:
    """Locate the panel's tty by VID/PID, falling back to the product name."""
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid in KNOWN_PIDS:
            return p.device
    for p in list_ports.comports():
        if p.vid == USB_VID:
            return p.device
        if "HONGTAI" in (p.manufacturer or "").upper():
            return p.device
    return None


def encode_jpeg(img: Image.Image, budget_kb: int) -> tuple[bytes, int]:
    """Encode JPEG at descending quality until the frame fits the budget.

    Without an explicit `subsampling` argument Pillow picks 4:2:0 at these
    quality levels. That is intentional: 4:4:4 renders as a black screen.
    """
    data = b""
    for quality in (92, 86, 80, 72, 64, 56, 46, 36):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= budget_kb * 1024:
            return data, quality
    return data, 36


class Panel:
    """A connected panel.

    Writes go through a lock because the keepalive thread shares the port with
    the frame writer; interleaving the two corrupts both streams.
    """

    def __init__(self, port: str | None = None, timeout: float = 2.0):
        self.port_path = port or find_port()
        if not self.port_path:
            raise PanelError("no HONGTAI panel found (VID 33c3)")
        self._serial = serial.Serial(self.port_path, BAUD, timeout=timeout, write_timeout=5.0)
        self._lock = threading.Lock()
        self._keepalive: threading.Thread | None = None
        self._stop = threading.Event()
        self.info = PanelInfo()

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Panel":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close(blank=False)

    def close(self, blank: bool = False) -> None:
        """Disconnect. `blank=True` actively clears the image."""
        self.stop_live()
        if blank:
            try:
                self.clear()
            except Exception:
                pass
        try:
            self._serial.close()
        except Exception:
            pass

    # -- primitives ------------------------------------------------------

    def _write(self, data: bytes) -> None:
        with self._lock:
            self._serial.write(data)
            self._serial.flush()

    def command(self, opcode: int, payload: bytes = b"", expect_reply: bool = False) -> dict | None:
        with self._lock:
            if expect_reply:
                self._serial.reset_input_buffer()
            self._serial.write(control_frame(opcode, payload))
            self._serial.flush()
            if not expect_reply:
                return None
            deadline = time.monotonic() + 3.0
            buf = b""
            while time.monotonic() < deadline:
                chunk = self._serial.read(4096)
                if chunk:
                    buf += chunk
                    if buf.rstrip(b"\x00"):
                        time.sleep(0.05)
                        buf += self._serial.read(4096)
                        break
                else:
                    time.sleep(0.02)
            if not buf:
                raise PanelError(f"no reply to opcode 0x{opcode:02x}")
            return parse_reply(buf)

    def clear(self) -> None:
        self._write(CLEAR_SEQUENCE)
        self._write(CLEAR_TAIL)

    def connect(self) -> PanelInfo:
        """Reset the link and read the device geometry."""
        self.clear()
        time.sleep(0.3)
        reply = self.command(Opcode.GET_DEVICE_INFO, expect_reply=True) or {}
        data = reply.get("data") or reply
        self.info = PanelInfo(
            width=int(data.get("width", 1920)),
            height=int(data.get("height", 462)),
            angle=int(data.get("angle", 0) or 0),
            version=str(data.get("version", "")),
            model=str(data.get("model", "")),
            uid=str(data.get("uid", "")),
            brightness=int(data.get("brightness", 100)),
            raw=data,
        )
        return self.info

    # -- commands --------------------------------------------------------

    def set_brightness(self, level: int) -> None:
        self.command(Opcode.SET_BRIGHTNESS, bytes([max(0, min(100, int(level)))]))

    def keepalive(self) -> None:
        self.command(Opcode.KEEPALIVE)

    # -- image -----------------------------------------------------------

    def to_wire(self, img: Image.Image) -> Image.Image:
        """Rotate an image from viewer orientation into wire orientation.

        Verified for angle=270: sent unrotated, the image's x axis runs down
        the panel and its y axis runs left. ROTATE_90 (counter-clockwise)
        corrects that. angle=90 needs the opposite direction; untested.
        """
        target = self.viewport
        if img.size != target:
            img = img.resize(target, Image.LANCZOS)
        if self.info.angle == 270:
            return img.transpose(Image.Transpose.ROTATE_90)
        if self.info.angle == 90:
            return img.transpose(Image.Transpose.ROTATE_270)
        return img

    @property
    def viewport(self) -> tuple[int, int]:
        """The canvas to render into, in viewer orientation."""
        return (self.info.width, self.info.height)

    def send_frame(self, jpeg: bytes) -> None:
        self._write(image_frame(jpeg) if self.info.uses_length_header else jpeg)

    def show(self, img: Image.Image) -> int:
        """Display an image given in viewer orientation. Returns the frame size."""
        jpeg, _ = encode_jpeg(self.to_wire(img), self.info.max_frame_kb)
        self.send_frame(jpeg)
        return len(jpeg)

    # -- live mode -------------------------------------------------------

    def start_live(self) -> None:
        """Enter live mode and hold it open with a background keepalive."""
        if self._keepalive and self._keepalive.is_alive():
            return
        self._stop.clear()
        self.command(Opcode.KEEPALIVE)

        def loop() -> None:
            while not self._stop.wait(KEEPALIVE_INTERVAL):
                try:
                    self.command(Opcode.KEEPALIVE)
                except Exception as exc:
                    log.warning("keepalive failed: %s", exc)
                    return

        self._keepalive = threading.Thread(target=loop, daemon=True, name="panel-keepalive")
        self._keepalive.start()

    def stop_live(self) -> None:
        self._stop.set()
        if self._keepalive:
            self._keepalive.join(timeout=2.0)
            self._keepalive = None
