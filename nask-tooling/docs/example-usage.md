# How to use this tool

## 1. Prerequisites

- You have a working set of translations in your language for your current openedx
  release (e.g. `release/teak.2`) already committed in `translations/`.
- `config.py` is set up correctly — check `RESOURCES`, `AI_OUTPUTS_DIR`,
  `STAGING_ROOT`, `READY_ROOT`, `TRANSLATIONS_ROOT`, and rate-limit constants
  (`SAFE_RPM`, `SAFE_RPD`, `MAX_ITEMS_PER_CHUNK`, `MAX_CHARS_PER_CHUNK`)
  match your Gemini quota and repo layout before running anything.
- `GEMINI_API_KEY` is set in `.env`.
- `gettext` (`msgfmt`) is installed system-wide.

## 2. Snapshot your current translations into ready/

Your existing translations become the baseline the pipeline trusts.
Anything already translated here is preserved as-is and never touched by AI.

    mkdir -p nask-tooling/ready
    cp -r translations/ nask-tooling/ready/translations

Result: `nask-tooling/ready/translations/<resource>/...` mirrors
`translations/<resource>/...` exactly.

## 3. Pull the new upstream release's English source

Add the upstream remote once, if you haven't:

    git remote add upstream https://github.com/openedx/openedx-translations

Fetch and check out just the translation catalog from the new release:

    git fetch upstream release/ulmo.3
    git checkout upstream/release/ulmo.3 -- translations

This overwrites `translations/` with the new release's structure — new
keys, renamed keys, updated English strings — but no Polish translations
(upstream's own `pl` files are NOT trusted or used; see step 4).

## 4. Run the pipeline via Makefile

Run the translation pipeline, then publish the results.

```bash
make translate
make publish
```

## 5. Review and commit

- Check `ai-outputs/summary.md` for any failed chunks.
- Spot-check a sample of `translations/**/pl.json` and `pl/LC_MESSAGES/django.po`, or `ai-outputs/results/`
- Commit `translations/` (and `ready/` if you track it).

## Retrying a failed chunk

If `ai-outputs/summary.md` shows a chunk as `failed` or you're not satisfied with a certain translation, retry just that
chunk instead of rerunning the whole batch:

    make retry CHUNK=ai-outputs/batch_chunks/<CHUNK_FILENAME>

`<CHUNK_FILENAME>` is the chunk file shown in `summary.md`, e.g.
`PO__000.json`. Every chunk gets one of these written automatically to
`ai-outputs/batch_chunks/` the first time the batch is processed, so
it's always there to retry from later.

**What happens on retry:**

- **`ai-outputs/raw/`** — new attempt files get *added* here, they never
  overwrite old ones. Attempt numbers keep counting up from whatever's
  already there (e.g. if attempts 1–3 already failed, a retry writes
  attempt 4, not attempt 1 again). This keeps the full history of every
  attempt for that chunk.
- **`ai-outputs/results/`** — this one *does* get overwritten. Once a
  retry succeeds, the chunk's result file is replaced with the new
  translation, ready for `make publish` to pick up on the next run. No
  separate merge step needed.

Note: `--retry` only works against a chunk file from `batch_chunks/` —
pointing it at the full `batch_todo.json` is rejected.

--- 

Example after 3 failed retries and a 4th that succeeded:

    ai-outputs/raw/
    ├── PO__000__attempt1__ok.json
    ├── PO__000__attempt2__ok.json
    ├── PO__000__attempt3__ok.json
    └── PO__000__attempt4__ok.json

    ai-outputs/results/
    └── PO__000.json          # from attempt 4, the latest

All four attempts stay in `raw/` for inspection — nothing is deleted.
`results/PO__000.json` reflects only the most recent attempt.

> **Note:** an `__ok` suffix means the response *validated* (parsed correctly, passed format/placeholder checks) — it does **not** guarantee the translation quality was what you wanted. If an attempt validates but the wording still isn't right, that's a content/prompt issue, not a pipeline failure. Check the file in `raw/`, and consider retrying again or adjusting the prompt/strict mode before the next attempt.

> **Note:** `--retry` only works against a chunk file from `batch_chunks/`, not the full `batch_todo.json`. Pointing it at the full batch is rejected by the CLI.
## Known limitations

- If upstream changes the **English source text** for an existing key
  without changing the key itself, the old Polish translation is kept
  as-is and NOT re-flagged for translation, even though it may now be
  stale. The pipeline only checks "does a Polish value exist", not
  "does it still match the current English". Not currently handled —
  a future improvement could hash/compare source text to detect this.