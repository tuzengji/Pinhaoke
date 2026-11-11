#!/usr/bin/env python3
"""Translate ALL remaining Chinese-containing fields to 7 languages.

Strategy:
  - Dedupe by source value (many notes/categories repeat). Cache by source text.
  - Batch all 7 langs in one API call per unique source.
  - Store in translations(course_id, field, lang, text) for every (course, field)
    pair so the lookup is simple at request time.

Fields covered:
  Short (Phase A):
    UG basic_info  : course_name, notes, pnp, classroom, major
    UG detail_info : prerequisites, ge_series, textbook
    GR basic_info  : course_name, notes, classroom, major
    GR detail_info : audience, term
  Long (Phase B):
    UG detail_info : syllabus, evaluation, reference_book
    GR detail_info : reference_book, syllabus (fall GR)

Already done elsewhere:
  intro_cn (UG), intro (GR), extra_notes (GR) — handled by translate_courses.py

For English: re-use UG english_name when source is course_name. Skip API call.
"""
import argparse
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

try:
    from .translation_common import (
        DATABASES,
        LANGUAGE_NAMES,
        LANGUAGES,
        SSL_CONTEXT,
        clean_translation,
        get_api_key,
        nonnegative_int,
        positive_int,
        setup_translation_db,
        write_translation_with_retry,
    )
except ImportError:
    from translation_common import (
        DATABASES,
        LANGUAGE_NAMES,
        LANGUAGES,
        SSL_CONTEXT,
        clean_translation,
        get_api_key,
        nonnegative_int,
        positive_int,
        setup_translation_db,
        write_translation_with_retry,
    )

API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.qnaigc.com/v1/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek/deepseek-v4-flash")
ENABLE_THINKING = os.environ.get("DEEPSEEK_ENABLE_THINKING")
THINKING = os.environ.get("DEEPSEEK_THINKING")

UG_DB = DATABASES["ug"]
GR_DB = DATABASES["gr"]
SUMMER_DB = DATABASES["summer"]
FALL_DB = DATABASES["fall"]
FALL_GR_DB = DATABASES["fall_gr"]
LANGS = LANGUAGES
LANG_NAMES = LANGUAGE_NAMES

