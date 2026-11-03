#!/usr/bin/env python3
"""Generate the explanatory SVG figures referenced by ../benchmark_update.md.

Run from anywhere:  python3 design/figures/make_figures.py
Each figure is a self-contained SVG with an opaque light surface, so it renders
identically inside <img> tags in light and dark viewers (GitHub, PyCharm).
Figure numbers follow their order of appearance in the design document.
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent

# Palette: light-mode reference instance; pairs that share a figure were validated.
BG = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
LINE = "#b8b7b1"
GRID = "#e4e3df"
BLUE = "#2a78d6"      # wait_time / first arm / accent
BLUE_TINT = "#cde2fb"
ORANGE = "#eb6834"    # comparator arm, or the one component that changes kind
RED = "#e34948"       # "do not do this" marks, always with a text label

SANS = "Helvetica Neue, Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Menlo, Consolas, monospace"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Fig:
    def __init__(self, w, h, label):
        self.w, self.h, self.label = w, h, label
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def rect(self, x, y, w, h, fill=BG, stroke=INK2, sw=1.2, rx=4, dash=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{extra}/>')

    def text(self, x, y, s, size=12, anchor="start", fill=INK, weight=None, mono=False, italic=False):
        fam = MONO if mono else SANS
        extra = f' font-weight="{weight}"' if weight else ""
        extra += ' font-style="italic"' if italic else ""
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" '
                 f'text-anchor="{anchor}" fill="{fill}"{extra}>{esc(s)}</text>')

    def lines(self, x, y, rows, size=12, lh=15, anchor="start", fill=INK, mono=False, weight=None):
        fam = MONO if mono else SANS
        extra = f' font-weight="{weight}"' if weight else ""
        spans = "".join(
            f'<tspan x="{x}" dy="{0 if i == 0 else lh}">{esc(r)}</tspan>' for i, r in enumerate(rows))
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" '
                 f'text-anchor="{anchor}" fill="{fill}"{extra}>{spans}</text>')

    def line(self, x1, y1, x2, y2, stroke=INK2, sw=1.2, dash=None, arrow=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        extra += f' marker-end="url(#arrow-{arrow})"' if arrow else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
                 f'stroke-width="{sw}"{extra}/>')

    def path(self, d, stroke=INK2, sw=1.2, fill="none", dash=None, arrow=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        extra += f' marker-end="url(#arrow-{arrow})"' if arrow else ""
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')

    def curve(self, x0, y0, x1, y1, stroke=INK2, sw=1.2, arrow=None):
        ym = (y0 + y1) / 2
        self.path(f"M{x0},{y0} C{x0},{ym} {x1 - 50},{y1} {x1},{y1}", stroke=stroke, sw=sw, arrow=arrow)

    def box(self, x, y, w, h, rows, size=12, lh=15, fill=BG, stroke=INK2, sw=1.2, mono=False,
            weight=None, color=INK, dash=None, rx=4):
        self.rect(x, y, w, h, fill=fill, stroke=stroke, sw=sw, dash=dash, rx=rx)
        total = lh * (len(rows) - 1)
        y0 = y + h / 2 - total / 2 + size * 0.36
        self.lines(x + w / 2, y0, rows, size=size, lh=lh, anchor="middle", fill=color, mono=mono,
                   weight=weight)

    def save(self, name):
        markers = "".join(
            f'<marker id="arrow-{k}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" '
            f'orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,4.5 L0,9 z" fill="{c}"/></marker>'
            for k, c in (("ink", INK2), ("blue", BLUE), ("orange", ORANGE), ("red", RED)))
        body = "\n".join(self.parts)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
               f'width="{self.w}" height="{self.h}" role="img" aria-label="{esc(self.label)}">\n'
               f'<title>{esc(self.label)}</title>\n<defs>{markers}</defs>\n'
               f'<rect x="0" y="0" width="{self.w}" height="{self.h}" fill="{BG}"/>\n{body}\n</svg>\n')
        (OUT / name).write_text(svg)
        print("wrote", OUT / name)


# --------------------------------------------------------------------------------------
def fig1_arms_and_contrasts():
    f = Fig(920, 480, "Four presets as stacked components and the three contrasts that all start "
                      "from wait_time; only wait_time versus decay differs by a single component.")
    arms = [
        ("wait_time", ["Basic, Constant scaling", "Lookahead, 0.5 / Q", "Decay, 0.001 / reset 5",
                       "Wait-time, weight 2.2e-5"]),
        ("decay", ["Basic, Constant scaling", "Lookahead, 0.5 / Q", "Decay, 0.001 / reset 5", None]),
        ("lookahead", ["Basic, Constant scaling", "Lookahead, 0.5 / Q", None, None]),
        ("basic", ["Basic, Size scaling", None, None, None]),
    ]
    differs = ["", "wait-time term", "decay + wait-time", "basic scaling + lookahead\n+ decay + wait-time"]
    x0, top, cw, gap, hh, rh = 35, 185, 190, 30, 34, 44
    card_h = hh + 4 * rh + 8
    centers = []
    for i, (name, comps) in enumerate(arms):
        x = x0 + i * (cw + gap)
        centers.append(x + cw / 2)
        f.rect(x, top, cw, card_h, fill=BLUE_TINT if i == 0 else BG, stroke=BLUE if i == 0 else INK2,
               sw=1.6 if i == 0 else 1.2)
        f.text(x + cw / 2, top + 22, name, size=13, anchor="middle", mono=True, weight="bold",
               fill=BLUE if i == 0 else INK)
        for r in range(4):                      # rows from top (wait-time) to bottom (basic)
            comp = comps[3 - r]
            ry = top + hh + r * rh + 4
            if comp is None:
                f.rect(x + 10, ry, cw - 20, rh - 8, fill=BG, stroke=LINE, dash="4 3")
                f.text(x + cw / 2, ry + rh / 2 + 1, "none", size=11, anchor="middle", fill=INK2, italic=True)
                continue
            accent = comp.startswith("Wait-time")
            changed = "Size" in comp
            f.rect(x + 10, ry, cw - 20, rh - 8, fill=BLUE if accent else BG,
                   stroke=BLUE if accent else (ORANGE if changed else INK2),
                   sw=1.6 if (accent or changed) else 1.2)
            f.text(x + cw / 2, ry + rh / 2 + 1, comp, size=11, anchor="middle",
                   fill="#ffffff" if accent else INK)
        if differs[i]:
            f.lines(x + cw / 2, top + card_h + 20, ["differs from wait_time by:"] + differs[i].split("\n"),
                    size=11, lh=14, anchor="middle", fill=INK2)
    ids = ["wait_time_vs_decay", "wait_time_vs_lookahead", "wait_time_vs_basic"]
    heights = [50, 92, 134]
    for k in range(3):
        xa, xb = centers[0] + 20 * k, centers[k + 1]
        ctrl = top - heights[k]
        f.path(f"M{xa},{top - 2} C{xa},{ctrl} {xb},{ctrl} {xb},{top - 2}", stroke=BLUE, sw=1.6, arrow="blue")
        apex = 0.25 * (top - 2) + 0.75 * ctrl       # highest point of the cubic
        f.text((xa + xb) / 2, apex - 10, ids[k], size=12, anchor="middle", mono=True, fill=BLUE)
    f.text(x0, 24, "Every contrast is wait_time / comparator (ratio) or wait_time - comparator (difference).",
           size=12, fill=INK)
    f.save("fig1_arms_and_contrasts.svg")


# --------------------------------------------------------------------------------------
def fig2_matched_routing():
    f = Fig(960, 650, "Controlled routing: one fixed prepared input and applied layout per case, "
                      "100 independent routing seeds per arm, and identical downstream passes.")
    f.text(480, 30, "One fixed case; four independently sampled routing distributions", size=16,
           anchor="middle", weight="bold")
    f.box(180, 52, 600, 52, ["Freeze circuit, target, optimization level, and preparation policy",
                            "Preparation must leave routing work for the selected preset"], size=13)
    f.line(480, 104, 480, 126, arrow="ink")
    f.box(180, 128, 600, 64, ["SabreLayout(skip_routing=True, seed=LAYOUT_SEED)",
                             "Fixed layout trials, swap trials, and max_iterations",
                             "FullAncillaAllocation → EnlargeWithAncilla → ApplyLayout"], size=12)
    f.line(480, 192, 480, 216)
    f.line(135, 216, 825, 216)
    for x, arm in zip((30, 260, 490, 720), ("wait_time", "decay", "lookahead", "basic")):
        accent = BLUE if arm == "wait_time" else INK2
        f.line(x + 105, 216, x + 105, 246, arrow="ink")
        f.box(x, 248, 210, 96, [arm, "100 own routing seeds", "SabreSwap; fixed trials=T",
                               "Fresh copy of fixed input"], size=12, lh=19,
              stroke=accent, fill=BLUE_TINT if arm == "wait_time" else BG)
        f.line(x + 105, 344, x + 105, 374)
    f.line(135, 374, 825, 374)
    f.line(480, 374, 480, 396, arrow="ink")
    f.box(180, 398, 600, 50, ["Identical downstream translation and optimization",
                             "Measure native D2/N2 and retain routing diagnostics"], size=13)
    f.line(480, 448, 480, 470, arrow="ink")
    f.box(180, 472, 600, 54, ["100 observations per arm; summarize each distribution separately",
                             "Shared conditions, independent samples; no seed matching"], size=13)
    f.box(80, 554, 800, 66, ["Activation check: inspect the actual pass trace",
                            "Default SabreLayout may route internally, causing CheckMap to skip routing.",
                            "Replacing only pm.routing can leave the selected preset with no work."],
          size=12, lh=18, stroke=RED, color=RED)
    f.save("fig2_matched_routing.svg")


def fig3_order_statistics():
    f = Fig(960, 280, "Sorted observations by rank: the nearest-rank P90 estimate is x_(90), "
                      "and the conservative P90 interval is [x_(84), x_(96)].")
    ax_y = 190
    f.text(40, 30, "100 sorted D2 observations of one case and arm; tick labels are the ranks r of x_(r), "
                   "ranks 81..100 expanded", size=12, fill=INK)
    lx0 = 40
    f.line(lx0, ax_y, lx0 + 140, ax_y, stroke=INK2, sw=1.4)
    for i, r in enumerate([1, 2, 3]):
        x = lx0 + i * 40
        f.line(x, ax_y - 6, x, ax_y + 6, stroke=INK2)
        f.text(x, ax_y + 22, str(r), size=10.5, anchor="middle", mono=True, fill=INK2)
    f.text(lx0 + 150, ax_y + 5, "...", size=14, anchor="middle", fill=INK2)
    bx = lx0 + 175
    f.path(f"M{bx - 4},{ax_y + 8} L{bx + 4},{ax_y - 8}", stroke=INK2, sw=1.4)
    f.path(f"M{bx + 5},{ax_y + 8} L{bx + 13},{ax_y - 8}", stroke=INK2, sw=1.4)
    ex0, step = 215, 32
    rank_x = lambda r: ex0 + (r - 81) * step
    f.line(ex0 - 10, ax_y, rank_x(100) + 50, ax_y, stroke=INK2, sw=1.4, arrow="ink")
    big = (84, 90, 96, 100)
    for r in range(81, 101):
        x = rank_x(r)
        f.line(x, ax_y - (8 if r in big else 4), x, ax_y + (8 if r in big else 4),
               stroke=INK if r in big else LINE, sw=1.4 if r in big else 1)
        if r in big:
            f.text(x, ax_y + 22, str(r), size=10.5, anchor="middle", mono=True, fill=INK)
    f.text(rank_x(100) + 56, ax_y + 4, "rank", size=11, fill=INK2)
    # P90 estimate
    p90x = rank_x(90)
    f.line(p90x, ax_y - 52, p90x, ax_y - 10, stroke=BLUE, sw=1.8, arrow="blue")
    f.rect(p90x - 4, ax_y - 4, 8, 8, fill=BLUE, stroke=BLUE, rx=4)
    f.lines(p90x, ax_y - 72, ["P90 = x_(90)", "nearest rank: ceil(100 x 0.90) = 90"],
            size=11, lh=14, anchor="middle", fill=BLUE)
    # P90 rank interval
    by = ax_y + 52
    f.path(f"M{rank_x(84)},{by - 8} L{rank_x(84)},{by} L{rank_x(96)},{by} L{rank_x(96)},{by - 8}",
           stroke=INK, sw=1.6)
    f.text((rank_x(84) + rank_x(96)) / 2, by + 16,
           "conservative P90 interval [x_(84), x_(96)]: coverage 95.6% from Binomial(100, 0.90)", size=11,
           anchor="middle", fill=INK)
    f.save("fig3_order_statistics.svg")


# --------------------------------------------------------------------------------------
def fig4_three_estimators():
    f = Fig(960, 540, "Summarize each independent arm distribution, then compare arithmetic means, "
                      "P90s, and independently normalized SDs. No individual observations are matched.")
    f.text(480, 30, "Summarize each distribution first; compare its summaries", size=16,
           anchor="middle", weight="bold")
    f.box(50, 54, 390, 55, ["wait_time: 100 observations", "Independent routing seed list"],
          size=13, stroke=BLUE, fill=BLUE_TINT)
    f.box(520, 54, 390, 55, ["comparator: 100 observations", "Independent routing seed list"],
          size=13, stroke=ORANGE)
    lanes = [
        (155, "(a) Primary mean comparison", "mean(W), mean(g)", "mean(W) / mean(g)",
         ["Weighted geometric mean", "of case mean ratios"]),
        (275, "(b) Upper-tail comparison", "P90(W), P90(g)", "P90(W) / P90(g)",
         ["Weighted geometric mean", "of case P90 ratios"]),
        (395, "(c) Relative-spread comparison", "CV(W), CV(g)", "100 * (CV(W) - CV(g))",
         ["Weighted mean across cases", "One CV difference + CI (pp)"]),
    ]
    for y, title, summaries, contrast, aggregate in lanes:
        f.text(50, y - 12, title, size=13, weight="bold")
        f.box(50, y, 250, 55, ["Separate arm summaries", summaries], size=12)
        f.line(300, y + 27, 340, y + 27, arrow="ink")
        f.box(342, y, 250, 55, ["One contrast per case", contrast], size=12)
        f.line(592, y + 27, 632, y + 27, arrow="ink")
        f.box(634, y, 276, 55, aggregate, size=12)
    f.lines(50, 494, ["CV = SD / own mean. IQR uses IQR / own median; median uses case ratios.",
                      "Each aggregate has a confidence interval. No per-run comparisons or rank matching."],
            size=12, lh=19, fill=INK2)
    f.save("fig4_three_estimators.svg")


def fig5_interval_reading():
    f = Fig(960, 330, "Reading a comparison interval against the no-change reference: entirely below favors "
                      "wait time, crossing is unresolved, entirely above is a regression; an adjusted "
                      "interval is wider than the pointwise one for the same estimate.")
    ax0, ax1, y0 = 300, 600, 60
    lo, hi = -12.0, 8.0
    px = lambda v: ax0 + (v - lo) / (hi - lo) * (ax1 - ax0)
    ay = 280
    f.line(ax0, ay, ax1, ay, stroke=INK2, sw=1.2)
    for v in range(-12, 9, 2):
        f.line(px(v), ay - 4, px(v), ay + 4, stroke=INK2)
        f.text(px(v), ay + 18, f"{v:+d}%" if v else "0", size=10.5, anchor="middle", fill=INK2)
    f.text((ax0 + ax1) / 2, ay + 36, "panel mean-ratio D2 change, wait_time versus the comparator: 100 * (ratio - 1)",
           size=11, anchor="middle", fill=INK2)
    f.line(px(0), y0 - 20, px(0), ay, stroke=INK, sw=1.4, dash="5 4")
    f.text(px(0), y0 - 28, "no change: 0% (ratio 1)", size=11, anchor="middle", fill=INK)
    rows = [
        ("-5%  [-8%, -2%]", -8, -2, -5, "entirely below: evidence for lower D2 with wait time", None),
        ("-1%  [-4%, +2%]", -4, 2, -1, "crosses zero: direction unresolved (not equivalence)", None),
        ("+3%  [+1%, +5%]", 1, 5, 3, "entirely above: evidence for a wait-time regression", None),
        ("-5%  two intervals", -8, -2, -5, "same estimate; the m = 6 adjusted interval is wider", (-9.3, -0.6)),
    ]
    for i, (lab, a, b, est, note, adj) in enumerate(rows):
        y = y0 + i * 52
        f.text(ax0 - 20, y + 4, lab, size=11.5, anchor="end", mono=True, fill=INK)
        if adj:
            f.line(px(adj[0]), y, px(adj[1]), y, stroke=BLUE, sw=1.2)
            f.line(px(adj[0]), y - 7, px(adj[0]), y + 7, stroke=BLUE, sw=1.2)
            f.line(px(adj[1]), y - 7, px(adj[1]), y + 7, stroke=BLUE, sw=1.2)
            f.text(px(est), y - 12, "pointwise 95%: [-8%, -2%]", size=10, anchor="middle", fill=INK)
            f.text(px(est), y + 22, "adjusted, m = 6: [-9.3%, -0.6%]", size=10, anchor="middle", fill=BLUE)
        f.line(px(a), y, px(b), y, stroke=INK, sw=2.2)
        f.line(px(a), y - 5, px(a), y + 5, stroke=INK, sw=2)
        f.line(px(b), y - 5, px(b), y + 5, stroke=INK, sw=2)
        f.rect(px(est) - 4.5, y - 4.5, 9, 9, fill=BLUE, stroke=BG, sw=1.5, rx=4.5)
        f.text(ax1 + 16, y + 4, note, size=10.5, fill=INK2)
    f.text(30, 30, "Illustrative intervals only; no measured results.", size=11, fill=INK2, italic=True)
    f.save("fig5_interval_reading.svg")


# --------------------------------------------------------------------------------------
def fig6_seed_vectors_bootstrap():
    f = Fig(960, 570, "Independent-arm bootstrap: independently resample 100 records within every "
                      "case and arm, keep D2/N2 together, then compare summaries and reuse the "
                      "wait-time resample for all three contrasts.")
    f.text(480, 30, "One bootstrap replicate, within one fixed case", size=16,
           anchor="middle", weight="bold")
    arms = ("wait_time", "decay", "lookahead", "basic")
    example_indices = ("17, 3, 3, 88, …", "8, 21, 9, 9, …", "34, 2, 55, 1, …", "12, 7, 7, 62, …")
    for x, arm, indices in zip((30, 260, 490, 720), arms, example_indices):
        acc = BLUE if arm == "wait_time" else INK2
        f.box(x, 62, 210, 50, [arm, "100 original records"], size=13, stroke=acc,
              fill=BLUE_TINT if arm == "wait_time" else BG)
        f.line(x + 105, 112, x + 105, 140, arrow="ink")
        f.box(x, 142, 210, 76, ["Own independent index draw", indices,
                               "100 records with replacement"], size=11, lh=18, stroke=acc)
        f.line(x + 105, 218, x + 105, 246, arrow="ink")
        f.box(x, 248, 210, 64, ["Recompute mean, median,", "SD, IQR, and P90"], size=12,
              stroke=acc, fill=BLUE_TINT if arm == "wait_time" else BG)
    f.text(480, 342, "Use the same resampled wait-time summaries in all three contrasts", size=13,
           anchor="middle", fill=BLUE, weight="bold")
    for x, comparator in zip((45, 355, 665), ("decay", "lookahead", "basic")):
        f.box(x, 362, 250, 58, [f"wait_time vs {comparator}",
                               "Mean/P90 ratios; spread changes"], size=12)
    f.box(160, 450, 640, 52, ["Repeat independently for every case; keep case weights fixed",
                             "Recompute panel statistics; take bootstrap interval endpoints"], size=12)
    f.lines(40, 532, ["Keep D2 and N2 from each selected record together. Never match records across arms.",
                      "The three contrasts share wait-time uncertainty; arms have independent sampling."],
            size=12, lh=19, fill=INK2)
    f.save("fig6_seed_vectors_bootstrap.svg")


def fig7_runtime_blocks():
    f = Fig(960, 355, "Runtime measurement: the current protocol times all seeds of one build and then the "
                      "other; the proposed protocol times balanced blocks that each contain all four arms "
                      "with arm-specific seeds, warm-up outside the timed region, and an A/A control.")
    f.text(30, 34, "Current quality timing, one case:", size=12, fill=INK, weight="bold")
    y = 50
    f.rect(30, y, 420, 30, fill=BG, stroke=INK2)
    f.text(240, y + 19, "build A: seeds 0, 1, 2, ..., 99 (all timed back to back)", size=11, anchor="middle")
    f.rect(470, y, 420, 30, fill=BG, stroke=INK2)
    f.text(680, y + 19, "build B: seeds 0, 1, 2, ..., 99", size=11, anchor="middle")
    f.line(30, y + 42, 890, y + 42, stroke=INK2, sw=1, arrow="ink")
    f.text(30, y + 58, "time; the order of A and B alternates only between cases, so drift and thermal state "
                       "line up with the build", size=10.5, fill=INK2)
    f.text(30, 150, "Proposed: balanced time blocks, one independent seed per arm in each block:", size=12,
           fill=INK, weight="bold")
    y, x = 168, 30
    f.rect(x, y, 70, 30, fill=BG, stroke=LINE, dash="4 3")
    f.text(x + 35, y + 19, "warm-up", size=10.5, anchor="middle", fill=INK2)
    f.text(x + 35, y + 44, "untimed", size=10, anchor="middle", fill=INK2)
    x += 84
    blocks = [("block 1", ["W", "D", "L", "B"]), ("block 2", ["D", "L", "B", "W"]),
              ("block 3", ["L", "B", "W", "D"]), ("block 4", ["B", "W", "D", "L"])]
    cellw = 30
    for name, order in blocks:
        f.rect(x - 3, y - 4, cellw * 4 + 6, 38, fill=BG, stroke=INK2, sw=1.2)
        for i, a in enumerate(order):
            acc = a == "W"
            f.rect(x + i * cellw + 1, y + 1, cellw - 2, 28, fill=BLUE if acc else BG,
                   stroke=BLUE if acc else LINE, sw=1, rx=2)
            f.text(x + i * cellw + cellw / 2, y + 19, a, size=12, anchor="middle", mono=True,
                   fill="#ffffff" if acc else INK)
        f.text(x + cellw * 2, y + 48, name, size=10.5, anchor="middle", fill=INK2)
        x += cellw * 4 + 20
    f.text(x + 6, y + 19, "...", size=14, fill=INK2)
    x += 34
    f.rect(x - 3, y - 4, cellw * 4 + 6, 38, fill=BG, stroke=INK2, sw=1.2)
    for i in range(4):
        f.rect(x + i * cellw + 1, y + 1, cellw - 2, 28, fill=BG, stroke=LINE, sw=1, rx=2)
        f.text(x + i * cellw + cellw / 2, y + 19, "D", size=12, anchor="middle", mono=True, fill=INK)
    f.text(x + cellw * 2, y + 48, "A/A control", size=10.5, anchor="middle", fill=INK2)
    x += cellw * 4 + 20
    f.text(x + 6, y + 19, "...", size=14, fill=INK2)
    f.line(30, y + 70, 890, y + 70, stroke=INK2, sw=1, arrow="ink")
    f.lines(30, y + 88, [
        "time; each arm sits once in every block position within each group of four blocks, and the order "
        "within that constraint is randomized.",
        "Repeat in independent sessions; each arm has its own 100-seed list. Compare arm means, not block ratios.",
        "Resample complete sessions across the fixed panel (section 7); runtime CIs condition on the frozen seed lists."], size=10.5, lh=15, fill=INK2)
    f.text(30, 333, "W = wait_time, D = decay, L = lookahead, B = basic", size=10.5, fill=INK2)
    f.save("fig7_runtime_blocks.svg")


if __name__ == "__main__":
    fig1_arms_and_contrasts()
    fig2_matched_routing()
    fig3_order_statistics()
    fig4_three_estimators()
    fig5_interval_reading()
    fig6_seed_vectors_bootstrap()
    fig7_runtime_blocks()
