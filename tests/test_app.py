from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        app._health_cache_payload = None
        app._health_cache_checked_at = None

    def health_endpoint(self):
        route = next((route for route in app.app.routes if route.path == "/api/health"), None)
        self.assertIsNotNone(route)
        if route is None:
            self.fail("/api/health route is missing")
        return route.endpoint

    def assert_unhealthy_response(self, response, temporary_directory):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b'{"status":"error"}')
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(str(temporary_directory).encode(), response.body)

    def test_get_db_defaults_to_fall_and_is_query_only(self):
        with app.get_db() as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden_write(id INTEGER)")

    def test_app_imports_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = "import app; print(app.root().path)"
            env = {"PYTHONPATH": str(app.BASE_DIR)}
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), app.BASE_DIR / "index.html")

    def test_reviews_page_resolves_from_project_directory(self):
        self.assertEqual(Path(app.reviews_page().path), app.BASE_DIR / "reviews.html")

    def test_page_routes_force_cache_revalidation(self):
        # 无 Cache-Control 时浏览器按启发式缓存旧页面，部署后用户可能长时间看不到新版
        for response in (app.root(None), app.reviews_page(None)):
            self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_attach_failure_closes_main_connection(self):
        fake = app.sqlite3.connect(":memory:")
        missing_db = Path("missing.db")
        self.addCleanup(missing_db.unlink, missing_ok=True)
        with patch.object(app.sqlite3, "connect", return_value=fake):
            with patch.dict(
                app.TERM_DBS,
                {"broken": [("main", Path("a.db"), "x"), ("gr", Path("missing.db"), "y")]},
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    with app.get_db("broken"):
                        pass
        with self.assertRaises(sqlite3.ProgrammingError):
            fake.execute("SELECT 1")

    def test_health_reports_all_five_databases(self):
        if not hasattr(app, "check_database_health"):
            self.fail("check_database_health is missing")
        payload = app.check_database_health()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["databases"]), 5)
        self.assertTrue(all(item["integrity"] == "ok" for item in payload["databases"]))
        self.assertEqual(payload["reviews"]["integrity"], "ok")
        self.assertEqual(payload["reviews"]["threads"], 47843)
        self.assertEqual(payload["reviews"]["snapshot_replies"], 210570)
        self.assertEqual(payload["reviews"]["highlights"], 188759)

    def test_health_endpoint_disables_caching(self):
        response = self.health_endpoint()()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_health_endpoint_caches_successful_deep_scans_for_ttl(self):
        if not hasattr(app, "get_cached_database_health"):
            self.fail("get_cached_database_health is missing")

        payload = {"status": "ok", "databases": []}
        with patch.object(app, "check_database_health", return_value=payload) as check:
            with patch.object(app.time, "monotonic", side_effect=[100.0, 101.0, 400.0]):
                first = self.health_endpoint()()
                second = self.health_endpoint()()
                expired = self.health_endpoint()()

        self.assertEqual([first.status_code, second.status_code, expired.status_code], [200, 200, 200])
        self.assertEqual(check.call_count, 2)

    def test_health_endpoint_does_not_cache_failures(self):
        if not hasattr(app, "get_cached_database_health"):
            self.fail("get_cached_database_health is missing")

        with patch.object(
            app,
            "check_database_health",
            side_effect=[RuntimeError("bad database"), {"status": "ok", "databases": []}],
        ) as check:
            first = self.health_endpoint()()
            second = self.health_endpoint()()

        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(check.call_count, 2)

    def test_health_endpoint_hides_missing_required_table_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "missing-translations.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER)")
                conn.execute("CREATE TABLE detail_info(course_id INTEGER)")
                conn.commit()
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)


    def test_health_endpoint_hides_basic_detail_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "count-mismatch.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER)")
                conn.execute("CREATE TABLE detail_info(course_id INTEGER)")
                conn.execute("CREATE TABLE translations(course_id INTEGER)")
                conn.execute("INSERT INTO basic_info VALUES (1)")
                conn.commit()
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)

    def test_health_endpoint_hides_equal_count_but_mismatched_course_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "relation-mismatch.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER PRIMARY KEY)")
                conn.execute("CREATE TABLE detail_info(course_id INTEGER PRIMARY KEY)")
                conn.execute("CREATE TABLE translations(course_id INTEGER)")
                conn.execute("INSERT INTO basic_info VALUES (1)")
                conn.execute("INSERT INTO detail_info VALUES (2)")
                conn.commit()
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)

    def test_health_endpoint_hides_duplicate_course_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "duplicate-ids.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER)")
                conn.execute("CREATE TABLE detail_info(course_id INTEGER)")
                conn.execute("CREATE TABLE translations(course_id INTEGER)")
                conn.executemany("INSERT INTO basic_info VALUES (?)", [(1,), (1,)])
                conn.executemany("INSERT INTO detail_info VALUES (?)", [(1,), (1,)])
                conn.commit()
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)

    def test_health_endpoint_hides_foreign_key_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "foreign-key-violation.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE detail_info("
                    "course_id INTEGER PRIMARY KEY REFERENCES basic_info(id))"
                )
                conn.execute(
                    "CREATE TABLE translations("
                    "course_id INTEGER REFERENCES basic_info(id))"
                )
                conn.execute("INSERT INTO basic_info VALUES (1)")
                conn.execute("INSERT INTO detail_info VALUES (1)")
                conn.execute("INSERT INTO translations VALUES (2)")
                conn.commit()
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)

    def test_health_endpoint_hides_integrity_check_failure(self):
        class IntegrityFailureConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql):
                if sql == "PRAGMA integrity_check":
                    return type("IntegrityResult", (), {"fetchone": lambda self: ("not ok",)})()
                return self.connection.execute(sql)

            def close(self):
                self.connection.close()

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "integrity-failure.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE basic_info(id INTEGER)")
                conn.execute("CREATE TABLE detail_info(course_id INTEGER)")
                conn.execute("CREATE TABLE translations(course_id INTEGER)")
                conn.commit()
            real_connect = sqlite3.connect
            with patch.dict(app.TERM_DBS, {"test": [("main", database, "x")]}, clear=True):
                with patch.object(
                    app.sqlite3,
                    "connect",
                    side_effect=lambda *args, **kwargs: IntegrityFailureConnection(real_connect(*args, **kwargs)),
                ):
                    response = self.health_endpoint()()

            self.assert_unhealthy_response(response, tmp)


class MessageBoardApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_path = app.MESSAGES_DB_PATH
        app.MESSAGES_DB_PATH = Path(self._tmp.name) / "留言板.db"
        self.addCleanup(setattr, app, "MESSAGES_DB_PATH", self._original_path)

    @staticmethod
    def request(ip="203.0.113.9"):
        return Mock(headers={"x-real-ip": ip}, client=None)

    def test_post_then_list_roundtrip_without_identity_fields(self):
        created = app.create_message(self.request(), {"content": "  希望增加成绩分布查询  "})
        self.assertEqual(set(created), {"id", "posted_at", "content", "nickname", "reply_count"})
        self.assertEqual(created["content"], "希望增加成绩分布查询")
        listing = app.list_messages(page=1, page_size=20)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(set(listing), {"total", "page", "page_size", "messages"})
        self.assertEqual(set(listing["messages"][0]), {"id", "posted_at", "content", "nickname", "reply_count"})
        self.assertEqual(listing["messages"][0]["nickname"], "路过的 PKUer")
        self.assertEqual(listing["messages"][0]["content"], "希望增加成绩分布查询")

    def test_list_is_newest_first_and_paginated(self):
        for index in range(3):
            app.create_message(self.request(f"198.51.100.{index}"), {"content": f"第 {index} 条"})
        listing = app.list_messages(page=1, page_size=2)
        self.assertEqual(listing["total"], 3)
        self.assertEqual([m["content"] for m in listing["messages"]], ["第 2 条", "第 1 条"])
        second = app.list_messages(page=2, page_size=2)
        self.assertEqual([m["content"] for m in second["messages"]], ["第 0 条"])

    def test_content_validation_rejects_bad_payloads(self):
        for payload in (
            {"content": ""},
            {"content": "   "},
            {"content": "x" * (app.MESSAGE_MAX_LENGTH + 1)},
            {"content": 42},
            {"content": None},
            {"other": "x"},
            ["content"],
            "content",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.create_message(self.request(), payload)
                self.assertEqual(ctx.exception.status_code, 422)

    def test_pagination_validation_rejects_bad_values(self):
        for values in (
            {"page": 0},
            {"page": 10001},
            {"page_size": 0},
            {"page_size": app.MESSAGE_PAGE_SIZE_MAX + 1},
            {"page": True},
        ):
            with self.subTest(values=values):
                args = {"page": 1, "page_size": 20}
                args.update(values)
                with self.assertRaises(app.HTTPException) as ctx:
                    app.list_messages(**args)
                self.assertEqual(ctx.exception.status_code, 422)

    def test_rate_limit_applies_per_ip_hash(self):
        hourly_limit = app.MESSAGE_RATE_LIMITS[0][1]
        for index in range(hourly_limit):
            app.create_message(self.request(), {"content": f"正常留言 {index}"})
        with self.assertRaises(app.HTTPException) as ctx:
            app.create_message(self.request(), {"content": "超限留言"})
        self.assertEqual(ctx.exception.status_code, 429)
        # 其他 IP 不受影响
        other = app.create_message(self.request("198.51.100.200"), {"content": "另一位用户"})
        self.assertEqual(other["content"], "另一位用户")

    def test_ip_hash_stays_out_of_api_and_message_db_is_separate(self):
        app.create_message(self.request(), {"content": "隐私检查"})
        listing = app.list_messages(page=1, page_size=10)
        self.assertNotIn("ip_hash", listing["messages"][0])
        # 留言库独立于六个只读正式库
        self.assertNotEqual(app.MESSAGES_DB_PATH.parent, app.DB_DIR)
        with closing(sqlite3.connect(app.MESSAGES_DB_PATH)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("messages", tables)
        self.assertNotIn("basic_info", tables)


class VisitStatsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_path = app.STATS_DB_PATH
        app.STATS_DB_PATH = Path(self._tmp.name) / "访问统计.db"
        self.addCleanup(setattr, app, "STATS_DB_PATH", self._original_path)

    @staticmethod
    def request(ip="203.0.113.7", ua="Mozilla/5.0"):
        return Mock(headers={"x-real-ip": ip, "user-agent": ua}, client=None)

    def stats(self):
        response = app.get_stats()
        return json.loads(response.body)

    def test_same_visitor_counts_once_per_day_but_accumulates_views(self):
        app.record_visit(self.request("1.1.1.1"))
        app.record_visit(self.request("1.1.1.1"))
        app.record_visit(self.request("2.2.2.2"))
        payload = self.stats()
        self.assertEqual(payload["today"], {"views": 3, "visitors": 2})
        self.assertEqual(payload["total"], {"views": 3, "visitors": 2})

    def test_bot_user_agents_are_ignored(self):
        for ua in ("Googlebot/2.1", "curl/8.0", "python-requests/2.0", ""):
            app.record_visit(self.request("9.9.9.9", ua))
        self.assertEqual(self.stats()["today"], {"views": 0, "visitors": 0})

    def test_trend_has_seven_days_ending_today_in_beijing_time(self):
        app.record_visit(self.request("1.1.1.1"))
        payload = self.stats()
        self.assertEqual(len(payload["trend"]), app.STATS_TREND_DAYS)
        today = app._beijing_day(int(app.time.time()))
        self.assertEqual(payload["trend"][-1]["day"], today)
        self.assertEqual(payload["trend"][-1]["views"], 1)
        days = [point["day"] for point in payload["trend"]]
        self.assertEqual(days, sorted(days))

    def test_week_window_and_backfilled_day_aggregate(self):
        app.record_visit(self.request("1.1.1.1"))
        now = int(app.time.time())
        with app.get_stats_db() as conn:
            yesterday = app._beijing_day(now - 86400)
            old = app._beijing_day(now - 30 * 86400)
            conn.execute(
                "INSERT INTO visit_days(day, ip_hash, views, last_at) VALUES (?,?,?,?)",
                (yesterday, "y1", 4, now - 86400),
            )
            conn.execute(
                "INSERT INTO visit_days(day, ip_hash, views, last_at) VALUES (?,?,?,?)",
                (old, "o1", 9, now - 30 * 86400),
            )
            conn.commit()
        payload = self.stats()
        self.assertEqual(payload["week"], {"views": 5, "visitors": 2})
        self.assertEqual(payload["total"], {"views": 14, "visitors": 3})

    def test_stats_response_is_no_store_and_has_no_identity_fields(self):
        app.record_visit(self.request())
        response = app.get_stats()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"ip_hash", response.body)

    def test_recording_never_raises_and_page_routes_still_return_files(self):
        # request=None 与坏库路径都不能抛出，页面照常返回
        app.record_visit(None)
        self.assertEqual(Path(app.root(None).path), app.BASE_DIR / "index.html")
        self.assertEqual(Path(app.reviews_page(None).path), app.BASE_DIR / "reviews.html")
        app.STATS_DB_PATH = Path(self._tmp.name) / "missing" / "访问统计.db"
        try:
            app.record_visit(self.request())  # 目录不存在，应被静默吞掉
        except Exception as exc:  # noqa: BLE001 - 明确断言不抛出
            self.fail(f"record_visit raised: {exc!r}")

    def test_head_requests_are_supported_on_page_routes_without_counting_visits(self):
        for path in ("/", "/reviews"):
            route = next((r for r in app.app.routes if getattr(r, "path", None) == path), None)
            self.assertIsNotNone(route, path)
            self.assertTrue({"GET", "HEAD"}.issubset(route.methods), path)
        head_req = Mock(method="HEAD", headers={"x-real-ip": "1.2.3.4", "user-agent": "Mozilla/5.0"}, client=None)
        app.root(head_req)
        app.reviews_page(head_req)
        self.assertEqual(self.stats()["today"], {"views": 0, "visitors": 0})


def _session_cookie(response):
    header = response.headers.get("set-cookie", "")
    match = re.match(r"pinhaoke_session=([^;]*)", header)
    return (match.group(1) if match else None), header


def _account_request(
    ip="203.0.113.9",
    cookie=None,
    origin="https://www.pinhaoke.love",
    host="www.pinhaoke.love",
    proto="https",
    referer=None,
):
    headers = {"x-real-ip": ip, "host": host, "x-forwarded-proto": proto}
    if origin is not None:
        headers["origin"] = origin
    if referer is not None:
        headers["referer"] = referer
    cookies = {app.SESSION_COOKIE: cookie} if cookie else {}
    return Mock(headers=headers, cookies=cookies, client=None, url=Mock(scheme="http"))


def _build_course_db(path, courses):
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE basic_info("
            " id INTEGER PRIMARY KEY, course_type TEXT, course_code TEXT, class_no TEXT,"
            " course_name TEXT, category TEXT, credits REAL, teacher TEXT, department TEXT,"
            " major TEXT, grade TEXT, schedule TEXT, classroom TEXT, enrollment TEXT,"
            " pnp TEXT, notes TEXT, weekdays TEXT, first_period INTEGER)"
        )
        conn.execute(
            "CREATE TABLE detail_info("
            " course_id INTEGER PRIMARY KEY REFERENCES basic_info(id), english_name TEXT,"
            " prerequisites TEXT, intro_cn TEXT, intro_en TEXT, grading TEXT, ge_series TEXT,"
            " language TEXT, textbook TEXT, reference_book TEXT, syllabus TEXT, evaluation TEXT)"
        )
        conn.execute(
            "CREATE TABLE translations(course_id INTEGER, field TEXT, lang TEXT, text TEXT)"
        )
        for course in courses:
            conn.execute(
                "INSERT INTO basic_info (id, course_type, course_code, class_no, course_name,"
                " category, credits, teacher, department, schedule, weekdays)"
                " VALUES (?, '专业课', ?, ?, ?, '专业必修', ?, ?, ?, ?, '周三')",
                (
                    course["id"], course["course_code"], course["class_no"],
                    course["course_name"], course["credits"], course["teacher"],
                    course["department"], course["schedule"],
                ),
            )
            conn.execute(
                "INSERT INTO detail_info (course_id, english_name) VALUES (?, ?)",
                (course["id"], "Course"),
            )
        conn.commit()


FIXTURE_COURSES = [
    {
        "id": 1, "course_code": "04831180", "class_no": "1",
        "course_name": "PSoC应用开发基础实验", "credits": 2.0, "teacher": "张三(教授)",
        "department": "信息科学技术学院", "schedule": "1~16周 每周周三3~4节",
    },
    {
        "id": 2, "course_code": "00131520", "class_no": "2",
        "course_name": "数学分析（一）", "credits": 5.0, "teacher": "",
        "department": "数学科学学院", "schedule": "1~16周 每周周一1~2节",
    },
]


class AccountApiTests(unittest.TestCase):
    QUESTIONS = [{"question": "最喜欢的课？", "answer": " Hello World "}]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.production_scrypt = dict(app.SCRYPT_PARAMS)
        self.addCleanup(setattr, app, "ACCOUNTS_DB_PATH", app.ACCOUNTS_DB_PATH)
        app.ACCOUNTS_DB_PATH = Path(self._tmp.name) / "账户.db"
        patcher = patch.object(app, "SCRYPT_PARAMS", {"n": 16, "r": 8, "p": 1})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, app, "_DUMMY_SECRET_HASH", None)
        app._DUMMY_SECRET_HASH = None

    request = staticmethod(_account_request)

    def register(self, username="Alice_01", password="correct-horse", ip="203.0.113.9", **extra):
        payload = {"username": username, "password": password, "questions": self.QUESTIONS}
        payload.update(extra)
        response = app.register_account(self.request(ip=ip), payload)
        token, _ = _session_cookie(response)
        return response, token

    def login(self, username="Alice_01", password="correct-horse", ip="203.0.113.9", cookie=None):
        response = app.login_account(
            self.request(ip=ip, cookie=cookie), {"username": username, "password": password}
        )
        token, _ = _session_cookie(response)
        return response, token

    @staticmethod
    def body(response):
        return json.loads(response.body)

    def db(self):
        return closing(sqlite3.connect(app.ACCOUNTS_DB_PATH))

    def assert_stale_verification_rejected(self, pending_call, concurrent_call):
        verified = threading.Event()
        resume = threading.Event()
        verify_secret = app._verify_secret

        def pause_after_verification(secret, stored):
            result = verify_secret(secret, stored)
            if threading.current_thread().name.startswith("stale-auth"):
                verified.set()
                if not resume.wait(5):
                    raise AssertionError("Concurrent account update did not finish")
            return result

        with patch.object(app, "_verify_secret", side_effect=pause_after_verification):
            with ThreadPoolExecutor(1, thread_name_prefix="stale-auth") as pool:
                pending = pool.submit(pending_call)
                try:
                    self.assertTrue(verified.wait(5), "Password verification did not start")
                    concurrent_call()
                finally:
                    resume.set()
                with self.assertRaises(app.HTTPException) as ctx:
                    pending.result(timeout=5)
                self.assertEqual(ctx.exception.status_code, 401)

    def test_register_logs_in_and_never_leaks_secrets(self):
        response, token = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.body(response), {"username": "Alice_01"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        _, header = _session_cookie(response)
        self.assertTrue(token)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=lax", header)
        self.assertIn("Secure", header)
        self.assertIn("Path=/", header)
        self.assertIn(f"Max-Age={app.SESSION_TTL_SECONDS}", header)

        account = app.get_account(self.request(cookie=token))
        self.assertEqual(account.status_code, 200)
        payload = self.body(account)
        self.assertEqual(
            set(payload),
            {"authenticated", "username", "nickname", "questions", "favorites", "limit",
             "collections", "collections_limit"},
        )
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["username"], "Alice_01")
        self.assertEqual(payload["questions"], [{"position": 1, "question": "最喜欢的课？"}])
        self.assertEqual(payload["favorites"], [])
        self.assertEqual(payload["limit"], app.FAVORITES_MAX)
        self.assertEqual(payload["collections_limit"], app.COLLECTIONS_MAX)
        self.assertEqual(len(payload["collections"]), 1)
        self.assertTrue(payload["collections"][0]["is_default"])
        self.assertEqual(payload["collections"][0]["count"], 0)
        self.assertEqual(
            set(payload["collections"][0]), {"id", "name", "is_default", "position", "count"}
        )
        for forbidden in (b"password_hash", b"answer_hash", b"ip_hash", b"token_hash", b"user_id"):
            self.assertNotIn(forbidden, account.body)
        with self.db() as conn:
            stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
            self.assertTrue(stored.startswith("scrypt$16$8$1$"))
            self.assertNotIn("correct-horse", stored)
            answer_hash = conn.execute("SELECT answer_hash FROM security_questions").fetchone()[0]
            self.assertNotIn("helloworld", answer_hash)
            token_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertNotEqual(token_hash, token)
            self.assertEqual(token_hash, app._session_token_hash(token))

    def test_cookie_secure_flag_follows_forwarded_proto(self):
        response, _ = self.register(ip="198.51.100.1")
        self.assertIn("Secure", response.headers["set-cookie"])
        plain = app.login_account(
            self.request(ip="198.51.100.2", proto="http", origin="http://127.0.0.1:8000", host="127.0.0.1:8000"),
            {"username": "alice_01", "password": "correct-horse"},
        )
        self.assertNotIn("Secure", plain.headers["set-cookie"])
        self.assertIn("HttpOnly", plain.headers["set-cookie"])

    def test_anonymous_account_lookup_does_not_touch_accounts_db(self):
        response = app.get_account(self.request())
        self.assertEqual(self.body(response), {"authenticated": False})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(app.ACCOUNTS_DB_PATH.exists())
        garbage = app.get_account(self.request(cookie="not-a-real-token"))
        self.assertEqual(self.body(garbage), {"authenticated": False})
        self.assertIn("Max-Age=0", garbage.headers["set-cookie"])

    def test_username_is_unique_case_insensitively_but_keeps_display_case(self):
        self.register(username="Alice_01")
        with self.assertRaises(app.HTTPException) as ctx:
            self.register(username="alice_01", ip="198.51.100.7")
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(app.HTTPException) as ctx:
            self.register(username="ALICE_01", ip="198.51.100.8")
        self.assertEqual(ctx.exception.status_code, 409)
        response, _ = self.login(username="ALICE_01", ip="198.51.100.9")
        self.assertEqual(self.body(response), {"username": "Alice_01"})

    def test_register_rejects_invalid_payloads(self):
        good = {"username": "Bob_2026", "password": "correct-horse", "questions": self.QUESTIONS}
        bad_cases = {
            "short username": {"username": "ab"},
            "long username": {"username": "a" * 21},
            "unicode username": {"username": "小明123"},
            "dash username": {"username": "bob-2026"},
            "non-string username": {"username": 123},
            "short password": {"password": "1234567"},
            "long password": {"password": "x" * 129},
            "password equals username": {"password": "bob_2026"},
            "non-string password": {"password": ["x"] * 8},
            "surrogate password": {"password": "password\ud800"},
            "no questions": {"questions": []},
            "too many questions": {"questions": [{"question": f"q{i}", "answer": "abc"} for i in range(4)]},
            "question not dict": {"questions": ["q"]},
            "questions not list": {"questions": {"question": "q", "answer": "abc"}},
            "empty question": {"questions": [{"question": "   ", "answer": "abc"}]},
            "long question": {"questions": [{"question": "问" * 61, "answer": "abc"}]},
            "surrogate question": {"questions": [{"question": "问\ud800", "answer": "abc"}]},
            "duplicate questions": {
                "questions": [
                    {"question": "Same", "answer": "abc"},
                    {"question": "same", "answer": "def"},
                ]
            },
            "short answer": {"questions": [{"question": "q", "answer": " a "}]},
            "long answer": {"questions": [{"question": "q", "answer": "a" * 65}]},
            "answer equals username": {"questions": [{"question": "q", "answer": "Bob_2026"}]},
            "non-string answer": {"questions": [{"question": "q", "answer": 12}]},
            "surrogate answer": {"questions": [{"question": "q", "answer": "ab\udfff"}]},
        }
        for label, override in bad_cases.items():
            with self.subTest(label):
                payload = dict(good)
                payload.update(override)
                with self.assertRaises(app.HTTPException) as ctx:
                    app.register_account(self.request(ip="198.51.100.20"), payload)
                self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(app.HTTPException) as ctx:
            app.register_account(self.request(ip="198.51.100.20"), ["not", "a", "dict"])
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertFalse(app.ACCOUNTS_DB_PATH.exists())

    def test_password_validation_keeps_existing_unicode_and_control_characters(self):
        password = "课表\npassword\x00🔒"
        self.register(password=password)
        self.assertEqual(self.login(password=password)[0].status_code, 200)
        with self.assertRaises(app.HTTPException) as ctx:
            self.login(password="password\ud800")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_login_errors_are_uniform(self):
        self.register()
        details = set()
        for username, password in (("Alice_01", "wrong-password"), ("nobody_99", "wrong-password")):
            with self.assertRaises(app.HTTPException) as ctx:
                self.login(username=username, password=password, ip="198.51.100.30")
            self.assertEqual(ctx.exception.status_code, 401)
            details.add(ctx.exception.detail)
        self.assertEqual(len(details), 1)
        with self.assertRaises(app.HTTPException) as ctx:
            app.login_account(self.request(ip="198.51.100.30"), {"username": "Alice_01"})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_login_failures_are_rate_limited_per_ip_and_per_user(self):
        self.register()
        window, limit = app.AUTH_RATE_LIMITS["login_fail_ip"][0]
        for index in range(limit):
            with self.assertRaises(app.HTTPException) as ctx:
                self.login(username=f"ghost_{index}", password="wrong-password", ip="198.51.100.40")
            self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(app.HTTPException) as ctx:
            self.login(username="Alice_01", password="correct-horse", ip="198.51.100.40")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers["Retry-After"], str(window))
        # 换 IP 之后按用户名计数仍然生效，且对不存在的用户名同样计数
        _, user_limit = app.AUTH_RATE_LIMITS["login_fail_user"][0]
        for index in range(user_limit):
            with self.assertRaises(app.HTTPException) as ctx:
                self.login(username="Alice_01", password="wrong-password", ip=f"198.51.101.{index}")
            self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(app.HTTPException) as ctx:
            self.login(username="alice_01", password="correct-horse", ip="198.51.101.200")
        self.assertEqual(ctx.exception.status_code, 429)
        for index in range(user_limit):
            with self.assertRaises(app.HTTPException):
                self.login(username="phantom", password="wrong-password", ip=f"198.51.102.{index}")
        with self.assertRaises(app.HTTPException) as ctx:
            self.login(username="phantom", password="wrong-password", ip="198.51.102.200")
        self.assertEqual(ctx.exception.status_code, 429)
        # 其它用户不受影响
        self.register(username="Carol_7", ip="198.51.103.1")
        response, _ = self.login(username="Carol_7", ip="198.51.103.2")
        self.assertEqual(response.status_code, 200)

    def test_register_is_rate_limited_per_ip(self):
        _, limit = app.AUTH_RATE_LIMITS["register_ip"][0]
        for index in range(limit):
            response, _ = self.register(username=f"user_{index}", ip="198.51.100.50")
            self.assertEqual(response.status_code, 201)
        with self.assertRaises(app.HTTPException) as ctx:
            self.register(username="user_extra", ip="198.51.100.50")
        self.assertEqual(ctx.exception.status_code, 429)
        response, _ = self.register(username="user_extra", ip="198.51.100.51")
        self.assertEqual(response.status_code, 201)

    def test_mutations_require_trusted_origin(self):
        payload = {"username": "Alice_01", "password": "correct-horse", "questions": self.QUESTIONS}
        for label, kwargs in (
            ("foreign origin", {"origin": "https://evil.example"}),
            ("null origin", {"origin": "null"}),
            ("foreign referer", {"origin": None, "referer": "https://evil.example/page"}),
            ("host mismatch", {"origin": "https://www.pinhaoke.love", "host": "pinhaoke.love"}),
        ):
            with self.subTest(label):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.register_account(self.request(**kwargs), payload)
                self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(app.ACCOUNTS_DB_PATH.exists())
        same_referer = self.request(origin=None, referer="https://www.pinhaoke.love/?term=fall")
        self.assertEqual(app.register_account(same_referer, payload).status_code, 201)
        no_headers = self.request(origin=None, ip="198.51.100.61")
        self.assertEqual(app.logout_account(no_headers).status_code, 204)

    def test_logout_revokes_session_and_is_idempotent(self):
        _, token = self.register()
        response = app.logout_account(self.request(cookie=token))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.body(app.get_account(self.request(cookie=token))), {"authenticated": False})
        with self.assertRaises(app.HTTPException) as ctx:
            app.list_favorites(self.request(cookie=token))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(app.logout_account(self.request(cookie=token)).status_code, 204)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_login_rotates_token_and_drops_the_previous_session(self):
        _, first = self.register()
        response, second = self.login(cookie=first)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(first, second)
        self.assertEqual(self.body(app.get_account(self.request(cookie=first))), {"authenticated": False})
        self.assertTrue(self.body(app.get_account(self.request(cookie=second)))["authenticated"])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_expired_sessions_are_rejected_and_purged(self):
        _, token = self.register()
        with self.db() as conn:
            conn.execute("UPDATE sessions SET expires_at = 1")
            conn.commit()
        response = app.get_account(self.request(cookie=token))
        self.assertEqual(self.body(response), {"authenticated": False})
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_account_lookup_slides_expiry_once_a_day(self):
        _, token = self.register()
        fresh = app.get_account(self.request(cookie=token))
        self.assertNotIn("set-cookie", fresh.headers)
        with self.db() as conn:
            stale_seen = int(time.time()) - 2 * 86400
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ?",
                (stale_seen, stale_seen + app.SESSION_TTL_SECONDS),
            )
            conn.commit()
        refreshed = app.get_account(self.request(cookie=token))
        refreshed_token, header = _session_cookie(refreshed)
        self.assertEqual(refreshed_token, token)
        self.assertIn("HttpOnly", header)
        with self.db() as conn:
            row = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()
        self.assertGreater(row[0], stale_seen)
        self.assertGreater(row[1], stale_seen + app.SESSION_TTL_SECONDS)

    def test_password_reset_flow_with_security_question(self):
        _, token = self.register()
        with self.assertRaises(app.HTTPException) as ctx:
            app.reset_questions(self.request(ip="198.51.100.70"), {"username": "nobody_99"})
        self.assertEqual(ctx.exception.status_code, 404)
        lookup = app.reset_questions(self.request(ip="198.51.100.70"), {"username": "ALICE_01"})
        self.assertEqual(
            self.body(lookup),
            {"username": "Alice_01", "questions": [{"position": 1, "question": "最喜欢的课？"}]},
        )
        self.assertNotIn(b"answer", lookup.body)

        def attempt(username="alice_01", position=1, answer="wrong answer", ip="198.51.100.71"):
            return app.reset_password(
                self.request(ip=ip),
                {"username": username, "position": position, "answer": answer, "new_password": "brand-new-pass"},
            )

        details = set()
        for kwargs in (
            {},
            {"position": 2},
            {"username": "nobody_99"},
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                attempt(**kwargs)
            self.assertEqual(ctx.exception.status_code, 401)
            details.add(ctx.exception.detail)
        self.assertEqual(len(details), 1)
        for bad in ({"position": 0}, {"position": "1"}, {"position": True}, {"answer": ""}):
            with self.subTest(bad), self.assertRaises(app.HTTPException) as ctx:
                attempt(**bad)
            self.assertEqual(ctx.exception.status_code, 422)

        # 答案不区分大小写、全半角与空白
        response = attempt(answer="ＨＥＬＬＯ world")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.body(app.get_account(self.request(cookie=token))), {"authenticated": False})
        with self.assertRaises(app.HTTPException) as ctx:
            self.login(password="correct-horse", ip="198.51.100.72")
        self.assertEqual(ctx.exception.status_code, 401)
        response, _ = self.login(password="brand-new-pass", ip="198.51.100.72")
        self.assertEqual(response.status_code, 200)

    def test_password_reset_locks_username_after_repeated_failures(self):
        self.register()
        _, limit = app.AUTH_RATE_LIMITS["reset_fail_user"][0]
        payload = {"username": "alice_01", "position": 1, "answer": "wrong", "new_password": "brand-new-pass"}
        for index in range(limit):
            with self.assertRaises(app.HTTPException) as ctx:
                app.reset_password(self.request(ip=f"198.51.104.{index}"), payload)
            self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(app.HTTPException) as ctx:
            app.reset_password(
                self.request(ip="198.51.104.100"), dict(payload, answer="helloworld")
            )
        self.assertEqual(ctx.exception.status_code, 429)
        # 用户名查询限流按 IP 计数
        _, lookup_limit = app.AUTH_RATE_LIMITS["reset_lookup_ip"][0]
        for _ in range(lookup_limit):
            app.reset_questions(self.request(ip="198.51.105.1"), {"username": "alice_01"})
        with self.assertRaises(app.HTTPException) as ctx:
            app.reset_questions(self.request(ip="198.51.105.1"), {"username": "alice_01"})
        self.assertEqual(ctx.exception.status_code, 429)

    def test_change_password_requires_current_and_revokes_other_sessions(self):
        _, phone = self.register()
        _, laptop = self.login(ip="198.51.100.80")
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_password(
                self.request(cookie=laptop),
                {"current_password": "wrong-password", "new_password": "third-password"},
            )
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_password(
                self.request(cookie=laptop),
                {"current_password": "correct-horse", "new_password": "short"},
            )
        self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_password(
                self.request(), {"current_password": "correct-horse", "new_password": "third-password"}
            )
        self.assertEqual(ctx.exception.status_code, 401)
        response = app.change_password(
            self.request(cookie=laptop),
            {"current_password": "correct-horse", "new_password": "third-password"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertTrue(self.body(app.get_account(self.request(cookie=laptop)))["authenticated"])
        self.assertEqual(self.body(app.get_account(self.request(cookie=phone))), {"authenticated": False})
        response, _ = self.login(password="third-password", ip="198.51.100.81")
        self.assertEqual(response.status_code, 200)

    def test_inflight_login_cannot_issue_session_after_password_change(self):
        _, token = self.register()
        request = self.request(cookie=token)
        self.assert_stale_verification_rejected(
            lambda: self.login(),
            lambda: app.change_password(request, {
                "current_password": "correct-horse", "new_password": "updated-password",
            }),
        )
        self.assertEqual(self.login(password="updated-password")[0].status_code, 200)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    def test_inflight_login_cannot_restore_a_session_after_password_reset(self):
        self.register()
        self.assert_stale_verification_rejected(
            lambda: self.login(),
            lambda: app.reset_password(self.request(), {
                "username": "Alice_01", "position": 1, "answer": "hello world",
                "new_password": "reset-password",
            }),
        )
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.login(password="reset-password")[0].status_code, 200)

    def test_sensitive_changes_reject_password_verification_superseded_by_change(self):
        for index, operation in enumerate(("password", "questions", "delete")):
            with self.subTest(operation=operation):
                username = f"race_user_{index}"
                _, token = self.register(username=username, ip=f"198.51.110.{index}")
                request = self.request(cookie=token)
                calls = {
                    "password": lambda: app.change_password(request, {
                        "current_password": "correct-horse", "new_password": "stale-password",
                    }),
                    "questions": lambda: app.change_questions(request, {
                        "current_password": "correct-horse",
                        "questions": [{"question": "stale question", "answer": "stale answer"}],
                    }),
                    "delete": lambda: app.delete_account(request, {"password": "correct-horse"}),
                }
                self.assert_stale_verification_rejected(
                    calls[operation],
                    lambda: app.change_password(request, {
                        "current_password": "correct-horse", "new_password": "updated-password",
                    }),
                )
                self.assertEqual(self.login(username=username, password="updated-password")[0].status_code, 200)
                account = self.body(app.get_account(request))
                self.assertEqual(account["questions"], [{"position": 1, "question": "最喜欢的课？"}])

    def test_sensitive_change_rejects_session_revoked_during_verification(self):
        _, token = self.register()
        request = self.request(cookie=token)
        self.assert_stale_verification_rejected(
            lambda: app.delete_account(request, {"password": "correct-horse"}),
            lambda: app.logout_account(request),
        )
        self.assertEqual(self.login()[0].status_code, 200)

    def test_inflight_reset_rejects_replaced_security_answers(self):
        _, token = self.register()
        self.assert_stale_verification_rejected(
            lambda: app.reset_password(self.request(), {
                "username": "Alice_01", "position": 1, "answer": "hello world",
                "new_password": "stale-password",
            }),
            lambda: app.change_questions(self.request(cookie=token), {
                "current_password": "correct-horse",
                "questions": [{"question": "updated question", "answer": "updated answer"}],
            }),
        )
        self.assertEqual(self.login()[0].status_code, 200)
        self.assertEqual(app.reset_password(self.request(), {
            "username": "Alice_01", "position": 1, "answer": "updated answer",
            "new_password": "updated-password",
        }).status_code, 204)

    def test_change_questions_requires_current_password_and_replaces_all(self):
        _, token = self.register()
        new_questions = [
            {"question": "第一门课", "answer": "高数"},
            {"question": "宿舍楼", "answer": " 45 楼 "},
        ]
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_questions(
                self.request(cookie=token),
                {"current_password": "wrong-password", "questions": new_questions},
            )
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_questions(
                self.request(cookie=token), {"current_password": "correct-horse", "questions": []}
            )
        self.assertEqual(ctx.exception.status_code, 422)
        response = app.change_questions(
            self.request(cookie=token),
            {"current_password": "correct-horse", "questions": new_questions},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.body(app.get_account(self.request(cookie=token)))["questions"],
            [{"position": 1, "question": "第一门课"}, {"position": 2, "question": "宿舍楼"}],
        )
        with self.assertRaises(app.HTTPException):
            app.reset_password(
                self.request(ip="198.51.100.90"),
                {"username": "alice_01", "position": 1, "answer": "helloworld", "new_password": "brand-new-pass"},
            )
        response = app.reset_password(
            self.request(ip="198.51.100.91"),
            {"username": "alice_01", "position": 2, "answer": "45楼", "new_password": "brand-new-pass"},
        )
        self.assertEqual(response.status_code, 204)

    def test_delete_account_cascades_and_clears_cookie(self):
        _, token = self.register()
        with self.assertRaises(app.HTTPException) as ctx:
            app.delete_account(self.request(cookie=token), {"password": "wrong-password"})
        self.assertEqual(ctx.exception.status_code, 401)
        response = app.delete_account(self.request(cookie=token), {"password": "correct-horse"})
        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        with self.db() as conn:
            for table in ("users", "sessions", "security_questions", "favorites", "collections", "favorite_collections"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(self.body(app.get_account(self.request(cookie=token))), {"authenticated": False})
        response, _ = self.register(ip="198.51.100.95")
        self.assertEqual(response.status_code, 201)

    def test_secret_hash_helpers(self):
        self.assertTrue(hasattr(hashlib, "scrypt"))
        self.assertEqual(self.production_scrypt, {"n": 2 ** 14, "r": 8, "p": 1})
        stored = app._hash_secret("correct-horse")
        self.assertTrue(stored.startswith("scrypt$16$8$1$"))
        self.assertTrue(app._verify_secret("correct-horse", stored))
        self.assertFalse(app._verify_secret("Correct-horse", stored))
        self.assertFalse(app._verify_secret("correct-horse", stored[:-3] + "xyz"))
        self.assertFalse(app._verify_secret("correct-horse", "plaintext"))
        self.assertFalse(app._verify_secret("correct-horse", "scrypt$16$8$1$!!$!!"))
        self.assertFalse(app._verify_secret(None, stored))
        self.assertFalse(app._secret_needs_rehash(stored))
        with patch.object(app, "SCRYPT_PARAMS", {"n": 8, "r": 8, "p": 1}):
            legacy = app._hash_secret("correct-horse")
        self.assertTrue(app._verify_secret("correct-horse", legacy))
        self.assertTrue(app._secret_needs_rehash(legacy))
        self.assertTrue(app._secret_needs_rehash("garbage"))
        self.assertEqual(app._normalize_answer("  Hello　World "), "helloworld")
        self.assertEqual(app._normalize_answer("ＨＥＬＬＯ"), "hello")
        self.assertEqual(app._normalize_answer("四十五 楼"), "四十五楼")

    def test_login_upgrades_legacy_password_hashes(self):
        with patch.object(app, "SCRYPT_PARAMS", {"n": 8, "r": 8, "p": 1}):
            self.register()
        with self.db() as conn:
            self.assertTrue(conn.execute("SELECT password_hash FROM users").fetchone()[0].startswith("scrypt$8$"))
        response, _ = self.login(ip="198.51.100.99")
        self.assertEqual(response.status_code, 200)
        with self.db() as conn:
            self.assertTrue(conn.execute("SELECT password_hash FROM users").fetchone()[0].startswith("scrypt$16$"))

    def test_accounts_db_is_separate_and_versioned(self):
        self.register()
        self.assertNotEqual(app.ACCOUNTS_DB_PATH.parent, app.DB_DIR)
        with self.db() as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            user_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(users)")
            }
            self.assertIn("last_collection_id", user_cols)
        self.assertTrue(
            {"users", "security_questions", "sessions", "favorites", "auth_events",
             "collections", "favorite_collections"} <= tables
        )
        self.assertNotIn("basic_info", tables)
        self.assertNotIn("messages", tables)

    def test_migrates_v2_database_preserving_data(self):
        # 手工构造一个 v2 账户库（含用户、默认夹、一条收藏与映射），模拟生产升级
        now = 1700000000
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            conn.executescript(app._ACCOUNTS_SCHEMA_V1)
            for statement in app._ACCOUNTS_SCHEMA_V2:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO users (id, username, username_key, password_hash,"
                " created_at, password_changed_at) VALUES (1, 'Old_01', 'old_01',"
                " 'scrypt$16384$8$1$aa$bb', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO collections (id, user_id, name, is_default, position, created_at)"
                " VALUES (7, 1, ?, 1, 0, ?)",
                (app.DEFAULT_COLLECTION_NAME, now),
            )
            conn.execute(
                "INSERT INTO favorites (user_id, fav_key, course_id, term, term_label, level,"
                " course_code, class_no, teacher, course_name, credits, schedule, department,"
                " added_at) VALUES (1, 'fall|ug|001|1|师', 'a1', 'fall', '2026秋季学期', 'ug',"
                " '001', '1', '师', '课', 2.0, 'x', 'y', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO favorite_collections (user_id, fav_key, collection_id, added_at)"
                " VALUES (1, 'fall|ug|001|1|师', 7, ?)",
                (now,),
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        # 打开一次即触发迁移
        with app.get_accounts_db() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT nickname FROM users WHERE id=1").fetchone()[0], "路过的 PKUer")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            self.assertIn("last_collection_id", cols)
            self.assertEqual(
                conn.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "Old_01"
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM favorite_collections").fetchone()[0], 1
            )
            self.assertIsNone(
                conn.execute("SELECT last_collection_id FROM users WHERE id=1").fetchone()[0]
            )


class FavoritesApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, app, "ACCOUNTS_DB_PATH", app.ACCOUNTS_DB_PATH)
        app.ACCOUNTS_DB_PATH = Path(self._tmp.name) / "账户.db"
        patcher = patch.object(app, "SCRYPT_PARAMS", {"n": 16, "r": 8, "p": 1})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, app, "_DUMMY_SECRET_HASH", None)
        app._DUMMY_SECRET_HASH = None
        self.course_db = Path(self._tmp.name) / "2026秋季学期本科生课程.db"
        _build_course_db(self.course_db, FIXTURE_COURSES)
        term_patcher = patch.dict(app.TERM_DBS, {"fall": [("main", self.course_db, "a")]}, clear=True)
        term_patcher.start()
        self.addCleanup(term_patcher.stop)
        self.token = self.register("Alice_01", "203.0.113.9")

    request = staticmethod(_account_request)

    def register(self, username, ip):
        response = app.register_account(
            self.request(ip=ip),
            {
                "username": username,
                "password": "correct-horse",
                "questions": [{"question": "q", "answer": "abc"}],
            },
        )
        token, _ = _session_cookie(response)
        return token

    @staticmethod
    def body(response):
        return json.loads(response.body)

    def add(self, course_id, token=None, ip="203.0.113.9"):
        return app.add_favorite(self.request(ip=ip, cookie=token or self.token), {"id": course_id})

    def favorites(self, token=None):
        return self.body(app.list_favorites(self.request(cookie=token or self.token)))["favorites"]

    def test_saved_courses_use_homepage_representative_badges_and_coherent_fallback(self):
        richer = {
            **FIXTURE_COURSES[0], "id": 3, "course_name": "完整课程名",
            "schedule": "1~16周 每周周二5~6节",
        }
        _build_course_db(self.course_db, [*FIXTURE_COURSES, richer])
        with closing(sqlite3.connect(self.course_db)) as conn:
            conn.execute("UPDATE basic_info SET classroom='理教 101' WHERE id=1")
            conn.execute("UPDATE basic_info SET course_type='通识课',category='任选' WHERE id=3")
            conn.execute("UPDATE detail_info SET intro_cn='完整介绍',syllabus='完整大纲',textbook='教材' WHERE course_id=3")
            conn.commit()
        with patch.dict(app.TERM_UNION_SQL, {"fall": app.LIST_SELECT_FALL_UG}):
            expected = CourseListTests().call(q=FIXTURE_COURSES[0]["course_code"])["courses"][0]
        self.assertEqual(expected["id"], "a3")
        self.assertEqual(expected["classroom"], "理教 101")
        for save in (
            lambda: self.body(self.add("a3"))["favorites"][0],
            lambda: self.body(app.add_timetable_course(self.request(cookie=self.token), {"id": "a3"}))["courses"][0],
        ):
            item = save()
            for field in ("id", "course_name", "teacher", "credits", "schedule", "classroom", "department", "course_type", "category"):
                self.assertEqual(item[field], expected[field], field)
            self.assertEqual(item["fav_key"], app._favorite_snapshot("a3")["fav_key"])

    def test_saved_course_without_teacher_keeps_its_independent_source_row(self):
        other = {**FIXTURE_COURSES[1], "id": 4, "teacher": None, "course_name": "另一门未公布教师的课程"}
        _build_course_db(self.course_db, [*FIXTURE_COURSES, other])
        request = self.request(cookie=self.token)
        favorite = self.body(self.add("a4"))["favorites"][0]
        app.add_timetable_course(request, {"id": "a4"})
        timetable = self.body(app.get_timetable(request))["courses"][0]
        for saved in (favorite, timetable):
            self.assertTrue(saved["available"])
            self.assertEqual(saved["id"], "a4")
            self.assertEqual(saved["course_name"], other["course_name"])

    def test_favorites_require_login(self):
        for call in (
            lambda: app.list_favorites(self.request()),
            lambda: app.add_favorite(self.request(), {"id": "a1"}),
            lambda: app.remove_favorite(self.request(), {"fav_key": "fall|ug|04831180|1|张三(教授)"}),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 401)

    def test_add_builds_snapshot_on_the_server(self):
        response = app.add_favorite(
            self.request(cookie=self.token),
            {"id": "a1", "course_name": "客户端伪造", "teacher": "伪造", "fav_key": "x"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = self.body(response)
        self.assertEqual(set(payload), {"favorites", "limit", "collections", "collections_limit"})
        self.assertEqual(payload["limit"], app.FAVORITES_MAX)
        self.assertEqual(payload["collections_limit"], app.COLLECTIONS_MAX)
        item = payload["favorites"][0]
        self.assertEqual(
            set(item),
            {
                "fav_key", "id", "available", "term", "term_label", "level", "course_code",
                "class_no", "teacher", "course_name", "credits", "schedule", "department",
                "added_at", "collection_ids", "classroom", "course_type", "category",
            },
        )
        self.assertEqual(item["collection_ids"], [payload["collections"][0]["id"]])
        self.assertTrue(payload["collections"][0]["is_default"])
        self.assertEqual(item["fav_key"], "fall|ug|04831180|1|张三(教授)")
        self.assertEqual(item["id"], "a1")
        self.assertTrue(item["available"])
        self.assertEqual(item["term"], "fall")
        self.assertEqual(item["term_label"], "2026秋季学期")
        self.assertEqual(item["level"], "ug")
        self.assertEqual(item["course_name"], "PSoC应用开发基础实验")
        self.assertEqual(item["teacher"], "张三(教授)")
        self.assertEqual(item["credits"], 2.0)
        self.assertEqual(item["schedule"], "1~16周 每周周三3~4节")
        self.assertEqual(item["department"], "信息科学技术学院")
        self.assertNotIn(b"user_id", response.body)
        # 空教师写成空段，客户端只需按同一规则拼接
        empty_teacher = self.body(self.add("a2"))["favorites"][0]
        self.assertEqual(empty_teacher["fav_key"], "fall|ug|00131520|2|")
        self.assertEqual(app._favorite_key("fall", "ug", "00131520", 2, None), "fall|ug|00131520|2|")

    def test_invalid_and_missing_course_ids(self):
        for bad in ("x1", "a0", "a01", 12, None, "a1 "):
            with self.subTest(bad), self.assertRaises(app.HTTPException) as ctx:
                app.add_favorite(self.request(cookie=self.token), {"id": bad})
            self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(app.HTTPException) as ctx:
            self.add("a99")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.favorites(), [])

    def test_duplicate_add_keeps_added_at_and_returns_200(self):
        first = self.body(self.add("a1"))["favorites"][0]
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            conn.execute("UPDATE favorites SET added_at = 1700000000, course_name = '旧名'")
            conn.commit()
        again = self.add("a1")
        self.assertEqual(again.status_code, 200)
        items = self.body(again)["favorites"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["added_at"], 1700000000)
        self.assertEqual(items[0]["course_name"], first["course_name"])

    def test_limit_is_enforced_without_touching_existing_rows(self):
        with patch.object(app, "FAVORITES_MAX", 1):
            self.assertEqual(self.add("a1").status_code, 201)
            with self.assertRaises(app.HTTPException) as ctx:
                self.add("a2")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(self.add("a1").status_code, 200)
            self.assertEqual([item["id"] for item in self.favorites()], ["a1"])
            self.assertEqual(self.body(app.list_favorites(self.request(cookie=self.token)))["limit"], 1)

    def test_users_only_see_their_own_favorites(self):
        other = self.register("Bob_02", "198.51.100.2")
        self.add("a1")
        self.add("a2", token=other, ip="198.51.100.2")
        self.assertEqual([item["id"] for item in self.favorites()], ["a1"])
        self.assertEqual([item["id"] for item in self.favorites(other)], ["a2"])
        account = self.body(app.get_account(self.request(cookie=other)))
        self.assertEqual([item["id"] for item in account["favorites"]], ["a2"])

    def test_remove_is_idempotent(self):
        key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        self.add("a2")
        response = app.remove_favorite(self.request(cookie=self.token), {"fav_key": key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in self.body(response)["favorites"]], ["a2"])
        response = app.remove_favorite(self.request(cookie=self.token), {"fav_key": key})
        self.assertEqual([item["id"] for item in self.body(response)["favorites"]], ["a2"])
        with self.assertRaises(app.HTTPException) as ctx:
            app.remove_favorite(self.request(cookie=self.token), {"fav_key": ""})
        self.assertEqual(ctx.exception.status_code, 422)

    def test_list_is_newest_first(self):
        self.add("a1")
        self.add("a2")
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            conn.execute("UPDATE favorites SET added_at = 1700000000 WHERE course_id = 'a1'")
            conn.execute("UPDATE favorites SET added_at = 1800000000 WHERE course_id = 'a2'")
            conn.commit()
        self.assertEqual([item["id"] for item in self.favorites()], ["a2", "a1"])

    def test_refresh_rewrites_drifted_ids_after_rebuild(self):
        self.add("a1")
        rebuilt = [
            dict(FIXTURE_COURSES[0], id=7, teacher=" 张三(教授) "),
            dict(FIXTURE_COURSES[1], id=8),
        ]
        _build_course_db(self.course_db, rebuilt)
        items = self.favorites()
        self.assertEqual(items[0]["id"], "a7")
        self.assertTrue(items[0]["available"])
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT course_id FROM favorites").fetchone()[0], "a7")
        detail = app.get_course_detail("a7", "zh")
        self.assertEqual(detail["course_name"], "PSoC应用开发基础实验")

    def test_refresh_marks_missing_courses_unavailable(self):
        self.add("a1")
        _build_course_db(self.course_db, [FIXTURE_COURSES[1]])
        items = self.favorites()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "a1")
        self.assertFalse(items[0]["available"])
        self.assertEqual(items[0]["course_name"], "PSoC应用开发基础实验")

    def test_refresh_marks_next_year_term_unavailable(self):
        self.add("a1")
        next_year = Path(self._tmp.name) / "2027秋季学期本科生课程.db"
        _build_course_db(next_year, FIXTURE_COURSES)
        with patch.dict(app.TERM_DBS, {"fall": [("main", next_year, "a")]}, clear=True):
            self.assertEqual(app._term_label("fall"), "2027秋季学期")
            items = self.favorites()
        self.assertFalse(items[0]["available"])
        self.assertEqual(items[0]["term_label"], "2026秋季学期")
        self.assertEqual(items[0]["id"], "a1")
        self.assertEqual(app._term_label("fall"), "2026秋季学期")
        self.assertEqual(app._term_label("winter"), "winter")

    def test_favorite_writes_are_rate_limited_per_ip(self):
        limits = dict(app.AUTH_RATE_LIMITS)
        limits["favorite_write_ip"] = ((3600, 2),)
        with patch.object(app, "AUTH_RATE_LIMITS", limits):
            self.add("a1")
            self.add("a2")
            with self.assertRaises(app.HTTPException) as ctx:
                self.add("a1")
            self.assertEqual(ctx.exception.status_code, 429)
            other = self.register("Bob_02", "198.51.100.2")
            self.assertEqual(self.add("a1", token=other, ip="198.51.100.2").status_code, 201)
        self.assertEqual(len(self.favorites()), 2)

    # ---- 收藏夹（collections）----
    def account(self, token=None):
        return self.body(app.get_account(self.request(cookie=token or self.token)))

    def collections(self, token=None):
        return self.account(token)["collections"]

    def default_collection_id(self, token=None):
        return next(c["id"] for c in self.collections(token) if c["is_default"])

    def create_coll(self, name, token=None, ip="203.0.113.9"):
        return app.create_collection(
            self.request(ip=ip, cookie=token or self.token), {"name": name}
        )

    def set_colls(self, fav_key, ids, token=None, ip="203.0.113.9"):
        return app.set_favorite_collections(
            self.request(ip=ip, cookie=token or self.token),
            {"fav_key": fav_key, "collection_ids": ids},
        )

    def test_register_creates_single_default_collection(self):
        cols = self.collections()
        self.assertEqual(len(cols), 1)
        self.assertTrue(cols[0]["is_default"])
        self.assertEqual(cols[0]["name"], app.DEFAULT_COLLECTION_NAME)
        self.assertEqual(cols[0]["count"], 0)

    def test_add_maps_course_into_default_collection(self):
        payload = self.body(self.add("a1"))
        default_id = payload["collections"][0]["id"]
        self.assertTrue(payload["collections"][0]["is_default"])
        self.assertEqual(payload["favorites"][0]["collection_ids"], [default_id])
        self.assertEqual(payload["collections"][0]["count"], 1)

    def test_create_collection_rename_and_conflicts(self):
        response = self.create_coll("课程表A")
        self.assertEqual(response.status_code, 201)
        cols = self.body(response)["collections"]
        self.assertEqual(len(cols), 2)
        custom = next(c for c in cols if not c["is_default"])
        self.assertEqual(custom["position"], 1)
        # 撞名 409（与默认夹重名）
        with self.assertRaises(app.HTTPException) as ctx:
            self.create_coll(app.DEFAULT_COLLECTION_NAME)
        self.assertEqual(ctx.exception.status_code, 409)
        # 改名成功
        renamed = app.rename_collection(
            self.request(cookie=self.token), {"collection_id": custom["id"], "name": "课程表B"}
        )
        self.assertEqual(renamed.status_code, 200)
        names = {c["name"] for c in self.body(renamed)["collections"]}
        self.assertIn("课程表B", names)
        # 默认夹也可改名
        default_id = self.default_collection_id()
        ok = app.rename_collection(
            self.request(cookie=self.token), {"collection_id": default_id, "name": "我的默认"}
        )
        self.assertEqual(ok.status_code, 200)
        # 改名撞名 409
        with self.assertRaises(app.HTTPException) as ctx:
            app.rename_collection(
                self.request(cookie=self.token), {"collection_id": custom["id"], "name": "我的默认"}
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_collection_name_validation(self):
        for bad in ("", "   ", "x" * (app.COLLECTION_NAME_MAX + 1), "夹\ud800", 5, None, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(app.HTTPException) as ctx:
                self.create_coll(bad)
            self.assertEqual(ctx.exception.status_code, 422)

    def test_create_collection_respects_limit(self):
        with patch.object(app, "COLLECTIONS_MAX", 2):
            self.assertEqual(self.create_coll("A").status_code, 201)
            self.assertEqual(self.create_coll("B").status_code, 201)
            with self.assertRaises(app.HTTPException) as ctx:
                self.create_coll("C")
            self.assertEqual(ctx.exception.status_code, 409)

    def test_set_collections_moves_between_folders(self):
        fav_key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        payload = self.body(self.create_coll("课程表A"))
        custom = next(c for c in payload["collections"] if not c["is_default"])
        default_id = next(c["id"] for c in payload["collections"] if c["is_default"])
        # 移出默认，只留自定义，仍是收藏
        moved = self.body(self.set_colls(fav_key, [custom["id"]]))
        self.assertEqual(moved["favorites"][0]["collection_ids"], [custom["id"]])
        counts = {c["id"]: c["count"] for c in moved["collections"]}
        self.assertEqual(counts[default_id], 0)
        self.assertEqual(counts[custom["id"]], 1)
        # 进多个夹仍只占一门课
        both = self.body(self.set_colls(fav_key, [custom["id"], default_id]))
        self.assertEqual(len(both["favorites"]), 1)
        self.assertEqual(sorted(both["favorites"][0]["collection_ids"]), sorted([custom["id"], default_id]))
        # 取消勾选全部即取消收藏
        emptied = self.body(self.set_colls(fav_key, []))
        self.assertEqual(emptied["favorites"], [])

    def test_set_collections_rejects_bad_targets(self):
        fav_key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        with self.assertRaises(app.HTTPException) as ctx:
            self.set_colls("fall|ug|nope|1|x", [self.default_collection_id()])
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(app.HTTPException) as ctx:
            self.set_colls(fav_key, [999999])
        self.assertEqual(ctx.exception.status_code, 404)
        for bad in ("x", [True], [1.5], [[1]]):
            with self.subTest(bad=bad), self.assertRaises(app.HTTPException) as ctx:
                self.set_colls(fav_key, bad)
            self.assertEqual(ctx.exception.status_code, 422)

    def test_collection_ids_outside_sqlite_range_return_validation_errors(self):
        fav_key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        request = self.request(cookie=self.token)
        for invalid_id in (0, -1, 2**63, -(2**63) - 1, 10**100, True):
            for operation in (
                lambda: app.rename_collection(request, {"collection_id": invalid_id, "name": "课程夹"}),
                lambda: app.remove_collection(request, {"collection_id": invalid_id}),
                lambda: self.set_colls(fav_key, [invalid_id]),
            ):
                with self.subTest(invalid_id=invalid_id), self.assertRaises(app.HTTPException) as ctx:
                    operation()
                self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(len(self.favorites()), 1)

    def test_saved_course_keys_reject_unencodable_text(self):
        request = self.request(cookie=self.token)
        for operation in (
            lambda: app.remove_favorite(request, {"fav_key": "course\ud800"}),
            lambda: self.set_colls("course\ud800", []),
            lambda: app.remove_timetable_course(request, {"course_key": "course\ud800"}),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                operation()
            self.assertEqual(ctx.exception.status_code, 422)

    def test_collection_cannot_be_deleted_between_ownership_check_and_assignment(self):
        favorite = self.body(self.add("a1"))["favorites"][0]
        created = self.body(self.create_coll("并发修改"))
        collection_id = next(c["id"] for c in created["collections"] if not c["is_default"])
        owned_collection_ids = app._owned_collection_ids

        def delete_after_ownership_check(conn, user_id, raw):
            result = owned_collection_ids(conn, user_id, raw)
            with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH, timeout=0)) as other:
                other.execute("PRAGMA foreign_keys=ON")
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("DELETE FROM collections WHERE id=?", (collection_id,))
            return result

        with patch.object(app, "_owned_collection_ids", side_effect=delete_after_ownership_check):
            result = self.body(self.set_colls(favorite["fav_key"], [collection_id]))
        self.assertEqual(result["favorites"][0]["collection_ids"], [collection_id])
        removed = app.remove_collection(self.request(cookie=self.token), {"collection_id": collection_id})
        self.assertEqual(self.body(removed)["favorites"], [])

    def test_remove_custom_collection_cleans_orphans(self):
        fav_key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        payload = self.body(self.create_coll("仅此夹"))
        only = next(c for c in payload["collections"] if not c["is_default"])
        self.set_colls(fav_key, [only["id"]])
        # a2 留在默认，同时也进该自定义夹
        fav2 = self.body(self.add("a2"))["favorites"]
        key2 = next(f["fav_key"] for f in fav2 if f["id"] == "a2")
        default_id = self.default_collection_id()
        self.set_colls(key2, [only["id"], default_id])
        result = self.body(app.remove_collection(self.request(cookie=self.token), {"collection_id": only["id"]}))
        ids = {f["id"] for f in result["favorites"]}
        self.assertNotIn("a1", ids)   # 仅在该夹 -> 取消收藏
        self.assertIn("a2", ids)      # 还在默认 -> 保留
        self.assertEqual([c for c in result["collections"] if not c["is_default"]], [])

    def test_cannot_remove_default_collection(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.remove_collection(self.request(cookie=self.token), {"collection_id": self.default_collection_id()})
        self.assertEqual(ctx.exception.status_code, 409)

    def test_collections_are_isolated_between_users(self):
        alice_default = self.default_collection_id()
        alice_key = self.body(self.add("a1"))["favorites"][0]["fav_key"]
        bob = self.register("Bob_02", "198.51.100.2")
        for call in (
            lambda: app.rename_collection(self.request(cookie=bob, ip="198.51.100.2"), {"collection_id": alice_default, "name": "x"}),
            lambda: app.remove_collection(self.request(cookie=bob, ip="198.51.100.2"), {"collection_id": alice_default}),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)
        # Bob 无该收藏，且引用 Alice 的夹 id 应被拒
        with self.assertRaises(app.HTTPException) as ctx:
            self.set_colls(alice_key, [alice_default], token=bob, ip="198.51.100.2")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_collection_writes_are_rate_limited_per_ip(self):
        limits = dict(app.AUTH_RATE_LIMITS)
        limits["favorite_write_ip"] = ((3600, 2),)
        with patch.object(app, "AUTH_RATE_LIMITS", limits):
            self.assertEqual(self.create_coll("A").status_code, 201)
            self.assertEqual(self.create_coll("B").status_code, 201)
            with self.assertRaises(app.HTTPException) as ctx:
                self.create_coll("C")
            self.assertEqual(ctx.exception.status_code, 429)

    def test_collection_responses_are_no_store(self):
        for response in (self.create_coll("A"), self.add("a1")):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            for forbidden in (b"user_id", b"ip_hash", b"password_hash"):
                self.assertNotIn(forbidden, response.body)


class ReviewApiTests(unittest.TestCase):
    def call(self, **overrides):
        args = {"q": "", "page": 1, "page_size": 20}
        args.update(overrides)
        return app.list_reviews(**args)

    def test_reviews_database_is_query_only(self):
        with app.get_reviews_db() as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden_write(id INTEGER)")

    def test_review_metadata_matches_full_extraction(self):
        metadata = app.get_review_meta()
        self.assertEqual(metadata["snapshot_date"], "2026-07-13")
        self.assertEqual(metadata["start_date"], "2022-12-21")
        self.assertEqual(metadata["end_date"], "2026-07-13")
        self.assertEqual(metadata["highlight_version"], "3")
        self.assertEqual(metadata["matched_threads"], 47843)
        self.assertEqual(metadata["matched_entries"], 90880)
        self.assertEqual(metadata["matched_replies"], 43037)
        self.assertEqual(metadata["snapshot_replies"], 210570)
        self.assertEqual(
            metadata["matched_threads"] + metadata["matched_replies"],
            metadata["matched_entries"],
        )
        self.assertEqual(metadata["source_shards"], 44)
        self.assertEqual(metadata["cached_reply_coverage_percent"], 95.24)
        self.assertEqual(metadata["highlighted_entries"], 59773)
        self.assertEqual(metadata["course_highlights"], 135241)
        self.assertEqual(metadata["teacher_highlights"], 53518)
        self.assertEqual(metadata["course_aliases"], 802)
        self.assertEqual(metadata["teacher_aliases"], 1062)
        self.assertEqual(metadata["course_alias_highlights"], 56168)
        self.assertEqual(metadata["teacher_alias_highlights"], 27962)

    def test_course_name_search_returns_grouped_threads_and_entries(self):
        result = self.call(q="博弈论", page_size=10)
        self.assertGreater(result["total"], 0)
        self.assertEqual(result["page"], 1)
        self.assertLessEqual(len(result["threads"]), 10)
        for thread in result["threads"]:
            searchable = "\n".join(
                [thread["content"], *thread["courses"]]
                + [entry["content"] for entry in thread["entries"]]
            )
            self.assertIn("博弈论", searchable)
            self.assertEqual(thread["entries"][0]["kind"], "post")
            for entry in thread["entries"]:
                self.assertIsInstance(entry["highlights"], list)
                for highlight in entry["highlights"]:
                    self.assertIn(highlight["entity_type"], {"course", "teacher"})
                    self.assertIn(highlight["match_kind"], {"full", "alias"})
                    self.assertGreater(highlight["end_offset"], highlight["start_offset"])
            self.assertNotIn("authorTag", repr(thread))
            self.assertNotIn("replyTo", repr(thread))

    def test_review_api_returns_course_and_teacher_highlights(self):
        with app.get_reviews_db() as conn:
            row = conn.execute(
                """
                SELECT t.*
                FROM threads t
                WHERE EXISTS (
                    SELECT 1
                    FROM entries e
                    JOIN entry_highlights h ON h.entry_key=e.entry_key
                    WHERE e.pid=t.pid AND h.entity_type='teacher'
                )
                  AND EXISTS (
                    SELECT 1
                    FROM entries e
                    JOIN entry_highlights h ON h.entry_key=e.entry_key
                    WHERE e.pid=t.pid AND h.entity_type='course'
                )
                ORDER BY t.pid
                LIMIT 1
                """
            ).fetchone()
            thread = app._load_review_threads(conn, [row])[0]

        highlights = [
            highlight
            for entry in thread["entries"]
            for highlight in entry["highlights"]
        ]
        self.assertIn("course", {item["entity_type"] for item in highlights})
        self.assertIn("teacher", {item["entity_type"] for item in highlights})
        self.assertNotIn("entry_key", repr(thread))

    def test_review_thread_detail_returns_all_snapshot_replies_without_identity_fields(self):
        with app.get_reviews_db() as conn:
            pid, expected_count = conn.execute(
                "SELECT pid, COUNT(*) AS reply_count FROM thread_replies "
                "GROUP BY pid ORDER BY reply_count DESC, pid LIMIT 1"
            ).fetchone()

        thread = app.get_review_thread(pid)
        self.assertEqual(thread["pid"], pid)
        self.assertEqual(thread["reply_count"], expected_count)
        self.assertEqual(len(thread["replies"]), expected_count)
        self.assertEqual(
            set(thread),
            {
                "pid", "source_month", "posted_at", "content", "source_url",
                "post_kind", "reply_count", "replies",
            },
        )
        for reply in thread["replies"]:
            self.assertEqual(set(reply), {"cid", "floor", "posted_at", "content"})
        serialized = repr(thread)
        for private_field in ("authorTag", "authorLabel", "replyTo", "entry_key"):
            self.assertNotIn(private_field, serialized)

    def test_review_thread_detail_validates_id_and_returns_404(self):
        for pid in (0, -1, True, "1"):
            with self.subTest(pid=pid):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.get_review_thread(pid)
                self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(app.HTTPException) as ctx:
            app.get_review_thread(9_999_999_999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_review_pagination_is_stable_and_non_overlapping(self):
        first = self.call(page=1, page_size=17)
        second = self.call(page=2, page_size=17)
        first_ids = [thread["pid"] for thread in first["threads"]]
        second_ids = [thread["pid"] for thread in second["threads"]]
        self.assertEqual(len(first_ids), 17)
        self.assertEqual(len(second_ids), 17)
        self.assertFalse(set(first_ids) & set(second_ids))
        self.assertEqual(first_ids, [thread["pid"] for thread in self.call(page_size=17)["threads"]])

    def test_review_default_order_features_top_2026_threads_first(self):
        threads = self.call(page_size=30)["threads"]
        featured = threads[:app.REVIEW_FEATURED_COUNT]
        self.assertEqual(len(featured), app.REVIEW_FEATURED_COUNT)
        start, end = app.REVIEW_FEATURED_RANGE
        for thread in featured:
            self.assertTrue(start <= thread["posted_at"] < end)
            self.assertGreaterEqual(thread["relevant_reply_count"], 20)
        self.assertEqual(featured[0]["pid"], 7949454)
        # 置顶之后的其余列表回到时间倒序
        rest_times = [t["posted_at"] for t in threads[app.REVIEW_FEATURED_COUNT:]]
        self.assertEqual(rest_times, sorted(rest_times, reverse=True))
        # 搜索结果不受置顶影响，仍按时间倒序
        searched = self.call(q="军理", page_size=20)["threads"]
        searched_times = [t["posted_at"] for t in searched]
        self.assertEqual(searched_times, sorted(searched_times, reverse=True))

    def test_review_course_suggestions_are_ranked_and_searchable(self):
        suggestions = app.list_review_courses(q="博弈", limit=10)
        self.assertTrue(suggestions)
        self.assertTrue(any("博弈" in item["course_name"] for item in suggestions))
        self.assertTrue(all(item["thread_count"] > 0 for item in suggestions))

    def test_review_query_validation_rejects_unsafe_shapes(self):
        invalid = (
            {"q": 1},
            {"q": "x" * 121},
            {"page": 0},
            {"page": 10001},
            {"page_size": 0},
            {"page_size": 101},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(app.HTTPException) as ctx:
                    self.call(**values)
                self.assertEqual(ctx.exception.status_code, 422)

        invalid_suggestions = (
            {"q": 1, "limit": 10},
            {"q": "x" * 121, "limit": 10},
            {"q": "", "limit": 0},
        )
        for values in invalid_suggestions:
            with self.subTest(values=values):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.list_review_courses(**values)
                self.assertEqual(ctx.exception.status_code, 422)

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(app._escape_like(r"50%_done\\"), r"50\%\_done\\\\")


class CourseListTests(unittest.TestCase):
    def call(self, **overrides):
        args = dict(
            q="",
            type="",
            category="",
            credits="",
            department="",
            weekday="",
            period="",
            grading="",
            classroom="",
            sort="",
            random_seed=0,
            lang="zh",
            term="fall",
            page=1,
            page_size=200,
        )
        args.update(overrides)
        return app.list_courses(**args)

    def all_courses(self, **overrides):
        first = self.call(**overrides)
        courses = list(first["courses"])
        pages = (first["total"] + first["page_size"] - 1) // first["page_size"]
        for page in range(2, pages + 1):
            courses.extend(self.call(page=page, **overrides)["courses"])
        self.assertEqual(len(courses), first["total"])
        return courses

    def test_all_terms_and_levels_ignore_translation_requests(self):
        for term in app.VALID_TERMS:
            chinese = self.call(term=term, page_size=10)
            with app.get_db(term) as conn:
                ids = [f"{prefix}1" for _, _, prefix in app.TERM_DBS[term]]
            for lang in sorted(app.VALID_LANGS - {"zh"}):
                with self.subTest(term=term, lang=lang):
                    self.assertEqual(self.call(term=term, lang=lang, page_size=10), chinese)
                    for course_id in ids:
                        self.assertEqual(app.get_course_detail(course_id, lang), app.get_course_detail(course_id, "zh"))

    def test_fall_refresh_keeps_old_courses_and_applies_reclassification(self):
        retained = self.call(term="fall", q="00131421", type="专业课")
        self.assertEqual(len([c for c in retained["courses"] if c["course_code"] == "00131421"]), 8)
        moved = self.call(term="fall", q="00100514")
        card = next(c for c in moved["courses"] if c["id"].startswith("a"))
        self.assertEqual(card["course_type"], ["公选课"])
        renamed = self.call(term="fall", q="02432477")
        self.assertEqual(renamed["courses"][0]["course_name"], "国家安全理论与实践")

    def all_ids(self, term, sort="", random_seed=0, lang="zh", q="", page_size=200):
        courses = self.all_courses(
            term=term,
            sort=sort,
            random_seed=random_seed,
            lang=lang,
            q=q,
            page_size=page_size,
        )
        return len(courses), [course["id"] for course in courses]

    def find_card(self, courses, course_code, class_no, teacher):
        return next(
            course
            for course in courses
            if course["course_code"] == course_code
            and course["class_no"] == class_no
            and course["teacher"] == teacher
        )

    def filters_payload(self, term):
        return json.loads(app.get_filters(term).body)

    def test_period_options_list_two_period_ranges_before_the_rest(self):
        schedules = [
            "1~16周 每周周三7~8节",
            "1~16周 每周周一1~4节\n1~16周 双周周四10~11节(习题或上机)",
            "1~8周 每周周五7~7节",
            "1~16周 每周周二1~2节",
            None,
            "",
            "1~16周 每周周日3~4节",
        ]
        self.assertEqual(
            app._period_options(schedules),
            ["1-2", "3-4", "7-8", "10-11", "1-4", "7-7"],
        )

    def test_filters_expose_period_ranges_two_period_first_and_each_matches_a_course(self):
        for term in ("fall", "spring", "summer"):
            with self.subTest(term=term):
                periods = self.filters_payload(term)["periods"]
                self.assertTrue(periods)
                self.assertTrue(all(app.PERIOD_RANGE_RE.match(period) for period in periods))
                bounds = [tuple(int(part) for part in period.split("-")) for period in periods]
                self.assertEqual(len(bounds), len(set(bounds)))
                self.assertEqual(bounds, sorted(bounds, key=app._period_option_sort_key))
                two_period = [pair for pair in bounds if pair[1] - pair[0] == 1]
                self.assertTrue(two_period)
                self.assertEqual(bounds[: len(two_period)], two_period)
                self.assertEqual(periods[0], "1-2")
                for period in periods:
                    total = self.call(term=term, period=period, page_size=1)["total"]
                    self.assertGreater(total, 0, period)

    def test_every_scheduled_card_range_is_an_offered_period_option(self):
        for term in ("fall", "summer"):
            with self.subTest(term=term):
                offered = set(self.filters_payload(term)["periods"])
                seen = set()
                for course in self.all_courses(term=term):
                    for start, end in app.SCHEDULE_PERIOD_RE.findall(course["schedule"] or ""):
                        seen.add(f"{int(start)}-{int(end)}")
                self.assertTrue(seen)
                self.assertLessEqual(seen, offered)

    def test_period_filter_matches_the_same_session_as_weekday(self):
        alone = self.all_courses(term="fall", period="10-12")
        self.assertTrue(alone)
        for course in alone:
            self.assertRegex(course["schedule"], r"周[一二三四五六日]10~12节")
        coupled = self.all_courses(term="fall", weekday="周二", period="10-12")
        self.assertTrue(coupled)
        coupled_ids = set()
        for course in coupled:
            self.assertIn("周二10~12节", course["schedule"])
            coupled_ids.add(course["id"])
        # Completeness: every Tuesday course with a 周二10~12节 slot is in the coupled result,
        # and a course meeting Tuesday elsewhere plus 10~12 on another day is not.
        for course in self.all_courses(term="fall", weekday="周二"):
            self.assertEqual(
                course["id"] in coupled_ids,
                "周二10~12节" in (course["schedule"] or ""),
                course["id"],
            )

    def test_period_like_pattern_anchors_on_the_weekday_token(self):
        where, params = app._build_source_where({"period": (1, 12)})
        self.assertEqual(where, " WHERE s.schedule LIKE ?")
        self.assertEqual(params, ["%周_1~12节%"])
        with closing(sqlite3.connect(":memory:")) as conn:
            for schedule, expected in (
                ("1~16周 每周周三1~12节", 1),
                ("1~16周 每周周三11~12节", 0),
                ("1~16周 每周周三1~2节", 0),
                ("1~16周 每周周一3~4节\n1~16周 单周周五1~12节", 1),
            ):
                got = conn.execute("SELECT ? LIKE ?", (schedule, params[0])).fetchone()[0]
                self.assertEqual(got, expected, schedule)
        where, params = app._build_source_where({"weekday": "周三", "period": (3, 4)})
        self.assertEqual(where, " WHERE s.weekdays LIKE ? AND s.schedule LIKE ?")
        self.assertEqual(params, ["%周三%", "%周三3~4节%"])

    def test_card_totals_keep_undergrad_and_graduate_separate(self):
        self.assertEqual(self.call(term="fall", page_size=1)["total"], 4529)
        self.assertEqual(self.call(term="spring", page_size=1)["total"], 3701)
        self.assertEqual(self.call(term="summer", page_size=1)["total"], 160)

    def test_filter_preserves_representative_id_and_all_badges(self):
        with app.get_db("fall") as conn:
            target = conn.execute(
                """
                SELECT course_code, class_no, teacher
                FROM basic_info
                WHERE TRIM(COALESCE(teacher, '')) != ''
                GROUP BY course_code, class_no, teacher
                HAVING COUNT(DISTINCT category) > 1
                ORDER BY course_code, class_no, teacher
                LIMIT 1
                """
            ).fetchone()

        course_code, class_no, teacher = target
        unfiltered = self.all_courses(term="fall", q=course_code)
        card = self.find_card(unfiltered, course_code, class_no, teacher)
        self.assertGreater(len(card["category"]), 1)

        for category in card["category"]:
            filtered = self.all_courses(term="fall", q=course_code, category=category)
            same = self.find_card(filtered, course_code, class_no, teacher)
            self.assertEqual(same["id"], card["id"])
            self.assertEqual(same["category"], card["category"])

    def test_representative_uses_long_detail_fields(self):
        with app.get_db("spring") as conn:
            rows = conn.execute(
                """
                SELECT b.id, d.evaluation
                FROM basic_info b
                JOIN detail_info d ON d.course_id = b.id
                WHERE b.course_code = '00137975'
                  AND b.class_no = '1'
                  AND b.teacher = '王杰(教授)'
                """
            ).fetchall()

        evaluation_by_id = {row["id"]: row["evaluation"] for row in rows}
        self.assertFalse((evaluation_by_id[288] or "").strip())
        self.assertTrue(evaluation_by_id[860].strip())

        courses = self.all_courses(term="spring", q="00137975")
        card = self.find_card(courses, "00137975", "1", "王杰(教授)")
        self.assertEqual(card["id"], "u860")

    def test_type_filter_preserves_representative_id_and_all_badges(self):
        courses = self.all_courses(term="spring", q="00137975")
        card = next(course for course in courses if course["course_code"] == "00137975")
        self.assertGreater(len(card["course_type"]), 1)

        for course_type in card["course_type"]:
            filtered = self.all_courses(term="spring", q="00137975", type=course_type)
            same = self.find_card(filtered, card["course_code"], card["class_no"], card["teacher"])
            self.assertEqual(same["id"], card["id"])
            self.assertEqual(same["course_type"], card["course_type"])

    def test_representative_counts_visible_basic_fields(self):
        courses = self.all_courses(term="spring", q="00430109")
        card = self.find_card(courses, "00430109", "1", "穆良柱(教授)")
        self.assertEqual(card["id"], "u2426")
        self.assertEqual(card["major"], "物理学类")

    def test_scalar_fallback_uses_one_coherent_source_row(self):
        columns = (
            "id", "course_type", "course_code", "class_no", "course_name",
            "category", "credits", "teacher", "department", "major", "grade",
            "schedule", "classroom", "weekdays", "first_period", "enrollment",
            "pnp", "notes", "english_name", "grading", "language", "audience",
            "_level", "detail_score", "completeness_score", "display_course_name",
            "display_classroom", "display_notes", "group_key",
        )
        base = {
            "course_type": "专业课",
            "course_code": "SYN001",
            "class_no": "1",
            "course_name": "Representative Name",
            "category": "任选",
            "credits": 2.0,
            "teacher": "Test Teacher",
            "department": "Representative Department",
            "major": "",
            "grade": "",
            "schedule": "Representative Schedule",
            "classroom": "R101",
            "weekdays": "周一",
            "first_period": 1,
            "enrollment": "10 / 0",
            "pnp": "可申请",
            "notes": "Representative Notes",
            "english_name": "Representative English Name",
            "grading": "百分制",
            "language": "中文",
            "audience": "",
            "_level": "x",
            "display_course_name": "Representative Name",
            "display_classroom": "R101",
            "display_notes": "Representative Notes",
            "group_key": "synthetic-group",
        }
        fixtures = [
            {**base, "id": "x1", "detail_score": 10, "completeness_score": 10},
            {
                **base,
                "id": "x2",
                "department": "Sibling Department A",
                "major": "Alpha major",
                "grade": "Z grade",
                "detail_score": 8,
                "completeness_score": 8,
            },
            {
                **base,
                "id": "x3",
                "department": "Sibling Department B",
                "major": "Zulu major",
                "grade": "A grade",
                "detail_score": 7,
                "completeness_score": 7,
            },
        ]
        row_placeholder = "(" + ",".join("?" for _ in columns) + ")"
        source_sql = (
            f"WITH fixture({','.join(columns)}) AS "
            f"(VALUES {','.join(row_placeholder for _ in fixtures)}) SELECT * FROM fixture"
        )
        params = [fixture[column] for fixture in fixtures for column in columns]
        sql = f"{app._grouped_course_ctes(source_sql, '')} SELECT * FROM grouped"

        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(sql, params).fetchone()

        self.assertEqual(row["id"], "x1")
        self.assertEqual(row["course_name"], "Representative Name")
        self.assertEqual(row["department"], "Representative Department")
        self.assertEqual(row["major"], "Alpha major")
        self.assertEqual(row["grade"], "Z grade")
        self.assertEqual(row["fallback_id"], "x2")

    def test_badge_arrays_have_deterministic_order(self):
        courses = self.all_courses(term="fall", q="00137975")
        card = next(course for course in courses if course["course_code"] == "00137975")
        self.assertEqual(card["course_type"], sorted(card["course_type"]))
        self.assertEqual(card["category"], sorted(card["category"]))

    def test_translated_course_name_does_not_replace_source_search(self):
        with app.get_db("spring") as conn:
            row = conn.execute(
                """
                WITH singleton AS (
                    SELECT course_code, class_no, teacher
                    FROM basic_info
                    WHERE TRIM(COALESCE(teacher, '')) != ''
                    GROUP BY course_code, class_no, teacher
                    HAVING COUNT(*) = 1
                )
                SELECT b.id, t.text
                FROM basic_info b
                JOIN singleton USING (course_code, class_no, teacher)
                JOIN detail_info d ON d.course_id = b.id
                JOIN translations t
                  ON t.course_id = b.id AND t.lang = 'ja' AND t.field = 'course_name'
                WHERE TRIM(t.text) != ''
                  AND t.text NOT IN (b.course_name, COALESCE(d.english_name, ''))
                ORDER BY b.id
                LIMIT 1
                """
            ).fetchone()

        sample_id, translated_name = row
        result = self.all_courses(term="spring", lang="ja", q=translated_name, sort="name_asc")
        card = next((course for course in result if course["id"] == f"u{sample_id}"), None)
        self.assertIsNone(card)

    def test_translated_classroom_does_not_replace_source_search(self):
        with app.get_db("spring") as conn:
            row = conn.execute(
                """
                SELECT b.id, t.text
                FROM basic_info b
                JOIN translations t
                  ON t.course_id = b.id AND t.lang = 'en' AND t.field = 'classroom'
                WHERE TRIM(t.text) != '' AND t.text != b.classroom
                ORDER BY b.id
                LIMIT 1
                """
            ).fetchone()

        sample_id, translated_classroom = row
        result = self.all_courses(term="spring", lang="en", q=translated_classroom)
        card = next((course for course in result if course["id"] == f"u{sample_id}"), None)
        self.assertIsNone(card)

    def test_translated_graduate_course_name_does_not_replace_source_search(self):
        with app.get_db("spring") as conn:
            row = conn.execute(
                """
                SELECT b.id, t.text
                FROM gr.basic_info b
                JOIN gr.detail_info d ON d.course_id = b.id
                JOIN gr.translations t
                  ON t.course_id = b.id AND t.lang = 'ja' AND t.field = 'course_name'
                WHERE TRIM(t.text) != ''
                  AND t.text NOT IN (b.course_name, COALESCE(d.english_name, ''))
                ORDER BY b.id
                LIMIT 1
                """
            ).fetchone()

        sample_id, translated_name = row
        result = self.all_courses(term="spring", lang="ja", q=translated_name)
        card = next((course for course in result if course["id"] == f"g{sample_id}"), None)
        self.assertIsNone(card)

    def test_cross_level_same_key_collision_returns_two_cards(self):
        with app.get_db("fall") as conn:
            row = conn.execute(
                """
                SELECT a.course_code, a.class_no, a.teacher, a.id, r.id
                FROM basic_info a
                JOIN gr.basic_info r
                  ON r.course_code = a.course_code
                 AND r.class_no = a.class_no
                 AND r.teacher = a.teacher
                WHERE TRIM(COALESCE(a.teacher, '')) != ''
                ORDER BY a.course_code, a.class_no, a.teacher
                LIMIT 1
                """
            ).fetchone()

        course_code, class_no, teacher, undergrad_id, graduate_id = row
        courses = self.all_courses(term="fall", q=course_code)
        matching = {
            course["id"]
            for course in courses
            if course["course_code"] == course_code
            and course["class_no"] == class_no
            and course["teacher"] == teacher
        }
        self.assertEqual(matching, {f"a{undergrad_id}", f"r{graduate_id}"})

    def test_name_sorts_use_display_name_and_legacy_aliases(self):
        ascending = self.all_courses(term="fall", lang="en", q="of", sort="name_asc")
        descending = self.all_courses(term="fall", lang="en", q="of", sort="name_desc")
        legacy_asc = self.all_courses(term="fall", lang="en", q="of", sort="pinyin")
        legacy_desc = self.all_courses(term="fall", lang="en", q="of", sort="pinyin_desc")

        ascending_names = [course["course_name"].casefold() for course in ascending]
        descending_names = [course["course_name"].casefold() for course in descending]
        self.assertEqual(ascending_names, sorted(ascending_names))
        self.assertEqual(descending_names, sorted(descending_names, reverse=True))
        self.assertEqual(
            [course["id"] for course in ascending],
            [course["id"] for course in legacy_asc],
        )
        self.assertEqual(
            [course["id"] for course in descending],
            [course["id"] for course in legacy_desc],
        )

    def test_random_sort_is_stable_complete_and_unique(self):
        total, first = self.all_ids("summer", sort="random", random_seed=731, page_size=37)
        _, second = self.all_ids("summer", sort="random", random_seed=731, page_size=37)
        _, different_seed = self.all_ids("summer", sort="random", random_seed=947, page_size=37)
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_seed)
        self.assertEqual(len(first), total)
        self.assertEqual(len(set(first)), total)

    def test_random_signed_seeds_are_stable_and_pairwise_distinct(self):
        orders = {}
        for seed in (0, 1, -1):
            total, first = self.all_ids("summer", sort="random", random_seed=seed, page_size=37)
            _, second = self.all_ids("summer", sort="random", random_seed=seed, page_size=37)
            self.assertEqual(first, second)
            self.assertEqual(len(first), total)
            self.assertEqual(len(set(first)), total)
            orders[seed] = tuple(first)

        self.assertEqual(len(set(orders.values())), 3)

    def test_whitespace_only_teachers_get_id_specific_group_keys(self):
        group_key_sql = getattr(app, "_group_key_sql", None)
        self.assertIsNotNone(group_key_sql)
        if group_key_sql is None:
            return

        with closing(sqlite3.connect(":memory:")) as conn:
            rows = conn.execute(
                f"""
                WITH sample(_level, course_code, class_no, teacher, id) AS (
                    VALUES ('x', 'C1', '1', '   ', 'x1'),
                           ('x', 'C1', '1', '\t', 'x2'),
                           ('x', 'C1', '1', 'Teacher', 'x3')
                )
                SELECT id, {group_key_sql('sample')} AS group_key
                FROM sample
                ORDER BY id
                """
            ).fetchall()

        keys = {row[0]: row[1] for row in rows}
        self.assertNotEqual(keys["x1"], keys["x2"])
        self.assertTrue(keys["x1"].endswith("x1"))
        self.assertTrue(keys["x2"].endswith("x2"))
        self.assertTrue(keys["x3"].endswith("Teacher"))

    def test_count_query_stops_after_matching_group_keys(self):
        count_course_sql = getattr(app, "_count_course_sql", None)
        self.assertIsNotNone(count_course_sql)
        if count_course_sql is None:
            return

        filters = {
            "q": "",
            "type": "",
            "category": "",
            "credits": "",
            "department": "",
            "weekday": "",
            "period": "",
            "grading": "",
            "classroom": "",
        }
        source_sql, params, matching_where = app._build_course_query("fall", "zh", filters)
        count_sql = count_course_sql(source_sql, matching_where)
        self.assertNotIn("ROW_NUMBER", count_sql)
        self.assertNotIn("badge_values", count_sql)
        self.assertNotIn("fallback_candidates", count_sql)

        with app.get_db("fall") as conn:
            total = conn.execute(count_sql, params).fetchone()[0]
            plan = conn.execute(f"EXPLAIN QUERY PLAN {count_sql}", params).fetchall()
        self.assertEqual(total, 4529)
        plan_details = [step["detail"] for step in plan]
        self.assertFalse(any("ranked" in detail or "badges" in detail for detail in plan_details))


class ValidationAndDetailTests(unittest.TestCase):
    def list_args(self, **overrides):
        args = {
            "q": "",
            "type": "",
            "category": "",
            "credits": "",
            "department": "",
            "weekday": "",
            "period": "",
            "grading": "",
            "classroom": "",
            "sort": "",
            "random_seed": 0,
            "lang": "zh",
            "term": "fall",
            "page": 1,
            "page_size": 20,
        }
        args.update(overrides)
        return args

    def test_course_id_is_canonical(self):
        self.assertEqual(app._parse_id("a1"), ("fall", "a", 1))
        for value in ("", "a0", "a01", "a+1", "a-1", "a 1", "a1 ", " x1", "x1"):
            self.assertEqual(app._parse_id(value), (None, None, None))

    def test_invalid_filter_values_raise_422(self):
        invalid_values = (
            ("credits", "abc"),
            ("credits", "NaN"),
            ("credits", "Infinity"),
            ("credits", True),
            ("credits", False),
            ("credits", []),
            ("weekday", "%"),
            ("period", "1~2"),
            ("period", "0-2"),
            ("period", "5-3"),
            ("period", "15-16"),
            ("period", "%"),
            ("period", "3-4 OR 1=1"),
            ("period", True),
            ("lang", "xx"),
            ("sort", "drop"),
            ("term", "winter"),
            ("page", 0),
            ("page", 10001),
            ("page_size", 0),
            ("page_size", 201),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.list_courses(**self.list_args(**{key: value}))
                self.assertEqual(ctx.exception.status_code, 422)

    def test_invalid_filter_term_raises_422(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.get_filters("winter")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_valid_but_out_of_range_page_is_empty(self):
        result = app.list_courses(**self.list_args(term="summer", page=10000))
        self.assertEqual(result["courses"], [])

    def test_detail_language_is_validated_before_opening_database(self):
        with patch.object(app, "get_db") as get_db:
            with self.assertRaises(app.HTTPException) as ctx:
                app.get_course_detail("a1", lang="xx")
        self.assertEqual(ctx.exception.status_code, 422)
        get_db.assert_not_called()

    def test_undergraduate_detail_exposes_books(self):
        detail = app.get_course_detail("a1", lang="zh")
        self.assertIn("textbook", detail)
        self.assertIn("reference_book", detail)

    def test_graduate_detail_has_empty_textbook_and_source_reference_book(self):
        with app.get_db("fall") as conn:
            row = conn.execute(
                """
                SELECT b.id, d.reference_book
                FROM gr.basic_info b
                JOIN gr.detail_info d ON d.course_id = b.id
                WHERE TRIM(COALESCE(d.reference_book, '')) != ''
                ORDER BY b.id
                LIMIT 1
                """
            ).fetchone()

        detail = app.get_course_detail(f"r{row['id']}", lang="zh")
        self.assertEqual(detail["textbook"], "")
        self.assertEqual(detail["reference_book"], row["reference_book"])

    def assert_translated_book_field_preserves_source_text(self, field):
        schema = importlib.import_module("北京大学选课网数据抓取.build_undergrad_2627_fall_db").SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "books.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.executescript(schema)
                conn.execute("INSERT INTO basic_info(id,course_type,course_code,class_no) VALUES(1,'专业课','TEST','1')")
                conn.execute("INSERT INTO detail_info(course_id,textbook,reference_book) VALUES(1,'中文教材','中文参考书')")
                conn.execute("INSERT INTO translations VALUES(1,?,'en','Translated book')", (field,))
                conn.commit()
            with patch.dict(app.TERM_DBS, {"spring": [("main", database, "u")]}):
                detail = app.get_course_detail("u1", lang="en")
            self.assertEqual(detail[field], {"textbook": "中文教材", "reference_book": "中文参考书"}[field])

    def test_translated_textbook_preserves_source_text(self):
        self.assert_translated_book_field_preserves_source_text("textbook")

    def test_translated_reference_book_preserves_source_text(self):
        self.assert_translated_book_field_preserves_source_text("reference_book")

    def test_blank_translation_does_not_replace_original(self):
        out = {"course_name": "Original name"}
        cursor = Mock()
        cursor.execute.return_value.fetchall.return_value = [("course_name", "   ")]
        app._apply_translations(cursor, "main", 1, "en", out)
        self.assertEqual(out["course_name"], "Original name")
