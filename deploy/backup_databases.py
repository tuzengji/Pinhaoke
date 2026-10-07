#!/usr/bin/env python3
"""拼好课三个可写数据库的服务器本机在线备份。

只用标准库。对每个 SQLite 库以只读连接做一致性在线快照（``source.backup``），
校验副本后原子落盘为带时间戳的文件名，默认保留全部旧快照。目标目录与
副本文件权限锁到 root-only，因为账户库保存密码与会话哈希。

源连接使用 ``mode=ro`` 和 ``query_only``，但 SQLite 仍可能创建 WAL/SHM 辅助文件。
root 父进程把 SQLite 快照交给 www-data 子进程，确保辅助文件归服务用户所有；
子进程从私有临时快照向 stdout 输出字节，父进程接收并校验后写入 root-only 目录。
任何单个库备份失败时以非零码退出，供 systemd 记录；源库尚未创建时跳过。

生产由 systemd 定时器调用，也可手动运行做一次性快照。
"""
from __future__ import annotations

import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 备份目标目录：root-only，不在仓库、不在 web 路径、不在 StateDirectory 内。
DEFAULT_BACKUP_DIR = Path(
    os.environ.get("PINHAOKE_BACKUP_DIR", "") or "/var/backups/pinhaoke"
)
# 非正值保留全部快照；正值只供将来另行授权的手动清理，初期定时服务固定为 0。
DEFAULT_RETENTION = int(os.environ.get("PINHAOKE_BACKUP_RETENTION", "") or "0")
BEIJING = timezone(timedelta(hours=8))
READER_TIMEOUT_SECONDS = 120
SOURCE_MISSING_EXIT = 3

# (环境变量, 生产默认路径, 副本文件名前缀)。环境变量与 systemd unit、app.py 一致。
SOURCE_SPECS = (
    ("PINHAOKE_ACCOUNTS_DB", "/var/lib/pinhaoke/账户.db", "账户"),
    ("PINHAOKE_MESSAGES_DB", "/var/lib/pinhaoke/留言板.db", "留言板"),
    ("PINHAOKE_STATS_DB", "/var/lib/pinhaoke/访问统计.db", "访问统计"),
)


def timestamp(now=None) -> str:
    """北京时间的定宽时间戳，按文件名字典序即为时间序。"""
    now = now or datetime.now(BEIJING)
    return now.strftime("%Y%m%d-%H%M%S")


def resolve_sources(specs=SOURCE_SPECS):
    """把 (环境变量, 默认路径, 前缀) 解析为 (源路径, 前缀)。"""
    resolved = []
    for env_var, default_path, prefix in specs:
        path = Path(os.environ.get(env_var, "") or default_path)
        resolved.append((path, prefix))
    return resolved


def ensure_backup_dir(backup_dir) -> Path:
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    return backup_dir


def _fsync_dir(directory) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reader_options() -> dict:
    # 不继承 root 的附加组；非 root 本地测试/手工运行沿用当前身份。
    options = {"umask": 0o027}
    if os.geteuid() == 0:
        reader = pwd.getpwnam("www-data")
        options.update(user=reader.pw_uid, group=reader.pw_gid, extra_groups=[])
    return options


def _stream_snapshot(source_path, output) -> None:
    """仅由非 root 读取者运行；不接收父进程的备份目录或目标路径。"""
    if os.geteuid() == 0:
        raise PermissionError("SQLite 快照读取者不得以 root 运行")
    with tempfile.TemporaryDirectory(prefix="pinhaoke-snapshot-") as temporary:
        descriptor, name = tempfile.mkstemp(suffix=".db", dir=temporary)
        os.close(descriptor)
        snapshot = Path(name)
        source_conn = dest_conn = None
        try:
            source_conn = sqlite3.connect(Path(source_path).resolve().as_uri() + "?mode=ro", uri=True)
            source_conn.execute("PRAGMA query_only = ON")
            dest_conn = sqlite3.connect(snapshot)
            source_conn.backup(dest_conn)
            # Backup API 会复制源页头的 WAL 模式；只转换临时副本，确保流是完整单文件。
            if dest_conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                raise RuntimeError("无法将临时快照转换为独立数据库文件")
        finally:
            if source_conn is not None:
                source_conn.close()
            if dest_conn is not None:
                dest_conn.close()
        with snapshot.open("rb") as handle:
            shutil.copyfileobj(handle, output, length=1024 * 1024)
        output.flush()


