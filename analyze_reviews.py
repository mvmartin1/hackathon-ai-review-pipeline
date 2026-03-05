#!/usr/bin/env python3
"""
AI Review Pipeline - Demo MVP
Analyzes customer reviews from a ReviewTrackers CSV export using Claude,
surfaces actionable bug reports, and outputs a Slack-formatted report.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from helpers import call_claude

load_dotenv()

RATING_CUTOFF = 5.0  # analyze all reviews regardless of rating
MAX_REVIEW_CHARS = 500  # truncate very long reviews to keep prompt size reasonable


def load_reviews(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()

    # Normalize rating to float
    df["Rating"] = pd.to_numeric(df["Rating"], errors="coerce")

    # Keep only negative reviews with actual text
    negative = df[
        (df["Rating"] <= RATING_CUTOFF) & (df["Review"].notna()) & (df["Review"].str.strip() != "")
    ].copy()

    negative = negative[["Review ID", "Published", "Author", "Source", "Rating", "Title", "Review", "URL"]].reset_index(drop=True)
    return df, negative


def build_prompt(reviews: pd.DataFrame) -> str:
    review_blocks = []
    for _, row in reviews.iterrows():
        text = str(row["Review"])[:MAX_REVIEW_CHARS]
        review_blocks.append(
            f"[ID: {row['Review ID']}]\n"
            f"Source: {row['Source']} | Rating: {row['Rating']}★ | Author: {row['Author']}\n"
            f"Title: {row['Title']}\n"
            f"Review: {text}"
        )

    reviews_text = "\n\n---\n\n".join(review_blocks)

    return f"""You are analyzing customer reviews for Perpay, a buy-now-pay-later and credit-building platform.
Your job is to identify which reviews contain ACTIONABLE items — meaning specific, reproducible bugs, technical failures, product defects, or process failures that an engineering, product, or operations team could investigate and fix.

Actionable examples: login failures, verification codes not being received, app crashes, payment processing errors, account access issues, order/shipping errors, unauthorized or over-authorized payroll deductions, credit bureau reporting not working despite paying for it, packages marked delivered but not received while payments still demanded, returns blocked or ignored, direct deposit not recognized by the app after setup, accounts opened in a user's name without their consent, items arriving defective or suspected counterfeit, credit limits not updating after on-time payments, gig-worker income (DoorDash/Uber) incorrectly rejected despite being advertised as accepted, slow or no shipment communication.
NOT actionable: vague pricing complaints with no specific failure, general dissatisfaction, competitor comparisons, subjective opinions about fees or interest rates where no system error is described.

Here are {len(reviews)} reviews to analyze:

{reviews_text}

Respond with a JSON object in this exact format:
{{
  "summary": "<2-3 sentence summary of the most common themes across all negative reviews>",
  "theme_counts": {{
    "<Theme Label>": <count>,
    ...
  }},
  "actionable_items": [
    {{
      "review_id": "<Review ID>",
      "actionability_score": <1-5>,
      "category": "<short category label>",
      "reason": "<one sentence explaining why this is actionable and what the specific issue is>"
    }},
    ...
  ]
}}

