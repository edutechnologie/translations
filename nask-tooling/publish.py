import shutil
from pathlib import Path

STAGING_ROOT = Path("staging/translations")
TRANSLATIONS_ROOT = Path("../translations")
READY_ROOT = Path("ready/translations")


def publish():
    po_files = list(STAGING_ROOT.rglob("*.po"))
    for staged in po_files:
        rel_path = staged.relative_to(STAGING_ROOT)

        dest = TRANSLATIONS_ROOT / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, dest)

        ready_dest = READY_ROOT / rel_path
        ready_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, ready_dest)

        print(f"  published: {rel_path}")
    print(f"published {len(po_files)} files (translations/ + ready/)")


if __name__ == "__main__":
    publish()
