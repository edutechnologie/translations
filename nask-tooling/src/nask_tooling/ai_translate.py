"""
Step 4 — AI translation call + retry.

Reads the batch json produced by step 3 (diff + batch builder), groups items
by target staging file, splits each file's items into chunks, and sends each
chunk to Gemini for translation. Results are NOT written back into the staging
files here — that is a separate sanitize/merge step (step 5). This module's
only job is: call the model, validate the response is structurally sound, and
record everything so step 5 has a clean, trustworthy source to read from.

Input batch item shape (from step 3), for reference:
    {
        "resource": str,
        "format": "PO" | "KEYVALUEJSON",
        "staging_path": str,
        "key": str,
        "source": str,
    }

We do NOT send "key" or "resource" to the model. We assign a local integer id
to each item inside a chunk, send only {id, source}, and expect {id, translated}
back. The id is matched against our own in-memory list, so there is no risk of
the model altering a key/msgid on the way back, and we don't waste tokens
re-sending long PO msgids as both key and payload.
"""

import json
import time
import hashlib
import re
import threading
import os
import click
from dotenv import load_dotenv
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

from pydantic import BaseModel
from google import genai
from google.genai import types

from .config import (
    AI_OUTPUTS_DIR,
    BATCH_CHUNKS_DIR,
    GEMINI_API_KEY_ENV,
    MAX_CHARS_PER_CHUNK,
    MAX_CONCURRENT_CALLS,
    MAX_ITEMS_PER_CHUNK,
    MAX_RETRIES,
    MODEL_NAME,
    RAW_DIR,
    REQUEST_TIMEOUT_SECONDS,
    RESULTS_DIR,
    RETRY_BACKOFF_SECONDS,
    SAFE_RPD,
    SAFE_RPM,
    TARGET_LANGUAGE_NAME,
    TARGET_LANGUAGE_N_OF_PLURAL_FORMS,
    TARGET_LANGUAGE_PLURAL_FORMS_PROMPT_EXPLANATION,
    TEMPERATURE,
    BATCH_TODO_PATH
)

class RateLimiter:
    """shared across all worker threads, so concurrency never lets us burst
    past the per-minute or per-day cap. one acquire() call = one api call,
    including retries, since a retry is still a real request against the quota."""

    def __init__(self, rpm: int, rpd: int):
        self.rpm = rpm
        self.rpd = rpd
        self.lock = threading.Lock()
        self.minute_window: List[float] = []  # timestamps of calls in the last 60s
        self.day_count = 0
        self.day_start = time.monotonic()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()

                # reset the day counter every 24h
                if now - self.day_start > 86400:
                    self.day_start = now
                    self.day_count = 0

                if self.day_count >= self.rpd:
                    raise RuntimeError(
                        "daily rate limit reached, refusing to call the model further"
                    )

                # drop timestamps older than 60s from the sliding window
                self.minute_window = [t for t in self.minute_window if now - t < 60]

                if len(self.minute_window) < self.rpm:
                    self.minute_window.append(now)
                    self.day_count += 1
                    return

                # window is full, figure out how long until the oldest entry ages out
                sleep_for = 60 - (now - self.minute_window[0]) + 0.1

            time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# schema for what we ask the model to return
# ---------------------------------------------------------------------------


class TranslatedItem(BaseModel):
    id: int
    translated: str


class TranslationBatchOut(BaseModel):
    translations: List[TranslatedItem]


class TranslatedPluralItem(BaseModel):
    id: int
    one: str  # count == 1
    few: str  # count%10 in 2..4, and not count%100 in 10..20
    many: str  # everything else (0, 5+, teens)


class TranslationPluralBatchOut(BaseModel):
    translations: List[TranslatedPluralItem]


# ---------------------------------------------------------------------------
# internal chunk representation
# ---------------------------------------------------------------------------


@dataclass
class ChunkItem:
    local_id: int
    key: str
    source: str
    format: str
    resource: str
    staging_path: str
    source_plural: str = (
        ""  # only set for format == PO_PLURAL, the english plural form for context
    )
    developer_comment: str = ""


