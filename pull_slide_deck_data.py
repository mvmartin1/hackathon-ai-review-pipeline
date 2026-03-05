import csv
import json
import sys
from collections import defaultdict


def main():
    csv_path = sys.argv[1]

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row["Rating"]:
                continue
            month = row["Published"][:7]  # YYYY-MM
            rating = float(row["Rating"])
            source = row["Source"]
            rows.append((month, rating, source))

    EXCLUDED_FROM_AGGREGATE = {"BBB", "CreditKarma"}

    # Group by month (all sources for source_breakdown, filtered for aggregates)
    by_month_all = defaultdict(list)
    by_month = defaultdict(list)
    for month, rating, source in rows:
        by_month_all[month].append((rating, source))
        if source not in EXCLUDED_FROM_AGGREGATE:
            by_month[month].append((rating, source))

    months_sorted = sorted(by_month_all)

    # Metric 1: sentiment_breakdown (most recent month, excluding BBB/CreditKarma)
    latest = by_month[months_sorted[-1]]
    pos = [r for r, _ in latest if r >= 4.0]
    neg = [r for r, _ in latest if r < 4.0]
    total = len(latest)
    sentiment_breakdown = {
        "positive": {"pct": round(len(pos) / total, 2), "vol": len(pos)},
        "negative": {"pct": round(len(neg) / total, 2), "vol": len(neg)},
    }

    # Metric 2: monthly_avg_rating (oldest -> newest, excluding BBB/CreditKarma)
    monthly_avg_rating = [
        round(sum(r for r, _ in by_month[m]) / len(by_month[m]), 2)
        for m in months_sorted
    ]

    # Metric 3: source_breakdown (last two months)
    last_two = months_sorted[-2:]
    by_source_month = defaultdict(lambda: defaultdict(list))
    for month, rating, source in rows:
        if month in last_two:
            by_source_month[source][month].append(rating)

    source_breakdown = {}
    for source in sorted(by_source_month):
        source_breakdown[source] = [
            {
                "avg_rating": round(
                    sum(by_source_month[source][m]) / len(by_source_month[source][m]), 2
                ),
                "vol": len(by_source_month[source][m]),
            }
            for m in last_two
            if m in by_source_month[source]
        ]

    # Metric 4: per-month positive/negative counts (excluding BBB/CreditKarma)
    monthly_pos_count = [
        len([r for r, _ in by_month[m] if r >= 4.0]) for m in months_sorted
    ]
    monthly_neg_count = [
        len([r for r, _ in by_month[m] if r < 4.0]) for m in months_sorted
    ]

    result = {
        "months": months_sorted,
        "sentiment_breakdown": sentiment_breakdown,
        "monthly_avg_rating": monthly_avg_rating,
        "monthly_pos_count": monthly_pos_count,
        "monthly_neg_count": monthly_neg_count,
        "source_breakdown": source_breakdown,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
