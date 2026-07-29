"""
Step 5 - merge ai-outputs/results into staging.

Reads every result file under ai-outputs/results/*.json (one per chunk from
step 4) and writes each translation into the staging PO/JSON file it belongs
to. The staging files already exist with empty placeholders (created by
process_po / process_json in step 3) - we are only filling them in here, not
creating them.

We open and save each staging file exactly once, even if its translations
came from several different chunks (a single django.po file can have both
PO and PO_PLURAL results targeting it).

If a key can't be found in a staging file, that means something upstream is
broken - we never generate keys here, we only copy them through - so this is
logged loudly as an error and skipped, not silently dropped.
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

import polib


AI_OUTPUTS_DIR = Path("ai-outputs")
RESULTS_DIR = AI_OUTPUTS_DIR / "results"


def load_all_results() -> list[dict]:
    items = []
    for f in sorted(RESULTS_DIR.glob("*.json")):
        items.extend(json.loads(f.read_text(encoding="utf-8")))
    return items


def group_by_staging_path(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["staging_path"]].append(item)
    return grouped


def merge_po_file(staging_path: str, items: list[dict], errors: list[str]) -> int:
    po = polib.pofile(staging_path)
    written = 0

    for item in items:
        entry = po.find(item["key"])
        if entry is None:
            errors.append(
                f"{staging_path}: key not found in staging po: {item['key']!r}"
            )
            continue

        if item["format"] == "PO_PLURAL":
            entry.msgstr_plural = item["forms"]
        else:
            entry.msgstr = item["translated"]
        written += 1

    po.save(staging_path)
    return written


def merge_json_file(staging_path: str, items: list[dict], errors: list[str]) -> int:
    path = Path(staging_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    written = 0

    for item in items:
        if item["key"] not in data:
            errors.append(
                f"{staging_path}: key not found in staging json: {item['key']!r}"
            )
            continue
        data[item["key"]] = item["translated"]
        written += 1

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def run():
    if not RESULTS_DIR.exists():
        print(f"no results dir at {RESULTS_DIR}, nothing to merge")
        return

    items = load_all_results()
    if not items:
        print("no result items found, nothing to merge")
        return

    grouped = group_by_staging_path(items)
    errors: list[str] = []
    total_written = 0

    for staging_path, group_items in grouped.items():
        fmt = group_items[0][
            "format"
        ]  # PO and PO_PLURAL both route to the po branch below

        if fmt in ("PO", "PO_PLURAL"):
            written = merge_po_file(staging_path, group_items, errors)
        else:
            written = merge_json_file(staging_path, group_items, errors)

        total_written += written
        print(f"{staging_path}: wrote {written}/{len(group_items)} translations")

    print(f"\ndone. {total_written} translations merged across {len(grouped)} files.")

    if errors:
        print(
            f"\n{len(errors)} key(s) could not be matched - this should never happen, "
            f"investigate before trusting this merge:",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)

        (AI_OUTPUTS_DIR / "merge_errors.log").write_text(
            "\n".join(errors), encoding="utf-8"
        )


if __name__ == "__main__":
    run()
