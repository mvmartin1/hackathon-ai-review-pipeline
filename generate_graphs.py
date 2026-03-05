import json
import os
import subprocess
import sys
from calendar import month_name
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Colors
BLUE = "#4285F4"
RED = "#EA4335"
RED_DARK = "#DB4437"
YELLOW = "#FBBC04"
GREEN = "#34A853"
GRAY_TITLE = "#5f6368"
GRAY_GRID = "#e0e0e0"


def fmt_month(ym):
    """Convert 'YYYY-MM' to 'Month\\nYYYY'."""
    y, m = ym.split("-")
    return f"{month_name[int(m)]}\n{y}"


def fmt_month_short(ym):
    """Convert 'YYYY-MM' to 'Mon YYYY'."""
    y, m = ym.split("-")
    return f"{month_name[int(m)][:3]} {y}"


def style_ax(ax, title, use_suptitle=False):
    ax.set_facecolor("white")
    ax.figure.set_facecolor("white")
    if use_suptitle:
        ax.figure.suptitle(title, color=GRAY_TITLE, fontsize=11, fontweight="bold", y=0.98)
    else:
        ax.set_title(title, color=GRAY_TITLE, fontsize=11, fontweight="bold", pad=12)
    ax.grid(axis="y", color=GRAY_GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=GRAY_TITLE, labelsize=8)


def graph_1(data):
    """Monthly Review Volume + Average Rating."""
    months = data["months"]
    pos = data["monthly_pos_count"]
    neg = data["monthly_neg_count"]
    avg = data["monthly_avg_rating"]

    fig, ax1 = plt.subplots(figsize=(6.7, 4.7), dpi=200)
    style_ax(ax1, "Monthly Review Volume + Average Rating", use_suptitle=True)

    x = np.arange(len(months))
    ax1.bar(x, pos, color=BLUE, label="Positive", width=0.6)
    ax1.bar(x, neg, bottom=pos, color=RED, label="Negative", width=0.6)
    ax1.set_ylabel("Count of Reviews", color=GRAY_TITLE, fontsize=9)
    ax1.set_xlabel("Month", color=GRAY_TITLE, fontsize=9)
    ax1.set_xticks(x)
    ax1.set_xticklabels([fmt_month(m) for m in months], fontsize=7)
    ax2 = ax1.twinx()
    ax2.plot(x, avg, color=YELLOW, marker="o", linewidth=2, label="Avg Rating")
    ax2.set_ylabel("Average Rating", color=GRAY_TITLE, fontsize=9)
    ax2.set_ylim(0, 5.3)
    ax1.set_xlim(-0.5, len(months) - 0.5)
    for spine in ax2.spines.values():
        spine.set_visible(False)
    ax2.tick_params(colors=GRAY_TITLE, labelsize=8)

    for i, v in enumerate(avg):
        ax2.annotate(
            f"{v:.2f}", (x[i], v), textcoords="offset points",
            xytext=(0, 10), ha="center", fontsize=7, color=YELLOW, fontweight="bold",
        )

    # Combine legends and place above the plot area
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="lower center", bbox_to_anchor=(0.5, 1.01),
               ncol=3, fontsize=7, framealpha=0.9)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(os.path.join(SCRIPT_DIR, "graph_1.png"))
    plt.close(fig)
    print("Saved graph_1.png")


