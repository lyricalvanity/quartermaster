"""Draws the two Raven backdrops for Quartermaster. Usage: python make_backdrops.py <out-dir>
cortex.svg  - a formation of amber chamfered slabs cut by a diagonal channel (Global Cortex).
concord.svg - a honeycomb cluster with nerve traces running out of it (Nerves Concord).
Original drawings in the manner of the two emblems; nothing traced."""
import json, math, random, sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
W, H = 1600, 1000


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- cortex
def slab(x0, x1, y, h, c):
    return f"M{f(x0 + c)},{f(y)}H{f(x1)}V{f(y + h - c)}L{f(x1 - c)},{f(y + h)}H{f(x0)}V{f(y + c)}Z"


# 2026-11-05, at the owner's eye: mirror the formation, then turn it a quarter, so its channel runs as the emblem's does.
# A mirror followed by a quarter turn is a flip across a diagonal; the two turn directions differ by a half turn.
# Then (his markup, same night): a further turn to the right. 30 degrees laid the channel just past level;
# 17.4 lays it level, parallel to the top and bottom of the screen, which is what he settled on.
ORIENT = {"none": "", "ccw": " matrix(0 1 1 0 0 0)", "cw": " matrix(0 -1 -1 0 0 0)"}


def cortex(opacity, orient="ccw", turn=17.4, scale=0.85):
    h, gap, c = 84, 20, 28
    pitch = h + gap
    half = [250, 430, 590, 690, 590, 430, 250]          # the diamond
    rows = []
    for i, hw in enumerate(half):
        k = i - 3
        y = k * pitch - h / 2
        lean = k * 34                                   # each row steps sideways: a staggered stack
        g = -k * 78 + 20                                # the channel runs the other diagonal
        rows.append((y, -hw + lean, g - 13, g + 13, hw + lean))
    fills, hi, lo = [], [], []
    for y, a, g0, g1, b in rows:
        for x0, x1 in ((a, g0), (g1, b)):
            if x1 - x0 < 2 * c + 20:
                continue
            fills.append(slab(x0, x1, y, h, c))
            hi.append(f"M{f(x0 + 2)},{f(y + h - 2)}V{f(y + c)}L{f(x0 + c)},{f(y + 2)}H{f(x1 - 2)}")
            lo.append(f"M{f(x1 - 2)},{f(y + 2)}V{f(y + h - c)}L{f(x1 - c)},{f(y + h - 2)}H{f(x0 + 2)}")
    # ghosts: the formation carries on, drawn in outline only
    ghosts = []
    for k, x0, x1 in ((-5, -40, 420), (-4, -260, 330), (-4, 370, 620), (4, -560, -330), (4, -290, 340), (5, -380, 60),
                      (-2, 720, 1010), (-1, 800, 1180), (0, 790, 960), (1, -1150, -720), (2, -1010, -790), (0, -980, -760)):
        ghosts.append(slab(x0 + k * 34, x1 + k * 34, k * pitch - h / 2, h, c))
    # hairlines running with the slabs out to the edges
    lines = []
    for k, x0, x1 in ((-6, -900, 1300), (-3, -1400, -520), (3, 660, 1400), (6, -1300, 700), (-1, -1500, -1220), (1, 1260, 1500)):
        y = k * pitch - h / 2 - gap / 2
        lines.append(f"M{f(x0)},{f(y)}H{f(x1)}")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">
