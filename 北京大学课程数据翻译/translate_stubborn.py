#!/usr/bin/env python3
"""Translate remaining stubborn rows one language at a time."""

import argparse
import json
import os
import sqlite3
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

LANGS = LANGUAGES
LANG_NAMES = LANGUAGE_NAMES

# (DB selector, field selector, path, source field, stored field, display label)
TASKS = (
    ("gr", "intro", DATABASES["gr"], "intro", "intro_cn", "GR intro"),
    ("gr", "extra_notes", DATABASES["gr"], "extra_notes", "extra_notes", "GR extra"),
    ("gr", "reference_book", DATABASES["gr"], "reference_book", "reference_book", "GR reference_book"),
    ("ug", "intro", DATABASES["ug"], "intro_cn", "intro_cn", "UG intro"),
    ("ug", "syllabus", DATABASES["ug"], "syllabus", "syllabus", "UG syllabus"),
    ("ug", "evaluation", DATABASES["ug"], "evaluation", "evaluation", "UG evaluation"),
    ("ug", "reference_book", DATABASES["ug"], "reference_book", "reference_book", "UG reference_book"),
    ("summer", "intro", DATABASES["summer"], "intro_cn", "intro_cn", "Summer intro"),
    ("summer", "syllabus", DATABASES["summer"], "syllabus", "syllabus", "Summer syllabus"),
    ("summer", "evaluation", DATABASES["summer"], "evaluation", "evaluation", "Summer evaluation"),
    ("summer", "reference_book", DATABASES["summer"], "reference_book", "reference_book", "Summer reference_book"),
    ("fall", "intro", DATABASES["fall"], "intro_cn", "intro_cn", "Fall intro"),
    ("fall", "syllabus", DATABASES["fall"], "syllabus", "syllabus", "Fall syllabus"),
    ("fall", "evaluation", DATABASES["fall"], "evaluation", "evaluation", "Fall evaluation"),
    ("fall", "reference_book", DATABASES["fall"], "reference_book", "reference_book", "Fall reference_book"),
    ("fall_gr", "intro", DATABASES["fall_gr"], "intro", "intro_cn", "Fall GR intro"),
    ("fall_gr", "extra_notes", DATABASES["fall_gr"], "extra_notes", "extra_notes", "Fall GR extra"),
    ("fall_gr", "syllabus", DATABASES["fall_gr"], "syllabus", "syllabus", "Fall GR syllabus"),
    ("fall_gr", "reference_book", DATABASES["fall_gr"], "reference_book", "reference_book", "Fall GR reference_book"),
)


def setup_db(db_path):
    setup_translation_db(db_path)


def translate_one(text, target, retries=3):
    prompt = (
        "Translate the following Chinese university course content to "
        f"{LANG_NAMES[target]} ({target}). Output the translation directly, "
        "no JSON wrapper, no commentary, no markdown.\n\n"
        "Preserve newlines and bullet-list structure. Keep Latin abbreviations, "
        "numbers, dates verbatim. Use standard academic terminology in the target "
        "language. Translate institutional names to their official forms.\n\n"
        f"Source (Chinese):\n{text}"
    )
    body_obj = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a professional academic translator."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    if ENABLE_THINKING is not None:
        body_obj["enable_thinking"] = ENABLE_THINKING.lower() in {"1", "true", "yes"}
    elif "qnaigc.com" in API_URL:
        body_obj["enable_thinking"] = False
    if "api.deepseek.com" in API_URL:
        body_obj["thinking"] = {"type": THINKING or "disabled"}
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body_obj).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        },
    )
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=300, context=SSL_CONTEXT) as response:
                result = json.loads(response.read())
            return clean_translation(result["choices"][0]["message"]["content"])
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_error


def gather_missing(db_path, src_field, store_field):
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT course_id, {src_field} FROM detail_info "
            f"WHERE {src_field} IS NOT NULL AND TRIM({src_field}) != ''"
        ).fetchall()
        existing_by_cid = {}
        for cid, lang in conn.execute(
            "SELECT course_id, lang FROM translations WHERE field=?",
            (store_field,),
        ):
            existing_by_cid.setdefault(cid, set()).add(lang)
        missing = []
        for cid, source in rows:
            existing = existing_by_cid.get(cid, ())
            needed = [lang for lang in LANGS if lang not in existing]
            if needed:
                missing.append((cid, source, needed))
    return missing


def process(db_path, src_field, store_field, label, workers=10, limit=0):
    missing = gather_missing(db_path, src_field, store_field)
    if not missing:
        print(f"[{label}] all done")
        return 0, 0
    tasks = [
        (cid, source, lang)
        for cid, source, langs in missing
        for lang in langs
    ]
    if limit:
        tasks = tasks[:limit]
    total = len(tasks)
    print(
        f"[{label}] {len(missing)} rows / "
        f"{sum(len(item[2]) for item in missing)} pending pairs; running {total}"
    )
    ok = 0
    failed = 0
    errors = []
    started = time.time()

    def work(item):
        cid, source, lang = item
        translated = translate_one(source, lang)
        write_translation_with_retry(db_path, cid, store_field, lang, translated)
        return cid, lang, len(translated)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(work, task): task for task in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            cid, _source, lang = futures[future]
            try:
                future.result()
                ok += 1
            except Exception as exc:
                failed += 1
                errors.append((cid, lang, f"{type(exc).__name__}: {str(exc)[:160]}"))
            if done % 50 == 0 or done == total:
                elapsed = time.time() - started
                rate = done / max(elapsed, 0.01)
                eta = (total - done) / max(rate, 0.01)
                print(
                    f"  [{label}] {done}/{total} ok={ok} fail={failed} "
                    f"elapsed={elapsed:.0f}s rate={rate:.2f}/s eta={eta:.0f}s"
                )
    for cid, lang, error in errors[:10]:
        print(f"    [{label}] cid={cid} {lang}: {error}")
    return ok, failed


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", choices=["gr", "ug", "summer", "fall", "fall_gr", "all"], default="all"
    )
    parser.add_argument(
        "--field",
        choices=["intro", "extra_notes", "syllabus", "evaluation", "reference_book", "all"],
        default="all",
    )
    parser.add_argument("--workers", type=positive_int, default=10)
    parser.add_argument("--limit", type=nonnegative_int, default=0)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    selected = [
        task
        for task in TASKS
        if args.db in ("all", task[0]) and args.field in ("all", task[1])
    ]
    selected_paths = list(dict.fromkeys(task[2] for task in selected))
    try:
        for db_path in selected_paths:
            setup_db(db_path)
    except Exception as exc:
        print(f"Setup failed: {type(exc).__name__}: {exc}")
        return 1

    print("== per-lang retranslate ==")
    total_ok = 0
    total_failed = 0
    remaining = args.limit if args.limit else None
    for _db_key, _field_key, db_path, src_field, store_field, label in selected:
        if remaining is not None and remaining <= 0:
            break
        try:
            ok, failed = process(
                db_path,
                src_field,
                store_field,
                label,
                workers=args.workers,
                limit=remaining or 0,
            )
        except Exception as exc:
            ok, failed = 0, 1
            print(f"[{label}] task failed: {type(exc).__name__}: {exc}")
        total_ok += ok
        total_failed += failed
        if remaining is not None:
            remaining -= ok + failed
    print(f"\nTOTAL: OK={total_ok} FAIL={total_failed}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
