#!/usr/bin/env python3
"""
Categorize reviews from the two most recent months using Claude.
Reads a ReviewTrackers CSV, splits into positive/negative, and outputs
categorized JSON to stdout.
"""

import csv
import json
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

from helpers import call_claude

load_dotenv()

MAX_REVIEW_CHARS = 500

POSITIVE_CATEGORIES = [
    "credit card",
    "credit building",
    "direct deposit",
    "fair prices",
    "fast shipping times",
    "good customer service experience",
    "good in-app experience",
    "good overall experience",
    "good products",
    "other",
]

NEGATIVE_CATEGORIES = [
    "customer experience",
    "functionality",
    "pricing",
    "product",
    "deliverability/shipping",
    "vendor issues",
    "credit card",
    "other",
]


def parse_reviews(csv_path: str) -> dict:
    """Read CSV and bucket reviews by sentiment and month (two most recent)."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rating_str = (row.get("Rating") or "").strip()
            review_text = (row.get("Review") or "").strip()
            if not rating_str or not review_text:
                continue
            rating = float(rating_str)
            month = row["Published"][:7]  # YYYY-MM
            rows.append({
                "review_id": row["Review ID"],
                "review": review_text[:MAX_REVIEW_CHARS],
                "rating": rating,
                "month": month,
            })

    # Determine two most recent months
    all_months = sorted(set(r["month"] for r in rows))
    last_two = set(all_months[-2:])

    buckets = {"pos": defaultdict(list), "neg": defaultdict(list)}
    for r in rows:
        if r["month"] not in last_two:
            continue
        sentiment = "pos" if r["rating"] >= 4.0 else "neg"
        buckets[sentiment][r["month"]].append({
            "review_id": r["review_id"],
            "review": r["review"],
        })

    # Convert defaultdicts to regular dicts for JSON serialization
    return {
        "pos": dict(buckets["pos"]),
        "neg": dict(buckets["neg"]),
    }


def build_categorize_prompt(reviews_by_month: dict, sentiment: str) -> str:
    if sentiment == "pos":
        categories = POSITIVE_CATEGORIES
        label = "positive"
    else:
        categories = NEGATIVE_CATEGORIES
        label = "negative"

    categories_str = ", ".join(f'"{c}"' for c in categories)

    return f"""You are categorizing {label} customer reviews for Perpay, a buy-now-pay-later and credit-building platform.

Here are the {label} reviews organized by month:

{json.dumps(reviews_by_month, indent=2)}

For each review, pick the single best category from this list:
[{categories_str}]

Process:
1. For each review, evaluate the top 3 most likely categories and assign each an independent confidence percentage (0-100%).
2. If no category is above 70% confidence, assign "other".
3. If multiple categories are above 70%, pick the one with the highest confidence.
4. If there is a tie, pick one at random.

Respond with a JSON object in this exact format (no extra keys, no explanation):
{{
  "<YYYY-MM>": [
    {{"review_id": "<id>", "category": "<chosen category>"}},
    ...
  ],
  ...
}}
"""


BATCH_SIZE = 150


def categorize(review_data: dict) -> dict:
    result = {"pos": {}, "neg": {}}

    for sentiment in ("pos", "neg"):
        months_data = review_data[sentiment]
        if not months_data:
            continue

        # Flatten all reviews with their month, then batch
        flat = []
        for month, reviews in months_data.items():
            for r in reviews:
                flat.append({**r, "_month": month})

        merged = defaultdict(list)
        for i in range(0, len(flat), BATCH_SIZE):
            batch = flat[i : i + BATCH_SIZE]
            # Re-group batch by month for the prompt
            batch_by_month = defaultdict(list)
            for r in batch:
                batch_by_month[r["_month"]].append({"review_id": r["review_id"], "review": r["review"]})

            batch_num = i // BATCH_SIZE + 1
            total_batches = (len(flat) + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"  {sentiment} batch {batch_num}/{total_batches} ({len(batch)} reviews)...", file=sys.stderr)

            prompt = build_categorize_prompt(dict(batch_by_month), sentiment)
            response = call_claude(prompt, max_tokens=16384)
            for month, categorized in response.items():
                merged[month].extend(categorized)

        result[sentiment] = dict(merged)

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python categorize_reviews.py <path_to_csv>", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("Parsing reviews...", file=sys.stderr)
    review_data = parse_reviews(csv_path)

    pos_count = sum(len(v) for v in review_data["pos"].values())
    neg_count = sum(len(v) for v in review_data["neg"].values())
    print(f"  Positive: {pos_count} reviews across {list(review_data['pos'].keys())}", file=sys.stderr)
    print(f"  Negative: {neg_count} reviews across {list(review_data['neg'].keys())}", file=sys.stderr)

    print("Categorizing with Claude...", file=sys.stderr)
    result = categorize(review_data)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
