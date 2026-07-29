"""
rm_pl.py — deletes all resolved PL files from translations/ after a pull.
Safe because ready/ (not translations/) is the durable store,
and translations/ is git-tracked (recoverable via git checkout if needed).
"""

import logging
from pathlib import Path

from transifex_parser import parse_transifex_yml

logger = logging.getLogger("rm_pl")


def remove_pl_files(yml_path: str, repo_root: str, lang: str = "pl") -> None:
    resources = parse_transifex_yml(yml_path, repo_root, lang=lang)

    removed, missing = 0, 0
    for rf in resources:
        if rf.pl_path.exists():
            rf.pl_path.unlink()
            removed += 1
        else:
            missing += 1  # fine - file just didn't exist yet (new resource)

    logger.info(
        f"Removed {removed} PL files, {missing} already absent "
        f"(out of {len(resources)} resolved)."
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    yml = sys.argv[1] if len(sys.argv) > 1 else "../transifex.yml"
    root = sys.argv[2] if len(sys.argv) > 2 else "../"
    remove_pl_files(yml, root)
