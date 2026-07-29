"""
build_staging.py — diffs current EN (translations/) against previous PL
(translations_ready/), writes translations_staging/ with what's unchanged
carried over, and a batch list of what needs (re)translation.
"""

import json
import logging
from pathlib import Path
import copy

import polib

from transifex_parser import parse_transifex_yml, ResourceFile

logger = logging.getLogger("build_staging")


def _remap_root(path: Path, repo_root: Path, new_root: str) -> Path:
    # repo root is /translations
    # relative polish is e.g. translations/xblock-google-drive/...
    # new path for processing is translations/nask-tooling/translations/xblock-google-drive/...
    rel = path.relative_to(repo_root)
    return repo_root / "nask-tooling" / new_root / rel
    


def _polish_translation_exists(entry, ready_map):
    ready_entry = ready_map.get(entry.msgid_with_context)
    if ready_entry is None:
        return False
    if entry.msgid_plural:
        # must have all 3 polish forms
        # each has to be a non-False string
        return len(ready_entry.msgstr_plural) >= 3 and all(
            s.strip() for s in ready_entry.msgstr_plural.values()
        )
    # if singular -> just retur non-empty check
    return ready_entry.msgstr.strip() != ""

def _is_entry_relevant(entry: polib.POEntry) -> bool:
    # skip the entry if msgid is empty for some reason
    # YES records as such exist
    return entry.msgid.strip() != ""

def process_po(rf: ResourceFile, repo_root: Path) -> list[dict]:
    # get paths ready
    ready_path = _remap_root(rf.pl_path, repo_root, "ready")
    staging_path = _remap_root(rf.pl_path, repo_root, "staging")
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    # read all required files and load using polib
    en_po = polib.pofile(str(rf.en_path))
    ready_po = polib.pofile(str(ready_path)) if ready_path.exists() else None

    # store whole entries from the .po file by msgid key with context
    # since we can have entries of the same key but under diff contexts
    # in the same file
    ready_map = {e.msgid_with_context: e for e in ready_po} if ready_po else {}

    out_po = polib.POFile()

    out_po.metadata = dict(en_po.metadata)
    # copy original (english) and fix the plural forms for polish
    out_po.metadata["Plural-Forms"] = (
        "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 "
        "&& (n%100<10 || n%100>=20) ? 1 : 2);"
    )
    
    batch = []
    # for each entry in original, most current msgids
    for entry in en_po:

        new_entry = copy.deepcopy(entry)

        if not _is_entry_relevant(entry):
            # nothing meaningful to translate -> just move it over to staging untouched
            out_po.append(new_entry)
            continue

        # check if translations for that entry already exist in our ready/
        if _polish_translation_exists(entry, ready_map):

            # create same entry in the staging/ catalog
            ready_entry = ready_map[entry.msgid_with_context]
            new_entry = polib.POEntry(
                msgid=entry.msgid,
                msgid_plural=entry.msgid_plural,
                msgstr=ready_entry.msgstr,
                msgstr_plural=ready_entry.msgstr_plural,
                msgctxt=entry.msgctxt,
                occurrences=entry.occurrences,
                comment=entry.comment,
                tcomment=entry.tcomment,
                flags=entry.flags,
            )
        else:
            # move everything over BUT the translations (singular/plural)
            new_entry.msgstr = ""
            new_entry.msgstr_plural = {}

            if entry.msgid_plural:
                batch.append({
                    "resource": rf.resource_name,
                    "format": "PO_PLURAL",
                    "staging_path": str(staging_path),
                    "key": entry.msgid,
                    "source": entry.msgid,
                    "source_plural": entry.msgid_plural,
                })
            else:
                batch.append({
                    "resource": rf.resource_name,
                    "format": "PO",
                    "staging_path": str(staging_path),
                    "key": entry.msgid,
                    "source": entry.msgid,
                })
        out_po.append(new_entry)
    # save full file
    out_po.save(str(staging_path))
    return batch


def process_json(rf: ResourceFile, repo_root: Path) -> list[dict]:
    ready_path = _remap_root(rf.pl_path, repo_root, "ready")
    staging_path = _remap_root(rf.pl_path, repo_root, "staging")
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    en_data = json.loads(rf.en_path.read_text(encoding="utf-8"))
    ready_data = (
        json.loads(ready_path.read_text(encoding="utf-8"))
        if ready_path.exists()
        else {}
    )

    out_data = {}
    batch = []

    # similar to process_po, just fill the key in later
    for key, en_val in en_data.items():
        if key in ready_data and ready_data[key]:
            out_data[key] = ready_data[key]
        else:
            out_data[key] = None
            batch.append(
                {
                    "resource": rf.resource_name,
                    "format": "KEYVALUEJSON",
                    "staging_path": str(staging_path),
                    "key": key,
                    "source": en_val,
                }
            )

    staging_path.write_text(
        json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return batch


def build_staging(yml_path: str, repo_root: str, lang: str = "pl") -> list[dict]:
    repo_root = Path(repo_root)
    resources = parse_transifex_yml(yml_path, repo_root, lang=lang)

    all_batches = []
    for rf in resources:
        if rf.file_format == "PO":
            all_batches += process_po(rf, repo_root)
        elif rf.file_format == "KEYVALUEJSON":
            all_batches += process_json(rf, repo_root)

    logger.info(
        f"{len(all_batches)} strings need (re)translation across {len(resources)} files"
    )
    return all_batches


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    yml = sys.argv[1] if len(sys.argv) > 1 else "../transifex.yml"
    root = sys.argv[2] if len(sys.argv) > 2 else "../"
    batches = build_staging(yml, root)
    Path("batch_todo.json").write_text(
        json.dumps(batches, ensure_ascii=False, indent=2)
    )
    print(f"wrote {len(batches)} items to batch_todo.json")
