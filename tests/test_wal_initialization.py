"""All writable stores serialize their first WAL switch without touching user data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class WritableDatabaseWalTests(unittest.TestCase):
    OPENERS = (
        ("MESSAGES_DB_PATH", "get_messages_db", 2),
        ("STATS_DB_PATH", "get_stats_db", 0),
        ("ACCOUNTS_DB_PATH", "get_accounts_db", 6),
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for field, _, _ in self.OPENERS:
            patcher = patch.object(app, field, self.root / (field + ".db"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_legacy_database(self, path):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE audit_sentinel (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO audit_sentinel VALUES (8, 'preserved synthetic record')")
            conn.commit()
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def assert_lock_released_across_processes(self, path):
        self.assertTrue(path.is_file())
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import fcntl,sys; f=open(sys.argv[1], 'a'); "
             "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.stat().st_size, 0)

    def test_non_wal_openers_wait_for_initialization_lock_and_preserve_records(self):
        # Hold the real lock until every worker reaches initialization. Events expose
        # an unguarded WAL switch deterministically; there is no stress-loop timing bet.
        for field, name, version in self.OPENERS:
            with self.subTest(database=field):
                database = getattr(app, field)
                self.make_legacy_database(database)
                lock_path = Path(str(database.resolve()) + ".init.lock")
                real_connect, real_flock = sqlite3.connect, app.fcntl.flock
                connected = threading.Barrier(4)
                ready, released = threading.Event(), threading.Event()
                guard = threading.Lock()
                reached, early_switches, lock_targets = set(), [], []

                def mark_reached():
                    reached.add(threading.get_ident())
                    if len(reached) == 4:
                        ready.set()

                class ObservedConnection(sqlite3.Connection):
                    def execute(self, sql, *args, **kwargs):
                        if sql.strip().upper() == "PRAGMA JOURNAL_MODE = WAL":
                            with guard:
                                if not released.is_set():
                                    early_switches.append(threading.get_ident())
                                mark_reached()
                        return super().execute(sql, *args, **kwargs)

                def connect(*args, **kwargs):
                    kwargs["factory"] = ObservedConnection
                    conn = real_connect(*args, **kwargs)
                    connected.wait(timeout=10)
                    return conn

                def flock(descriptor, operation):
                    if operation == app.fcntl.LOCK_EX:
                        stat = os.fstat(descriptor)
                        with guard:
                            lock_targets.append((stat.st_dev, stat.st_ino))
                            mark_reached()
                    return real_flock(descriptor, operation)

                def open_database(_):
                    with getattr(app, name)() as conn:
                        return (tuple(conn.execute("SELECT * FROM audit_sentinel").fetchone()),
                                conn.execute("PRAGMA journal_mode").fetchone()[0],
                                conn.execute("PRAGMA user_version").fetchone()[0])

                with lock_path.open("a") as held:
                    real_flock(held.fileno(), app.fcntl.LOCK_EX)
                    stat = os.fstat(held.fileno())
                    expected_lock = (stat.st_dev, stat.st_ino)
                    with patch.object(app.sqlite3, "connect", side_effect=connect), \
                         patch.object(app.fcntl, "flock", side_effect=flock), \
                         ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [pool.submit(open_database, i) for i in range(4)]
                        try:
                            self.assertTrue(ready.wait(10), "workers did not reach initialization")
                            self.assertEqual(early_switches, [], "WAL switched before the shared lock was released")
                            self.assertEqual(lock_targets, [expected_lock] * 4)
                            self.assertFalse(any(future.done() for future in futures))
                        finally:
                            released.set()
                            real_flock(held.fileno(), app.fcntl.LOCK_UN)
                        results = [future.result(timeout=10) for future in futures]
                self.assertEqual(results, [((8, "preserved synthetic record"), "wal", version)] * 4)
                self.assert_lock_released_across_processes(lock_path)

    def test_existing_wal_openers_skip_initialization_lock(self):
        for field, name, _ in self.OPENERS:
            with self.subTest(database=field):
                with getattr(app, name)() as conn:
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                with patch.object(app.fcntl, "flock", side_effect=AssertionError("WAL fast path must not lock")):
                    with getattr(app, name)() as conn:
                        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_failed_wal_switch_closes_connection_and_releases_lock(self):
        for field, name, _ in self.OPENERS:
            with self.subTest(database=field):
                database = getattr(app, field)
                self.make_legacy_database(database)
                real_connect, opened = sqlite3.connect, []

                class FailingConnection(sqlite3.Connection):
                    def execute(self, sql, *args, **kwargs):
                        if sql.strip().upper() == "PRAGMA JOURNAL_MODE = WAL":
                            raise sqlite3.OperationalError("synthetic WAL switch failure")
                        return super().execute(sql, *args, **kwargs)

                def connect(*args, **kwargs):
                    kwargs["factory"] = FailingConnection
                    conn = real_connect(*args, **kwargs)
                    opened.append(conn)
                    return conn

                with patch.object(app.sqlite3, "connect", side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic WAL switch failure"):
                        with getattr(app, name)():
                            self.fail("connection must not be yielded after failed initialization")
                self.assertEqual(len(opened), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].execute("SELECT 1")
                lock_path = Path(str(database.resolve()) + ".init.lock")
                self.assert_lock_released_across_processes(lock_path)
                with closing(real_connect(database)) as conn:
                    self.assertEqual(conn.execute("SELECT * FROM audit_sentinel").fetchall(),
                                     [(8, "preserved synthetic record")])
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")


if __name__ == "__main__":
    unittest.main()
