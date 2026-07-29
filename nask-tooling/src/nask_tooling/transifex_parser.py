"""
Parses transifex.yml and resolves each resource to concrete
(source EN file, target PL file) pairs on disk.

Only PO (dir-based) and KEYVALUEJSON (file-based) are supported.
Everything else (YAML_GENERIC, etc.) is skipped and logged.
"""

from __future__ import annotations
import glob
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

from .config import REPO_ROOT, SUPPORTED_FORMATS, TRANSIFEX_YML_PATH, TARGET_LANG_CODE

logger = logging.getLogger("transifex_parser")




@dataclass
class ResourceFile:
    resource_name: str  # e.g. "xblock-submit-and-compare"
    file_format: str  # "PO" or "KEYVALUEJSON"
    en_path: Path  # absolute path to EN source file
    pl_path: Path  # absolute path to PL target file (may not exist yet)


def _resolve_dir_po(
    repo_root: Path, name: str, entry: dict, lang: str
) -> Iterator[ResourceFile]:
    """
    filter_type: dir, file_format: PO
    Walks source_file_dir for *.po files, maps each to translation_files_expression/<lang>/
    preserving the relative subpath after the source dir (e.g. LC_MESSAGES/django.po).
    """
    source_dir = repo_root / entry["source_file_dir"]
    target_template = entry["translation_files_expression"]  # e.g. '.../locale/<lang>/'

    if not source_dir.exists():
        logger.warning(f"[{name}] source_file_dir not found: {source_dir}")
        return

    po_files = sorted(source_dir.rglob("*.po"))
    if not po_files:
        logger.warning(f"[{name}] no .po files found under {source_dir}")
        return

    target_root = repo_root / target_template.replace("<lang>", lang)

    # even tho it's usually just two...
    for po_file in po_files:
        rel_subpath = po_file.relative_to(source_dir)  # e.g. LC_MESSAGES/django.po
        pl_path = target_root / rel_subpath
        yield ResourceFile(
            resource_name=name,
            file_format="PO",
            en_path=po_file,
            pl_path=pl_path,
        )


def _resolve_file_json(
    repo_root: Path, name: str, entry: dict, lang: str
) -> Iterator[ResourceFile]:
    """
    filter_type: file, file_format: KEYVALUEJSON
    Single source_file -> single translation_files_expression/<lang> substituted file.
    """
    source_file = repo_root / entry["source_file"]
    target_template = entry[
        "translation_files_expression"
    ]  # e.g. '.../messages/<lang>.json'

    if not source_file.exists():
        logger.warning(f"[{name}] source_file not found: {source_file}")
        return

    pl_path = repo_root / target_template.replace("<lang>", lang)

    yield ResourceFile(
        resource_name=name,
        file_format="KEYVALUEJSON",
        en_path=source_file,
        pl_path=pl_path,
    )


def parse_transifex_yml(
    yml_path: str | Path,
    repo_root: str | Path,
    lang: str = TARGET_LANG_CODE,
) -> list[ResourceFile]:
    """
    Main entrypoint. Returns a flat list of ResourceFile, one per actual
    on-disk EN file (dir-type PO resources expand to multiple entries).
    """
    yml_path = Path(yml_path)
    repo_root = Path(repo_root)

    with open(yml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    entries = config.get("git", {}).get("filters", [])
    if not entries:
        logger.warning("No entries found under git.filters — check yml structure")

    results: list[ResourceFile] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        file_format = entry.get("file_format", "")
        filter_type = entry.get("filter_type", "")

        name = _infer_name(entry)

        if file_format not in SUPPORTED_FORMATS:
            logger.info(f"[{name}] skipping unsupported format: {file_format}")
            continue

        if filter_type == "dir" and file_format == "PO":
            results.extend(_resolve_dir_po(repo_root, name, entry, lang))
        elif filter_type == "file" and file_format == "KEYVALUEJSON":
            results.extend(_resolve_file_json(repo_root, name, entry, lang))
        else:
            logger.info(
                f"[{name}] skipping unsupported filter_type/format combo: "
                f"{filter_type}/{file_format}"
            )

    return results


def _infer_name(entry: dict) -> str:

    # always take the part after the first slash
    # translations/frontend-app-learner-portal-enterprise/src/i18n/transifex_input.json
    # becomes ->
    # frontend-app-learner-portal-enterprise

    path = entry.get("source_file_dir") or entry.get("source_file") or "unknown"
    parts = Path(path).parts
    return parts[1] if parts[0] == "translations" and len(parts) > 1 else path


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    yml = sys.argv[1] if len(sys.argv) > 1 else TRANSIFEX_YML_PATH
    root = sys.argv[2] if len(sys.argv) > 2 else REPO_ROOT

    files = parse_transifex_yml(yml, root, lang=TARGET_LANG_CODE)
    print(f"\nResolved {len(files)} translatable files:\n")
    for rf in files:
        print(f"  [{rf.file_format:14}] {rf.resource_name}")
        print(f"      EN: {rf.en_path}")
        print(f"      {TARGET_LANG_CODE.upper()}: {rf.pl_path}\n")