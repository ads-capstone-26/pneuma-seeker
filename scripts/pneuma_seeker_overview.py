# scripts/pneuma_seeker_overview.py
"""
Simplified Pneuma-Seeker architecture for slides (16:9, UChicago colours).

A reduced version of docs/figures/pneuma_seeker.png: the Conductor plans and
keeps the state, the Retriever finds tables, the Materializer computes, and two
supporting services provide data and language models.

Usage (from the project root; needs matplotlib):
    python scripts/pneuma_seeker_overview.py
Output: docs/figures/pneuma_seeker_overview.png / .svg
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "docs" / "figures" / "pneuma_seeker_overview"
MAROON, LAKE, GOLDENROD, DARK_GREY, LIGHT_GREY, INK = "#800000", "#155F83", "#FFA319", "#767676", "#D6D6CE", "#2B2B2B"


def box(ax, x, y, w, h, title, sub, fill, colour, rounding=0.02, title_size=22):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={rounding}",
                                facecolor=fill, edgecolor="none"))
    ax.text(x + w / 2, y + h / 2 + (0.03 if sub else 0), title, ha="center", va="center",
            fontsize=title_size, weight="bold", color=colour)
    if sub:
        ax.text(x + w / 2, y + h / 2 - 0.035, sub, ha="center", va="center", fontsize=13, color=colour)


def arrow(ax, start, end, both=False, dashed=False, colour=DARK_GREY):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="<|-|>" if both else "-|>", mutation_scale=22,
                                 color=colour, lw=2.4, linestyle=(0, (4, 3)) if dashed else "-",
                                 shrinkA=0, shrinkB=0))


def main() -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    fig = plt.figure(figsize=(13.33, 7.5), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Question in, answer out
    box(ax, 0.04, 0.68, 0.17, 0.13, "Question", "", LIGHT_GREY, INK, rounding=0.05, title_size=18)
    box(ax, 0.04, 0.42, 0.17, 0.13, "Answer", "", LIGHT_GREY, INK, rounding=0.05, title_size=18)

    # Core: conductor and its two helpers
    box(ax, 0.29, 0.38, 0.24, 0.47, "Conductor", "plans and tracks\nthe target tables", MAROON, "white")
    box(ax, 0.64, 0.65, 0.31, 0.20, "Retriever", "finds tables and context", LAKE, "white")
    box(ax, 0.64, 0.38, 0.31, 0.20, "Materializer", "combines tables, runs SQL", GOLDENROD, INK)

    arrow(ax, (0.21, 0.745), (0.29, 0.745))
    arrow(ax, (0.29, 0.485), (0.21, 0.485))
    arrow(ax, (0.53, 0.75), (0.64, 0.75), both=True)
    arrow(ax, (0.53, 0.48), (0.64, 0.48), both=True)

    # Supporting services
    box(ax, 0.29, 0.08, 0.24, 0.15, "Data", "datasets and workspaces", "white", INK, title_size=18)
    box(ax, 0.64, 0.08, 0.31, 0.15, "Language models", "OpenAI · Azure OpenAI", "white", INK, title_size=18)
    for x, w in ((0.29, 0.24), (0.64, 0.31)):
        ax.add_patch(FancyBboxPatch((x, 0.08), w, 0.15, boxstyle="round,pad=0,rounding_size=0.02",
                                    facecolor="none", edgecolor=LIGHT_GREY, lw=2.4))
    arrow(ax, (0.41, 0.38), (0.41, 0.23), both=True, dashed=True)
    arrow(ax, (0.795, 0.38), (0.795, 0.23), both=True, dashed=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{OUT}.png", facecolor="white")
    fig.savefig(f"{OUT}.svg", facecolor="white")
    print(f"Figure -> {OUT}.png / .svg")


if __name__ == "__main__":
    main()
