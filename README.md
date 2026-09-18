# AI Review Pipeline

An AI-powered pipeline that ingests customer review exports from ReviewTrackers, analyzes them with Claude, and surfaces actionable bug reports and sentiment themes — formatted as a Slack-ready report.

Built at the Perpay Hackathon 2026 by **Nitish Raju** and **Matthew Martin**, with CX consultation from **Chris Anderson**.

---

## The Problem

Customer reviews regularly contain one-off bug reports that slip through the cracks due to volume. Reading through hundreds of reviews manually is not practical, and actionable issues — app crashes, login failures, payment errors — have historically gone unnoticed for months. This pipeline automates that triage, so bugs get caught early and surfaced to the right teams.

---

## The Solution

A Python script that:

1. Reads a ReviewTrackers CSV export
2. Filters to low-rated reviews (≤ 2 stars)
3. Sends them to Claude (`claude-sonnet-4-6`) for structured analysis
4. Ranks reviews by actionability (specific, reproducible bugs score highest)
5. Prints a Slack-formatted report to stdout with:
   - **Most Actionable Items** — ranked bug reports with context and direct links
   - **Sentiment Summary** — recurring themes across all negative reviews
   - **Stats** — volume, date range, source breakdown

---

## Demo Scope

**In scope:**
- One-time analysis of a ReviewTrackers CSV export
- Actionable item extraction and ranking
- Sentiment theme summarization
- Slack-formatted terminal output

**Out of scope (future work):**
- Automated scheduling / recurring pipeline
- Direct Slack API integration
- ReviewTrackers API integration (manual CSV export used for demo)
- Multi-language review support
- Historical trend comparison

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your API key

```bash
cp .env.example .env
# Edit .env and set your ANTHROPIC_API_KEY
```

### 3. Export reviews from ReviewTrackers

Export your reviews as a CSV from the ReviewTrackers dashboard. The script expects the standard ReviewTrackers export format with columns including: `Review ID`, `Published`, `Author`, `Source`, `Rating`, `Title`, `Review`, `URL`.

---

## Stripping PII

`strip_pii.py` removes personal information from a review export before it is
analyzed, shared, or handed to an LLM. It has no dependencies beyond the standard
library and works on ReviewTrackers exports as well as other sources.

```bash
python strip_pii.py export-2026-09-18.csv --salt "$PII_SALT" --allow-domain perpay.com
# -> export-2026-09-18.clean.csv
```

The cleaned file drops straight into the rest of the pipeline:

```bash
python analyze_reviews.py export-2026-09-18.clean.csv
```

### What it does

Every column gets one of five actions — `keep`, `scrub`, `redact`, `hash`, `drop`.
ReviewTrackers exports are detected by their headers and use a built-in profile;
any other file falls back to header-name heuristics, so a Trustpilot or App Store
export with columns like `customer_email` or `ssn` is handled without configuration.

Inside free-text columns (`Review`, `Title`, `Responses`, `Notes`, …) it redacts
emails, phone numbers, SSNs, Luhn-validated payment cards, IP addresses, street
addresses, dates of birth, account numbers, and URL query strings. It also removes
the reviewer's own name where it appears in the review body, and names people
volunteer in the text ("My name is …", "Sincerely, …").

Names and IDs become stable pseudonyms rather than being deleted, so rows stay
joinable:

```
Author: Dana Whitfield  →  [PERSON:0a3fa8ce]
Review: "Dana here, call 215-555-0147"  →  "[PERSON:0a3fa8ce] here, call [PHONE]"
```

Pass the same `--salt` (or set `$PII_SALT`) to get identical pseudonyms across
runs and across exports; without one, a random salt is generated per run.

### Options

| Flag | Purpose |
|---|---|
| `-o, --output` | Output path (default `<input>.clean.<ext>`) |
| `--salt` | Pseudonym salt; also read from `$PII_SALT` |
| `--allow-domain` | Keep emails at a company domain, e.g. `perpay.com` |
| `--profile` | `auto` (default), `reviewtrackers`, or `generic` |
| `--set COL=ACTION` | Override one column, e.g. `--set City=drop` |
| `--config FILE` | JSON file of `{"Column": "action"}` overrides |
| `--unknown` | Action for unrecognized columns (default `scrub`) |
| `--audit` | Report what would be redacted; write nothing |
| `--report FILE` | Write the per-column and per-detector counts to JSON |
| `--llm` | Second pass with Claude for residual names (opt-in) |
| `--selftest` | Run the built-in detector tests |

`--llm` sends the already-scrubbed text to the Anthropic API. The regex pass runs
first either way, so raw identifiers are removed before anything leaves the machine.

### Verifying

Every run re-scans its own output and reports anything a detector still matches:

```
profile: reviewtrackers | columns: 28 | name columns: ['Author']
dropping: Address, Zip
wrote 15450 records -> export-2026-09-18.clean.csv
verify: clean
detections: {"email": 59, "name_from_column": 2180, "phone": 9, "street_address": 26, ...}
```

Automated redaction is a safety net, not a guarantee. Spot-check the output before
sharing an export outside the team, and route anything questionable to Compliance.

---

## Usage

```bash
python analyze_reviews.py path/to/export.csv
```

**Example:**
```bash
python analyze_reviews.py export-2026-03-04.csv
```

**Example output:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PERPAY AI REVIEW ANALYSIS REPORT
  Generated: 2026-03-04 14:32  |  Source: export-2026-03-04.csv
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATS
  Total reviews in export:          533
  Negative reviews analyzed (≤2★):  87
  Date range:                       2026-01-01 → 2026-02-18
  Sources (negative):               Google Play: 34 | iOS App Store: 21 | ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  MOST ACTIONABLE ITEMS  (12 flagged)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [1] Actionability: 5/5  █████  |  Verification Code Failure
      Source: Google Play  |  Rating: ★☆☆☆☆  |  Author: Bunny D. Pain
      WHY: User explicitly describes verification codes not being delivered...
      Review: "I can no longer access my account...it keeps asking for a
               verification code that's not being sent..."
      URL: https://play.google.com/...
...
```

---

## Architecture

```
ReviewTrackers CSV
        │
        ▼
  load_reviews()       ← pandas, filters ≤2★
        │
        ▼
  build_prompt()       ← formats reviews into structured prompt
        │
        ▼
  call_claude()        ← claude-sonnet-4-6, returns JSON
        │
        ▼
  print_report()       ← Slack-formatted terminal output
```

---

## Future Vision

- **Scheduled pipeline:** Auto-export from ReviewTrackers → Claude analysis → post to Slack monthly
- **Slack API integration:** Direct posting to a dedicated `#review-alerts` channel
- **Platform filtering:** Separate reports per source (Google Play, iOS App Store, etc.)
- **CX feedback loop:** Iterative prompt refinement based on CX team validation of surfaced items
- **Trend analysis:** Month-over-month comparison of issue categories

---

## Requirements

- Python 3.9+
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- ReviewTrackers CSV export