@dataclass
class Chunk:
    chunk_id: str
    format: str
    items: List[ChunkItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# batch loading / grouping / chunking
# ---------------------------------------------------------------------------


def load_batch(batch_path: Path) -> List[dict]:
    return json.loads(batch_path.read_text(encoding="utf-8"))


def group_by_format(batch: List[dict]) -> Dict[str, List[dict]]:
    # not by file - a small incremental diff might only add 2 strings per file,
    # grouping by file alone would waste a whole request per file. group by
    # format instead so items from many files can share one call, we just
    # never want to mix PO and JSON prompts together.
    grouped: Dict[str, List[dict]] = {}
    for item in batch:
        grouped.setdefault(item["format"], []).append(item)
    return grouped

def save_chunk_todo(chunk: Chunk):
    """
    Dump this chunk's items back out in batch_todo.json format, so a failed/
    partial chunk can be re-fed into the pipeline standalone without rerunning
    the whole 10k-line batch.
    """
    BATCH_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "resource": it.resource,
            "format": it.format,
            "staging_path": it.staging_path,
            "key": it.key,
            "source": it.source,
            **({"source_plural": it.source_plural} if it.source_plural else {}),
            **({"developer_comment": it.developer_comment} if it.developer_comment else {}),
        }
        for it in chunk.items
    ]
    
    fname = f"{chunk.chunk_id}.json"
    (BATCH_CHUNKS_DIR / fname).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def normalize_source(raw: dict) -> dict:
    """
    Some KEYVALUEJSON entries carry source as {"string": ..., "developer_comment": ...}
    instead of a plain string (paragon does this for a11y strings). Flatten it here,
    once, so everything downstream (build_chunks, match_whitespace, payload) only
    ever sees raw["source"] as a str. Comment is kept for prompt context, not
    translated, not given its own key.
    """
    source = raw["source"]
    if isinstance(source, dict):
        raw = {**raw, "source": source["string"], "developer_comment": source.get("developer_comment", "")}
    return raw

def build_chunks(format_name: str, items: List[dict]) -> List[Chunk]:
    """Pack items of one format into chunks up to the count/char caps,
    items can come from different files/resources, each ChunkItem carries
    its own resource/staging_path so nothing is lost on the way back."""

    chunks: List[Chunk] = []
    current: List[ChunkItem] = []
    current_chars = 0
    next_local_id = 0
    chunk_index = 0

    def flush():
        nonlocal current, current_chars, chunk_index, next_local_id
        if not current:
            return
        chunk_id = f"{format_name}__{chunk_index:03d}"
        chunks.append(Chunk(chunk_id=chunk_id, format=format_name, items=current))
        chunk_index += 1
        current = []
        current_chars = 0
        next_local_id = 0

    for raw in items:
        source_len = len(raw["source"])
        would_overflow = (
            len(current) >= MAX_ITEMS_PER_CHUNK
            or (current_chars + source_len) > MAX_CHARS_PER_CHUNK
        )
        if would_overflow and current:
            flush()

        current.append(
            ChunkItem(
                local_id=next_local_id,
                key=raw["key"],
                source=raw["source"],
                format=raw["format"],
                resource=raw["resource"],
                staging_path=raw["staging_path"],
                source_plural=raw.get("source_plural", ""),
                developer_comment=raw.get("developer_comment", ""),
            )
        )
        next_local_id += 1
        current_chars += source_len

    flush()
    return chunks


# ---------------------------------------------------------------------------
# prompt building
# ---------------------------------------------------------------------------


def build_prompt(chunk: Chunk, strict: bool = False) -> str:
    if chunk.format == "PO_PLURAL":
        return _build_plural_prompt(chunk, strict)
    return _build_singular_prompt(chunk, strict)


def _build_singular_prompt(chunk: Chunk, strict: bool) -> str:
    payload = [{"id": it.local_id, "source": it.source} for it in chunk.items]

    instructions = f"""You are translating UI/software strings from English to {TARGET_LANGUAGE_NAME}.

Rules:
- Translate the "source" text of every item, keep the meaning natural for a software UI, not a literal word-for-word translation.
- Preserve placeholders exactly as they appear: things like %s, %d, {{name}}, {{count}}, {{{{var}}}}, HTML tags, and any punctuation used as a format specifier. Do not translate or alter these.
- Do not translate variable names, brand names, or code-like tokens.
- If a string contains a numeric placeholder (like %(count)d or {{attempts}}) and is describing a countable noun, this codebase cannot select between plural word forms at runtime for these strings, so prefer the Polish plural/genitive noun form (the "many" form, e.g. "znakow" not "znak") since that reads naturally for the majority of real values shown.
- Keep the same id for each item, do not renumber or reorder.
- Return exactly one translation per item, no more, no fewer.
- Preserve leading/trailing whitespace and internal line breaks exactly as in source

Format: {chunk.format}

Items to translate:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    if strict:
        instructions += (
            "\nReturn ONLY valid JSON matching the schema. No prose, no markdown "
            "code fences, no explanation before or after the JSON."
        )

    return instructions


def _build_plural_prompt(chunk: Chunk, strict: bool) -> str:
    payload = [
        {"id": it.local_id, "singular": it.source, "plural": it.source_plural}
        for it in chunk.items
    ]

    instructions = f"""You are translating gettext plural strings from English to {TARGET_LANGUAGE_NAME}.

