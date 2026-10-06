from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
from tests.test_app import _account_request, _session_cookie


def _migrate_messages_process(database, barrier, output):
    # 每个 worker 独立导入 app，模拟生产多个进程首次打开同一临时旧库。
    app.MESSAGES_DB_PATH = Path(database)
    try:
        barrier.wait(timeout=15)
        with app.get_messages_db() as conn:
            row = dict(conn.execute("SELECT * FROM messages WHERE id=8").fetchone())
            output.put(("ok", row, conn.execute("PRAGMA user_version").fetchone()[0],
                        conn.execute("PRAGMA journal_mode").fetchone()[0]))
    except Exception as error:
        output.put(("error", type(error).__name__, str(error)))


class MessageRepliesAndNicknameTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name in ("ACCOUNTS_DB_PATH", "MESSAGES_DB_PATH"):
            patcher = patch.object(app, name, Path(temp.name) / (name + ".db"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(app, "SCRYPT_PARAMS", {"n": 16, "r": 8, "p": 1})
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self):
        response = app.register_account(_account_request(), {
            "username": "Private_Login", "password": "a-long-password",
            "questions": [{"question": "最喜欢的课", "answer": "高等数学"}],
        })
        token, _ = _session_cookie(response)
        return _account_request(cookie=token)

    def root(self, content="一条旧留言"):
        return app.create_message(_account_request(), {"content": content})

    def replies(self, message_id, **kwargs):
        return app.list_message_replies(message_id, before_id=kwargs.get("before_id", 0),
                                        page_size=kwargs.get("page_size", 20))

    def test_default_and_edited_nickname_are_server_derived_for_messages_and_replies(self):
        request = self.register()
        account = json.loads(app.get_account(request).body)
        self.assertEqual(account["nickname"], "路过的 PKUer")
        root = self.root()
        default_reply = app.create_message_reply(root["id"], request, {"content": "默认昵称"})
        self.assertEqual(default_reply["nickname"], "路过的 PKUer")
        response = app.change_nickname(request, {"nickname": "  燕园猫 🐱  "})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(json.loads(response.body), {"nickname": "燕园猫 🐱"})
        reply = app.create_message_reply(root["id"], request, {
            "content": "  最新回复  ", "nickname": "伪造昵称", "user_id": 999,
        })
        self.assertEqual(reply["nickname"], "燕园猫 🐱")
        self.assertEqual(reply["content"], "最新回复")
        posted = app.create_message(request, {"content": "登录后的留言"})
        self.assertEqual(posted["nickname"], "燕园猫 🐱")
        account = json.loads(app.get_account(request).body)
        self.assertEqual(account["username"], "Private_Login")
        self.assertEqual(account["nickname"], "燕园猫 🐱")
        public = self.replies(root["id"])["replies"]
        self.assertEqual([row["nickname"] for row in public], ["燕园猫 🐱", "路过的 PKUer"])
        for row in public:
            self.assertEqual(set(row), {"id", "posted_at", "content", "nickname", "reply_count"})
        self.assertNotIn("Private_Login", json.dumps(public))

    def test_anonymous_invalid_and_expired_sessions_use_default_without_spoofing(self):
        root = self.root()
        request = self.register()
        app.change_nickname(request, {"nickname": "已登录昵称"})
        with app.get_accounts_db() as conn:
            conn.execute("UPDATE sessions SET expires_at=1")
            conn.commit()
        for anonymous in (_account_request(), _account_request(cookie="invalid-token"), request):
            reply = app.create_message_reply(root["id"], anonymous, {"content": "访客", "nickname": "假冒"})
            self.assertEqual(reply["nickname"], "路过的 PKUer")

    def test_reply_cursor_does_not_skip_or_repeat_when_new_replies_arrive(self):
        first, second = self.root(), self.root("另一个话题")
        with patch.object(app, "MESSAGE_RATE_LIMITS", ()):
            replies = [app.create_message_reply(first["id"], _account_request(), {"content": str(i)})
                       for i in range(5)]
            newest = self.replies(first["id"], page_size=2)
            self.assertTrue(newest["has_more"])
            self.assertEqual([row["id"] for row in newest["replies"]], [replies[4]["id"], replies[3]["id"]])
            app.create_message_reply(first["id"], _account_request(), {"content": "并发新回复"})
        older = self.replies(first["id"], before_id=newest["replies"][-1]["id"], page_size=3)
        self.assertEqual([row["id"] for row in older["replies"]], [r["id"] for r in replies[:3]][::-1])
        self.assertFalse(older["has_more"])
        self.assertEqual(self.replies(second["id"])["replies"], [])
        listing = app.list_messages(page=1, page_size=20)
        self.assertEqual(listing["total"], 2)
        self.assertEqual([row["reply_count"] for row in listing["messages"]], [0, 6])

    def test_invalid_parents_and_reply_pagination_are_rejected(self):
        root = self.root()
        reply = app.create_message_reply(root["id"], _account_request(), {"content": "回复"})
        for parent in (reply["id"], 999, 0, -1, True, 2**63):
            for action in (lambda: self.replies(parent),
                           lambda: app.create_message_reply(parent, _account_request(), {"content": "x"})):
                with self.assertRaises(app.HTTPException) as ctx:
                    action()
                self.assertEqual(ctx.exception.status_code, 404)
        for kwargs in ({"before_id": -1}, {"before_id": True}, {"before_id": 2**63}, {"page_size": 51}):
            with self.assertRaises(app.HTTPException) as ctx:
                self.replies(root["id"], **kwargs)
            self.assertEqual(ctx.exception.status_code, 422)

    def test_nickname_requires_login_and_valid_length(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.change_nickname(_account_request(), {"nickname": "陌生人"})
        self.assertEqual(ctx.exception.status_code, 401)
        request = self.register()
        for nickname in (None, 42, "", "  ", "字" * 31, "a\nb", "a\x00b"):
            with self.assertRaises(app.HTTPException) as ctx:
                app.change_nickname(request, {"nickname": nickname})
            self.assertEqual(ctx.exception.status_code, 422)
        app.change_nickname(request, {"nickname": "字" * 30})
        self.assertEqual(json.loads(app.get_account(request).body)["nickname"], "字" * 30)

    def test_messages_and_replies_share_atomic_rate_limit(self):
        root = self.root()
        barrier = threading.Barrier(6)
        def post(index):
            barrier.wait()
            try:
                app.create_message_reply(root["id"], _account_request(), {"content": str(index)})
                return 201
            except app.HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(post, range(6)))
        self.assertEqual(statuses.count(201), 4)
        self.assertEqual(statuses.count(429), 2)

    def test_existing_messages_migrate_concurrently_without_data_loss(self):
        with closing(sqlite3.connect(app.MESSAGES_DB_PATH)) as conn:
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, posted_at INTEGER NOT NULL, content TEXT NOT NULL, ip_hash TEXT NOT NULL)")
            conn.execute("INSERT INTO messages VALUES (8, 1700000000, '<script>旧留言</script>', 'old-hash')")
            conn.commit()
        barrier = threading.Barrier(2)
        def migrate(_):
            barrier.wait()
            with app.get_messages_db() as conn:
                return dict(conn.execute("SELECT * FROM messages WHERE id=8").fetchone())
        with ThreadPoolExecutor(max_workers=2) as pool:
            migrated = list(pool.map(migrate, range(2)))
        self.assertEqual(migrated[0], migrated[1])
        self.assertEqual(migrated[0]["nickname"], "路过的 PKUer")
        self.assertIsNone(migrated[0]["parent_id"])
        self.assertEqual(migrated[0]["content"], "<script>旧留言</script>")
        self.assertEqual(migrated[0]["ip_hash"], "old-hash")
        self.assertGreater(self.root()["id"], 8)

    def make_legacy_messages(self, database):
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, posted_at INTEGER NOT NULL, content TEXT NOT NULL, ip_hash TEXT NOT NULL)")
            conn.execute("INSERT INTO messages VALUES (8, 1700000000, '<script>旧留言</script>', 'old-hash')")
            conn.commit()

    def assert_legacy_message_preserved(self, row):
        self.assertEqual(row, {"id": 8, "posted_at": 1700000000, "content": "<script>旧留言</script>",
                               "ip_hash": "old-hash", "nickname": "路过的 PKUer",
                               "parent_id": None, "course_key": ""})

    def test_legacy_messages_wal_initialization_is_stable_under_threads(self):
        for iteration in range(25):
            with self.subTest(iteration=iteration):
                database = app.MESSAGES_DB_PATH.parent / f'thread-migration-{iteration}.db'
                self.make_legacy_messages(database)
                barrier = threading.Barrier(4)
                def migrate(_):
                    barrier.wait(timeout=10)
                    with app.get_messages_db() as conn:
                        return (dict(conn.execute("SELECT * FROM messages WHERE id=8").fetchone()),
                                conn.execute("PRAGMA user_version").fetchone()[0])
                with patch.object(app, 'MESSAGES_DB_PATH', database), ThreadPoolExecutor(max_workers=4) as pool:
                    migrated = list(pool.map(migrate, range(4)))
                for row, version in migrated:
                    self.assert_legacy_message_preserved(row)
                    self.assertEqual(version, 2)

    def test_legacy_messages_wal_initialization_is_stable_across_processes(self):
        context = multiprocessing.get_context('spawn')
        for iteration in range(5):
            with self.subTest(iteration=iteration):
                database = app.MESSAGES_DB_PATH.parent / f'process-migration-{iteration}.db'
                self.make_legacy_messages(database)
                barrier = context.Barrier(4)
                output = context.Queue()
                workers = [context.Process(target=_migrate_messages_process,
                           args=(str(database), barrier, output), name=f'pinhaoke-migration-{index}')
                           for index in range(4)]
                try:
                    for worker in workers:
                        worker.start()
                    for _ in workers:
                        result = output.get(timeout=20)
                        self.assertEqual(result[0], 'ok', result)
                        self.assert_legacy_message_preserved(result[1])
                        self.assertEqual(result[2:], (2, 'wal'))
                    for worker in workers:
                        worker.join(timeout=10)
                        self.assertEqual(worker.exitcode, 0)
                finally:
                    for worker in workers:
                        if worker.pid is not None and worker.is_alive():
                            worker.terminate()
                        if worker.pid is not None:
                            worker.join(timeout=5)
                            worker.close()
                    output.close()
                    output.join_thread()

    def test_existing_wal_messages_do_not_acquire_initialization_lock(self):
        with app.get_messages_db() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        lock = Path(str(app.MESSAGES_DB_PATH.resolve()) + '.init.lock')
        self.assertEqual(lock.stat().st_size, 0)
        with patch.object(app.fcntl, 'flock', side_effect=AssertionError('existing WAL must skip initialization lock')):
            root = self.root()
            self.assertEqual(self.replies(root['id']), {'replies': [], 'has_more': False})

    def test_accounts_migrate_v3_concurrently_and_preserve_sessions(self):
        request = self.register()
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            conn.execute("ALTER TABLE users DROP COLUMN nickname")
            # A real v3 database predates the timetable table and its v6 columns.
            conn.execute("DROP TABLE timetable_courses")
            conn.execute("PRAGMA user_version=3")
            conn.commit()
        barrier = threading.Barrier(2)
        def migrate(_):
            barrier.wait()
            return json.loads(app.get_account(request).body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            accounts = list(pool.map(migrate, range(2)))
        for account in accounts:
            self.assertTrue(account["authenticated"])
            self.assertEqual(account["nickname"], "路过的 PKUer")
            self.assertEqual(len(account["collections"]), 1)

    def test_fresh_accounts_schema_cannot_be_downgraded_by_another_worker(self):
        with closing(sqlite3.connect(app.ACCOUNTS_DB_PATH)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        barrier = threading.Barrier(2)
        def migrate(_):
            barrier.wait()
            with app.get_accounts_db() as conn:
                return conn.execute("PRAGMA user_version").fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(migrate, range(2))), [6, 6])
        with app.get_accounts_db() as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
            self.assertEqual(columns.count("nickname"), 1)
            self.assertEqual(columns.count("last_collection_id"), 1)
            timetable_columns = [row[1] for row in conn.execute("PRAGMA table_info(timetable_courses)")]
            self.assertEqual(timetable_columns.count("customization"), 1)
            self.assertEqual(timetable_columns.count("is_custom"), 1)

    def test_content_validation_and_origin_guard_for_new_writes(self):
        root = self.root()
        for origin in ("https://evil.example", "null"):
            request = _account_request(origin=origin)
            for action in (
                lambda: app.create_message(request, {"content": "x"}),
                lambda: app.create_message_reply(root["id"], request, {"content": "x"}),
                lambda: app.change_nickname(request, {"nickname": "x"}),
            ):
                with self.assertRaises(app.HTTPException) as ctx:
                    action()
                self.assertEqual(ctx.exception.status_code, 403)
        for payload in ({"content": " "}, {"content": "x" * 501}, {"content": 9}):
            with self.assertRaises(app.HTTPException) as ctx:
                app.create_message_reply(root["id"], _account_request(), payload)
            self.assertEqual(ctx.exception.status_code, 422)

    def test_changelog_has_shared_chinese_release_content(self):
        response = app.get_changelog()
        self.assertEqual(response.status_code, 200)
        entries = json.loads(response.body)["entries"]
        self.assertGreaterEqual(len(entries), 4)
        self.assertEqual([entry["date"] for entry in entries], sorted((e["date"] for e in entries), reverse=True))
        for entry in entries:
            self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(entry["title"] and entry["changes"])
        self.assertIn("3152", json.dumps(entries, ensure_ascii=False))
        self.assertNotIn("语言切换与手机导航优化", {e["title"] for e in entries})
        self.assertEqual({e["title"] for e in entries if e.get("major")},
                         {"2026 秋季课程数据更新", "账号、收藏夹与个人中心", "我的课表与收藏课程卡片", "课程留言与信息补充",
                          "课表编辑与自定义课程", "课表 PNG 图片导出"})


if __name__ == "__main__":
    unittest.main()
