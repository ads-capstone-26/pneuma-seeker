# scripts/pneumarx_architecture.py
"""
Simplified PneumaRx architecture for slides (16:9, UChicago colours): the three-module
workflow from the revised workflow document, showing which parts are new in PneumaRx
and which existing Pneuma-Seeker parts are reused.

Usage (from the project root; needs matplotlib):
    python scripts/pneumarx_architecture.py
Output: docs/figures/pneumarx_architecture.png / .svg
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "docs" / "figures" / "pneumarx_architecture"
MAROON, LAKE, DARK_GREY, LIGHT_GREY, INK = "#800000", "#155F83", "#767676", "#D6D6CE", "#2B2B2B"


def box(ax, x, y, w, h, title, sub="", fill="white", colour=INK, outline=None, rounding=0.018, size=17):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={rounding}",
                                facecolor=fill, edgecolor=outline or "none", lw=2.4))
    ax.text(x + w / 2, y + h / 2 + (0.024 if sub else 0), title, ha="center", va="center",
            fontsize=size, weight="bold", color=colour, linespacing=1.15)
    if sub:
        ax.text(x + w / 2, y + h / 2 - 0.036, sub, ha="center", va="center", fontsize=11.5, color=colour,
                linespacing=1.2)


def arrow(ax, start, end, both=False, dashed=False, colour=DARK_GREY, rad=0.0):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="<|-|>" if both else "-|>", mutation_scale=20,
                                 color=colour, lw=2.2, linestyle=(0, (4, 3)) if dashed else "-",
                                 connectionstyle=f"arc3,rad={rad}", shrinkA=0, shrinkB=0))


def main() -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    fig = plt.figure(figsize=(13.33, 7.5), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Columns of the three modules
    cols = [0.165, 0.405, 0.645]
    w = 0.18
    top_y, top_h = 0.50, 0.22

    # Question in, answer out
    box(ax, 0.02, 0.555, 0.10, 0.11, "Question", fill=LIGHT_GREY, rounding=0.05, size=14)
    box(ax, 0.875, 0.555, 0.105, 0.11, "Answer", fill=LIGHT_GREY, rounding=0.05, size=14)

    # New in PneumaRx: data preparation (offline) and the three modules
    box(ax, cols[0], 0.80, w, 0.13, "Data preparation", "schema profile,\nquery conventions", MAROON, "white",
        size=15)
    modules = [("1  Task\nunderstanding", "IntentSpec"), ("2  Planning and\nexecution", "SQL plan and result"),
               ("3  Result review", "sanity check,\nexplanation")]
    for x, (title, sub) in zip(cols, modules):
        box(ax, x, top_y, w, top_h, title, sub, MAROON, "white", size=15.5)

    arrow(ax, (0.12, 0.61), (cols[0], 0.61))
    arrow(ax, (cols[0] + w, 0.61), (cols[1], 0.61))
    arrow(ax, (cols[1] + w, 0.61), (cols[2], 0.61))
    arrow(ax, (cols[2] + w, 0.61), (0.875, 0.61))
    arrow(ax, (cols[0] + w / 2, 0.80), (cols[0] + w / 2, top_y + top_h))
    # One information adjustment from planning back to task understanding
    arrow(ax, (cols[1] + 0.03, top_y + top_h), (cols[0] + w - 0.03, top_y + top_h), colour=MAROON, rad=0.6)
    ax.text(cols[1] + 0.045, 0.77, "adjust at most once", ha="left", va="center", fontsize=10.5, color=MAROON)

    # Existing Pneuma-Seeker parts, reused
    reused = [(cols[0], "Memory · Retriever"), (cols[1], "SQL execution\nand feedback")]
    for x, title in reused:
        box(ax, x, 0.30, w, 0.12, title, fill=LAKE, colour="white", size=14)
        arrow(ax, (x + w / 2, top_y), (x + w / 2, 0.42), both=True, dashed=True)

    # Existing supporting services
    box(ax, cols[0], 0.07, 0.30, 0.13, "Data", "DuckDB datasets and workspaces", outline=LIGHT_GREY, size=15)
    box(ax, cols[0] + 0.36, 0.07, 0.30, 0.13, "Language models", "OpenAI · Azure OpenAI", outline=LIGHT_GREY, size=15)

    # Key
    key = [(MAROON, None, "New in PneumaRx"), (LAKE, None, "Existing Pneuma-Seeker, reused"),
           ("white", LIGHT_GREY, "Existing supporting service")]
    kx, ky = 0.70, 0.915
    for fill, outline, label in key:
        ax.add_patch(FancyBboxPatch((kx, ky - 0.016), 0.022, 0.032, boxstyle="round,pad=0,rounding_size=0.006",
                                    facecolor=fill, edgecolor=outline or "none", lw=2))
        ax.text(kx + 0.033, ky, label, va="center", fontsize=12, color=INK)
        ky -= 0.05

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{OUT}.png", facecolor="white")
    fig.savefig(f"{OUT}.svg", facecolor="white")
    print(f"Figure -> {OUT}.png / .svg")


if __name__ == "__main__":
    main()