Each item has an english "singular" and "plural" source form (both english, this is just gettext convention,
not the translation). For each item, return {TARGET_LANGUAGE_N_OF_PLURAL_FORMS} {TARGET_LANGUAGE_NAME} forms following the standard Polish
gettext plural rule (nplurals={TARGET_LANGUAGE_N_OF_PLURAL_FORMS}):
{TARGET_LANGUAGE_PLURAL_FORMS_PROMPT_EXPLANATION}

Rules:
- Preserve placeholders exactly as they appear: %(count)d, %(count)s, {{count}}, etc. Do not translate or alter these.
- Do not translate variable names, brand names, or code-like tokens.
- Keep the same id for each item, do not renumber or reorder.
- Return exactly one set of {{one, few, many}} per item, no more, no fewer.
- Preserve leading/trailing whitespace and internal line breaks exactly as in source

Items to translate:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    if strict:
        instructions += (
            "\nReturn ONLY valid JSON matching the schema. No prose, no markdown "
            "code fences, no explanation before or after the JSON."
        )

    return instructions


# ---------------------------------------------------------------------------
# single chunk call, with retry
# ---------------------------------------------------------------------------


def call_model(client: genai.Client, chunk: Chunk, strict: bool = False) -> str:
    print(
        f"  -> calling model for {chunk.chunk_id} ({len(chunk.items)} items, strict={strict})"
    )
    prompt = build_prompt(chunk, strict=strict)
    schema = (
        TranslationPluralBatchOut
        if chunk.format == "PO_PLURAL"
        else TranslationBatchOut
    )
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=TEMPERATURE,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    return response.text


def validate_response(raw_text: str, chunk: Chunk):
    """Structural validation only - do the ids line up, is it valid json against
    the schema. Content quality (bad translation, mangled placeholder, etc) is
    left for the later sanitize step, on purpose."""

    is_plural = chunk.format == "PO_PLURAL"
    schema = TranslationPluralBatchOut if is_plural else TranslationBatchOut
    parsed = schema.model_validate_json(raw_text)

    expected_ids = {it.local_id for it in chunk.items}
    got_ids = {t.id for t in parsed.translations}

    if got_ids != expected_ids:
        missing = expected_ids - got_ids
        extra = got_ids - expected_ids
        raise ValueError(f"id mismatch, missing={missing}, extra={extra}")

    if is_plural:
        for t in parsed.translations:
            if not (t.one.strip() and t.few.strip() and t.many.strip()):
                raise ValueError(f"empty plural form returned for id {t.id}")
    else:
        for t in parsed.translations:
            if t.translated.strip() == "":
                raise ValueError(f"empty translation returned for id {t.id}")

    return parsed


def save_raw_output(chunk: Chunk, attempt: int, raw_text: str, ok: bool):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{chunk.chunk_id}__attempt{attempt}__{'ok' if ok else 'fail'}.json"
    (RAW_DIR / fname).write_text(raw_text, encoding="utf-8")

def match_whitespace(source: str, translated: str) -> str:
    """
    Reattach source's leading/trailing whitespace to translated.

    msgfmt fatal-errors if msgid/msgstr leading/trailing whitespace differ
    (indentation, \n from mako/html templates). Model output doesn't preserve
    it reliably even when asked, and stripping pre-send would mean carrying
    extra state through chunking/schema just to undo it here. Simplest fix:
    strip whatever the model gave us, wrap it in source's real edges.
    Internal whitespace (mid-string \n) isn't part of this check - left as-is.
    """
    if not isinstance(source, str):
        raise TypeError(
            f"match_whitespace: source is {type(source).__name__}, not str: {source!r}"
        )
    if not isinstance(translated, str):
        raise TypeError(
            f"match_whitespace: translated is {type(translated).__name__}, not str: {translated!r}"
        )

    leading = source[: len(source) - len(source.lstrip())]
    trailing = source[len(source.rstrip()) :] if source.strip() else ""
    return f"{leading}{translated.strip()}{trailing}"

