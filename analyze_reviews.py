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
from anthropic import Anthropic
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

RATING_CUTOFF = 2.0
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
Your job is to identify which negative reviews contain ACTIONABLE items — meaning specific, reproducible bugs, technical failures, or product issues that an engineering or product team could investigate and fix.

Actionable examples: login failures, verification codes not being received, app crashes, payment processing errors, account access issues, order/shipping errors.
NOT actionable: pricing complaints, general dissatisfaction, competitor comparisons, subjective opinions.

Here are {len(reviews)} negative reviews (≤{int(RATING_CUTOFF)}★) to analyze:

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
- Use consistent category labels (e.g. "Login/Auth Issue", "Verification Code Failure", "Payment Error", "Account Access", "App Crash", "Order/Shipping Issue", "Fraud/Identity Theft", "Pricing Complaint", "Customer Service Issue").
- theme_counts should cover ALL negative reviews, not just actionable ones.
"""


def call_claude(prompt: str) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text

    # Extract JSON from response (Claude may wrap it in markdown code fences)
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    return json.loads(raw)


def format_stars(rating: float) -> str:
    filled = int(rating)
    return "★" * filled + "☆" * (5 - filled)


def build_slack_blocks(all_reviews: pd.DataFrame, negative: pd.DataFrame, analysis: dict, csv_path: str) -> list:
    """Build Slack Block Kit blocks for the report."""
    review_lookup = negative.set_index("Review ID").to_dict("index")

    dates = pd.to_datetime(all_reviews["Published"], errors="coerce").dropna()
    date_min = dates.min().strftime("%Y-%m-%d") if not dates.empty else "N/A"
    date_max = dates.max().strftime("%Y-%m-%d") if not dates.empty else "N/A"

    source_counts = negative["Source"].value_counts().to_dict()
    source_str = "  |  ".join(f"{src}: *{cnt}*" for src, cnt in source_counts.items())

    actionable = analysis.get("actionable_items", [])
    themes = analysis.get("theme_counts", {})
    summary = analysis.get("summary", "")

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
                    f"Total reviews in export: *{len(all_reviews)}*\n"
                    f"Negative reviews analyzed (≤{int(RATING_CUTOFF)}★): *{len(negative)}*\n"
                    f"Date range: *{date_min} → {date_max}*\n"
                    f"Sources (negative): {source_str}"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":warning: *Most Actionable Items* ({len(actionable)} flagged)"},
        },
    ]

    if not actionable:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No actionable items identified._"}})
    else:
        for i, item in enumerate(actionable, 1):
            rid = item["review_id"]
            row = review_lookup.get(rid, {})
            score = item["actionability_score"]
            score_bar = "█" * score + "░" * (5 - score)
            rating = row.get("Rating", "?")
            stars = ("★" * int(float(rating)) + "☆" * (5 - int(float(rating)))) if rating != "?" else "?"
            review_text = str(row.get("Review", ""))[:200]
            if len(str(row.get("Review", ""))) > 200:
                review_text += "..."
            url = row.get("URL", "")
            url_line = f"\n<{url}|View review>" if url else ""

            blocks.append(
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
            )

    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": ":bar_chart: *Sentiment Summary — Recurring Themes*"}}
    )
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})

    if themes:
        theme_lines = "\n".join(
            f"• {theme}: *{count}*"
            for theme, count in sorted(themes.items(), key=lambda x: x[1], reverse=True)
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Theme breakdown:*\n{theme_lines}"}})

    return blocks


def post_to_slack(blocks: list):
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel:
        print("Slack credentials not set — skipping Slack post.")
        return

    client = WebClient(token=token)
    try:
        client.chat_postMessage(channel=channel, blocks=blocks, text="Perpay AI Review Analysis Report")
        print(f"Report posted to Slack channel {channel}")
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}")


def print_report(all_reviews: pd.DataFrame, negative: pd.DataFrame, analysis: dict, csv_path: str):
    # Build a lookup from review_id -> row
    review_lookup = negative.set_index("Review ID").to_dict("index")

    # Date range
    dates = pd.to_datetime(all_reviews["Published"], errors="coerce").dropna()
    date_min = dates.min().strftime("%Y-%m-%d") if not dates.empty else "N/A"
    date_max = dates.max().strftime("%Y-%m-%d") if not dates.empty else "N/A"

    # Source breakdown (negative reviews)
    source_counts = negative["Source"].value_counts().to_dict()
    source_str = " | ".join(f"{src}: {cnt}" for src, cnt in source_counts.items())

    total = len(all_reviews)
    neg_count = len(negative)
    actionable = analysis.get("actionable_items", [])
    themes = analysis.get("theme_counts", {})
    summary = analysis.get("summary", "")

    sep = "━" * 60

    print(f"\n{sep}")
    print("  PERPAY AI REVIEW ANALYSIS REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Source: {Path(csv_path).name}")
    print(sep)

    print(f"\nSTATS")
    print(f"  Total reviews in export:          {total}")
    print(f"  Negative reviews analyzed (≤{int(RATING_CUTOFF)}★): {neg_count}")
    print(f"  Date range:                       {date_min} → {date_max}")
    print(f"  Sources (negative):               {source_str}")

    print(f"\n{sep}")
    print(f"  MOST ACTIONABLE ITEMS  ({len(actionable)} flagged)")
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
            url = row.get("URL", "")
            if url:
                print(f"      URL:    {url}")

    print(f"\n{sep}")
    print("  SENTIMENT SUMMARY — Recurring Themes")
    print(sep)
    print(f"\n  {summary}")

    if themes:
        print("\n  Theme breakdown across negative reviews:")
        for theme, count in sorted(themes.items(), key=lambda x: x[1], reverse=True):
            bar = "▪" * min(count, 20)
            print(f"    {theme:<35} {count:>3}  {bar}")

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
        blocks = build_slack_blocks(all_reviews, negative, analysis, csv_path)
        post_to_slack(blocks)


if __name__ == "__main__":
    main()