<defs>
<radialGradient id="spot"><stop offset="0" stop-color="#fff" stop-opacity=".16"/><stop offset=".55" stop-color="#fff" stop-opacity=".05"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
<linearGradient id="amber" gradientUnits="userSpaceOnUse" x1="-520" y1="-380" x2="520" y2="380">
<stop offset="0" stop-color="#FFD83C"/><stop offset=".45" stop-color="#F7921E"/><stop offset="1" stop-color="#B03C0A"/></linearGradient>
</defs>
<g opacity="{opacity}">
<ellipse cx="800" cy="500" rx="780" ry="480" fill="url(#spot)"/>
<g transform="translate(800 500) rotate({f(turn)}) scale({scale}){ORIENT[orient]} rotate(-33) skewX(-24)">
<path d="{' '.join(lines)}" fill="none" stroke="#F7921E" stroke-width="1.5" opacity=".45"/>
<path d="{' '.join(ghosts)}" fill="#F7921E" fill-opacity=".05" stroke="#F7A63A" stroke-width="2" opacity=".5"/>
<path d="{' '.join(fills)}" fill="#1a0500" opacity=".7" transform="translate(7 11)"/>
<path d="{' '.join(fills)}" fill="url(#amber)"/>
<path d="{' '.join(lo)}" fill="none" stroke="#5a1600" stroke-width="4" opacity=".75"/>
<path d="{' '.join(hi)}" fill="none" stroke="#FFF0B0" stroke-width="3" opacity=".8"/>
</g>
</g>
</svg>
"""


# ---------------------------------------------------------------- concord
CONCORD_CELLS = {}


def hexpath(cx, cy, r):
    pts = [(cx + r * math.cos(math.radians(60 * i)), cy + r * math.sin(math.radians(60 * i))) for i in range(6)]
    return "M" + "L".join(f"{f(x)},{f(y)}" for x, y in pts) + "Z"


def concord(opacity, seed):
    rnd = random.Random(seed)
    R = 58
    cx0, cy0 = W / 2, H / 2

    def pos(q, r):
        return cx0 + 1.5 * R * q, cy0 + math.sqrt(3) * R * (r + q / 2)

    def dist(q, r):
        return max(abs(q), abs(r), abs(q + r))

    cells = {}
    for q in range(-9, 10):
        for r in range(-9, 10):
            d = dist(q, r)
            x, y = pos(q, r)
            if not (-R < x < W + R and -R < y < H + R):
                continue
            # the cluster is lopsided: it spreads further along one diagonal
            reach = 2.2 + 2.6 * max(0, math.cos(math.atan2(y - cy0, x - cx0) - math.radians(-35))) ** 2 \
                        + 2.0 * max(0, math.cos(math.atan2(y - cy0, x - cx0) - math.radians(150))) ** 2
            p = 1.0 if d <= 1 else max(0.0, 1.3 - d / (reach + 2.2))
            if rnd.random() < p:
                kind = rnd.choices(("bright", "dark", "line"), (5, 4, 2) if d < 3 else (2, 3, 4))[0]
                cells[(q, r)] = (kind, max(0.3, 1 - 0.13 * d))
    bright, dark, line = [], [], []
    CONCORD_CELLS.clear()
    CONCORD_CELLS.update(w=W, h=H, r=R - 5, cells=[[round(v, 1) for v in pos(q, r)] for q, r in cells])
    for (q, r), (kind, a) in cells.items():
        x, y = pos(q, r)
        if kind == "bright":
            bright.append(f'<g opacity="{f(a)}"><path d="{hexpath(x, y, R - 5)}" fill="url(#cell)" stroke="#A8F2FF" stroke-width="1.5"/>'
                          f'<path d="{hexpath(x, y - 3, R - 16)}" fill="url(#gloss)"/></g>')
        elif kind == "dark":
            dark.append(f'<g opacity="{f(a)}"><path d="{hexpath(x, y, R - 5)}" fill="#0A4A62" stroke="#2BA6C4" stroke-width="2"/>'
                        f'<path d="{hexpath(x, y, R - 15)}" fill="none" stroke="#1E86A4" stroke-width="2"/></g>')
        else:
            line.append(f'<path d="{hexpath(x, y, R - 5)}" opacity="{f(a * .8)}"/>')
    # nerve traces: leave an outer cell and run along the lattice directions to a terminal
    dirs = [(1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1)]
    traces, nodes = [], []
    outer = [c for c in cells if sum((c[0] + dq, c[1] + dr) in cells for dq, dr in dirs) <= 3]
    rnd.shuffle(outer)
    for q, r in outer[:16]:
        x, y = pos(q, r)
        ang = math.atan2(y - cy0, x - cx0)
        heading = round(ang / (math.pi / 3)) * 60 + 30 * rnd.choice((-1, 1))   # edge-normal directions: 30, 90, 150...
        pts = [(x, y)]
        for leg in range(rnd.choice((2, 2, 3))):
            ln = rnd.choice((90, 140, 200, 280))
            px, py = pts[-1]
            pts.append((px + ln * math.cos(math.radians(heading)), py + ln * math.sin(math.radians(heading))))
            heading += 60 * rnd.choice((-1, 1))
        traces.append("M" + "L".join(f"{f(a)},{f(b)}" for a, b in pts))
        nodes.append(f'<circle cx="{f(pts[-1][0])}" cy="{f(pts[-1][1])}" r="5"/>')
        nodes.append(f'<circle cx="{f(pts[1][0])}" cy="{f(pts[1][1])}" r="2.5"/>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">
<defs>
<radialGradient id="spot"><stop offset="0" stop-color="#5FE0FF" stop-opacity=".16"/><stop offset=".6" stop-color="#5FE0FF" stop-opacity=".04"/><stop offset="1" stop-color="#5FE0FF" stop-opacity="0"/></radialGradient>
<radialGradient id="cell" cx=".5" cy=".42" r=".62"><stop offset="0" stop-color="#8CEBFF"/><stop offset=".55" stop-color="#2FB4DC"/><stop offset="1" stop-color="#127AA4"/></radialGradient>
<linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".30"/><stop offset=".6" stop-color="#fff" stop-opacity="0"/></linearGradient>
</defs>
<g opacity="{opacity}">
<ellipse cx="800" cy="500" rx="780" ry="480" fill="url(#spot)"/>
<path d="{' '.join(traces)}" fill="none" stroke="#3FC8E4" stroke-width="1.5" stroke-linejoin="round" opacity=".55"/>
<g fill="#3FC8E4" opacity=".7">{''.join(nodes)}</g>
<g fill="none" stroke="#2BA6C4" stroke-width="1.5">{''.join(line)}</g>
{''.join(dark)}
{''.join(bright)}
</g>
</svg>
"""


op_cortex = float(sys.argv[2]) if len(sys.argv) > 2 else 0.34
op_concord = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 8
orient = sys.argv[5] if len(sys.argv) > 5 else "ccw"
turn = float(sys.argv[6]) if len(sys.argv) > 6 else 17.4
(out / "cortex.svg").write_text(cortex(op_cortex, orient, turn), encoding="utf-8", newline="\n")
(out / "concord.svg").write_text(concord(op_concord, seed), encoding="utf-8", newline="\n")
# where the cells are, for the page's pulse lamps (app.js pulseSync): viewBox size, cell radius, cell centres
(out / "concord-cells.json").write_text(json.dumps(CONCORD_CELLS, separators=(",", ":")), encoding="utf-8", newline="\n")
print("wrote", out / "cortex.svg", (out / "cortex.svg").stat().st_size, out / "concord.svg", (out / "concord.svg").stat().st_size)
