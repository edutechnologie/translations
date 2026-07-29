"""
Step 6 - validate translation files, meant to run over the merged output
(after the ai-outputs results have been reviewed and merged into place),
across the whole translations tree, not just files this pipeline touched.

PO files: shell out to `msgfmt --check`, same tool gettext itself uses to
compile .mo files, so this is exactly what would catch a broken plural
header or a mismatched placeholder before it reaches production. This does
NOT write a .mo file anywhere - actual compilation is its own later step,
this only checks.

JSON files: there's no gettext-equivalent tool for arbitrary key/value json,
so we do what's actually checkable by hand:
  - the file parses as valid json
  - no leftover None values (an unfilled step-3 placeholder that never got
    a translation written into it)
  - if an english counterpart file can be found next to it, compare keys
    (nothing missing or extra) and compare placeholder tokens per key
    (e.g. "%(count)s" in the english value must also appear in the polish
    one) - this is the closest thing to what msgfmt --check does for PO

Android/iOS resource files (xml, strings, etc) are assumed fine and skipped
entirely, they aren't part of this pipeline.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
import shutil


PLACEHOLDER_RE = re.compile(r"%\([a-zA-Z0-9_]+\)[sd]|%[sd]|\{\{?[a-zA-Z0-9_]+\}?\}")


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def find_po_files(root: Path) -> list[Path]:
    # skip the english source locale, it's not a translation, nothing to validate
    return [p for p in root.rglob("*.po") if "/en/LC_MESSAGES/" not in str(p)]


def find_json_files(root: Path) -> list[Path]:
    return [
        p for p in root.rglob("*.json") if "/en/" not in str(p) and p.name != "en.json"
    ]


def infer_en_counterpart(pl_path: Path) -> Path | None:
    # best effort, translation layouts vary. try the common patterns, if
    # none exist just skip the cross-file checks for this file
    candidates = [
        Path(str(pl_path).replace("/pl/", "/en/")),
        Path(str(pl_path).replace("/pl.json", "/en.json")),
        pl_path.parent / "en.json",
    ]
    for c in candidates:
        if c.exists() and c != pl_path:
            return c
    return None


# ---------------------------------------------------------------------------
# po validation
# ---------------------------------------------------------------------------

try:
    import i18n.validate as edx_i18n_validate

    HAVE_EDX_I18N_TOOLS = True
except ImportError:
    HAVE_EDX_I18N_TOOLS = False


def validate_po_file(po_file: Path) -> dict:
    valid = True
    output = ""

    if not shutil.which("msgfmt"):
        print(
            "Error: 'msgfmt' not found. Install gettext:\n"
            "  Ubuntu/Debian: sudo apt install gettext\n"
            "  macOS: brew install gettext\n"
            "  Windows: https://mlocati.github.io/articles/gettext-iconv-windows.html"
        )
        return {"valid": False, "output": "msgfmt not found"}

    # catches syntax/compile errors and (with the po-format flag) percent-format
    # mismatches between msgid and msgstr
    result = subprocess.run(
        ["msgfmt", "-v", "--strict", "--check", str(po_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        valid = False
    output += result.stdout.decode("utf-8", errors="replace")
    output += result.stderr.decode("utf-8", errors="replace")

    if HAVE_EDX_I18N_TOOLS:
        # catches what msgfmt doesn't: non-bmp characters (breaks django's js catalog),
        # mismatched html tags/placeholders between source and translation, and
        # plural-aware empty translation detection
        try:
            problems = edx_i18n_validate.check_messages(str(po_file))
        except Exception as exc:
            problems = []
            output += f"\ncheck_messages crashed: {exc}"
            valid = False

        if problems:
            valid = False
            for problem in problems:
                desc, msgid = problem[0], problem[1]
                output += f"\n{desc}: {msgid!r}"
                for extra in problem[2:]:
                    output += f"\n  -> {extra!r}"
    else:
        output += "\n(note: edx-i18n-tools not installed, skipped tag/placeholder/astral checks)"

    return {"file": str(po_file), "valid": valid, "output": output.strip()}


# ---------------------------------------------------------------------------
# json validation
# ---------------------------------------------------------------------------


def validate_json_file(json_file: Path) -> dict:
    problems = []

    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "file": str(json_file),
            "valid": False,
            "output": f"invalid json: {exc}",
        }

    for key, value in data.items():
        if value is None:
            problems.append(f"key {key!r} was never translated (still null)")
        elif isinstance(value, str) and value.strip() == "":
            problems.append(f"key {key!r} has an empty translation")

    en_file = infer_en_counterpart(json_file)
    if en_file is None:
        problems.append(
            "(info) no english counterpart found, skipped key-parity and placeholder checks"
        )
    else:
        en_data = json.loads(en_file.read_text(encoding="utf-8"))

        missing = set(en_data) - set(data)
        extra = set(data) - set(en_data)
        if missing:
            problems.append(f"missing keys vs english: {sorted(missing)}")
        if extra:
            problems.append(f"extra keys not present in english: {sorted(extra)}")

        for key in set(en_data) & set(data):
            en_val, pl_val = en_data[key], data[key]
            if not isinstance(en_val, str) or not isinstance(pl_val, str):
                continue
            en_tokens = set(PLACEHOLDER_RE.findall(en_val))
            pl_tokens = set(PLACEHOLDER_RE.findall(pl_val))
            if en_tokens != pl_tokens:
                problems.append(
                    f"key {key!r}: placeholder mismatch, english has {en_tokens}, polish has {pl_tokens}"
                )

    # only count real problems as failures, the "(info)" line above is just a note
    hard_problems = [p for p in problems if not p.startswith("(info)")]
    return {
        "file": str(json_file),
        "valid": len(hard_problems) == 0,
        "output": "\n".join(problems),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run(translations_dir: str):
    root = Path(translations_dir)
    po_files = find_po_files(root)
    json_files = find_json_files(root)

    print(
        f"found {len(po_files)} po files and {len(json_files)} json files under {root}"
    )

    results = []
    for f in po_files:
        results.append(validate_po_file(f))
    for f in json_files:
        results.append(validate_json_file(f))

    failed = [r for r in results if not r["valid"]]

    for r in results:
        status = "VALID" if r["valid"] else "INVALID"
        print(f"{status}: {r['file']}")

    if failed:
        print(f"\n{len(failed)} file(s) failed validation:\n", file=sys.stderr)
        for r in failed:
            print(f"--- {r['file']} ---", file=sys.stderr)
            print(r["output"], file=sys.stderr)
            print(file=sys.stderr)

        print("---------------------------------------", file=sys.stderr)
        print("FAILURE: some translations are invalid.", file=sys.stderr)
        print("---------------------------------------", file=sys.stderr)
        return 1

    print("\n-----------------------------------------")
    print("SUCCESS: all translation files are valid.")
    print("-----------------------------------------")
    return 0


if __name__ == "__main__":
    translations_dir = sys.argv[1] if len(sys.argv) > 1 else "translations"
    sys.exit(run(translations_dir))
