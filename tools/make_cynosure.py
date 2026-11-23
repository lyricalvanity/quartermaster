"""Draws the Cynosure mark for the Cynosure theme. Usage: python make_cynosure.py <out-dir> [dim-opacity]
cynosure.svg      - lit, for the start-up screen: near white, with the glow and colour fringe of a tube.
cynosure-dim.svg  - the same drawing at rest, in the theme's red at lower strength, for the page background.
The mark on the facility's mainframe screens (Cyberpunk 2077: Phantom Liberty), redrawn from its geometry
and measured against a reference still, not traced: a disc of thirteen bars with an eye at its centre,
the name in inscriptional capitals, the motto beneath. An SVG used as an image cannot load the page's web
fonts, so the lettering names faces that ship with Windows and Office and is pinned to its measured
width: a fallback face keeps the composition.
The dim drawing is ruled like a raster picture, as weyland-dim.svg is, except the disc: its bars are its
raster. Red carries less light than MU/TH/UR's green, so it rests at a higher opacity to be as present.
Its strength was 0.34 while the theme's ground was kept nearly black. The ground is now 2.2 times as
bright (see theme-cynosure.css), and the mark with it: 0.34 x 2.2."""
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
dim = float(sys.argv[2]) if len(sys.argv) > 2 else 0.748

VW, VH = 1240, 340
CX, CY, R = 160, 170, 100          # the disc
RING, IRIS, PUPIL = 52, 28, 6      # the eye: a lit ring, a dark iris, a lit pupil
# the disc's bars, top to bottom: (offset of the bar's top from the disc's top, its height). Symmetric.
BARS = [(1.5, 2.9), (10.3, 5.9), (20.6, 8.8), (35.3, 8.8), (48.5, 8.9), (63.2, 23.6), (92.65, 14.7),
        (113.2, 23.6), (142.6, 8.9), (155.9, 8.8), (170.6, 8.8), (183.8, 5.9), (195.6, 2.9)]
STUB = (82, 8.8)                   # the dark bar across the iris: width, height
NAME_FACE = "'Perpetua Titling MT', 'Palatino Linotype', 'Book Antiqua', Georgia, serif"
MOTTO_FACE = "Arial, Helvetica, sans-serif"


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


bars = "".join(f'<rect x="{CX - R}" y="{f(CY - R + top)}" width="{2 * R}" height="{f(height)}"/>' for top, height in BARS)
DRAWING = f"""<clipPath id="disc"><circle cx="{CX}" cy="{CY}" r="{R}"/></clipPath>
<mask id="eye" maskUnits="userSpaceOnUse" x="{CX - R - 10}" y="{CY - R - 10}" width="{2 * R + 20}" height="{2 * R + 20}">
<g clip-path="url(#disc)" fill="#fff">{bars}</g>
<circle cx="{CX}" cy="{CY}" r="{RING}" fill="#fff"/>
<circle cx="{CX}" cy="{CY}" r="{IRIS}" fill="#000"/><rect x="{f(CX - STUB[0] / 2)}" y="{f(CY - STUB[1] / 2)}" width="{STUB[0]}" height="{STUB[1]}" fill="#000"/>
<circle cx="{CX}" cy="{CY}" r="{PUPIL}" fill="#fff"/>
</mask>
<g id="mark"><rect x="{CX - R - 10}" y="{CY - R - 10}" width="{2 * R + 20}" height="{2 * R + 20}" mask="url(#eye)"/></g>
<g id="name" font-family="{NAME_FACE}" font-weight="700">
<text x="296" y="180" font-size="150" textLength="878" lengthAdjust="spacingAndGlyphs">CYNOSURE</text>
</g>
<g id="motto" font-family="{MOTTO_FACE}">
<text x="308" y="234" font-size="37" textLength="872" lengthAdjust="spacing">ENCRYPTION MEASURES &amp; DEFENSIVE ICE</text>
</g>"""

LIT = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VW} {VH}">
<defs>
{DRAWING}
<filter id="glow" x="-10%" y="-40%" width="120%" height="180%"><feGaussianBlur stdDeviation="7"/></filter>
</defs>
<g filter="url(#glow)" opacity=".55" fill="#ff9c8f"><use href="#mark"/><use href="#name"/><use href="#motto"/></g>
<g opacity=".55">
<g fill="#7fb4ff" transform="translate(-2.5 0)"><use href="#mark"/><use href="#name"/></g>
<g fill="#ffcf7a" transform="translate(2.5 0)"><use href="#mark"/><use href="#name"/></g>
</g>
<g fill="#fff8f6"><use href="#mark"/><use href="#name"/></g>
<g fill="#dcc3c1"><use href="#motto"/></g>
</svg>
"""

DIM = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VW} {VH}">
<defs>
{DRAWING}
<pattern id="raster" width="8" height="7" patternUnits="userSpaceOnUse"><rect width="8" height="5" fill="#fff"/></pattern>
<mask id="lines"><rect width="{VW}" height="{VH}" fill="url(#raster)"/></mask>
<filter id="glow" x="-10%" y="-40%" width="120%" height="180%"><feGaussianBlur stdDeviation="9"/></filter>
</defs>
<g opacity="{dim}" fill="#ff5a52">
<g filter="url(#glow)" opacity=".5"><use href="#mark"/><use href="#name"/><use href="#motto"/></g>
<use href="#mark"/>
<g mask="url(#lines)"><use href="#name"/><use href="#motto"/></g>
</g>
</svg>
"""

(out / "cynosure.svg").write_text(LIT, encoding="utf-8", newline="\n")
(out / "cynosure-dim.svg").write_text(DIM, encoding="utf-8", newline="\n")
print("wrote", out / "cynosure.svg", (out / "cynosure.svg").stat().st_size, out / "cynosure-dim.svg", (out / "cynosure-dim.svg").stat().st_size)
