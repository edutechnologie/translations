import shutil

from .config import (
    READY_ROOT,
    STAGING_ROOT,
    TRANSLATIONS_ROOT,
)


def run() -> None:
    staged_files = list(STAGING_ROOT.rglob("*.po")) + list(STAGING_ROOT.rglob("*.json"))

    for staged in staged_files:
        rel_path = staged.relative_to(STAGING_ROOT)

        ready_dest = READY_ROOT / rel_path
        ready_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, ready_dest)

        translations_dest = TRANSLATIONS_ROOT / rel_path
        translations_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, translations_dest)

        print(f"  published: {rel_path}")

    print(f"published {len(staged_files)} files (translations/ + ready/)")


if __name__ == "__main__":
    run()
