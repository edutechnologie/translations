"""
config.py — Centralized configuration for the nask-tooling translation pipeline.
It defines constants extracted from the existing scripts.
"""

from pathlib import Path

# ============================================================
# PATHS AND DIRECTORIES
# ============================================================

REPO_ROOT = Path(__file__).resolve().parents[3]

NASK_TOOLING_ROOT = Path(__file__).resolve().parents[2]

TRANSIFEX_YML_PATH = REPO_ROOT / "transifex.yml"

NASK_TOOLING_DIR_NAME = "nask-tooling"

READY_DIR_NAME = "ready"

STAGING_DIR_NAME = "staging"


STAGING_ROOT = NASK_TOOLING_ROOT / STAGING_DIR_NAME / "translations"

READY_ROOT = NASK_TOOLING_ROOT / READY_DIR_NAME / "translations"


TRANSLATIONS_ROOT = REPO_ROOT / "translations"


AI_OUTPUTS_DIR = NASK_TOOLING_ROOT / "ai-outputs"

RAW_DIR = AI_OUTPUTS_DIR / "raw"

RESULTS_DIR = AI_OUTPUTS_DIR / "results"

BATCH_CHUNKS_DIR = AI_OUTPUTS_DIR / "batch_chunks"
# ============================================================
# FILE NAMES
# ============================================================

BATCH_TODO_FILENAME = "batch_todo.json"
BATCH_TODO_PATH = NASK_TOOLING_ROOT / BATCH_TODO_FILENAME


# ============================================================
# TRANSLATION & CATALOG SETTINGS
# ============================================================


TARGET_LANG_CODE = "pl"

TARGET_LANGUAGE_NAME = "Polish"
TARGET_LANGUAGE_N_OF_PLURAL_FORMS = 3
TARGET_LANGUAGE_PLURAL_FORMS_PROMPT_EXPLANATION = (
    '- "one": used when count == 1'
    '- "few": used when count%10 is 2..4, and count%100 is NOT 10..20 (e.g. 2, 3, 4, 22, 23, 24)'
    '- "many": used for everything else (0, 5..21, 25..31, etc)'
)


SUPPORTED_FORMATS = {"PO", "KEYVALUEJSON"}

PLURAL_FORMS_HEADER = (
    "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 "
    "&& (n%100<10 || n%100>=20) ? 1 : 2);"
)

# ============================================================
# AI TRANSLATION SETTINGS
# ============================================================

MODEL_NAME = "gemini-3.5-flash-lite"

TEMPERATURE = 0.3

# the sdk's default http timeout is None, meaning no timeout at all - there are known
# reports of generate_content stalling for minutes with no error raised. set one explicitly,
# a genuine hang will now raise instead of parking a worker thread forever, and our normal
# retry loop already treats any exception the same way
REQUEST_TIMEOUT_SECONDS = 120

# ============================================================
# CHUNKING & CONTEXT SETTINGS
# ============================================================

# hard cap on item count per call
MAX_ITEMS_PER_CHUNK = 40
# secondary cap, so a chunk of a few long PO strings doesn't balloon
MAX_CHARS_PER_CHUNK = 6000

MAX_RETRIES = 3
# multiplied by attempt number
RETRY_BACKOFF_SECONDS = 5

# this is network IO bound, GIL is released during the request,
# so threads are fine here - keep this modest to respect rate limits
MAX_CONCURRENT_CALLS = 5

# ============================================================
# RATE LIMITS
# ============================================================

# flash-lite free tier: 15 rpm, 500 rpd. never actually call at the real limit,
# leave headroom in case of clock drift, other processes using the same key, etc.
RPM_LIMIT = 15
RPD_LIMIT = 500
RATE_LIMIT_SAFETY_MARGIN = 0.9
# Derived values (computed here to keep ai_translate.py clean)
SAFE_RPM = int(RPM_LIMIT * RATE_LIMIT_SAFETY_MARGIN)
SAFE_RPD = int(RPD_LIMIT * RATE_LIMIT_SAFETY_MARGIN)

# ============================================================
# VALIDATION SETTINGS
# ============================================================

PLACEHOLDER_RE_PATTERN = r"%\([a-zA-Z0-9_]+\)[sd]|%[sd]|\{\{?[a-zA-Z0-9_]+\}?\}"

MSGFMT_BIN = "msgfmt"
MSGFMT_ARGS = ["-v", "--strict", "--check"]

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

# TODO: Replace "GEMINI_API_KEY" string in:
# - gemini_hello_world.py: line 8
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