# (db_path, source_field, source_table, store_field, hint, is_long)
# 'hint' guides the translator (e.g. department names use official forms).
SHORT_JOBS = [
    (UG_DB, "course_name",   "basic_info",  "course_name",
     "Chinese university course title", False),
    (UG_DB, "notes",         "basic_info",  "notes",
     "Short course-registration note (may contain abbreviations, room numbers)", False),
    (UG_DB, "pnp",           "basic_info",  "pnp",
     "Whether P/NP grading can be elected", False),
    (UG_DB, "classroom",     "basic_info",  "classroom",
     "Classroom or building+room (e.g. 二教403 = Building 2 Room 403)", False),
    (UG_DB, "major",         "basic_info",  "major",
     "Major / specialization restriction", False),
    (UG_DB, "prerequisites", "detail_info", "prerequisites",
     "Course prerequisites (often a list of other course names)", False),
    (UG_DB, "ge_series",     "detail_info", "ge_series",
     "General Education core/elective series name", False),
    (UG_DB, "textbook",      "detail_info", "textbook",
     "Textbook citation(s)", False),
    (GR_DB, "course_name",   "basic_info",  "course_name",
     "Chinese university course title", False),
    (GR_DB, "notes",         "basic_info",  "notes",
     "Short course-registration note", False),
    (GR_DB, "classroom",     "basic_info",  "classroom",
     "Classroom or building+room", False),
    (GR_DB, "major",         "basic_info",  "major",
     "Major / specialization restriction", False),
    (GR_DB, "audience",      "detail_info", "audience",
     "Target audience (e.g. 硕博 = Master & PhD)", False),
    (GR_DB, "term",          "detail_info", "term",
     "Academic term / semester", False),

    # Summer undergrad (same schema as spring UG)
    (SUMMER_DB, "course_name",   "basic_info",  "course_name",
     "Chinese university course title", False),
    (SUMMER_DB, "notes",         "basic_info",  "notes",
     "Short course-registration note (may contain abbreviations, room numbers)", False),
    (SUMMER_DB, "pnp",           "basic_info",  "pnp",
     "Whether P/NP grading can be elected", False),
    (SUMMER_DB, "classroom",     "basic_info",  "classroom",
     "Classroom or building+room (e.g. 二教403 = Building 2 Room 403)", False),
    (SUMMER_DB, "major",         "basic_info",  "major",
     "Major / specialization restriction", False),
    (SUMMER_DB, "prerequisites", "detail_info", "prerequisites",
     "Course prerequisites (often a list of other course names)", False),
    (SUMMER_DB, "ge_series",     "detail_info", "ge_series",
     "General Education core/elective series name", False),
    (SUMMER_DB, "textbook",      "detail_info", "textbook",
     "Textbook citation(s)", False),

    # 26-27 fall undergrad (same schema as spring/summer UG)
    (FALL_DB, "course_name",   "basic_info",  "course_name",
     "Chinese university course title", False),
    (FALL_DB, "notes",         "basic_info",  "notes",
     "Short course-registration note (may contain abbreviations, room numbers)", False),
    (FALL_DB, "pnp",           "basic_info",  "pnp",
     "Whether P/NP grading can be elected", False),
    (FALL_DB, "classroom",     "basic_info",  "classroom",
     "Classroom or building+room (e.g. 二教403 = Building 2 Room 403)", False),
    (FALL_DB, "major",         "basic_info",  "major",
     "Major / specialization restriction", False),
    (FALL_DB, "prerequisites", "detail_info", "prerequisites",
     "Course prerequisites (often a list of other course names)", False),
    (FALL_DB, "ge_series",     "detail_info", "ge_series",
     "General Education core/elective series name", False),
    (FALL_DB, "textbook",      "detail_info", "textbook",
     "Textbook citation(s)", False),

    # 26-27 fall graduate (same schema as spring GR, plus syllabus handled as long)
    (FALL_GR_DB, "course_name", "basic_info",  "course_name",
     "Chinese university course title", False),
    (FALL_GR_DB, "notes",       "basic_info",  "notes",
     "Short course-registration note", False),
    (FALL_GR_DB, "classroom",   "basic_info",  "classroom",
     "Classroom or building+room", False),
    (FALL_GR_DB, "major",       "basic_info",  "major",
     "Major / specialization restriction", False),
    (FALL_GR_DB, "audience",    "detail_info", "audience",
     "Target audience (e.g. 硕博 = Master & PhD)", False),
    (FALL_GR_DB, "term",        "detail_info", "term",
     "Academic term / semester", False),
]
LONG_JOBS = [
    (UG_DB, "syllabus",     "detail_info", "syllabus",
     "Long-form syllabus / weekly schedule (preserve newlines and structure)", True),
    (UG_DB, "evaluation",   "detail_info", "evaluation",
     "Long-form teacher/student course evaluations (preserve structure)", True),
    (UG_DB, "reference_book","detail_info","reference_book",
     "Reference book list", True),
    (GR_DB, "reference_book","detail_info","reference_book",
     "Reference book list (citations, may be long)", True),

    # Summer undergrad
    (SUMMER_DB, "syllabus",     "detail_info", "syllabus",
     "Long-form syllabus / weekly schedule (preserve newlines and structure)", True),
    (SUMMER_DB, "evaluation",   "detail_info", "evaluation",
     "Long-form teacher/student course evaluations (preserve structure)", True),
    (SUMMER_DB, "reference_book","detail_info","reference_book",
     "Reference book list", True),

    # 26-27 fall undergrad
    (FALL_DB, "syllabus",     "detail_info", "syllabus",
     "Long-form syllabus / weekly schedule (preserve newlines and structure)", True),
    (FALL_DB, "evaluation",   "detail_info", "evaluation",
     "Long-form teacher/student course evaluations (preserve structure)", True),
    (FALL_DB, "reference_book","detail_info","reference_book",
     "Reference book list", True),

    # 26-27 fall graduate
    (FALL_GR_DB, "reference_book","detail_info","reference_book",
     "Reference book list (citations, may be long)", True),
    (FALL_GR_DB, "syllabus",     "detail_info", "syllabus",
     "Long-form syllabus / weekly schedule (preserve newlines and structure)", True),
]


def has_cn(s: str) -> bool:
    return bool(s) and bool(re.search(r"[一-鿿]", s))


def setup_db(db_path: str):
    setup_translation_db(db_path)


