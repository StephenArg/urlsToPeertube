#!/usr/bin/env python3
import argparse
import csv
import html
import json
import re
import sys
import unicodedata
from pathlib import Path


QUOTE_TRANSLATIONS = str.maketrans({
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "«": '"',
    "»": '"',
    "＂": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "＇": "'",
    "`": "'",
    "´": "'",
    "′": "'",
    "‵": "'",
    "–": "-",
    "—": "-",
    "−": "-",
    "‐": "-",
    "-": "-",
    "\u00A0": " ",   # non-breaking space
    "\u200B": "",    # zero-width space
    "\u200C": "",
    "\u200D": "",
    "\uFEFF": "",    # BOM / zero-width no-break space
})


def normalize_title(s: str) -> str:
    if s is None:
        return ""

    # Convert HTML entities like &quot; if they appear
    s = html.unescape(str(s))

    # Unicode normalization first
    s = unicodedata.normalize("NFKC", s)

    # Normalize quotes, apostrophes, dashes, weird spaces
    s = s.translate(QUOTE_TRANSLATIONS)

    # Common DB/export oddities
    s = s.replace("''", "'")
    s = s.replace('""', '"')

    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Case-insensitive comparison
    s = s.casefold()

    return s


def load_csv_titles(csv_path: Path) -> set[str]:
    titles = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)

        for row in reader:
            if not row:
                continue

            # For a 1-column CSV export, row[0] is the title.
            # If a weird file has multiple columns, join them back.
            raw = row[0] if len(row) == 1 else ",".join(row)
            raw = raw.strip()

            if not raw:
                continue

            # Skip obvious headers
            if raw.casefold() in {"title", "name"}:
                continue

            titles.add(normalize_title(raw))

    return titles


def extract_title(item):
    # Supports:
    #   ["title", "url"]
    #   {"title": "..."}
    #   {"name": "..."}
    if isinstance(item, list):
        if item and isinstance(item[0], str):
            return item[0]
        return None

    if isinstance(item, dict):
        for key in ("title", "name"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return None

    return None


def filter_items(items, csv_titles: set[str]):
    kept = []
    removed = []
    skipped = []

    for item in items:
        title = extract_title(item)

        if title is None:
            skipped.append(item)
            kept.append(item)
            continue

        norm = normalize_title(title)

        if norm in csv_titles:
            removed.append(item)
        else:
            kept.append(item)

    return kept, removed, skipped


def main():
    parser = argparse.ArgumentParser(
        description="Filter JSON entries whose titles match titles from a CSV after normalization."
    )
    parser.add_argument("json_file", help="Input JSON file")
    parser.add_argument("csv_file", help="Input CSV file with one title per line")
    parser.add_argument("output_json", help="Output JSON file with unmatched entries")
    parser.add_argument(
        "--removed-json",
        help="Optional file to write removed/matched entries",
        default=None,
    )
    args = parser.parse_args()

    json_path = Path(args.json_file)
    csv_path = Path(args.csv_file)
    output_path = Path(args.output_json)

    try:
        with json_path.open("r", encoding="utf-8") as f:
            items = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {json_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(items, list):
        print("Top-level JSON must be a list.", file=sys.stderr)
        sys.exit(1)

    csv_titles = load_csv_titles(csv_path)
    kept, removed, skipped = filter_items(items, csv_titles)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)

    if args.removed_json:
        with Path(args.removed_json).open("w", encoding="utf-8") as f:
            json.dump(removed, f, ensure_ascii=False, indent=2)

    print(f"CSV normalized titles loaded: {len(csv_titles)}")
    print(f"Input JSON items: {len(items)}")
    print(f"Removed matched items: {len(removed)}")
    print(f"Kept unmatched items: {len(kept)}")
    print(f"Skipped (no detectable title): {len(skipped)}")


if __name__ == "__main__":
    main()