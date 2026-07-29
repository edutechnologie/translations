"""
Step 2 — Remove existing target language files after a pull.

Deletes all resolved PL files from translations/ after a pull.
Safe because ready/ (not translations/) is the durable store,
and translations/ is git-tracked (recoverable via git checkout if needed).
"""

import logging
import click
from pathlib import Path

from .config import (
    REPO_ROOT,
    TARGET_LANG_CODE,
    TRANSIFEX_YML_PATH,
)
from .transifex_parser import parse_transifex_yml


logger = logging.getLogger("rm_pl")


def remove_pl_files(
    yml_path: Path = TRANSIFEX_YML_PATH,
    repo_root: Path = REPO_ROOT,
    lang: str = TARGET_LANG_CODE,
) -> None:
    resources = parse_transifex_yml(yml_path, repo_root, lang=lang)

    removed, missing = 0, 0

    for rf in resources:
        if rf.pl_path.exists():
            rf.pl_path.unlink()
            removed += 1
        else:
            missing += 1

    logger.info(
        f"Removed %s {TARGET_LANG_CODE} files, %s already absent (out of %s resolved).",
        removed,
        missing,
        len(resources),
    )


@click.command()
@click.option(
    "--yml-path",
    type=click.Path(exists=True, path_type=Path),
    default=TRANSIFEX_YML_PATH,
    show_default=True,
    help="Path to transifex.yml.",
)
@click.option(
    "--repo-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=REPO_ROOT,
    show_default=True,
    help="Repository root containing translations/.",
)
@click.option(
    "--lang",
    default=TARGET_LANG_CODE,
    show_default=True,
    help="Language code to remove from translations.",
)
def main(yml_path: Path, repo_root: Path, lang: str):
    """Remove existing translated files before pulling fresh sources."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    remove_pl_files(yml_path, repo_root, lang)


if __name__ == "__main__":
    main()
