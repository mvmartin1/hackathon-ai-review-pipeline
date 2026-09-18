#!/usr/bin/env python3
"""
Strip PII from review exports (CSV/TSV/JSON/JSONL) before they are analyzed,
shared, or sent to an LLM.

Two layers of defense:

  1. Column policy  — each column gets an action: keep / scrub / redact / hash / drop.
     ReviewTrackers has a built-in profile; unknown columns are classified by
     header-name heuristics so other sources work out of the box.

  2. Free-text scrubbing — regex detectors for emails, phone numbers, SSNs,
     payment cards (Luhn-checked), IPs, street addresses, DOBs, account numbers
     and URL query strings, plus per-row redaction of the reviewer's own name
     where it appears inside the review body.

Usage:
    python strip_pii.py export.csv                       # -> export.clean.csv
    python strip_pii.py export.csv -o clean.csv --report report.json
    python strip_pii.py export.csv --audit               # scan only, write nothing
    python strip_pii.py feed.jsonl --profile generic
    python strip_pii.py export.csv --salt "$PII_SALT"    # stable pseudonyms across runs
    python strip_pii.py --selftest

Importable API:
    from strip_pii import scrub_text, Scrubber
"""

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(10_000_000)

# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

SSN_RE = re.compile(r"(?<![\d\-])\d{3}[-\s]\d{2}[-\s]\d{4}(?![\d\-])")
SSN_CONTEXT_RE = re.compile(
    r"((?:ssn|social\s*security(?:\s*(?:number|no\.?|#))?)\W{0,12})(?<!\d)\d{9}(?!\d)",
    re.IGNORECASE,
)

# 13-19 digits, optionally separated by spaces or dashes. Luhn-validated below.
CARD_CANDIDATE_RE = re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])")

PHONE_RE = re.compile(
    r"(?<![\d\-])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d\-])"
    r"|(?<![\d\-])\(?\d{3}\)\s?\d{3}[\s.\-]?\d{4}(?![\d\-])"
    r"|(?<![\d\-])\+?1?\d{10}(?![\d\-])"
)

IPV4_RE = re.compile(
    r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\w.])"
)

DOB_RE = re.compile(
    r"((?:d\.?o\.?b\.?|date\s+of\s+birth|born\s+(?:on|in)|birth\s*day)\W{0,12})"
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})",
    re.IGNORECASE,
)

# Two ways to qualify as a street address, because "1 star way too expensive"
# and "took 3 days drive" are not addresses:
#   a) the street-name word is capitalized or an ordinal — "123 Main St", "123 42nd Ave"
#   b) the suffix is a full word that almost never follows a number in prose
STREET_SUFFIXES = (
    r"st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|ct|court|"
    r"cir|circle|pl|place|way|ter|terrace|pkwy|parkway|hwy|highway|trl|trail|loop"
)
STRONG_SUFFIXES = r"street|avenue|boulevard|parkway|highway|terrace"
_NAME_WORD = r"(?:[A-Z][A-Za-z0-9'\-.]*|\d{1,3}(?:st|nd|rd|th))"

STREET_RE = re.compile(
    # a) capitalized/ordinal street name — case-sensitive except the suffix itself
    rf"(?<!\w)\d{{1,6}}\s+(?:[NSEW]\.?\s+)?(?:{_NAME_WORD}\s+){{0,3}}{_NAME_WORD}\s+"
    rf"(?i:{STREET_SUFFIXES})\b\.?"
    # b) unambiguous full-word suffix, any casing
    rf"|(?i:(?<!\w)\d{{1,6}}\s+(?:[A-Za-z0-9'\-.]+\s+){{0,4}}(?:{STRONG_SUFFIXES})\b\.?)"
)

UNIT_RE = re.compile(
    r"(?<!\w)(?:apt|apartment|unit|suite|ste|bldg)\b\.?\s*#?\s*"
    r"(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{1,8}\b",
    re.IGNORECASE,
)

