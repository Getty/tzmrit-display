# HONGTAI USB LCD wire protocol

Recorded for `33c3:7792`, model `D215-NOR-FL7707N-9.16inch-hor`, firmware 3.2.
Everything here was verified against the device, not copied from a spec sheet.

## Connection

The panel enumerates as **USB CDC-ACM** (`bDeviceClass 2`, abstract modem).
Linux binds it automatically with the in-tree `cdc_acm` driver and it appears
as `/dev/ttyACM0`. **There is no kernel driver to write.** The Windows "driver
software" is an Electron application that opens the COM port and pushes JPEG
frames at it.

Access requires membership in `dialout`, which `cdc_acm` sets up itself.

Opening the port asserts DTR; that is how the panel notices a host is present
and leaves its standby screen.

## Frame formats

Two formats share the same port.

**Control frame**

```
55 AA | len_lo len_hi | opcode | payload | ck_lo ck_hi
len = len(payload) + 7
ck  = sum of every preceding byte & 0xFFFF   (little endian)
```

**Image frame** (firmware > 2.8)

```
len_le32 | JPEG | ck_lo ck_hi
ck = sum of the length prefix and the JPEG & 0xFFFF
```

Firmware <= 2.8 takes a bare JPEG with no header and no checksum.

## Opcodes

| Code | Meaning |
|------|---------|
| `0x01` | restart |
| `0x03` | set brightness (1 byte, 0-100) |
| `0x06` | get device info → JSON |
| `0x11` | keepalive |
| `0x20` | set region |
| `0x21` | close |

Replies are JSON between the 5-byte head and the 2-byte checksum. Non-JSON
replies are hex-encoded single-byte error codes (`01` operation failed …
`07` file write failed).

## This panel's reply

```json
{"cmd":"info","data":{
  "uid":"<device serial>",
  "width":1920, "height":462, "angle":270,
  "model":"D215-NOR-FL7707N-9.16inch-hor", "version":"3.2",
  "brightness":80, "diplay_on":false,
  "i_blocks":24520, "i_block_size":512, "i_block_free":5448, "i_path":"/data",
  "region":"colinbao_v1"
}}
```

The typo `diplay_on` is the firmware's own.

## Three pitfalls

Each one shows up as a **black screen with no error message**.

**1. The JPEG must use 4:2:0 subsampling.** An image encoded with
`subsampling=0` (4:4:4) is discarded without a word. Pillow picks 4:2:0 on its
own at quality ≤ 92 — the trick is not to override it.

**2. Without keepalives the firmware leaves live mode** and blanks. Opcode
`0x11` has to arrive roughly every 1.5 s. A still image therefore needs a
running process; there is no "set an image and walk away" mode over this path.

**3. The reported geometry is pre-rotation.** The panel reports 1920×462 with
`angle: 270`. Sending an unrotated 1920×462 image gets you a 90°-tilted
picture. Measured: the sent image's x axis runs down the panel, its y axis
runs left. The fix is a 90° **counter-clockwise** rotation before sending
(`Image.Transpose.ROTATE_90`).

## Frame budget

The firmware drops oversized frames. The budget follows the vendor app's own
rule: long edge ≥ 1024 px → 260 KB. A typical dashboard frame for this panel
lands at 35-60 KB, well below that.

## Related devices

`33c3:7791` is the same protocol on a 480×480 panel (LovingCool AIO pump head).
A reference implementation for it is
[GOG1071/hongtai-panel](https://github.com/GOG1071/hongtai-panel) — published
**without a license**, so it is not suitable to copy from; here it served only
as protocol evidence.

Unrelated: Turing Smart Screen / TURZX (`1cbe:*`) speak a different protocol
over raw USB rather than a serial port.
