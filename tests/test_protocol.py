"""Tests for the wire protocol.

The expected values come from a real capture against
D215-NOR-FL7707N-9.16inch-hor (firmware 3.2), not from a specification.
"""

import pytest

from display_panel.panel import (
    PanelError,
    PanelInfo,
    checksum,
    control_frame,
    image_frame,
    parse_reply,
)


class TestFrames:
    def test_control_frame_matches_captured_bytes(self):
        # These exact bytes got a reply from the device (opcode 0x06)
        assert control_frame(0x06).hex() == "55aa0700060c01"

    def test_control_frame_length_counts_payload_plus_seven(self):
        frame = control_frame(0x03, b"\x64")
        assert int.from_bytes(frame[2:4], "little") == 8

    def test_checksum_is_little_endian_sum(self):
        assert checksum(b"\x01\x02\x03") == b"\x06\x00"
        # Overflow is masked to 16 bits
        assert checksum(b"\xff" * 300) == ((255 * 300) & 0xFFFF).to_bytes(2, "little")

    def test_image_frame_prefixes_length_and_appends_checksum(self):
        jpeg = b"\xff\xd8\xff\xe0payload"
        frame = image_frame(jpeg)
        assert int.from_bytes(frame[:4], "little") == len(jpeg)
        assert frame[4:-2] == jpeg
        assert frame[-2:] == checksum(frame[:-2])


class TestReplies:
    def test_parses_device_info(self):
        payload = b'{"cmd":"info","data":{"width":1920,"height":462}}'
        raw = b"\x55\xaa\x00\x00\x06" + payload + b"\x00\x00"
        assert parse_reply(raw)["data"]["width"] == 1920

    def test_short_reply_raises(self):
        with pytest.raises(PanelError, match="short reply"):
            parse_reply(b"\x55\xaa\x00")

    def test_error_code_is_translated(self):
        raw = b"\x55\xaa\x00\x00\x06" + b"\x03" + b"\x00\x00"
        with pytest.raises(PanelError, match="internal storage full"):
            parse_reply(raw)


class TestPanelInfo:
    def test_firmware_above_2_8_uses_length_header(self):
        assert PanelInfo(version="3.2").uses_length_header
        assert not PanelInfo(version="2.8").uses_length_header

    def test_unparsable_version_falls_back_to_legacy_format(self):
        # Prefer the older, less demanding format over a rejected frame
        assert not PanelInfo(version="unknown").uses_length_header

    def test_rotation_detected_for_90_and_270(self):
        assert PanelInfo(angle=270).rotated
        assert PanelInfo(angle=90).rotated
        assert not PanelInfo(angle=0).rotated

    def test_frame_budget_for_this_panel(self):
        info = PanelInfo(model="D215-NOR-FL7707N-9.16inch-hor", version="3.2",
                         width=1920, height=462)
        # The long edge decides first: >= 1024 px -> 260 KB
        assert info.max_frame_kb == 260
