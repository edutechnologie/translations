import logging

import click

from . import (
    ai_translate,
    merge_into_staging,
    prepare_translation_batch,
    publish as publish_module,
    rm_pl,
    validate_staging,
)
from .config import (
    READY_ROOT,
    REPO_ROOT,
    TRANSIFEX_YML_PATH,
)


logger = logging.getLogger("orchestrator")


def verify_environment():
    required = [
        REPO_ROOT,
        TRANSIFEX_YML_PATH,
        READY_ROOT,
    ]

    missing = [str(p) for p in required if not p.exists()]

    if missing:
        raise RuntimeError(
            "Missing required paths:\n" + "\n".join(f"- {p}" for p in missing)
        )


def run_pipeline(force: bool = False):
    verify_environment()

    rm_pl.remove_pl_files()

    prepare_translation_batch.run()

    ai_translate.run()

    merge_into_staging.run()

    validation_ok = validate_staging.run()

    if not validation_ok and not force:
        raise RuntimeError(
            "Validation failed. Review staging/ output before publishing. "
            "Run publish manually after fixing issues, or use --force."
        )

    logger.info("Pipeline finished. Review staging/ before publishing.")


@click.group()
def cli():
    """NASK translation tooling."""


@cli.command()
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help=(
        "Continue even if validation reports errors. "
        "Use only when you have reviewed the validation output."
    ),
)
def run(force: bool):
    """Run translation pipeline up to validation."""
    logging.basicConfig(level=logging.INFO)
    run_pipeline(force)


@cli.command()
def publish():
    """Publish validated staging translations."""
    logging.basicConfig(level=logging.INFO)

    verify_environment()

    publish_module.run()

    logger.info("Publishing completed.")
