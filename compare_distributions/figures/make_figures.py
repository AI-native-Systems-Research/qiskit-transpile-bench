#!/usr/bin/env python3
"""Generate the explanatory SVG figures referenced by ../benchmark_update.md.

Run from the repository root:  python3 compare_distributions/figures/make_figures.py
Each figure is a self-contained SVG with an opaque light surface, so it renders
identically inside <img> tags in light and dark viewers (GitHub, PyCharm).
Filenames are stable asset identifiers; captions in the design document set figure numbers.
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
def fig3_order_statistics():
    f = Fig(960, 235, "Sorted observations by rank: the nearest-rank P90 estimate is the "
                      "90th observed value x_(90) among 100 sorted results.")
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
    big = (85, 90, 95, 100)
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
         ["Weighted geometric mean", "Case mean ratios + analytic CI"]),
        (275, "(b) Upper-tail comparison", "P90(W), P90(g)", "P90(W) / P90(g)",
         ["Weighted geometric mean", "of case P90 ratios"]),
        (395, "(c) Relative-spread comparison", "CV(W), CV(g)", "100 * (CV(W) - CV(g))",
         ["Weighted mean across cases", "One CV difference (pp)"]),
    ]
    for y, title, summaries, contrast, aggregate in lanes:
        f.text(50, y - 12, title, size=13, weight="bold")
        f.box(50, y, 250, 55, ["Separate arm summaries", summaries], size=12)
        f.line(300, y + 27, 340, y + 27, arrow="ink")
        f.box(342, y, 250, 55, ["One contrast per case", contrast], size=12)
        f.line(592, y + 27, 632, y + 27, arrow="ink")
        f.box(634, y, 276, 55, aggregate, size=12)
    f.lines(50, 494, ["CV = SD / own mean. IQR uses IQR / own median; median uses case ratios.",
                      "Only mean comparisons have confidence intervals. No per-run comparisons or rank matching."],
            size=12, lh=19, fill=INK2)
    f.save("fig4_three_estimators.svg")


def fig5_interval_reading():
    f = Fig(960, 330, "Reading a mean-ratio confidence interval against the no-change reference: entirely below favors "
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
    f.text(30, 30, "Illustrative mean-ratio intervals only; no measured results.", size=11, fill=INK2, italic=True)
    f.save("fig5_interval_reading.svg")


# --------------------------------------------------------------------------------------
def fig6_mean_confidence():
    f = Fig(960, 620, "Analytic confidence intervals for the mean ratio: use each independent arm's "
                      "sample mean and variance, propagate uncertainty on the log scale, combine "
                      "case variances with squared fixed weights, and exponentiate the interval.")
    f.text(480, 30, "Analytic uncertainty for one mean comparison", size=16,
           anchor="middle", weight="bold")
    f.text(480, 52, "Apply separately to D2 and N2, and to each of the three comparators", size=12,
           anchor="middle", fill=INK2)
    for x, arm, suffix, accent, fill in (
        (60, "wait_time", "W", BLUE, BLUE_TINT),
        (520, "comparator: decay, lookahead, or basic", "g", ORANGE, BG),
    ):
        f.box(x, 76, 380, 94,
              [arm, "n = 100; sample mean m; sample SD s",
               f"v_{suffix} = s_{suffix}^2 / (100 * m_{suffix}^2)"],
              size=12, lh=23, stroke=accent, fill=fill)
        f.line(x + 190, 170, x + 190, 190)
    f.line(250, 190, 710, 190)
    f.line(480, 190, 480, 214, arrow="ink")
    f.box(100, 216, 760, 76,
          ["Within each case: independent arm estimates",
           "ell_c = log(m_W / m_g)",
           "SE_case = sqrt(v_W + v_g)"], size=13, lh=20)
    f.line(480, 292, 480, 318, arrow="ink")
    f.box(100, 320, 760, 86,
          ["Across the fixed panel: use the frozen case weights w_c",
           "L = sum_c w_c * ell_c",
           "SE_panel = sqrt(sum_c w_c^2 * (v_W,c + v_g,c))"], size=13, lh=22)
    f.line(480, 406, 480, 432, arrow="ink")
    f.box(100, 434, 760, 82,
          ["Mean-ratio estimate R = exp(L)",
           "Ratio CI = [exp(L - z * SE_panel), exp(L + z * SE_panel)]",
           "Convert each endpoint to percentage change with 100 * (ratio - 1)"],
          size=12.5, lh=22, stroke=BLUE, fill=BLUE_TINT)
    f.lines(480, 546,
            ["Pointwise 95%: z = 1.960     |     Six-effect adjustment: z = 2.638",
             "Approximate intervals; require positive means and independent case/arm samples.",
             "The three contrasts share the same wait-time estimates and uncertainty."],
            size=12, lh=23, anchor="middle", fill=INK2)
    f.save("fig6_mean_confidence.svg")


if __name__ == "__main__":
    fig1_arms_and_contrasts()
    fig3_order_statistics()
    fig4_three_estimators()
    fig5_interval_reading()
    fig6_mean_confidence()
