"""Colors, fonts and metrics for the 1920x462 panel.

On color: the six columns deliberately do NOT get six colors. Their identity
comes from position and label; one color per column would be decoration
carrying no information. The accent color is therefore uniform, and the
reserved status colors appear only when a threshold is crossed - so color on
this panel always means something.

Verified (OKLab dE x100 / WCAG against #0B0D12; deuteranopia uses Machado et
al.'s full-severity matrix in linear RGB):
  warn <-> crit        dE 20.099 normal vision, 13.001 deuteranopia
  red accent <-> crit  dE 9.912 normal vision, 9.626 deuteranopia
  contrast             ink 16.1:1, ink_dim 6.4:1, blue accent 7.7:1,
                       red accent 5.240:1, warn 12.6:1, crit 7.7:1
  red sparkline fill   #482125, 1.407:1 against surface (alpha 72)
  red limit track      #572629, 1.587:1 against surface; 3.303:1 from accent

The status colors sit deliberately above the lightness band used for
categorical palettes: they are not peer series, they are meant to stand out.
They never appear alone, always alongside a marker shape.
"""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# -- Colors --------------------------------------------------------------
SURFACE = "#0B0D12"
SURFACE_TILE = "#12151C"
INK = "#E6EAF2"
INK_DIM = "#8B94A6"
INK_FAINT = "#4A5262"
ACCENT = "#58A6FF"
# The normal red is deliberately saturated and darker than the reserved CRIT
# coral. Its contrast and OKLab separation are recorded in the module docstring.
RED_ACCENT = "#E35454"
WARN = "#F2CC60"
CRIT = "#FF7B72"
# A muted warm amber, complementary to ACCENT, for a categorical (non-status)
# tint - the redundant project prefix in a session name. Deliberately kept
# clear of the status colors: hue 26.8 deg sits between CRIT (3.8) and WARN
# (44.4), ~18-23 deg from each, so it never reads as a warning. Contrast on
# SURFACE 8.2:1.
ACCENT_WARM = "#E0985E"


@dataclass(frozen=True)
class Palette:
    """All color roles a renderer needs, fixed for its lifetime."""

    surface: str
    surface_tile: str
    ink: str
    ink_dim: str
    ink_faint: str
    accent: str
    warn: str
    crit: str
    accent_warm: str
    spark_fill_alpha: int


BLUE_PALETTE = Palette(
    surface=SURFACE,
    surface_tile=SURFACE_TILE,
    ink=INK,
    ink_dim=INK_DIM,
    ink_faint=INK_FAINT,
    accent=ACCENT,
    warn=WARN,
    crit=CRIT,
    accent_warm=ACCENT_WARM,
    spark_fill_alpha=46,
)
RED_PALETTE = Palette(
    surface=SURFACE,
    surface_tile=SURFACE_TILE,
    ink=INK,
    ink_dim=INK_DIM,
    ink_faint=INK_FAINT,
    accent=RED_ACCENT,
    warn=WARN,
    crit=CRIT,
    accent_warm=ACCENT_WARM,
    spark_fill_alpha=72,
)
_PALETTES = MappingProxyType({"blue": BLUE_PALETTE, "red": RED_PALETTE})


def palette(name: str) -> Palette:
    """Return one of the immutable, renderer-owned color-role palettes."""
    return _PALETTES[name]

# -- Geometry ------------------------------------------------------------
WIDTH, HEIGHT = 1920, 462
MARGIN_X = 34
HEADER_Y = 26
RULE_Y = 78
TILE_TOP = 104
SPARK_TOP = 258
SPARK_H = 112
FOOTER_Y = 410

# -- Fonts ---------------------------------------------------------------
_ROBOTO = FONT_DIR / "roboto"
_MONO = FONT_DIR / "jetbrains-mono"

FONT_LABEL = _ROBOTO / "Roboto-Medium.ttf"
FONT_TEXT = _ROBOTO / "Roboto-Regular.ttf"
FONT_VALUE = _MONO / "JetBrainsMono-Bold.ttf"      # tabular figures: values don't jitter
FONT_UNIT = _ROBOTO / "Roboto-Medium.ttf"


def font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(str(path), size)
