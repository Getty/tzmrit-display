"""The board view: drives, short metric cards, limits per account."""

from collections import namedtuple

from tzmrit_display import sources, theme as T
from tzmrit_display.claude_limits import AccountLimits, Limit, Limits
from tzmrit_display.claude_sessions import Session
from tzmrit_display.render import DashboardRenderer, _BOARD_DRIVES, _SPLIT_UTILITY
from tzmrit_display.sources import Drive, SystemSource, list_drives

Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free")


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def has_color(image, color, box):
    return any(found == color for _, found in image.crop(box).getcolors(1 << 24))


class TestListDrives:
    def fake(self, monkeypatch, windows, parts, usage):
        monkeypatch.setattr(sources, "WINDOWS", windows)
        monkeypatch.setattr(sources.psutil, "disk_partitions", lambda all=False: parts)

        def disk_usage(mountpoint):
            value = usage[mountpoint]
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(sources.psutil, "disk_usage", disk_usage)

    def test_windows_letters_local_first_and_same_share_merged(self, monkeypatch):
        """Captured shape: A, M and S map the same NAS volume, F is an empty
        card reader, X a DVD drive."""
        share = Usage(35854e9, 18644e9, 17210e9)
        self.fake(monkeypatch, True, [
            Part("A:\\", "A:\\", "NTFS", "rw,remote"),
            Part("C:\\", "C:\\", "NTFS", "rw,fixed"),
            Part("F:\\", "F:\\", "", "removable"),
            Part("K:\\", "K:\\", "NTFS", "rw,remote"),
            Part("M:\\", "M:\\", "NTFS", "rw,remote"),
            Part("S:\\", "S:\\", "NTFS", "rw,remote"),
            Part("X:\\", "X:\\", "UDF", "ro,cdrom"),
        ], {"A:\\": share, "M:\\": share, "S:\\": share,
            "C:\\": Usage(2000e9, 1480e9, 520e9),
            "K:\\": Usage(248e9, 17e9, 231e9),
            "F:\\": PermissionError(13, "not ready")})
        drives = list_drives()
        assert [(d.label, d.percent, d.remote) for d in drives] == [
            ("C", 74, False), ("AMS", 52, True), ("K", 7, True)]
        assert drives[0].reset_text() == "520G"
        assert drives[1].reset_text() == "17.2T"

    def test_unix_keeps_real_devices_and_network_mounts(self, monkeypatch):
        self.fake(monkeypatch, False, [
            Part("/dev/nvme0n1p2", "/", "ext4", "rw"),
            Part("/dev/nvme0n1p2", "/home", "ext4", "rw"),      # same device
            Part("/dev/nvme0n1p1", "/boot/efi", "vfat", "rw"),
            Part("/dev/loop3", "/snap/core/1", "squashfs", "ro"),
            Part("tmpfs", "/run", "tmpfs", "rw"),
            Part("nas:/vol", "/mnt/nas", "nfs4", "rw"),
        ], {"/": Usage(100e9, 40e9, 60e9), "/mnt/nas": Usage(1000e9, 900e9, 100e9)})
        assert [(d.label, d.percent, d.remote) for d in list_drives()] == [
            ("/", 40, False), ("nas", 90, True)]

    def test_source_hands_out_the_cached_reading(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sources, "list_drives",
                            lambda: calls.append(1) or [Drive("C", 1, 1.0)])
        src = SystemSource()
        assert [d.label for d in src.drives()] == ["C"]
        src.drives()
        assert len(calls) == 1, "a second frame must not re-read the drives"


class TestBoardRender:
    def metrics(self):
        src = SystemSource()
        for _ in range(3):
            src.sample()
        return src.metrics

    def render(self, drives=(), sessions=(), accounts=(), scale=1):
        return DashboardRenderer(scale=scale).render_board(
            self.metrics(), list(drives), list(sessions), "summary",
            list(accounts), [("HOST", "box"), ("UPTIME", "1d 2h")])

    def test_frame_has_the_panel_geometry(self):
        img = self.render(scale=2)
        assert img.size == (T.WIDTH, T.HEIGHT) and img.mode == "RGB"

    def test_network_drives_get_the_warm_family_local_ones_the_accent(self):
        local = self.render(drives=[Drive("C", 80, 1e9)])
        remote = self.render(drives=[Drive("A", 80, 1e9, remote=True)])
        assert has_color(local, rgb(T.ACCENT), _BOARD_DRIVES)
        assert not has_color(local, rgb(T.ACCENT_WARM), _BOARD_DRIVES)
        assert has_color(remote, rgb(T.ACCENT_WARM), _BOARD_DRIVES)

    def test_second_account_is_drawn_in_its_own_color(self):
        limits = Limits(session=Limit("Session", 60), weekly=Limit("Weekly", 60))
        utility = (_SPLIT_UTILITY[0], 94, _SPLIT_UTILITY[1], 432)
        one = self.render(accounts=[AccountLimits("GETTY", limits)])
        two = self.render(accounts=[AccountLimits("GETTY", limits),
                                    AccountLimits("ALEX", limits)])
        assert not has_color(one, rgb(T.ACCENT_WARM), utility)
        assert has_color(two, rgb(T.ACCENT_WARM), utility)
        assert has_color(two, rgb(T.ACCENT), utility)

    def test_account_without_data_and_no_account_at_all_still_render(self):
        self.render(accounts=[AccountLimits("GETTY", None)])
        self.render(accounts=[])
        self.render(accounts=[AccountLimits(str(i), None) for i in range(5)])

    def test_eleven_to_many_sessions_switch_to_dense_rows(self):
        """Ten sessions keep the two-line rows; from eleven on the lanes go
        dense, which must change the picture and must not crash at any count."""
        def sessions(n):
            return [Session(i, f"proj-{i:02d}", "/x/proj", "idle", "i", host="atlas")
                    for i in range(n)]
        lanes = (660, 94, 1570, 440)
        ten = self.render(sessions=sessions(10)).crop(lanes)
        eleven = self.render(sessions=sessions(11)).crop(lanes)
        assert ten.tobytes() != eleven.tobytes()
        for n in (19, 20, 21, 40):
            self.render(sessions=sessions(n))

    def test_many_drives_do_not_overflow_the_card(self):
        img = self.render(drives=[Drive(chr(65 + i), 50, 1e9) for i in range(14)])
        below = (_BOARD_DRIVES[0], _BOARD_DRIVES[3] + 1, _BOARD_DRIVES[2], T.HEIGHT)
        assert not has_color(img, rgb(T.ACCENT), below)
