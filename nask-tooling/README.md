# nask-translation-tooling

## Overview

`nask-tooling` is a translation pipeline for Open edX. It diffs current English source strings against previously completed Polish translations, sends only the new or changed entries to Google Gemini for translation, merges the results back into staging catalogs, validates them, and publishes the finished files.

The pipeline was originally created for Polish (`pl`) translations. Some filenames, assumptions, and code paths may be language-specific, but the code was structured to be modular where possible.

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

The `Makefile` exposes two targets:

- `make translate` — runs `nask-translation-tooling run`, which executes the full pipeline up to validation. This runs the pipeline and should only be executed after configuration and setup are complete. It is not a first-time setup command.
- `make publish` — runs `nask-translation-tooling publish`, which copies validated staging files into `ready/` and `translations/`.

### Pipeline overview

The pipeline is a sequence of independent stages. Each stage can be run on its own and exposes reusable functions, but the normal flow is coordinated end-to-end by a single orchestrator.

1. **Clear** — Existing target-language files are removed from the freshly checked-out source tree. This is safe because the durable copy of accepted translations lives elsewhere, and the source tree is git-tracked and recoverable.

2. **Diff and batch** — Current English sources are compared against the last accepted translations. Entries that already have a valid translation are carried over. Entries that are missing or changed are collected into a batch list. Staging catalogs are written with placeholders for the missing entries.

3. **Translate** — The batch list is grouped by format, split into chunks, and sent to the AI model. Only the text to translate is sent; keys and catalog structure stay local. Responses are validated structurally and saved as intermediate results.

4. **Merge** — The intermediate results are read and written back into the staging catalogs, filling in the placeholders created during the diff stage.

5. **Validate** — Staging files are checked for syntax, placeholder parity, plural form correctness, and key completeness. Validation blocks publishing.

6. **Publish** — Validated staging files are copied into both the durable store and the source tree. The durable store is the diff baseline for future runs. The source tree is published there because Dockerfiles consume it directly during image builds, so translations must land in that location to reach production containers.

## Project layout

```
nask-tooling/
├── Makefile
├── pyproject.toml
├── .env.example
├── ready/
│   └── translations/
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

# Translation Pipeline Flow

## Overview

`nask-tooling` is a translation pipeline for Open edX. It diffs current English source strings against previously completed Polish translations, sends only the new or changed entries to Google Gemini for translation, merges the results back into staging catalogs, validates them, and publishes the finished files.

The pipeline was originally created for Polish (`pl`) translations. Some filenames, assumptions, and language-specific rules may exist because of that origin. The implementation was intentionally separated into modules to allow extension to other languages and formats.

The core design ideas are:

- Keep existing translations safe.
- Separate source catalogs from generated output.
- Never let AI directly modify production translation files.
- Use intermediate artifacts for review, debugging, and retries.
- Validate before publishing.

## Repository concepts

| Path | Purpose |
|---|---|
| `translations/` | Checked-out source translation tree. The working input state. |
| `ready/` | Durable storage of accepted translations. Survives future refreshes. |
| `staging/` | Temporary assembled output before publishing. The only location validated before promotion. |
| `ai-outputs/` | AI intermediate data: raw responses, chunks, and structured results. |
| `batch_todo.json` | Generated list of strings requiring translation. |

## Full execution flow

### 1. Remove existing target language files

`rm_pl.py` deletes existing Polish files from the freshly checked-out `translations/` tree. This ensures missing translations are detected by the batch preparation step. The durable copy is `ready/`, not `translations/`, so deleting from `translations/` is safe and recoverable via git.

### 2. Prepare translation batch

`prepare_translation_batch.py` parses `transifex.yml` via `transifex_parser.py`, which resolves each resource to concrete English/Polish file pairs on disk. Only `PO` and `KEYVALUEJSON` formats are supported; other formats are skipped.

For each catalog, the module compares the current English source against the existing Polish translation in `ready/`:

```
Does the translation already exist?
|
+-- yes -> reuse existing translation from ready/
|
+-- no -> create empty staging entry + add item to batch_todo.json
```

- **PO entries**: Singular entries reuse the existing `msgstr` if non-empty. Plural entries require all three Polish forms to be present and non-empty.
- **Plural strings**: Handled as `PO_PLURAL` format in the batch, carrying both `source` and `source_plural`.
- **JSON key/value entries**: Existing non-empty values are reused. Missing keys are set to `null` in staging and added to the batch.
- **Unsupported formats**: Skipped by `transifex_parser.py` and logged.

The staging catalogs are written with placeholders, and `batch_todo.json` is written with the list of items needing translation.

### 3. AI translation stage

`ai_translate.py` reads `batch_todo.json` and sends items to Gemini. AI works on a generated batch instead of catalog files to avoid sending irrelevant data, to control prompt size, and to keep catalog structure out of the model's reach.

`batch_todo.json` is converted into chunks because:

- Chunks are grouped by format to avoid mixing PO and JSON prompts.
- Context size limits require chunking.
- Each chunk is capped by `MAX_ITEMS_PER_CHUNK` and `MAX_CHARS_PER_CHUNK`.

The chunk lifecycle:

```
batch_todo.json
    |
    v