def call_api(text: str, langs, hint: str, max_retries: int = 3):
    """Batch-translate one Chinese text to multiple langs in one JSON call."""
    lang_spec = ", ".join(f"{l} ({LANG_NAMES[l]})" for l in langs)
    user_prompt = (
        f"Translate the following Chinese text ({hint}) to: {lang_spec}.\n\n"
        f"Return strict JSON with exactly these keys: {langs}. "
        f"No markdown, no commentary.\n\n"
        f"Guidelines:\n"
        f"- DO NOT translate proper nouns of people (人名).\n"
        f"- DO translate institutional names (院系/部门) using the OFFICIAL Peking "
        f"University English translation (e.g. 光华管理学院 → Guanghua School of "
        f"Management; 国家发展研究院 → National School of Development).\n"
        f"- Preserve numbers, codes, dates, room numbers verbatim.\n"
        f"- For room numbers like '二教403', expand to readable form in each target "
        f"language (e.g. EN: 'Building 2 Room 403', JA: '第2教学楼403教室').\n"
        f"- Preserve newlines and bullet structures.\n"
        f"- Use standard academic terminology.\n\n"
        f"Source (Chinese):\n{text}\n\nOutput JSON:"
    )
    body_obj = {
        "model": MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a professional academic translator. Output only valid JSON."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if ENABLE_THINKING is not None:
        body_obj["enable_thinking"] = ENABLE_THINKING.lower() in {"1", "true", "yes"}
    elif "qnaigc.com" in API_URL:
        body_obj["enable_thinking"] = False
    if "api.deepseek.com" in API_URL:
        body_obj["thinking"] = {"type": THINKING or "disabled"}
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        },
    )
    last = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=300, context=SSL_CONTEXT) as resp:
                r = json.loads(resp.read())
            content = r["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return {lang: clean_translation(parsed.get(lang)) for lang in langs}
        except Exception as e:
            last = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise last


def fetch_jobs(jobs, allow_non_cn=False):
    """Return list of (db_path, store_field, hint, course_id, source) needing work.

    allow_non_cn=True keeps rows whose source is pure English/Latin (no CJK).
    Use this to translate English-only course names into the other 6 langs.
    """
    out = []
    for db_path, src_field, src_table, store_field, hint, is_long in jobs:
        with closing(sqlite3.connect(db_path)) as conn:
            id_column = "course_id" if src_table == "detail_info" else "id"
            rows = conn.execute(
                f"SELECT {id_column} AS cid, {src_field} AS src FROM {src_table} "
                f"WHERE {src_field} IS NOT NULL AND TRIM({src_field}) != ''"
            ).fetchall()
            for cid, src in rows:
                if not allow_non_cn and not has_cn(src):
                    continue
                have = {
                    row[0]
                    for row in conn.execute(
                        "SELECT lang FROM translations "
                        "WHERE course_id=? AND field=?",
                        (cid, store_field),
                    )
                }
                missing = [lang for lang in LANGS if lang not in have]
                if missing:
                    out.append((db_path, store_field, hint, cid, src, missing))
    return out


def reuse_english_for_course_names(db_paths, limit=0):
    """Copy english_name into course_name/en, respecting a global write limit."""
    total = 0
    for db_path in db_paths:
        if limit and total >= limit:
            break
        with closing(sqlite3.connect(db_path)) as conn:
            query = (
                "SELECT course_id, english_name FROM detail_info "
                "WHERE english_name IS NOT NULL AND TRIM(english_name) != '' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM translations t WHERE t.course_id=detail_info.course_id "
                "AND t.field='course_name' AND t.lang='en'"
                ") ORDER BY course_id"
            )
            params = ()
            if limit:
                query += " LIMIT ?"
                params = (limit - total,)
            rows = conn.execute(query, params).fetchall()
        n = 0
        for cid, en in rows:
            if limit and total >= limit:
                break
            write_translation_with_retry(
                db_path, cid, "course_name", "en", clean_translation(en)
            )
            n += 1
            total += 1
        print(f"[reuse en] {db_path}: {n} course_name 'en' rows seeded from english_name")
    return total


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["short", "long", "all"], default="short")
    ap.add_argument("--workers", type=positive_int, default=15)
    ap.add_argument("--limit", type=nonnegative_int, default=0)
    ap.add_argument("--db", choices=["ug", "gr", "summer", "fall", "fall_gr", "all"], default="all",
                    help="Restrict jobs to one DB (ug = spring undergrad, gr = spring graduate, summer = summer undergrad, fall = 26-27 fall undergrad, fall_gr = 26-27 fall graduate). Default: all.")
    ap.add_argument("--allow-non-cn", action="store_true",
                    help="Also process rows whose source has no Chinese characters "
                         "(e.g. course_name is English-only). Use to translate "
                         "purely English course names into the other 6 target langs.")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    selected_paths = (
        list(DATABASES.values()) if args.db == "all" else [DATABASES[args.db]]
    )

    jobs = []
    if args.phase in ("short", "all"):
        jobs.extend(SHORT_JOBS)
    if args.phase in ("long", "all"):
        jobs.extend(LONG_JOBS)
    jobs = [job for job in jobs if job[0] in selected_paths]

    try:
        for db_path in selected_paths:
            setup_db(db_path)
        reused = 0
        if args.phase in ("short", "all"):
            reused = reuse_english_for_course_names(selected_paths, limit=args.limit)
        remaining = max(args.limit - reused, 0) if args.limit else 0
        pending = (
            fetch_jobs(jobs, allow_non_cn=args.allow_non_cn)
            if not args.limit or remaining
            else []
        )
    except Exception as exc:
        print(f"Setup, reuse, or pending scan failed: {type(exc).__name__}: {exc}")
        return 1
    if args.limit:
        pending = pending[:remaining]

    if not pending:
        print("Nothing pending.")
        return 0

    # Dedup cache: source -> {lang: translation}
    # Each unique source is translated at most once.
    cache: dict = {}
    cache_lock = threading.Lock()

    # Group pending items by source text. For each source, find the union of
    # missing langs across all rows that use it.
    by_source: dict = {}
    for db_path, store_field, hint, cid, src, missing in pending:
        key = (src, hint)
        by_source.setdefault(key, {"missing": set(), "rows": []})
        by_source[key]["missing"].update(missing)
        by_source[key]["rows"].append((db_path, store_field, cid, missing))

    total_unique = len(by_source)
    total_rows = len(pending)
    print(f"Unique sources : {total_unique}")
    print(f"Total (cid,fld): {total_rows}")

    completed_sources = 0
    completed_rows = 0
    errors = []
    t0 = time.time()

    def process_one(item):
        nonlocal completed_sources, completed_rows
        (src, hint), info = item
        missing_langs = sorted(info["missing"])
        try:
            translations = call_api(src, missing_langs, hint)
        except Exception as e:
            with cache_lock:
                errors.append((src[:60], str(e)[:200]))
            return False
        with cache_lock:
            cache[src] = translations
        # Now write to DB for each row that needs this source
        for db_path, store_field, cid, want_langs in info["rows"]:
            row_ok = True
            for lang in want_langs:
                try:
                    write_translation_with_retry(
                        db_path, cid, store_field, lang, translations[lang]
                    )
                except Exception as e:
                    row_ok = False
                    with cache_lock:
                        errors.append((f"write cid={cid} lang={lang}", str(e)))
            if row_ok:
                with cache_lock:
                    completed_rows += 1
        with cache_lock:
            completed_sources += 1
        return True

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(process_one, item) for item in by_source.items()]
        last_print = 0
        for future in as_completed(futs):
            try:
                future.result()
            except Exception as exc:
                with cache_lock:
                    errors.append(("worker future", f"{type(exc).__name__}: {exc}"))
            if completed_sources - last_print >= 25 or completed_sources == total_unique:
                elapsed = time.time() - t0
                rate = completed_sources / max(elapsed, 0.01)
                eta = (total_unique - completed_sources) / max(rate, 0.01)
                print(
                    f"  [{completed_sources}/{total_unique} unique  "
                    f"{completed_rows}/{total_rows} rows] "
                    f"elapsed={elapsed:.0f}s rate={rate:.1f}/s eta={eta:.0f}s "
                    f"errors={len(errors)}"
                )
                last_print = completed_sources

    elapsed = time.time() - t0
    print(f"\n== Done in {elapsed:.0f}s ==")
    print(f"  unique sources translated: {completed_sources}/{total_unique}")
    print(f"  rows updated             : {completed_rows}/{total_rows}")
    print(f"  errors                   : {len(errors)}")
    for src, err in errors[:10]:
        print(f"    {src!r}: {err}")
    return 1 if errors or completed_sources != total_unique else 0


if __name__ == "__main__":
    raise SystemExit(main())
