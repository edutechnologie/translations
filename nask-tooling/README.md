# nask-translation-tooling

## Overview

`nask-tooling` is a translation pipeline for Open edX. It diffs current English source strings against previously completed Polish translations, sends only the new or changed entries to Google Gemini for translation, merges the results back into staging catalogs, validates them, and publishes the finished files.

The pipeline was originally created for Polish (`pl`) translations. Some filenames, assumptions, and code paths may be language-specific, but the code was structured to be modular where possible.

For a full step-by-step walkthrough (including upgrading to a new openedx release), see [`docs/USAGE.md`](docs/USAGE.md). For a deeper look at what each pipeline stage does internally, see [`docs/pipeline-flow.md`](docs/pipeline-flow.md).

## Installation

```bash
pip install -e .
```

This performs an editable install from the `src/` layout. The `nask-translation-tooling` console entry point is provided by `pyproject.toml` and maps to `nask_tooling.orchestrator:cli`.

## Configuration

All configuration is centralized in `src/nask_tooling/config.py`. Users should review and adjust config values before running the pipeline. Do not run the pipeline before configuring it.

### Environment setup

A Google Cloud project is required, and a Gemini API key must be generated. The key must be provided through a `.env` file.

Create `.env` based on `.env.example`:

```bash
cp .env.example .env
```

The required environment variable is:

- `GEMINI_API_KEY` — used by `ai_translate.py` and `gemini_hello_world.py` to authenticate with the Gemini API.

## Usage

### Makefile workflow

- `make translate` — runs `nask-translation-tooling run`, which executes the full pipeline up to validation. This runs the pipeline and should only be executed after configuration and setup are complete. It is not a first-time setup command.
- `make publish` — runs `nask-translation-tooling publish`, which copies validated staging files into `ready/` and `translations/`.
- `make retry CHUNK=<path>` — retries a single failed chunk instead of rerunning the whole batch. See below.

> **Note:** `translate` and `publish` go through the `nask-translation-tooling` console command (`orchestrator.py`). `retry` currently calls `ai_translate.py` directly as a module, since retry logic isn't (yet) wired into the orchestrator's CLI. Functionally this is fine, but keep in mind it's a slightly different entrypoint than the other two targets.

### Retrying a failed chunk

If `ai-outputs/summary.md` shows a chunk as `failed`, or you're not satisfied with a translation, retry just that chunk instead of rerunning the whole batch:

```bash
make retry CHUNK=nask-tooling/ai-outputs/batch_chunks/<CHUNK_FILENAME>
```

See [`docs/example-usage.md`](docs/example-usage.md) for what happens under the hood (attempt numbering, `raw/` vs `results/`, an example).

## Pipeline overview

The pipeline is a sequence of independent stages. Each stage can be run on its own and exposes reusable functions, but the normal flow is coordinated end-to-end by a single orchestrator.

1. **Clear** — Existing target-language files are removed from the freshly checked-out source tree. This is safe because the durable copy of accepted translations lives elsewhere, and the source tree is git-tracked and recoverable.

2. **Diff and batch** — Current English sources are compared against the last accepted translations. Entries that already have a valid translation are carried over. Entries that are missing or changed are collected into a batch list. Staging catalogs are written with placeholders for the missing entries.

3. **Translate** — The batch list is grouped by format, split into chunks, and sent to the AI model. Only the text to translate is sent; keys and catalog structure stay local. Responses are validated structurally and saved as intermediate results.

4. **Merge** — The intermediate results are read and written back into the staging catalogs, filling in the placeholders created during the diff stage.

5. **Validate** — Staging files are checked for syntax, placeholder parity, plural form correctness, and key completeness. Validation blocks publishing.

6. **Publish** — Validated staging files are copied into both the durable store and the source tree. The durable store is the diff baseline for future runs. The source tree is published there because Dockerfiles consume it directly during image builds, so translations must land in that location to reach production containers.

See [`docs/pipeline-flow.md`](docs/pipeline-flow.md) for the detailed internals of each stage.

## Known limitations

- **Stale translations aren't detected.** The diff stage only checks *whether* a Polish value exists for a given key — it does not compare the current English source text against what was translated last time. If upstream changes the English wording for an existing key without renaming the key, the old Polish translation is kept as-is and is **not** re-flagged for translation, even though it may now be outdated. This is a known gap, not currently handled.
- **Renamed keys are treated as new.** If upstream renames a key (even if the English text is unchanged), the old translation under the old key becomes orphaned and the new key is treated as a fresh gap — sent to AI translation from scratch.
- **`__ok` means "valid", not "correct".** See the retry caveat above — structural validation and translation quality are two different things.
- **`ready/` is the trust boundary.** Anything already present in `ready/translations/` is treated as authoritative and is never overwritten by AI output, only by an explicit `publish`. If you want to force a re-translation of an already-translated key, you need to manually clear or edit its value in `ready/` first — the pipeline has no "force retranslate" flag.
- **Language-specific assumptions.** As noted above, this was built for Polish first; other locales may hit filename/format assumptions that haven't been tested.

## Project layout

```
nask-tooling/
├── Makefile
├── pyproject.toml
├── .env.example
├── docs/
│   └── *.md
├── ready/
│   └── translations/
├── ai-outputs/
│   ├── batch_chunks/     # one file per chunk, used for retries
│   ├── raw/              # every attempt, per chunk, never overwritten
│   ├── results/          # latest successful result per chunk, overwritten on retry
│   └── summary.md        # per-run status of every chunk
├── src/
│   └── nask_tooling/
│       ├── ai_translate.py
│       ├── config.py
│       ├── gemini_hello_world.py
│       ├── merge_into_staging.py
│       ├── orchestrator.py
│       ├── prepare_translation_batch.py
│       ├── publish.py
│       ├── rm_pl.py
│       ├── transifex_parser.py
│       └── validate_staging.py
└── transifex.yml
```

## Development

The project uses a src layout and package-based imports. All modules live under `src/nask_tooling/` and are installed as the `nask_tooling` package.