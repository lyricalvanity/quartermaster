"""Draws the Weyland-Yutani emblem for the MU/TH/UR theme. Usage: python make_weyland.py <out-dir> [dim-opacity]
weyland.svg      - full strength, for the start-up screen.
weyland-dim.svg  - the same drawing at low strength, for the page background.
Drawn from the geometry (a wide W of four slanted arms, a triangle in each mouth), striped like a
raster picture; not traced from a still. The wordmark uses Consolas, which ships with Windows:
an SVG used as an image cannot load the page's web fonts."""
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
dim = float(sys.argv[2]) if len(sys.argv) > 2 else 0.2

T, RUN, H = 110, 200, 260          # arm thickness (horizontal), horizontal run of one arm, height
K = RUN / H                        # dx per dy along an arm
A = 34                             # horizontal gap between an arm and the triangle in its mouth
W = 4 * RUN + T                    # 910


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def pts(*p):
    return " ".join(f"{f(x)},{f(y)}" for x, y in p)


mouth_y = (2 * RUN - T) / (2 * K)              # where two arms meet inside an upper mouth
peak_y = T / (2 * K)                           # underside of the centre peak
tri_h = (2 * RUN - T - 2 * A) / (2 * K)        # height of a nested triangle

w = pts((0, 0), (T, 0), (RUN + T / 2, mouth_y), (2 * RUN, 0), (2 * RUN + T, 0), (3 * RUN + T / 2, mouth_y), (4 * RUN, 0),
        (W, 0), (3 * RUN + T, H), (3 * RUN, H), (2 * RUN + T / 2, peak_y), (RUN + T, H), (RUN, H))
tri_l = pts((T + A, 0), (2 * RUN - A, 0), (RUN + T / 2, tri_h))
tri_r = pts((2 * RUN + T + A, 0), (4 * RUN - A, 0), (3 * RUN + T / 2, tri_h))
tri_b = pts((RUN + T + A, H), (3 * RUN - A, H), (2 * RUN + T / 2, H - tri_h))

VW, VH = 1100, 520
ox, oy = (VW - W) / 2, 40


def svg(opacity):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VW} {VH}">
<defs>
<pattern id="raster" width="8" height="7" patternUnits="userSpaceOnUse"><rect width="8" height="5" fill="#fff"/></pattern>
<mask id="lines"><rect width="{VW}" height="{VH}" fill="url(#raster)"/></mask>
<filter id="glow" x="-10%" y="-20%" width="120%" height="140%"><feGaussianBlur stdDeviation="9"/></filter>
<g id="mark" transform="translate({f(ox)} {oy})">
<polygon points="{w}"/><polygon points="{tri_l}"/><polygon points="{tri_r}"/><polygon points="{tri_b}"/>
</g>
<g id="words" font-family="Consolas, 'Lucida Console', 'Courier New', monospace" font-weight="700" text-anchor="middle">
<text x="{VW / 2}" y="388" font-size="58" textLength="820" lengthAdjust="spacingAndGlyphs">WEYLAND-YUTANI CORP</text>
<text x="{VW / 2}" y="462" font-size="34" textLength="620" lengthAdjust="spacingAndGlyphs">BUILDING BETTER WORLDS</text>
</g>
</defs>
<g opacity="{opacity}" fill="#7CF29C">
<g filter="url(#glow)" opacity=".5"><use href="#mark"/><use href="#words"/></g>
<g mask="url(#lines)"><use href="#mark"/><use href="#words"/></g>
</g>
</svg>
"""


(out / "weyland.svg").write_text(svg(1), encoding="utf-8", newline="\n")
(out / "weyland-dim.svg").write_text(svg(dim), encoding="utf-8", newline="\n")
print("wrote", out / "weyland.svg", (out / "weyland.svg").stat().st_size, out / "weyland-dim.svg", (out / "weyland-dim.svg").stat().st_size)