format groups
    |
    v
chunks
    |
    v
minimal AI prompt payload
    |
    v
model response
    |
    v
map results back using local IDs
    |
    v
saved AI results
```

- **Local IDs**: Each item in a chunk gets a local integer ID. Only `{id, source}` is sent to the model. The ID is matched back to the full item locally, so the model cannot alter keys or msgids.
- **Raw outputs**: Saved to `ai-outputs/raw/` for debugging.
- **Structured outputs**: Saved to `ai-outputs/results/` for the merge step.
- **Retry support**: Failed chunks are saved to `ai-outputs/batch_chunks/` and can be retried individually.
- **Original catalog structure**: Preserved outside AI processing. The model never sees catalog files.

### 4. Merge AI results into staging

`merge_into_staging.py` loads all result files from `ai-outputs/results/`, groups them by `staging_path`, and writes translations into the corresponding staging PO/JSON files. The staging files already exist with empty placeholders from step 2; this step fills them in.

Merge happens into `staging/` instead of `translations/` to keep production files untouched until validation passes.

### 5. Validate staging

`validate_staging.py` validates all files under `staging/`.

**PO files**:
- `msgfmt --check` catches syntax errors, plural header issues, and placeholder mismatches.
- `edx-i18n-tools` adds checks for non-BMP characters, HTML tag parity, and plural-aware empty translation detection when available.
- `msgfmt` must be installed on the system.

**JSON files**:
- Valid JSON parsing.
- No leftover `null` values.
- No empty translations.
- Key parity against the English counterpart.
- Placeholder token matching against the English counterpart.

Validation blocks publishing. If validation fails, the orchestrator raises an error unless `--force` is used.

### 6. Publish

`publish.py` copies validated staging files into both:
- `translations/` — the checked-out source tree.
- `ready/` — the durable store.

`ready/` is necessary for future pipeline runs: it is the source of existing translations that the batch preparation step diffs against.

## Catalog handling details

### PO catalogs

- **gettext structure**: Each entry has `msgid`, `msgstr`, optional `msgctxt`, and optional `msgid_plural` with `msgstr_plural` forms.
- **Singular/plural entries**: Singular entries use `msgstr`. Plural entries use `msgstr_plural` with three Polish forms.
- **Plural forms**: Polish uses `nplurals=3` with `one`, `few`, and `many` forms. The plural header is set in staging catalogs.
- **Placeholders**: Preserved exactly. `msgfmt --check` validates placeholder parity between `msgid` and `msgstr`.
- **English source strings**: Used as the translation basis. The staging catalog starts from the English metadata and entries, with Polish plural forms applied.

### JSON catalogs

- **key/value model**: Each key maps to a string value. Some keys carry a `developer_comment` alongside the string.
- **key matching**: Staging keys must match English counterpart keys. Missing or extra keys are reported.
- **placeholder checks**: Placeholder tokens in the English value must appear in the Polish value.
- **limitations**: No gettext-equivalent compiler. Validation is limited to JSON validity, key parity, and placeholder matching.

### Unsupported formats

Android, iOS, YAML, and other formats may appear in Transifex configuration. They are intentionally skipped unless explicit support exists in `transifex_parser.py`.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `config.py` | Centralized configuration: paths, language settings, AI parameters, rate limits, validation settings. |
| `transifex_parser.py` | Parses `transifex.yml`, resolves resources to English/Polish file pairs. |
| `rm_pl.py` | Removes existing Polish files from `translations/` before pulling fresh sources. |
| `prepare_translation_batch.py` | Diffs English sources against `ready/`, writes staging placeholders, generates `batch_todo.json`. |
| `ai_translate.py` | Chunks batch items, calls Gemini, validates responses, saves raw and structured results. |
| `merge_into_staging.py` | Merges AI results into staging PO/JSON files. |
| `validate_staging.py` | Validates staging files with `msgfmt` and JSON checks. |
| `publish.py` | Copies validated staging files into `translations/` and `ready/`. |
| `orchestrator.py` | Coordinates the pipeline stages via a CLI. |

## Design principles

- Configuration belongs in `config.py`.
- Each module can run independently.
- Orchestrator coordinates functions instead of shell commands.
- Intermediate files make debugging and retries possible.
- Publishing only happens after validation.