Rules:
- Only include reviews in actionable_items if they score 3 or higher (genuinely actionable).
- Sort actionable_items by actionability_score descending.
- Use consistent category labels (e.g. "Login/Auth Issue", "Verification Code Failure", "Payment Error", "Account Access", "App Crash", "Order/Shipping Issue", "Slow Shipping", "Missing Delivery", "Return/Refund Blocked", "Unauthorized Charge", "Credit Reporting Failure", "Direct Deposit Not Recognized", "Defective/Counterfeit Product", "Fraudulent Account Opened", "Credit Limit Not Updating", "Gig Worker Eligibility Rejected", "Subscription/Fee Dispute", "Customer Service Issue").
- theme_counts should cover ALL negative reviews, not just actionable ones.
"""



PLATFORMS_WITHOUT_DIRECT_LINKS = {"BBB", "CreditKarma"}

# Maps display group name → (emoji, set of category label substrings that belong to it)
# Items not matching Order/Shipping, Customer Service, or Return/Refund fall into Core Issues.
CATEGORY_GROUPS = [
    ("Order/Shipping",    ":package:",                   {"order", "shipping", "delivery", "slow shipping", "missing delivery", "defective", "counterfeit"}),
    ("Return/Refund",     ":leftwards_arrow_with_hook:", {"return", "refund"}),
    ("Billing/Payments",  ":money_with_wings:",          {"payment", "charge", "billing", "subscription", "fee", "direct deposit"}),
    ("Credit",            ":credit_card:",               {"credit reporting", "credit limit"}),
    ("Fraud/Identity",    ":lock:",                      {"fraud", "identity", "fraudulent account"}),
    ("Customer Service",  ":headphones:",                {"customer service"}),
    ("Core Issues",       ":hammer_and_wrench:",         set()),  # catch-all
]


def review_url(row: dict) -> str:
    if row.get("Source", "") in PLATFORMS_WITHOUT_DIRECT_LINKS:
        return f"https://app.reviewtrackers.com/reviews/{row.get('Review ID', '')}"
    return row.get("URL", "")


def format_stars(rating: float) -> str:
    filled = int(rating)
    return "★" * filled + "☆" * (5 - filled)


def assign_group(category: str) -> str:
    cat_lower = category.lower()
    for group_name, _, keywords in CATEGORY_GROUPS[:-1]:  # skip catch-all
        if any(kw in cat_lower for kw in keywords):
            return group_name
    return "Core Issues"


def group_actionable_items(actionable: list) -> dict:
    groups = {name: [] for name, _, _ in CATEGORY_GROUPS}
    for item in actionable:
        groups[assign_group(item["category"])].append(item)
    return {k: v for k, v in groups.items() if v}  # drop empty groups


def build_platform_stats(all_reviews: pd.DataFrame) -> str:
    """Build per-platform volume + positive/negative breakdown string for Slack."""
    lines = []
    for src, grp in all_reviews.groupby("Source"):
        total = len(grp)
        pos = (grp["Rating"] >= 4).sum()
        neg = (grp["Rating"] <= 3).sum()
        pos_pct = round(pos / total * 100) if total else 0
        neg_pct = round(neg / total * 100) if total else 0
        avg = grp["Rating"].mean()
        lines.append(
            f"  • *{src}*: {total} reviews  |  ★ avg {avg:.1f}  |  :thumbsup: {pos} ({pos_pct}%)  :thumbsdown: {neg} ({neg_pct}%)"
        )
    return "\n".join(lines)


def build_main_blocks(all_reviews: pd.DataFrame, analysis: dict, csv_path: str) -> list:
    """Main message: header, stats, group summary, sentiment."""
    dates = pd.to_datetime(all_reviews["Published"], errors="coerce").dropna()
    date_min = dates.min().strftime("%Y-%m-%d") if not dates.empty else "N/A"
    date_max = dates.max().strftime("%Y-%m-%d") if not dates.empty else "N/A"

    avg_rating = all_reviews["Rating"].mean()
    platform_stats = build_platform_stats(all_reviews)

    actionable = analysis.get("actionable_items", [])
    summary = analysis.get("summary", "")
    grouped = group_actionable_items(actionable)

    group_lines = "\n".join(
        f"{emoji}  *{name}*: {len(grouped.get(name, []))} reviews"
        for name, emoji, _ in CATEGORY_GROUPS
        if grouped.get(name)
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Perpay AI Review Analysis Report", "emoji": True}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Source: `{Path(csv_path).name}`"}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Stats*\n"
                    f"Total reviews in export: *{len(all_reviews)}*  |  Avg rating: *★ {avg_rating:.2f}*\n"
                    f"Date range: *{date_min} → {date_max}*\n\n"
                    f"*Per-platform breakdown:*\n{platform_stats}"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":warning: *Most Actionable Themes* ({len(actionable)} flagged) — see threads below\n\n{group_lines}"},
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": ":bar_chart: *Sentiment Summary — Recurring Themes*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]

    return blocks


def build_category_header_blocks(group_name: str, emoji: str, items: list, review_lookup: dict, total_neg: int) -> list:
    """Top-level message for a category — thread replies will contain individual items."""
    count = len(items)
    pct = round(count / total_neg * 100) if total_neg else 0

    # Pick the top-scoring item's review as the example
    top_item = max(items, key=lambda x: x.get("actionability_score", 0))
    top_row = review_lookup.get(top_item["review_id"], {})
    example_text = str(top_row.get("Review", ""))[:200]
    if len(str(top_row.get("Review", ""))) > 200:
        example_text += "..."
    example_author = top_row.get("Author", "Unknown")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{group_name}* — {count} actionable review{'s' if count != 1 else ''} ({pct}% of negative reviews)\n\n"
                    f"_Example: \"{example_text}\" — {example_author}_"
                ),
            },
        }
    ]


def build_item_blocks(i: int, item: dict, row: dict) -> list:
    """Thread reply blocks for a single actionable item."""
    score = item["actionability_score"]
    score_bar = "█" * score + "░" * (5 - score)
    rating = row.get("Rating", "?")
    stars = ("★" * int(float(rating)) + "☆" * (5 - int(float(rating)))) if rating != "?" else "?"
    review_text = str(row.get("Review", ""))[:200]
    if len(str(row.get("Review", ""))) > 200:
        review_text += "..."
    url = review_url(row)
    url_line = f"\n<{url}|View review>" if url else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*[{i}] {item['category']}*  `{score}/5  {score_bar}`\n"
                    f"*Source:* {row.get('Source', 'N/A')}  |  *Rating:* {stars}  |  *Author:* {row.get('Author', 'N/A')}\n"
                    f"*Why actionable:* {item['reason']}\n"
                    f"_{review_text}_{url_line}"
                ),
            },
        }
    ]


def post_to_slack(all_reviews: pd.DataFrame, negative: pd.DataFrame, analysis: dict, csv_path: str):
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel:
        print("Slack credentials not set — skipping Slack post.")
        return

    client = WebClient(token=token)
    review_lookup = negative.set_index("Review ID").to_dict("index")
    actionable = analysis.get("actionable_items", [])
    grouped = group_actionable_items(actionable)
    total_neg = int((all_reviews["Rating"] <= 3).sum())

    try:
        client.chat_postMessage(
            channel=channel,
            blocks=build_main_blocks(all_reviews, analysis, csv_path),
            text="Perpay AI Review Analysis Report",
            unfurl_links=False,
            unfurl_media=False,
        )
        print(f"Main report posted to Slack channel {channel}")

        for group_name, emoji, _ in CATEGORY_GROUPS:
            items = grouped.get(group_name, [])
            if not items:
                continue

            # Post category as its own top-level message
            cat_resp = client.chat_postMessage(
                channel=channel,
                blocks=build_category_header_blocks(group_name, emoji, items, review_lookup, total_neg),
                text=f"{group_name} ({len(items)} reviews)",
                unfurl_links=False,
                unfurl_media=False,
            )
            cat_ts = cat_resp["ts"]

            # Thread each item under the category message
            for i, item in enumerate(items, 1):
                row = review_lookup.get(item["review_id"], {})
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=cat_ts,
                    blocks=build_item_blocks(i, item, row),
                    text=f"[{i}] {item['category']}",
                    unfurl_links=False,
                    unfurl_media=False,
                )

            print(f"  Posted: {group_name} ({len(items)} items in thread)")

    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}")


def print_report(all_reviews: pd.DataFrame, negative: pd.DataFrame, analysis: dict, csv_path: str):
    # Build a lookup from review_id -> row
    review_lookup = negative.set_index("Review ID").to_dict("index")

    # Date range
    dates = pd.to_datetime(all_reviews["Published"], errors="coerce").dropna()
    date_min = dates.min().strftime("%Y-%m-%d") if not dates.empty else "N/A"
    date_max = dates.max().strftime("%Y-%m-%d") if not dates.empty else "N/A"

    total = len(all_reviews)
    avg_rating = all_reviews["Rating"].mean()
    actionable = analysis.get("actionable_items", [])
    summary = analysis.get("summary", "")

    sep = "━" * 60

    print(f"\n{sep}")
    print("  PERPAY AI REVIEW ANALYSIS REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Source: {Path(csv_path).name}")
    print(sep)

    print(f"\nSTATS")
    print(f"  Total reviews in export:          {total}")
    print(f"  Avg star rating:                  ★ {avg_rating:.2f}")
    print(f"  Date range:                       {date_min} → {date_max}")
    print("\n  Per-platform breakdown:")
    for src, grp in all_reviews.groupby("Source"):
        t = len(grp)
        pos = int((grp["Rating"] >= 4).sum())
        neg = int((grp["Rating"] <= 3).sum())
        pos_pct = round(pos / t * 100) if t else 0
        neg_pct = round(neg / t * 100) if t else 0
        avg = grp["Rating"].mean()
        print(f"    {src}: {t} reviews  |  ★ avg {avg:.1f}  |  👍 {pos} ({pos_pct}%)  👎 {neg} ({neg_pct}%)")

    print(f"\n{sep}")
    print(f"  MOST ACTIONABLE THEMES  ({len(actionable)} flagged)")
    print(sep)

    if not actionable:
        print("\n  No actionable items identified.\n")
    else:
        for i, item in enumerate(actionable, 1):
            rid = item["review_id"]
            row = review_lookup.get(rid, {})
            score = item["actionability_score"]
            score_bar = "█" * score + "░" * (5 - score)
            rating = row.get("Rating", "?")
            stars = format_stars(float(rating)) if rating != "?" else "?"

            print(f"\n  [{i}] Actionability: {score}/5  {score_bar}  |  {item['category']}")
            print(f"      Source: {row.get('Source', 'N/A')}  |  Rating: {stars}  |  Author: {row.get('Author', 'N/A')}")
            print(f"      Date:   {row.get('Published', 'N/A')}")
            print(f"      WHY:    {item['reason']}")
            review_text = str(row.get("Review", ""))[:200]
            if len(str(row.get("Review", ""))) > 200:
                review_text += "..."
            print(f"      Review: \"{review_text}\"")
            url = review_url(row)
            if url:
                print(f"      URL:    {url}")

    print(f"\n{sep}")
    print("  SENTIMENT SUMMARY — Recurring Themes")
    print(sep)
    print(f"\n  {summary}")

    print(f"\n{sep}\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_reviews.py <path_to_csv>")
        print("Example: python analyze_reviews.py export-2026-03-04.csv")
        sys.exit(1)

    csv_path = sys.argv[1]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")
        sys.exit(1)

    print(f"Loading reviews from: {csv_path}")
    all_reviews, negative = load_reviews(csv_path)
    print(f"  {len(all_reviews)} total reviews | {len(negative)} negative (≤{int(RATING_CUTOFF)}★)")

    if negative.empty:
        print("No negative reviews found in this export.")
        sys.exit(0)

    print("Sending to Claude for analysis...")
    prompt = build_prompt(negative)
    analysis = call_claude(prompt)

    print_report(all_reviews, negative, analysis, csv_path)

    if os.environ.get("SLACK_BOT_TOKEN"):
        print("Posting to Slack...")
        post_to_slack(all_reviews, negative, analysis, csv_path)


if __name__ == "__main__":
    main()