def graph_2(data):
    """iOS vs Google Play (Last 2 Months)."""
    months = data["months"][-2:]
    sb = data["source_breakdown"]

    ios_data = sb.get("iOS App Store", [])
    gp_data = sb.get("Google Play", [])

    ios_vols = [d["vol"] for d in ios_data]
    gp_vols = [d["vol"] for d in gp_data]
    ios_rats = [d["avg_rating"] for d in ios_data]
    gp_rats = [d["avg_rating"] for d in gp_data]

    fig, ax1 = plt.subplots(figsize=(6.7, 4.7), dpi=200)
    style_ax(ax1, "iOS vs Google Play (Last 2 Months)", use_suptitle=True)

    x = np.arange(len(months))
    w = 0.3
    bars_ios = ax1.bar(x - w / 2, ios_vols, w, color=BLUE, label="iOS")
    bars_gp = ax1.bar(x + w / 2, gp_vols, w, color=YELLOW, label="Google Play")
    ax1.set_ylabel("Review Count", color=GRAY_TITLE, fontsize=9)
    ax1.set_xticks(x)
    ax1.set_xticklabels([fmt_month(m) for m in months], fontsize=8)
    ax2 = ax1.twinx()
    ax2.plot(x, ios_rats, color=RED, marker="o", linewidth=2, label="iOS Avg Rating")
    ax2.plot(x, gp_rats, color=GREEN, marker="s", linewidth=2, label="GP Avg Rating")
    ax2.set_ylabel("Rating", color=GRAY_TITLE, fontsize=9)
    ax2.set_ylim(0, 5.3)
    for spine in ax2.spines.values():
        spine.set_visible(False)
    ax2.tick_params(colors=GRAY_TITLE, labelsize=8)

    for i, v in enumerate(ios_rats):
        ax2.annotate(
            f"{v:.2f}", (x[i], v), textcoords="offset points",
            xytext=(20, 8), ha="center", fontsize=7, color=RED, fontweight="bold",
        )
    for i, v in enumerate(gp_rats):
        ax2.annotate(
            f"{v:.2f}", (x[i], v), textcoords="offset points",
            xytext=(20, -12), ha="center", fontsize=7, color=GREEN, fontweight="bold",
        )

    # Combine legends and place above the plot area
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="lower center", bbox_to_anchor=(0.5, 1.01),
               ncol=4, fontsize=7, framealpha=0.9)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(os.path.join(SCRIPT_DIR, "graph_2.png"))
    plt.close(fig)
    print("Saved graph_2.png")


def _grouped_bar(cat_data, months_sorted, title, filename, xlabel="Category"):
    """Helper for MOM grouped bar charts (graphs 3 & 4)."""
    # Collect all categories across both months
    all_cats = sorted(
        set(cat for month_counts in cat_data.values() for cat in month_counts)
    )

    m1, m2 = months_sorted[0], months_sorted[1]
    vals1 = [cat_data[m1].get(c, 0) for c in all_cats]
    vals2 = [cat_data[m2].get(c, 0) for c in all_cats]

    fig, ax = plt.subplots(figsize=(6.7, 4.5), dpi=200)
    style_ax(ax, title)

    x = np.arange(len(all_cats))
    w = 0.35
    ax.bar(x - w / 2, vals1, w, color=BLUE, label=fmt_month_short(m1))
    ax.bar(x + w / 2, vals2, w, color=RED_DARK, label=fmt_month_short(m2))

    # Title-case labels, wrap long ones to ~14 chars per line
    labels = []
    for c in all_cats:
        label = c.title()
        if len(label) > 14:
            words = label.split()
            lines, cur = [], ""
            for w2 in words:
                if cur and len(cur) + 1 + len(w2) > 14:
                    lines.append(cur)
                    cur = w2
                else:
                    cur = f"{cur} {w2}".strip() if cur else w2
            if cur:
                lines.append(cur)
            label = "\n".join(lines)
        labels.append(label)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=6, rotation=45, ha="right")
    ax.set_xlabel(xlabel, color=GRAY_TITLE, fontsize=9)
    ax.set_ylabel("Count", color=GRAY_TITLE, fontsize=9)
    ax.legend(fontsize=7, framealpha=0.9)

    fig.tight_layout()
    fig.savefig(os.path.join(SCRIPT_DIR, filename))
    plt.close(fig)
    print(f"Saved {filename}")


def graph_3(cat_reviews):
    """MOM Positive Review Count (Publicly Posted)."""
    pos = cat_reviews["pos"]
    months_sorted = sorted(pos.keys())

    cat_data = {}
    for month, reviews in pos.items():
        cat_data[month] = Counter(r["category"] for r in reviews)

    _grouped_bar(
        cat_data, months_sorted,
        "MOM Positive Review Count (Publicly Posted)", "graph_3.png",
    )


def graph_4(cat_reviews):
    """MOM Negative Review Count (Publicly Posted)."""
    neg = cat_reviews["neg"]
    months_sorted = sorted(neg.keys())

    cat_data = {}
    for month, reviews in neg.items():
        cat_data[month] = Counter(r["category"] for r in reviews)

    _grouped_bar(
        cat_data, months_sorted,
        "MOM Negative Review Count (Publicly Posted)", "graph_4.png",
        xlabel="Review Type",
    )


def main():
    csv_path = sys.argv[1]

    # Run pull_slide_deck_data.py to get metrics
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "pull_slide_deck_data.py"), csv_path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)

    # Load categorized reviews
    with open(os.path.join(SCRIPT_DIR, "categorized_reviews.json")) as f:
        cat_reviews = json.load(f)

    graph_1(data)
    graph_2(data)
    graph_3(cat_reviews)
    graph_4(cat_reviews)
    print("All graphs generated.")


if __name__ == "__main__":
    main()
