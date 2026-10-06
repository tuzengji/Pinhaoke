"""deploy/backup_databases.py 的回归测试：一致性快照、权限、保留与失败处理。

只用临时目录与临时 SQLite 库，不触网、不产生任何 API 费用。
"""
from contextlib import closing
import importlib.util
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "pinhaoke_backup", ROOT / "deploy" / "backup_databases.py"
)
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


def _make_wal_db(path, rows):
    """建一个 WAL 库并写入且不检查点，返回持有的写连接（保留未落盘的 WAL）。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class BackupOneTests(unittest.TestCase):
    def test_snapshot_captures_uncheckpointed_wal_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "账户.db"
            writer = _make_wal_db(src, 500)
            try:
                self.assertTrue((src.parent / "账户.db-wal").exists())
                out = Path(tmp) / "out"
                backup.ensure_backup_dir(out)
                copy = backup.backup_one(src, out, "账户", "20260101-000000")
            finally:
                writer.close()
            self.assertEqual(copy, out / "账户-20260101-000000.db")
            self.assertEqual(_mode(copy), 0o600)
            self.assertEqual(copy.stat().st_uid, os.geteuid())
            # Darwin 新文件继承父目录组；Linux 的 SGID 目录也如此。
            expected_gid = (out.stat().st_gid if sys.platform == "darwin" or out.stat().st_mode & stat.S_ISGID
                            else os.getegid())
            self.assertEqual(copy.stat().st_gid, expected_gid)
            # 副本是干净的非 WAL 文件，没有自己的 -wal/-shm。
            self.assertFalse((out / "账户-20260101-000000.db-wal").exists())
            conn = sqlite3.connect(copy)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 500)
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            finally:
                conn.close()

    def test_idle_wal_snapshot_handles_missing_sidecars_and_escaped_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "账户?#%.db"
            writer = _make_wal_db(src, 7)
            writer.close()
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), [src.name])
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            copy = backup.backup_one(src, out, "账户", "20260101-000000")
            with closing(sqlite3.connect(copy)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 7)
            self.assertEqual(copy.read_bytes()[:16], b"SQLite format 3\0")
            for sidecar in src.parent.glob(src.name + "-*"):
                self.assertEqual(sidecar.stat().st_uid, src.stat().st_uid)

    def test_root_reader_uses_service_identity_and_clears_supplementary_groups(self):
        with patch.object(backup.os, "geteuid", return_value=0), \
             patch.object(backup.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=33, pw_gid=33)) as lookup:
            self.assertEqual(backup._reader_options(),
                             {"user": 33, "group": 33, "extra_groups": [], "umask": 0o027})
            lookup.assert_called_once_with("www-data")

    def test_missing_service_user_fails_closed_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            with patch.object(backup.os, "geteuid", return_value=0), \
                 patch.object(backup.pwd, "getpwnam", side_effect=KeyError("no service user")), \
                 patch.object(backup.subprocess, "run") as run:
                with self.assertRaises(KeyError):
                    backup.backup_one(Path(tmp) / "source.db", out, "账户", "20260101-000000")
            run.assert_not_called()
            self.assertEqual(list(out.iterdir()), [])

    def test_snapshot_reader_refuses_root(self):
        with patch.object(backup.os, "geteuid", return_value=0):
            with self.assertRaises(PermissionError):
                backup._stream_snapshot(Path("unused.db"), None)

    def test_backup_dir_created_root_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "backups"
            backup.ensure_backup_dir(out)
            self.assertTrue(out.is_dir())
            self.assertEqual(_mode(out), 0o700)

    def test_missing_source_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            with self.assertRaises(FileNotFoundError):
                backup.backup_one(Path(tmp) / "缺失.db", out, "账户", "20260101-000000")

    def test_corrupt_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "坏.db"
            src.write_bytes(b"this is not a sqlite database" * 8)
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            with self.assertRaisesRegex(RuntimeError, "SQLite 读取者失败"):
                backup.backup_one(src, out, "账户", "20260101-000000")
            # 失败不留临时文件。
            self.assertEqual(list(out.glob(".*")), [])
            self.assertEqual(list(out.glob("账户-*.db")), [])


class PruneAndRunTests(unittest.TestCase):
    def assert_failed_reader_preserves_snapshots(self, reader):
        with tempfile.TemporaryDirectory() as tmp:
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            old = {f"账户-2026010{i}-000000.db": bytes([i]) for i in (1, 2)}
            for name, data in old.items():
                (out / name).write_bytes(data)
            with patch.object(backup.subprocess, "run", side_effect=reader):
                created, skipped, failed = backup.run_backup(
                    [(Path(tmp) / "source.db", "账户")], out, retention=1, stamp="20260103-000000"
                )
            self.assertEqual(created, [])
            self.assertEqual(skipped, [])
            self.assertEqual(len(failed), 1)
            self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, old)

    def test_failed_reader_partial_output_is_not_published_or_pruned(self):
        def reader(command, **kwargs):
            kwargs["stdout"].write(b"partial SQLite stream")
            kwargs["stderr"].write(b"synthetic reader failure")
            return subprocess.CompletedProcess(command, 7)
        self.assert_failed_reader_preserves_snapshots(reader)

    def test_successful_reader_with_invalid_output_is_not_published_or_pruned(self):
        def reader(command, **kwargs):
            kwargs["stdout"].write(b"not a valid SQLite database")
            return subprocess.CompletedProcess(command, 0)
        self.assert_failed_reader_preserves_snapshots(reader)

    def test_empty_or_truncated_successful_stream_is_not_published_or_pruned(self):
        for data in (b"", b"SQLite format 3\0"):
            with self.subTest(length=len(data)):
                def reader(command, **kwargs):
                    kwargs["stdout"].write(data)
                    return subprocess.CompletedProcess(command, 0)
                self.assert_failed_reader_preserves_snapshots(reader)

    def test_reader_timeout_is_not_published_or_pruned(self):
        def reader(command, **kwargs):
            kwargs["stdout"].write(b"partial stream before timeout")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        self.assert_failed_reader_preserves_snapshots(reader)

    def test_directory_sync_failure_is_reported_without_pruning_older_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.db"
            writer = _make_wal_db(src, 2)
            writer.close()
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            old = out / "账户-20260101-000000.db"
            old.write_bytes(b"keep the older snapshot")
            real_fsync = os.fsync
            def fail_directory_sync(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("synthetic directory fsync failure")
                return real_fsync(descriptor)
            with patch.object(backup.os, "fsync", side_effect=fail_directory_sync):
                created, skipped, failed = backup.run_backup(
                    [(src, "账户")], out, retention=1, stamp="20260102-000000"
                )
            self.assertEqual((created, skipped), ([], []))
            self.assertEqual(len(failed), 1)
            self.assertEqual(old.read_bytes(), b"keep the older snapshot")
            # rename 已完成但持久性未确认；明确报错，不能据此删掉旧快照。
            self.assertTrue((out / "账户-20260102-000000.db").is_file())

    def test_prune_keeps_only_newest_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = backup.ensure_backup_dir(Path(tmp) / "out")
            stamps = [f"2026010{i}-000000" for i in range(1, 6)]
            for s in stamps:
                (out / f"账户-{s}.db").write_bytes(b"x")
            removed = backup.prune(out, "账户", 2)
            kept = sorted(p.name for p in out.glob("账户-*.db"))
            self.assertEqual(kept, ["账户-20260104-000000.db", "账户-20260105-000000.db"])
            self.assertEqual(len(removed), 3)

    def test_run_backup_rolls_snapshots_and_reports_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            acc = Path(tmp) / "账户.db"
            writer = _make_wal_db(acc, 10)
            out = Path(tmp) / "backups"
            sources = [(acc, "账户"), (Path(tmp) / "留言板.db", "留言板")]
            try:
                for i in range(1, 4):
                    created, skipped, failed = backup.run_backup(
                        sources, out, retention=2, stamp=f"2026010{i}-000000"
                    )
                    self.assertEqual(len(created), 1)  # 只有账户库存在
                    self.assertEqual(len(skipped), 1)  # 留言板库未创建，跳过
                    self.assertEqual(failed, [])
            finally:
                writer.close()
            # retention=2：三次运行后只留最近两份。
            self.assertEqual(len(list(out.glob("账户-*.db"))), 2)
            self.assertEqual(list(out.glob("留言板-*.db")), [])

    def test_run_backup_collects_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "账户.db"
            bad.write_bytes(b"not sqlite" * 16)
            out = Path(tmp) / "backups"
            created, skipped, failed = backup.run_backup(
                [(bad, "账户")], out, retention=14, stamp="20260101-000000"
            )
            self.assertEqual(created, [])
            self.assertEqual(len(failed), 1)

    def test_main_returns_nonzero_when_a_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "账户.db"
            bad.write_bytes(b"not sqlite" * 16)
            out = Path(tmp) / "backups"
            orig_dir = backup.DEFAULT_BACKUP_DIR
            orig_resolve = backup.resolve_sources
            backup.DEFAULT_BACKUP_DIR = out
            backup.resolve_sources = lambda: [(bad, "账户")]
            try:
                self.assertEqual(backup.main(), 1)
            finally:
                backup.DEFAULT_BACKUP_DIR = orig_dir
                backup.resolve_sources = orig_resolve

    def test_main_returns_zero_on_clean_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            acc = Path(tmp) / "账户.db"
            writer = _make_wal_db(acc, 5)
            out = Path(tmp) / "backups"
            orig_dir = backup.DEFAULT_BACKUP_DIR
            orig_resolve = backup.resolve_sources
            backup.DEFAULT_BACKUP_DIR = out
            backup.resolve_sources = lambda: [(acc, "账户")]
            try:
                self.assertEqual(backup.main(), 0)
                self.assertEqual(len(list(out.glob("账户-*.db"))), 1)
            finally:
                backup.DEFAULT_BACKUP_DIR = orig_dir
                backup.resolve_sources = orig_resolve
                writer.close()


if __name__ == "__main__":
    unittest.main()