def _stream_main(source_path) -> int:
    # 只有源路径确实不存在才作为可跳过的未初始化库；权限/损坏等均必须失败。
    try:
        source_stat = Path(source_path).stat()
    except FileNotFoundError:
        return SOURCE_MISSING_EXIT
    except OSError as exc:
        print(str(exc)[:2000], file=sys.stderr)
        return 1
    try:
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("源路径不是普通 SQLite 文件")
        _stream_snapshot(source_path, sys.stdout.buffer)
    except Exception as exc:
        print(str(exc)[:2000], file=sys.stderr)
        return 1
    return 0


def backup_one(source_path, backup_dir, prefix, stamp) -> Path:
    """读取者流出 SQLite 在线快照；父进程校验后原子发布私有副本。"""
    source_path = Path(source_path)
    backup_dir = Path(backup_dir)
    final_path = backup_dir / f"{prefix}-{stamp}.db"
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{prefix}-{stamp}.", suffix=".tmp", dir=backup_dir
    )
    os.close(descriptor)
    temp_path = Path(raw_temp)
    dest_conn = None
    try:
        os.chmod(temp_path, 0o600)
        # stdout 只获得已经打开的私有文件描述符；子进程不能遍历 root-only 目录。
        with temp_path.open("wb") as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--stream", str(source_path)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                timeout=READER_TIMEOUT_SECONDS, check=False, **_reader_options(),
            )
            if result.returncode == SOURCE_MISSING_EXIT:
                raise FileNotFoundError(source_path)
            if result.returncode:
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"SQLite 读取者失败 ({result.returncode}): {detail}")
            output.flush()
            os.fsync(output.fileno())
        # SQLite 会把零字节文件当成空数据库，quick_check 单独使用不能拒绝空流。
        with temp_path.open("rb") as snapshot:
            if snapshot.read(16) != b"SQLite format 3\0" or temp_path.stat().st_size < 512:
                raise RuntimeError("SQLite 读取者输出为空或文件头不完整")
        dest_conn = sqlite3.connect(temp_path.resolve().as_uri() + "?mode=ro", uri=True)
        dest_conn.execute("PRAGMA query_only = ON")
        row = dest_conn.execute("PRAGMA quick_check").fetchone()
        dest_conn.close()
        dest_conn = None
        if not row or row[0] != "ok":
            raise RuntimeError(f"quick_check 未通过：{source_path} -> {row}")
        os.replace(temp_path, final_path)
        temp_path = None
        os.chmod(final_path, 0o600)
        _fsync_dir(backup_dir)
        return final_path
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if temp_path is not None and Path(temp_path).exists():
            os.unlink(temp_path)


def prune(backup_dir, prefix, retention) -> list:
    """非正值不清理；正值只保留某个库最近 retention 份，返回被删除的路径。"""
    if retention <= 0:
        return []
    backup_dir = Path(backup_dir)
    snapshots = sorted(backup_dir.glob(f"{prefix}-*.db"))
    removed = []
    for path in snapshots[: max(0, len(snapshots) - retention)]:
        os.unlink(path)
        removed.append(path)
    if removed:
        _fsync_dir(backup_dir)
    return removed


def run_backup(sources, backup_dir, retention, stamp=None):
    """备份全部源库；仅正 retention 清理，返回 (成功列表, 跳过列表, 失败列表)。"""
    stamp = stamp or timestamp()
    backup_dir = ensure_backup_dir(backup_dir)
    created, skipped, failed = [], [], []
    for source_path, prefix in sources:
        try:
            created.append(backup_one(source_path, backup_dir, prefix, stamp))
            if retention > 0:
                prune(backup_dir, prefix, retention)
        except FileNotFoundError:
            skipped.append((Path(source_path), prefix))
        except Exception as exc:  # noqa: BLE001 - 汇总后统一退出码
            failed.append((Path(source_path), prefix, exc))
    return created, skipped, failed


def main(argv=None) -> int:
    argv = [] if argv is None else list(argv)
    if len(argv) == 2 and argv[0] == "--stream":
        return _stream_main(argv[1])
    if argv:
        print("用法：backup_databases.py", file=sys.stderr)
        return 2
    backup_dir = DEFAULT_BACKUP_DIR
    retention = DEFAULT_RETENTION
    created, skipped, failed = run_backup(
        resolve_sources(), backup_dir, retention
    )
    for path in created:
        print(f"已备份 {path}")
    for source_path, _prefix in skipped:
        print(f"跳过（源库不存在）{source_path}")
    for source_path, _prefix, exc in failed:
        print(f"备份失败 {source_path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