# "account #12345678", "routing number 021000021", "card ending in 4242"
ACCOUNT_CONTEXT_RE = re.compile(
    r"((?:account|acct|routing|aba|iban|member|customer|order|card)\s*"
    r"(?:number|num|no\.?|#|id|ending\s+(?:in|with))?\W{0,6})"
    r"([X*x]{0,12}[\-\s]?\d{6,20})",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Signature / self-identification patterns: group 2 is the name to redact.
# Inline (?i:...) scopes case-insensitivity to the cue words only, so the name
# group still requires real capitalization and "thanks for nothing" won't match.
NAME_CONTEXT_RES = [
    re.compile(r"((?i:\bmy\s+name\s+is\s+))([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,2})"),
    re.compile(r"((?i:\bthis\s+is\s+))([A-Z][\w'\-]+\s+[A-Z][\w'\-]+)"),
    re.compile(r"((?i:\bi\s*'?\s*a?m\s+))([A-Z][\w'\-]+\s+[A-Z][\w'\-]+)"),
    re.compile(r"((?i:\b(?:sincerely|regards|thanks|thank\s+you|signed)\s*,?)\s*[\r\n\-]*\s*)"
               r"([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,2})\s*$", re.MULTILINE),
    re.compile(r"((?i:\b(?:dear|hi|hello|hey)\s+))([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+)?)(?=[,!.\s])"),
    re.compile(r"((?i:\b(?:mr|mrs|ms|miss|dr|prof)\.?\s+))([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+)?)"),
]

# Words that can appear in a display name but must never be redacted globally.
# Real first names that are also ordinary words. The full-name match still
# applies; only the single-token redaction is skipped for these.
AMBIGUOUS_NAME_WORDS = {
    "will", "mark", "grace", "faith", "hope", "joy", "angel", "rich", "art",
    "drew", "chase", "may", "june", "april", "august", "sunny", "star", "king",
    "queen", "love", "blessed", "real", "true", "young", "new", "sky", "rose",
    "summer", "autumn", "dawn", "penny", "bill", "frank", "jack", "don", "van",
    "lane", "field", "story", "case", "price", "gift", "card", "pay", "cash",
}

NAME_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "customer", "customers", "user", "users",
    "happy", "sad", "angry", "verified", "anonymous", "anon", "guest", "review",
    "reviewer", "buyer", "member", "client", "account", "name", "none", "null",
    "n/a", "na", "mr", "mrs", "ms", "dr", "miss", "jr", "sr", "not", "no", "yes",
    "perpay", "app", "team", "support", "service", "google", "trustpilot", "bbb",
}


def luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum — keeps order numbers from being flagged as cards."""
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# --------------------------------------------------------------------------
# Column policy
# --------------------------------------------------------------------------

ACTIONS = ("keep", "scrub", "redact", "hash", "drop")

# ReviewTrackers export. Address/City/State/Zip describe the *business location*
# being reviewed, not the reviewer — Address and Zip are still dropped as the
# most identifying of them. Adjust here if your export means something else.
REVIEWTRACKERS_PROFILE = {
    "Review ID": "keep",
    "Published": "keep",
    "Originally Published": "keep",
    "Updated": "keep",
    "Deleted": "keep",
    "Author": "hash",
    "Source": "keep",
    "Groups": "keep",
    "External ID": "hash",
    "Location": "keep",
    "Address": "drop",
    "City": "keep",
    "State": "keep",
    "Country": "keep",
    "Zip": "drop",
    "Rating": "keep",
    "Title": "scrub",
    "Review": "scrub",
    "Original Content": "scrub",
    "Original Language Name": "keep",
    "Original Language Code": "keep",
    "Extra Information": "scrub",
    "URL": "keep",
    "Status": "keep",
    "Notes": "scrub",
    "Tags": "keep",
    "Responses": "scrub",
    "Responses Published": "keep",
}

PROFILES = {
    "reviewtrackers": REVIEWTRACKERS_PROFILE,
    "generic": {},
}

# Header-name heuristics, applied in order, for columns not named in a profile.
COLUMN_HEURISTICS = [
    (re.compile(r"e-?mail", re.I), "redact"),
    (re.compile(r"\b(ssn|social.?security|tax.?id|ein|passport|license|dl.?num)", re.I), "drop"),
    (re.compile(r"(card.?num|cardnumber|credit.?card|cvv|iban|routing|bank.?acct|bank.?account)", re.I), "drop"),
    (re.compile(r"(dob|birth)", re.I), "drop"),
    (re.compile(r"(ip.?address|\bip\b|device.?id|user.?agent|mac.?address|fingerprint)", re.I), "redact"),
    (re.compile(r"(street|address|addr\b|zip|postal|postcode)", re.I), "drop"),
    (re.compile(r"(latitude|longitude|\blat\b|\blng\b|\blon\b|geo)", re.I), "drop"),
    (re.compile(r"(phone|mobile|cell|telephone|\btel\b|fax)", re.I), "redact"),
    (re.compile(r"(product|item|brand|company|business|merchant|store|language|"
                r"file|field|event|page|screen|plan|tier|category)[\s_\-]*name", re.I), "keep"),
    (re.compile(r"(author|reviewer|\bname\b|full.?name|first.?name|last.?name|customer.?name|"
                r"user.?name|username|display.?name|contact|nickname|handle|profile)", re.I), "hash"),
    (re.compile(r"(\btoken\b|api.?key|secret|password|passwd|credential|auth.?key)", re.I), "drop"),
    (re.compile(r"(\buuid\b|\bguid\b|session.?id|visitor.?id|anonymous.?id)", re.I), "hash"),
    (re.compile(r"(customer.?id|user.?id|member.?id|account.?id|external.?id|"
                r"client.?id|subscriber.?id|employee.?id)", re.I), "hash"),
    (re.compile(r"(review|comment|text|body|content|title|description|note|response|"
                r"reply|feedback|message|summary|detail|verbatim)", re.I), "scrub"),
]

# Columns whose values seed per-row name redaction inside text fields.
NAME_COLUMN_RE = re.compile(
    r"(author|reviewer|name|contact|nickname|handle|profile|user)", re.I
)


def collect_names(record, profile=None):
    """Walk a (possibly nested) record and collect person-name values, so JSON
    sources feed real strings into name redaction instead of a stringified dict."""
    found = []
    if isinstance(record, dict):
        for k, v in record.items():
            if isinstance(v, (dict, list)):
                found.extend(collect_names(v, profile))
            elif isinstance(v, str) and v.strip() and NAME_COLUMN_RE.search(k):
                if classify_column(k, profile or {}, "scrub") == "hash":
                    found.append(v)
    elif isinstance(record, list):
        for item in record:
            found.extend(collect_names(item, profile))
    return found


def classify_column(header: str, profile: dict, unknown_action: str) -> str:
    if header in profile:
        return profile[header]
    stripped = header.strip()
    if stripped in profile:
        return profile[stripped]
    for pattern, action in COLUMN_HEURISTICS:
        if pattern.search(stripped):
            return action
    return unknown_action


# --------------------------------------------------------------------------
# Scrubber
# --------------------------------------------------------------------------

class Scrubber:
    """Applies the free-text detectors. Counts every hit for the audit report."""

    def __init__(self, salt: str, strip_urls: bool = True, scrub_units: bool = True,
                 allow_domains=()):
        self.salt = salt
        self.strip_urls = strip_urls
        self.scrub_units = scrub_units
        self.allow_domains = {d.lower().lstrip("@") for d in allow_domains}
        self.hits = Counter()

    # -- pseudonyms --------------------------------------------------------

    def pseudonym(self, value: str, label: str = "ID") -> str:
        value = (value or "").strip()
        if not value:
            return ""
        digest = hashlib.sha256((self.salt + "\x00" + value.lower()).encode("utf-8")).hexdigest()
        return f"[{label}:{digest[:8]}]"

    # -- detectors ---------------------------------------------------------

    def _sub(self, pattern, repl, text, name):
        new, n = pattern.subn(repl, text)
        if n:
            self.hits[name] += n
        return new

    def _scrub_cards(self, text: str) -> str:
        def repl(m):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                self.hits["card"] += 1
                return "[CARD]"
            return m.group(0)
        return CARD_CANDIDATE_RE.sub(repl, text)

    def _scrub_emails(self, text: str) -> str:
        def repl(m):
            addr = m.group(0)
            domain = addr.rsplit("@", 1)[-1].lower()
            if any(domain == d or domain.endswith("." + d) for d in self.allow_domains):
                self.hits["email_allowed"] += 1
                return addr
            self.hits["email"] += 1
            return "[EMAIL]"
        return EMAIL_RE.sub(repl, text)

    def _scrub_urls(self, text: str) -> str:
        def repl(m):
            url = m.group(0)
            base = url.split("?", 1)[0].split("#", 1)[0]
            if base != url:
                self.hits["url_query"] += 1
            return base
        return URL_RE.sub(repl, text)

    def scrub(self, text: str, names=None) -> str:
        """Redact identifiers in a free-text value. `names` are extra literal
        names (e.g. the row's Author) to redact with a stable pseudonym."""
        if not text:
            return text
        out = text

        out = self._scrub_emails(out)
        if self.strip_urls:
            out = self._scrub_urls(out)
        out = self._sub(SSN_RE, "[SSN]", out, "ssn")
        out = self._sub(SSN_CONTEXT_RE, r"\1[SSN]", out, "ssn")
        out = self._scrub_cards(out)
        out = self._sub(ACCOUNT_CONTEXT_RE, r"\1[ACCOUNT]", out, "account_number")
        out = self._sub(PHONE_RE, "[PHONE]", out, "phone")
        out = self._sub(IPV4_RE, "[IP]", out, "ip")
        out = self._sub(DOB_RE, r"\1[DOB]", out, "dob")
        out = self._sub(STREET_RE, "[ADDRESS]", out, "street_address")
        if self.scrub_units:
            out = self._sub(UNIT_RE, "[UNIT]", out, "unit")

        out = self._scrub_names(out, names or [])
        return out

    def _scrub_names(self, text: str, names) -> str:
        # 1. Literal names carried over from this row's name columns.
        for raw in names:
            raw = (raw or "").strip()
            if not raw:
                continue
            token = self.pseudonym(raw, "PERSON")
            full = re.compile(r"(?<!\w)" + re.escape(raw) + r"(?!\w)", re.IGNORECASE)
            text = self._sub(full, token, text, "name_from_column")
            for part in re.split(r"[\s,]+", raw):
                part = part.strip(".'\"")
                if len(part) < 3 or not part[0].isalpha():
                    continue
                if part.lower() in NAME_STOPWORDS or not part[0].isupper():
                    continue
                if part.lower() in AMBIGUOUS_NAME_WORDS:
                    continue
                part_re = re.compile(r"(?<!\w)" + re.escape(part) + r"(?!\w)", re.IGNORECASE)
                text = self._sub(part_re, token, text, "name_from_column")

        # 2. Names the writer volunteered in the text itself.
        for pattern in NAME_CONTEXT_RES:
            def repl(m):
                name = m.group(2)
                if name.lower() in NAME_STOPWORDS:
                    return m.group(0)
                self.hits["name_in_text"] += 1
                return m.group(1) + self.pseudonym(name, "PERSON")
            text = pattern.sub(repl, text)
        return text


# --------------------------------------------------------------------------
# Optional LLM pass for residual names
# --------------------------------------------------------------------------

LLM_BATCH = 30

LLM_PROMPT = """You are a PII reviewer. Below are {n} short customer-review snippets that have
already been run through a regex PII scrubber (placeholders like [EMAIL], [PHONE], [PERSON:ab12cd34]
are already-redacted values — ignore them).

Find any REMAINING direct personal identifiers: real person names (reviewers, family members,
named employees), physical addresses, account or order numbers tied to an individual, or any
other string that could identify a specific private person.

Do NOT flag: company names, product names, brand names, generic job titles, city/state names on
their own, dollar amounts, dates, or existing placeholders.

{snippets}

Respond with JSON only:
{{"spans": ["<exact substring to redact>", ...]}}
Return the substrings verbatim as they appear. Return {{"spans": []}} if there is nothing to redact.
"""


def llm_find_spans(texts, scrubber: Scrubber):
    """Ask Claude for residual identifiers. Returns {literal: replacement}."""
    try:
        from helpers import call_claude
    except ImportError:
        print("warning: --llm requires helpers.py and the anthropic package; skipping",
              file=sys.stderr)
        return {}

    mapping = {}
    batch = [t for t in texts if t and len(t.strip()) > 10]
    for i in range(0, len(batch), LLM_BATCH):
        chunk = batch[i:i + LLM_BATCH]
        snippets = "\n\n".join(f"[{j}] {t[:1200]}" for j, t in enumerate(chunk))
        try:
            result = call_claude(LLM_PROMPT.format(n=len(chunk), snippets=snippets), max_tokens=2048)
        except Exception as exc:  # network / parse failures shouldn't lose the run
            print(f"warning: LLM pass failed on batch {i // LLM_BATCH}: {exc}", file=sys.stderr)
            continue
        for span in result.get("spans", []):
            span = (span or "").strip()
            if len(span) < 3 or span.startswith("["):
                continue
            mapping[span] = scrubber.pseudonym(span, "PERSON")
    return mapping


def apply_spans(text: str, mapping: dict, scrubber: Scrubber) -> str:
    if not text or not mapping:
        return text
    for literal in sorted(mapping, key=len, reverse=True):
        pattern = re.compile(r"(?<!\w)" + re.escape(literal) + r"(?!\w)")
        text, n = pattern.subn(mapping[literal], text)
        if n:
            scrubber.hits["llm_span"] += n
    return text


# --------------------------------------------------------------------------
# Row processing
# --------------------------------------------------------------------------

def build_policy(headers, profile, unknown_action, overrides):
    policy = {}
    for h in headers:
        policy[h] = classify_column(h, profile, unknown_action)
    for h, action in overrides.items():
        # Allow overriding a column that isn't in the file without failing.
        policy[h] = action
    return policy


def process_row(row, policy, scrubber, name_columns):
    names = [str(row.get(c, "") or "") for c in name_columns]
    out = {}
    for key, value in row.items():
        action = policy.get(key, "scrub")
        value = "" if value is None else str(value)
        if action == "drop":
            scrubber.hits[f"col:{key}:drop"] += 1
            continue
        if action == "keep":
            out[key] = value
        elif action == "redact":
            if value.strip():
                scrubber.hits[f"col:{key}:redact"] += 1
            out[key] = "[REDACTED]" if value.strip() else ""
        elif action == "hash":
            if value.strip():
                scrubber.hits[f"col:{key}:hash"] += 1
            label = "PERSON" if NAME_COLUMN_RE.search(key) else "ID"
            out[key] = scrubber.pseudonym(value, label)
        else:  # scrub
            out[key] = scrubber.scrub(value, names=names)
    return out


def scrub_json_value(value, policy, scrubber, names, key=None):
    """Recursive variant for JSON records with nested objects."""
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            action = policy.get(k)
            if action is None:
                action = classify_column(k, policy.get("__profile__", {}), "scrub")
            if action == "drop":
                scrubber.hits[f"col:{k}:drop"] += 1
                continue
            if action == "redact" and not isinstance(v, (dict, list)):
                result[k] = "[REDACTED]" if str(v or "").strip() else v
                scrubber.hits[f"col:{k}:redact"] += 1
            elif action == "hash" and not isinstance(v, (dict, list)):
                label = "PERSON" if NAME_COLUMN_RE.search(k) else "ID"
                result[k] = scrubber.pseudonym(str(v or ""), label)
                scrubber.hits[f"col:{k}:hash"] += 1
            elif action == "keep" and not isinstance(v, (dict, list)):
                result[k] = v
            else:
                result[k] = scrub_json_value(v, policy, scrubber, names, key=k)
        return result
    if isinstance(value, list):
        return [scrub_json_value(v, policy, scrubber, names, key=key) for v in value]
    if isinstance(value, str):
        return scrubber.scrub(value, names=names)
    return value


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

VERIFY_DETECTORS = [
    ("email", EMAIL_RE),
    ("ssn", SSN_RE),
    ("phone", PHONE_RE),
    ("ip", IPV4_RE),
    ("street_address", STREET_RE),
]


def verify(rows, policy=None, allow_domains=()):
    """Re-scan the scrubbed text columns and report residual detector matches.
    Columns marked keep (URL, Review ID) are deliberate and excluded — their long
    digit runs otherwise look like phone numbers and drown out real findings."""
    leftovers = Counter()
    for row in rows:
        if policy is None:
            fields = row.values()
        else:
            fields = [v for k, v in row.items() if policy.get(k, "scrub") == "scrub"]
        blob = " ".join(str(v) for v in fields)
        for name, pattern in VERIFY_DETECTORS:
            found = pattern.findall(blob)
            if name == "email" and allow_domains:
                found = [m for m in found
                         if not any(m.lower().rsplit("@", 1)[-1] == d
                                    or m.lower().endswith("." + d) for d in allow_domains)]
            if found:
                leftovers[name] += len(found)
    return leftovers


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def default_output(path: Path) -> Path:
    return path.with_suffix("") .with_name(path.stem + ".clean" + path.suffix)


def read_records(path: Path):
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        delim = "\t" if suffix == ".tsv" else ","
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delim)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            for row in reader:
                yield {(k.strip() if k else k): v for k, v in row.items()}, headers
    elif suffix == ".jsonl":
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line), None
    elif suffix == ".json":
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        records = data if isinstance(data, list) else data.get("reviews") or data.get("data") or [data]
        for rec in records:
            yield rec, None
    else:
        raise SystemExit(f"unsupported input type: {suffix} (use .csv, .tsv, .json or .jsonl)")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Strip PII from review exports (CSV/TSV/JSON/JSONL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", nargs="?", help="input file (.csv, .tsv, .json, .jsonl)")
    p.add_argument("-o", "--output", help="output path (default: <input>.clean.<ext>)")
    p.add_argument("--profile", default="auto",
                   choices=["auto", "reviewtrackers", "generic"],
                   help="column profile; 'auto' detects ReviewTrackers by its headers")
    p.add_argument("--config", help="JSON file of {\"Column\": \"action\"} overrides")
    p.add_argument("--set", action="append", default=[], metavar="COL=ACTION",
                   help="override one column, e.g. --set Author=drop (repeatable)")
    p.add_argument("--unknown", default="scrub", choices=ACTIONS,
                   help="action for columns not matched by profile or heuristics")
    p.add_argument("--salt", default=os.environ.get("PII_SALT"),
                   help="pseudonym salt; set it (or $PII_SALT) for stable IDs across runs")
    p.add_argument("--allow-domain", action="append", default=[], metavar="DOMAIN",
                   help="keep emails at this domain, e.g. --allow-domain perpay.com "
                        "(company support addresses; repeatable)")
    p.add_argument("--keep-url-queries", action="store_true",
                   help="keep URL query strings (they often carry tracking identifiers)")
    p.add_argument("--no-unit-scrub", action="store_true",
                   help="don't redact apt/unit/suite fragments")
    p.add_argument("--llm", action="store_true",
                   help="second pass with Claude for residual names "
                        "(sends already-scrubbed text to the Anthropic API)")
    p.add_argument("--audit", action="store_true", help="scan and report only; write nothing")
    p.add_argument("--report", help="write the detection report to this JSON file")
    p.add_argument("--no-verify", action="store_true", help="skip the post-write re-scan")
    p.add_argument("--selftest", action="store_true", help="run built-in detector tests and exit")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.input:
        p.error("input file required (or use --selftest)")

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"no such file: {in_path}")

    salt = args.salt
    if not salt:
        salt = secrets.token_hex(16)
        print("note: no --salt/$PII_SALT given; using a random salt. Pseudonyms will not "
              "match across runs.", file=sys.stderr)

    overrides = {}
    if args.config:
        overrides.update(json.loads(Path(args.config).read_text()))
    for item in args.set:
        if "=" not in item:
            p.error(f"--set expects COL=ACTION, got {item!r}")
        col, action = item.split("=", 1)
        if action not in ACTIONS:
            p.error(f"unknown action {action!r}; choose from {', '.join(ACTIONS)}")
        overrides[col.strip()] = action.strip()

    scrubber = Scrubber(salt=salt,
                        strip_urls=not args.keep_url_queries,
                        scrub_units=not args.no_unit_scrub,
                        allow_domains=args.allow_domain)

    records = list(read_records(in_path))
    if not records:
        raise SystemExit("input contained no records")

    first_row, headers = records[0]
    if headers is None:
        headers = list(first_row.keys())

    profile_name = args.profile
    if profile_name == "auto":
        overlap = len(set(headers) & set(REVIEWTRACKERS_PROFILE))
        profile_name = "reviewtrackers" if overlap >= 8 else "generic"
    profile = PROFILES[profile_name]

    policy = build_policy(headers, profile, args.unknown, overrides)
    name_columns = [h for h in headers if policy.get(h) == "hash" and NAME_COLUMN_RE.search(h)]

    print(f"profile: {profile_name} | columns: {len(headers)} | "
          f"name columns: {name_columns or 'none'}", file=sys.stderr)
    dropped = [h for h, a in policy.items() if a == "drop"]
    if dropped:
        print(f"dropping: {', '.join(dropped)}", file=sys.stderr)

    is_json = in_path.suffix.lower() in (".json", ".jsonl")
    out_rows = []
    for rec, _ in records:
        if is_json:
            names = collect_names(rec, profile)
            json_policy = dict(policy)
            json_policy["__profile__"] = profile
            out_rows.append(scrub_json_value(rec, json_policy, scrubber, names))
        else:
            out_rows.append(process_row(rec, policy, scrubber, name_columns))

    if args.llm:
        text_cols = [h for h, a in policy.items() if a == "scrub"]
        texts = [str(r.get(c, "")) for r in out_rows for c in text_cols if r.get(c)]
        mapping = llm_find_spans(texts, scrubber)
        if mapping:
            print(f"LLM pass: {len(mapping)} residual span(s) to redact", file=sys.stderr)
            for row in out_rows:
                for c in text_cols:
                    if row.get(c):
                        row[c] = apply_spans(str(row[c]), mapping, scrubber)

    report = {
        "input": str(in_path),
        "records": len(out_rows),
        "profile": profile_name,
        "policy": {h: policy[h] for h in headers},
        "detections": dict(sorted(scrubber.hits.items())),
    }

    if not args.audit:
        out_path = Path(args.output) if args.output else default_output(in_path)
        write_records(out_path, out_rows, headers, policy, is_json, in_path.suffix.lower())
        report["output"] = str(out_path)
        print(f"wrote {len(out_rows)} records -> {out_path}", file=sys.stderr)

    if not args.no_verify:
        leftovers = verify(out_rows, policy, scrubber.allow_domains)
        report["verify_leftovers"] = dict(leftovers)
        if leftovers:
            print(f"verify: possible residual matches {dict(leftovers)}", file=sys.stderr)
        else:
            print("verify: clean", file=sys.stderr)

    summary = {k: v for k, v in sorted(scrubber.hits.items()) if not k.startswith("col:")}
    col_actions = Counter()
    for k, v in scrubber.hits.items():
        if k.startswith("col:"):
            col_actions[k.rsplit(":", 1)[1]] += v
    print("column actions: " + (json.dumps(dict(sorted(col_actions.items())))
                                if col_actions else "none"), file=sys.stderr)
    print("in-text detections: " + (json.dumps(summary) if summary else "none"), file=sys.stderr)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
        print(f"report -> {args.report}", file=sys.stderr)
    return 0


