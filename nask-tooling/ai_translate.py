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
import threading
import os
from dotenv import load_dotenv
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

from pydantic import BaseModel
from google import genai
from google.genai import types


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

MODEL_NAME = "gemini-3.5-flash-lite"
TARGET_LANGUAGE = "Polish"

TEMPERATURE = 0.3  # low, we want consistent literal translation, not creative variance
MAX_ITEMS_PER_CHUNK = 40  # hard cap on item count per call
MAX_CHARS_PER_CHUNK = (
    6000  # secondary cap, so a chunk of a few long PO strings doesn't balloon
)

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5  # multiplied by attempt number

MAX_CONCURRENT_CALLS = (
    5  # this is network IO bound, GIL is released during the request,
)
# so threads are fine here - keep this modest to respect rate limits

# the sdk's default http timeout is None, meaning no timeout at all - there are known
# reports of generate_content stalling for minutes with no error raised. set one explicitly,
# a genuine hang will now raise instead of parking a worker thread forever, and our normal
# retry loop already treats any exception the same way
REQUEST_TIMEOUT_SECONDS = 120

# flash-lite free tier: 15 rpm, 500 rpd. never actually call at the real limit,
# leave headroom in case of clock drift, other processes using the same key, etc.
RPM_LIMIT = 15
RPD_LIMIT = 500
SAFETY_MARGIN = 0.9
SAFE_RPM = int(RPM_LIMIT * SAFETY_MARGIN)  # floored
SAFE_RPD = int(RPD_LIMIT * SAFETY_MARGIN)  # floored

AI_OUTPUTS_DIR = Path("ai-outputs")


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

    instructions = f"""You are translating UI/software strings from English to {TARGET_LANGUAGE}.

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

    instructions = f"""You are translating gettext plural strings from English to {TARGET_LANGUAGE}.

Each item has an english "singular" and "plural" source form (both english, this is just gettext convention,
not the translation). For each item, return three {TARGET_LANGUAGE} forms following the standard Polish
gettext plural rule (nplurals=3):
- "one": used when count == 1
- "few": used when count%10 is 2..4, and count%100 is NOT 10..20 (e.g. 2, 3, 4, 22, 23, 24)
- "many": used for everything else (0, 5..21, 25..31, etc)

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
    outputs_dir = AI_OUTPUTS_DIR / "raw"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{chunk.chunk_id}__attempt{attempt}__{'ok' if ok else 'fail'}.json"
    (outputs_dir / fname).write_text(raw_text, encoding="utf-8")

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

    leading = source[: len(source) - len(source.lstrip())]
    trailing = source[len(source.rstrip()) :] if source.strip() else ""
    return f"{leading}{translated.strip()}{trailing}"

def save_chunk_result(chunk: Chunk, parsed):
    """This is what step 5 (merge/sanitize) will actually read. It carries the
    key/resource/staging_path info that we deliberately never sent to the model,
    joined back in locally by id."""

    results_dir = AI_OUTPUTS_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

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

    out_path = results_dir / f"{chunk.chunk_id}.json"
    out_path.write_text(
        json.dumps(result_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def process_chunk(
    client: genai.Client, chunk: Chunk, rate_limiter: RateLimiter
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

    for attempt in range(1, MAX_RETRIES + 1):
        log_record["attempts"] = attempt
        strict = attempt == MAX_RETRIES  # only tighten the prompt on the last try

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


def run(batch_path: Path):
    AI_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    batch = load_batch(batch_path)
    grouped = group_by_format(batch)

    all_chunks: List[Chunk] = []
    for format_name, items in grouped.items():
        all_chunks.extend(build_chunks(format_name, items))

    print(
        f"loaded {len(batch)} items across {len(grouped)} format(s), "
        f"packed into {len(all_chunks)} chunks"
    )

    load_dotenv()
    client = genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),  # ms
    )
    rate_limiter = RateLimiter(rpm=SAFE_RPM, rpd=SAFE_RPD)
    logs: List[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS) as pool:
        futures = {
            pool.submit(process_chunk, client, chunk, rate_limiter): chunk
            for chunk in all_chunks
        }
        done = 0
        for future in as_completed(futures):
            record = future.result()
            logs.append(record)
            done += 1
            status = record["status"]
            print(
                f"[{done}/{len(all_chunks)}] {record['chunk_id']} -> {status} "
                f"({record['attempts']} attempt(s), {record['duration_seconds']}s)"
            )

    write_summary(logs)

    failed_count = sum(1 for l in logs if l["status"] == "failed")
    print(
        f"\ndone. {len(logs) - failed_count}/{len(logs)} chunks succeeded. "
        f"see {AI_OUTPUTS_DIR}/summary.md for details."
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python translate_batch.py <path-to-batch.json>")
        raise SystemExit(1)
    run(Path(sys.argv[1]))