def save_chunk_result(chunk: Chunk, parsed):
    """This is what step 5 (merge/sanitize) will actually read. It carries the
    key/resource/staging_path info that we deliberately never sent to the model,
    joined back in locally by id."""

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    is_plural = chunk.format == "PO_PLURAL"
    by_id = {t.id: t for t in parsed.translations}
    result_items = []

    for it in chunk.items:
        entry = {
            "resource": it.resource,
            "format": it.format,
            "staging_path": it.staging_path,
            "key": it.key,
        }
        if it.developer_comment:
            # paragon specific
            entry["developer_comment"] = it.developer_comment

        t = by_id[it.local_id]
        if is_plural:
            # reattach post-validation (parsed.translations confirmed present/shaped),
            # pre-.po-write - see match_whitespace() docstring for why
            entry["forms"] = {
                "0": match_whitespace(it.source, t.one),
                "1": match_whitespace(it.source_plural, t.few),
                "2": match_whitespace(it.source_plural, t.many),
            }
        else:
            entry["translated"] = match_whitespace(it.source, t.translated)
        result_items.append(entry)

    out_path = RESULTS_DIR / f"{chunk.chunk_id}.json"
    out_path.write_text(
        json.dumps(result_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def last_attempt_number(chunk_id: str) -> int:
    pattern = re.compile(rf"^{re.escape(chunk_id)}__attempt(\d+)__(ok|fail)\.json$")
    nums = [
        int(m.group(1))
        for f in RAW_DIR.glob(f"{chunk_id}__attempt*__*.json")
        if (m := pattern.match(f.name))
    ]
    return max(nums, default=0)

def process_chunk(
    client: genai.Client,
    chunk: Chunk,
    rate_limiter: RateLimiter,
    start_attempt: int = 0,
) -> dict:
    """Runs the retry loop for a single chunk. Returns a log record, never raises."""

    log_record = {
        "chunk_id": chunk.chunk_id,
        "format": chunk.format,
        "resources": sorted({it.resource for it in chunk.items}),
        "item_count": len(chunk.items),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "attempts": 0,
        "status": "failed",
        "error": None,
    }

    start = time.monotonic()
    save_chunk_todo(chunk)

    for i in range(1, MAX_RETRIES + 1):
        attempt = start_attempt + i
        strict = i == MAX_RETRIES  # strict on the last try of *this* run
        log_record["attempts"] = attempt

        try:
            rate_limiter.acquire()  # blocks until we're under the rpm cap, raises if rpd is used up
            raw_text = call_model(client, chunk, strict=strict)
            parsed = validate_response(raw_text, chunk)

            save_raw_output(chunk, attempt, raw_text, ok=True)
            save_chunk_result(chunk, parsed)

            log_record["status"] = "success"
            log_record["error"] = None
            break

        except RuntimeError as exc:
            # daily quota is gone, retrying won't help, stop this chunk now
            log_record["status"] = "skipped_daily_limit"
            log_record["error"] = str(exc)
            break

        except Exception as exc:
            # save whatever we got, even if it failed validation, useful for debugging
            raw_text = raw_text if "raw_text" in locals() else ""
            save_raw_output(chunk, attempt, raw_text, ok=False)
            log_record["error"] = str(exc)

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            # else: falls through, stays "failed", loop ends

    log_record["duration_seconds"] = round(time.monotonic() - start, 2)
    return log_record


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def write_summary(logs: List[dict]):
    total = len(logs)
    ok = sum(1 for l in logs if l["status"] == "success")
    failed = [l for l in logs if l["status"] == "failed"]
    skipped = [l for l in logs if l["status"] == "skipped_daily_limit"]

    lines = [
        "# Translation batch summary",
        "",
        f"Total chunks: {total}",
        f"Succeeded: {ok}",
        f"Failed: {len(failed)}",
        f"Skipped (daily limit hit): {len(skipped)}",
        "",
    ]

    if skipped:
        lines.append(
            "## Skipped - daily quota exhausted, rerun tomorrow (or raise RPD_LIMIT if wrong)"
        )
        lines.append("")
        for l in skipped:
            lines.append(
                f"- `{l['chunk_id']}` resources=`{l['resources']}` items={l['item_count']}"
            )
        lines.append("")

    if failed:
        lines.append("## Failed chunks")
        lines.append("")
        for l in failed:
            lines.append(
                f"- `{l['chunk_id']}` resources=`{l['resources']}` "
                f"items={l['item_count']} attempts={l['attempts']} "
                f"error: {l['error']}"
            )
        lines.append("")

    (AI_OUTPUTS_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    log_path = AI_OUTPUTS_DIR / "log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for l in logs:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


def execute_chunks(
    chunks: List[Chunk],
    client,
    rate_limiter: RateLimiter,
    start_attempts: dict[str, int] | None = None,
) -> List[dict]:

    start_attempts = start_attempts or {}
    logs: List[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS) as pool:
        futures = {
            pool.submit(
                process_chunk,
                client,
                chunk,
                rate_limiter,
                start_attempts.get(chunk.chunk_id, 0),
            ): chunk
            for chunk in chunks
        }
        done = 0
        for future in as_completed(futures):
            record = future.result()
            logs.append(record)
            done += 1
            print(
                f"[{done}/{len(chunks)}] {record['chunk_id']} -> {record['status']} "
                f"({record['attempts']} attempt(s), {record['duration_seconds']}s)"
            )
    return logs

def translate_all(batch_path: Path):
    AI_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    batch = load_batch(batch_path)
    grouped = group_by_format(batch)

    all_chunks: List[Chunk] = []
    for format_name, items in grouped.items():
        all_chunks.extend(build_chunks(format_name, items))

    print(
        f"loaded {len(batch)} items across {len(grouped)} format(s), packed into {len(all_chunks)} chunks"
    )

    load_dotenv()
    client = genai.Client(
        api_key=os.getenv(GEMINI_API_KEY_ENV),
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
    )
    rate_limiter = RateLimiter(rpm=SAFE_RPM, rpd=SAFE_RPD)

    logs = execute_chunks(all_chunks, client, rate_limiter)

    write_summary(logs)
    failed_count = sum(1 for l in logs if l["status"] == "failed")
    print(
        f"\ndone. {len(logs) - failed_count}/{len(logs)} chunks succeeded. see {AI_OUTPUTS_DIR}/summary.md for details."
    )

def retry_chunk(chunk_path: Path):
    chunk_id = chunk_path.stem
    raw_items = json.loads(chunk_path.read_text(encoding="utf-8"))

    items = [
        ChunkItem(
            local_id=i,
            key=r["key"],
            source=r["source"],
            format=r["format"],
            resource=r["resource"],
            staging_path=r["staging_path"],
            source_plural=r.get("source_plural", ""),
            developer_comment=r.get("developer_comment", ""),
        )
        for i, r in enumerate(raw_items)
    ]
    chunk = Chunk(chunk_id=chunk_id, format=raw_items[0]["format"], items=items)

    load_dotenv()
    client = genai.Client(
        api_key=os.getenv(GEMINI_API_KEY_ENV),
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
    )
    rate_limiter = RateLimiter(rpm=SAFE_RPM, rpd=SAFE_RPD)
    start_attempt = last_attempt_number(chunk_id)

    logs = execute_chunks([chunk], client, rate_limiter, {chunk_id: start_attempt})
    write_summary(logs)

def run(
    path: Path = BATCH_TODO_PATH,
    retry: bool = False,
):
    if retry:
        retry_chunk(path)
    else:
        translate_all(path)



@click.command()
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=BATCH_TODO_PATH,
    show_default="batch_todo.json (generated by prepare_translation_batch)",
    help=(
        "Path to the translation batch file. By default this uses "
        "batch_todo.json generated by prepare_translation_batch and stored "
        f"at {BATCH_TODO_PATH}. "
        "When using --retry, this must point to a single chunk file from "
        "batch_chunks/ instead."
    ),
)
@click.option(
    "--retry",
    "-r",
    is_flag=True,
    help=(
        "Retry translation for a single chunk instead of processing the full "
        "batch. Requires --path to point to a chunk file from batch_chunks/. "
        "Using --retry with the default batch_todo.json has no effect and is "
        "equivalent to running without --retry."
    ),
)
def main(path: Path, retry: bool):
    if retry and path == BATCH_TODO_PATH:
        raise click.UsageError(
            "--retry requires --path pointing to a specific chunk file "
            "from batch_chunks/."
        )
    run(path,retry)


if __name__ == "__main__":
    main()