def write_records(out_path, rows, headers, policy, is_json, suffix):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if is_json:
        if suffix == ".jsonl":
            with out_path.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    out_headers = [h for h in headers if policy.get(h) != "drop"]
    delim = "\t" if suffix == ".tsv" else ","
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_headers, delimiter=delim,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def selftest():
    s = Scrubber(salt="test-salt")
    cases = [
        ("Email me at jane.doe@example.com", "[EMAIL]", True),
        ("Call 215-555-0134 please", "[PHONE]", True),
        ("call (215) 555-0134", "[PHONE]", True),
        ("my ssn 078-05-1120 was exposed", "[SSN]", True),
        ("card 4111 1111 1111 1111 declined", "[CARD]", True),
        ("order 1234567890123456 shipped", "[CARD]", False),   # fails Luhn
        ("I live at 123 Main Street", "[ADDRESS]", True),
        ("shipped to 456 N Oak Ave", "[ADDRESS]", True),
        ("at 1200 42nd St now", "[ADDRESS]", True),
        ("sent to 789 maple avenue", "[ADDRESS]", True),      # strong suffix, lowercase
        ("1 star way too expensive", "[ADDRESS]", False),
        ("took 3 days drive to get here", "[ADDRESS]", False),
        ("2 place order limit", "[ADDRESS]", False),
        ("apt 4B flooded", "[UNIT]", True),
        ("unit 12 was fine", "[UNIT]", True),
        ("the unit I received was broken", "[UNIT]", False),   # no digit
        ("great for building credit", "[UNIT]", False),        # "building" is not an address
        ("they kept stealing from me", "[UNIT]", False),       # "ste" must hit a word boundary
        ("take the next step", "[UNIT]", False),
        ("logged from 192.168.1.44", "[IP]", True),
        ("dob 04/12/1988 rejected", "[DOB]", True),
        ("account number 998877665544 is wrong", "[ACCOUNT]", True),
        ("My name is Jordan Smith and I am upset", "[PERSON:", True),
        ("Sincerely,\nJordan Smith", "[PERSON:", True),
        ("Rated 5 stars, shipped in 2 days", "[", False),
        ("Perpay charged me $49.99 on 3/14", "[", False),
    ]
    failures = []
    for text, token, should_hit in cases:
        got = s.scrub(text)
        hit = token in got
        if hit != should_hit:
            failures.append(f"  {text!r}\n    -> {got!r}  (expected {token!r} "
                            f"{'present' if should_hit else 'absent'})")

    # Ambiguous first names are kept as words when only the token matches
    got = s.scrub("I will never order again", names=["Will Turner"])
    if "[PERSON:" in got:
        failures.append(f"  ambiguous name token over-redacted: {got!r}")
    got = s.scrub("Will Turner here, unhappy", names=["Will Turner"])
    if "[PERSON:" not in got:
        failures.append(f"  full ambiguous name not redacted: {got!r}")

    # Allowed company domains survive; customer addresses don't
    a = Scrubber(salt="test-salt", allow_domains=["perpay.com"])
    got = a.scrub("email support@perpay.com not jane@gmail.com")
    if "support@perpay.com" not in got or "[EMAIL]" not in got:
        failures.append(f"  allow-domain handling wrong: {got!r}")

    # verify() ignores kept columns, flags scrubbed ones
    rows = [{"URL": "https://x.com/r/2155550134", "Review": "call 215-555-0134"}]
    if verify(rows, {"URL": "keep", "Review": "scrub"}).get("phone") != 1:
        failures.append("  verify() should count only scrubbed columns")

    # URL query stripping
    got = s.scrub("see https://x.com/r/9?utm_source=email&uid=abc123")
    if "?" in got:
        failures.append(f"  URL query not stripped: {got!r}")

    # Per-row name redaction + pseudonym stability
    got = s.scrub("Jordan had a bad time", names=["Jordan Reyes"])
    if "[PERSON:" not in got:
        failures.append(f"  name-from-column not redacted: {got!r}")
    if s.pseudonym("Jordan Reyes", "PERSON") != s.pseudonym("jordan reyes", "PERSON"):
        failures.append("  pseudonym not case-stable")
    if Scrubber(salt="other").pseudonym("Jordan Reyes") == s.pseudonym("Jordan Reyes"):
        failures.append("  pseudonym not salt-dependent")

    # Column classification
    checks = [("Email Address", "redact"), ("Customer SSN", "drop"), ("Author", "hash"),
              ("Review Body", "scrub"), ("Rating", "scrub"),
              ("ip_address", "redact"), ("Mailing Address", "drop"),
              ("name", "hash"), ("Product Name", "keep"),
              ("uuid", "hash"), ("session_id", "hash"), ("api_key", "drop"),
              ("password", "drop"),
              ("Original Language Name", "keep")]
    for header, expected in checks:
        actual = classify_column(header, {}, "scrub")
        if actual != expected:
            failures.append(f"  classify_column({header!r}) -> {actual!r}, expected {expected!r}")

    if failures:
        print("SELFTEST FAILED:\n" + "\n".join(failures))
        return 1
    print(f"selftest: {len(cases) + 12} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
