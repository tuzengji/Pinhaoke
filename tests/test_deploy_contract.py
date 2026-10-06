import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requirements = (ROOT / "requirements.txt").read_text()
        cls.update = (ROOT / "deploy/update.sh").read_text()
        cls.service = (ROOT / "deploy/pinhaoke.service").read_text()
        cls.nginx = (ROOT / "deploy/nginx.conf").read_text()
        cls.main = cls.update[cls.update.index("\nmain() {") :]
        readme = ROOT / "deploy/README.md"
        cls.deploy_readme = readme.read_text() if readme.exists() else ""
        backup_service = ROOT / "deploy/pinhaoke-backup.service"
        backup_timer = ROOT / "deploy/pinhaoke-backup.timer"
        cls.backup_service = backup_service.read_text() if backup_service.exists() else ""
        cls.backup_timer = backup_timer.read_text() if backup_timer.exists() else ""

    def test_direct_dependencies_are_exactly_pinned_to_audited_versions(self):
        self.assertEqual(
            self.requirements.splitlines(),
            [
                "fastapi==0.136.1",
                "starlette==1.3.1",
                "uvicorn[standard]==0.44.0",
            ],
        )

    def test_update_is_strict_root_only_and_nonblocking(self):
        self.assertRegex(self.update, r"set -[^\n]*e[^\n]*u[^\n]*o pipefail")
        self.assertIn('EUID', self.update)
        self.assertIn('flock -n', self.update)
        self.assertLess(self.main.index('flock -n'), self.main.index('git fetch'))
        required_tools = {
            "awk",
            "chmod",
            "chown",
            "curl",
            "find",
            "git",
            "grep",
            "install",
            "journalctl",
            "mktemp",
            "mv",
            "python3",
            "rm",
            "sha256sum",
            "sleep",
            "systemctl",
        }
        tool_loop = re.search(r"for tool in ([^;]+); do", self.update)
        self.assertIsNotNone(tool_loop)
        self.assertTrue(required_tools.issubset(set(tool_loop.group(1).split())))
        self.assertIn('command -v "$tool"', self.update)

    def test_update_resolves_and_prefetches_exact_origin_main_before_stop(self):
        stop = self.main.index('systemctl stop "$SERVICE"')
        for text in (
            'git fetch --prune origin "+refs/heads/main:refs/remotes/origin/main"',
            'git rev-parse --verify "refs/remotes/origin/main^{commit}"',
            'git show "$TARGET_COMMIT:.gitattributes"',
            'command -v git-lfs',
            'git lfs fetch origin "$TARGET_COMMIT"',
            'git lfs fsck --objects "$TARGET_COMMIT"',
        ):
            self.assertIn(text, self.main)
            self.assertLess(self.main.index(text), stop)
        self.assertIn('git reset --hard "$TARGET_COMMIT"', self.main)

    def test_update_builds_verified_candidate_from_target_requirements_before_stop(self):
        stop = self.main.index('systemctl stop "$SERVICE"')
        for text in (
            '"$TARGET_TREE/requirements.txt"',
            'sha256sum "$TARGET_REQUIREMENTS"',
            'python3 -m venv "$CANDIDATE_VENV"',
            '"$CANDIDATE_VENV/bin/python" -m pip install -r "$TARGET_REQUIREMENTS"',
            '"$CANDIDATE_VENV/bin/python" -m pip check',
            '"$CANDIDATE_VENV/bin/python"',
        ):
            self.assertIn(text, self.main)
            self.assertLess(self.main.index(text), stop)
        self.assertIn('.requirements.sha256', self.update)
        self.assertNotIn('rm -rf venv', self.update)

    def test_relocated_venv_requires_python_module_launch(self):
        self.assertIn(
            "ExecStart=/opt/pinhaoke/venv/bin/python -m uvicorn app:app",
            self.service,
        )
        self.assertNotIn("/venv/bin/uvicorn", self.service)

        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original"
            moved = Path(tmp) / "moved"
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", original],
                check=True,
                capture_output=True,
                text=True,
            )
            console_script = original / "bin" / "uvicorn-old"
            console_script.write_text(
                f"#!{original / 'bin' / 'python'}\n"
                "import runpy\nrunpy.run_module('uvicorn', run_name='__main__')\n"
            )
            console_script.chmod(0o755)
            shutil.move(original, moved)

            try:
                stale_returncode = subprocess.run(
                    [moved / "bin" / "uvicorn-old", "--version"],
                    capture_output=True,
                    text=True,
                ).returncode
            except FileNotFoundError:
                stale_returncode = 127
            module = subprocess.run(
                [moved / "bin" / "python", "-m", "uvicorn", "--version"],
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(stale_returncode, 0)
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertIn("uvicorn", module.stdout.lower())
        self.assertIn('"$LIVE_VENV/bin/python" -m uvicorn --version', self.update)
        self.assertNotIn('! -x "$LIVE_VENV/bin/uvicorn"', self.update)

    def test_update_swaps_on_same_filesystem_and_rolls_back_failed_rename(self):
        self.assertIn('mktemp -d "$APP_DIR/.deploy-stage.', self.update)
        self.assertIn('mv "$LIVE_VENV" "$BACKUP_VENV"', self.update)
        self.assertIn('mv "$CANDIDATE_VENV" "$LIVE_VENV"', self.update)
        self.assertGreaterEqual(self.update.count('mv "$BACKUP_VENV" "$LIVE_VENV"'), 1)

    def test_update_stops_fail_fast_then_installs_and_starts_service(self):
        stop = self.main.index('systemctl stop "$SERVICE"')
        reset = self.main.index('git reset --hard "$TARGET_COMMIT"')
        unit = self.main.index('deploy/pinhaoke.service')
        reload_service = self.main.index('systemctl daemon-reload')
        start = self.main.index('systemctl start "$SERVICE"')
        self.assertLess(stop, reset)
        self.assertLess(reset, unit)
        self.assertLess(unit, reload_service)
        self.assertLess(reload_service, start)
        self.assertNotIn('systemctl stop "$SERVICE" ||', self.main)
        self.assertNotIn('chown -R', self.update)
        self.assertIn('find "$APP_DIR" -xdev', self.update)
        self.assertIn('chown root:www-data', self.update)

    def test_update_materializes_lfs_and_sets_read_only_service_permissions(self):
        self.assertIn('git lfs checkout', self.update)
        self.assertGreaterEqual(self.update.count('git lfs fsck --objects "$TARGET_COMMIT"'), 2)
        self.assertIn(
            'materialize_release_tree "$TARGET_COMMIT" "$TARGET_TREE" "$TARGET_INDEX"',
            self.update,
        )
        self.assertIn('GIT_INDEX_FILE="$index" git read-tree "$commit"', self.update)
        self.assertIn('GIT_ATTR_SOURCE="$commit" GIT_INDEX_FILE="$index"', self.update)
        self.assertIn('verify_materialized_lfs "$TARGET_TREE" "$TARGET_COMMIT"', self.update)
        self.assertLess(
            self.main.index('verify_materialized_lfs "$TARGET_TREE" "$TARGET_COMMIT"'),
            self.main.index('systemctl stop "$SERVICE"'),
        )
        self.assertIn('chown -h root:www-data', self.update)
        self.assertIn('chown -h root:root', self.update)
        for excluded in ('.git', '.deploy-stage.*', '.venv-backup-*', '.venv-failed-*'):
            self.assertIn(excluded, self.update)
        self.assertNotIn('find "$APP_DIR" -xdev -exec chown', self.update)
        self.assertIn('chmod 0750', self.update)
        self.assertIn('chmod 0640', self.update)
        self.assertIn('"$LIVE_VENV/bin"', self.update)
        self.assertIn('"$APP_DIR/Images"', self.update)
        self.assertIn('"$APP_DIR/数据库"', self.update)

    def test_previous_lfs_release_is_fetched_and_materialized_before_stop(self):
        stop = self.main.index('systemctl stop "$SERVICE"')
        preflight = 'preflight_previous_lfs_release'
        self.assertIn(f'{preflight} "$PREVIOUS_COMMIT"', self.main)
        self.assertLess(self.main.index(f'{preflight} "$PREVIOUS_COMMIT"'), stop)
        for fragment in (
            'git lfs fetch origin "$commit"',
            'git lfs fsck --objects "$commit"',
            'materialize_release_tree "$commit" "$tree" "$index"',
            'verify_materialized_lfs "$tree" "$commit"',
        ):
            self.assertIn(fragment, self.update)

    def test_materialized_lfs_verification_rejects_failed_file_listing(self):
        script = textwrap.dedent(f"""\
            source {shlex.quote(str(ROOT / 'deploy/update.sh'))}
            git() {{
                if [[ "$*" == "lfs ls-files --name-only broken" ]]; then
                    return 23
                fi
                return 99
            }}
            verify_materialized_lfs /unused broken
        """)
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot list LFS paths", result.stderr)

    def test_materialized_lfs_verification_checks_successful_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = root / "course data.db"
            script = textwrap.dedent(f"""\
                source {shlex.quote(str(ROOT / 'deploy/update.sh'))}
                git() {{ printf '%s\\n' 'course data.db'; }}
                verify_materialized_lfs {shlex.quote(str(root))} release
            """)
            for contents, expected in ((None, False), ("version https://git-lfs.github.com/spec/v1\n", False),
                                       ("materialized data\n", True)):
                with self.subTest(contents=contents):
                    if contents is not None:
                        asset.write_text(contents)
                    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, expected, result.stderr)

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is unavailable")
    def test_materialized_tree_uses_target_attributes_for_new_lfs_paths(self):
        git_version = subprocess.run(
            ["git", "version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        match = re.search(r"(\d+)\.(\d+)", git_version)
        if not match or tuple(map(int, match.groups())) < (2, 42):
            self.skipTest("GIT_ATTR_SOURCE requires Git 2.42 or newer")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            tree = Path(tmp) / "target-tree"
            index = Path(tmp) / "target-index"
            repo.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init")
            git("config", "user.name", "Deploy Test")
            git("config", "user.email", "deploy-test@example.invalid")
            git("lfs", "install", "--local")
            (repo / ".gitattributes").write_text("", encoding="utf-8")
            git("add", ".gitattributes")
            git("commit", "-m", "old release")
            old_commit = git("rev-parse", "HEAD").stdout.strip()

            payload = b"new target lfs payload\n" * 1024
            (repo / ".gitattributes").write_text(
                "new.bin filter=lfs diff=lfs merge=lfs -text\n",
                encoding="utf-8",
            )
            (repo / "new.bin").write_bytes(payload)
            git("add", ".gitattributes", "new.bin")
            git("commit", "-m", "target release")
            target_commit = git("rev-parse", "HEAD").stdout.strip()
            pointer = git("show", f"{target_commit}:new.bin").stdout
            self.assertTrue(pointer.startswith("version https://git-lfs.github.com/spec/v1"))

            git("reset", "--hard", old_commit)
            command = textwrap.dedent(
                f"""
                set -euo pipefail
                source {shlex.quote(str(ROOT / 'deploy/update.sh'))}
                materialize_release_tree \
                    {shlex.quote(target_commit)} \
                    {shlex.quote(str(tree))} \
                    {shlex.quote(str(index))}
                """
            )
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((tree / "new.bin").read_bytes(), payload)

    def test_missing_previous_lfs_object_fails_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            systemctl_log = root / "systemctl.log"
            git_log = root / "git.log"

            (bin_dir / "git").write_text(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' "$*" >>"$GIT_LOG"\n'
                'if [[ "$*" == "lfs fsck --objects old" ]]; then exit 23; fi\n'
                "exit 0\n"
            )
            (bin_dir / "git-lfs").write_text("#!/usr/bin/env bash\nexit 0\n")
            (bin_dir / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' "$*" >>"$SYSTEMCTL_LOG"\n'
                "exit 0\n"
            )
            for stub in bin_dir.iterdir():
                stub.chmod(0o755)

            harness = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export PATH={bin_dir!s}:$PATH
                export GIT_LOG={git_log!s}
                export SYSTEMCTL_LOG={systemctl_log!s}
                source {ROOT / 'deploy/update.sh'}
                PREVIOUS_USES_LFS=1
                PREVIOUS_TREE={root / 'previous-tree'!s}
                PREVIOUS_INDEX={root / 'previous-index'!s}
                preflight_previous_lfs_release old
                systemctl stop pinhaoke
                """
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 23, result.stderr)
            git_calls = git_log.read_text().splitlines()
            self.assertIn("lfs fetch origin old", git_calls)
            self.assertIn("lfs fsck --objects old", git_calls)
            self.assertFalse(systemctl_log.exists())

    def test_update_smoke_tests_all_five_api_contracts_with_bounded_retries(self):
        for endpoint in (
            "/api/health",
            "/api/filters?term=fall",
            "/api/courses?term=fall&page_size=1",
            "/api/reviews?page_size=1",
            "/api/reviews/$review_pid",
        ):
            self.assertIn(endpoint, self.update)
        self.assertIn(
            'for ((attempt = 1; attempt <= SMOKE_ATTEMPTS; attempt++)); do',
            self.update,
        )
        self.assertNotIn("$(seq", self.update)
        self.assertIn('curl --silent --show-error --max-time', self.update)
        for invariant in (
            '"status"', '"databases"', '"reviews"', '"courses"', '"threads"',
            '"snapshot_replies"', '"highlights"', '"reply_count"', '"replies"',
        ):
            self.assertIn(invariant, self.update)
        self.assertNotIn('== 4421', self.update)
        self.assertRegex(self.update, r'isinstance\(data\.get\("total"\), int\)')
        self.assertIn('journalctl -u "$SERVICE"', self.update)
        self.assertIn('systemctl is-active --quiet "$SERVICE"', self.update)

    def test_post_stop_errors_and_signals_share_automatic_rollback(self):
        for fragment in (
            'trap \'on_error "$LINENO"\' ERR',
            "trap 'on_signal INT' INT",
            "trap 'on_signal TERM' TERM",
            "trap 'on_signal HUP' HUP",
            'rollback_activation "error at line $line"',
            'rollback_activation "signal $signal"',
            'rollback_activation "unexpected exit $status"',
            'PREVIOUS_COMMIT=$(git rev-parse --verify "HEAD^{commit}")',
            'PREVIOUS_SERVICE_ACTIVE=1',
            'UNIT_BACKUP=',
        ):
            self.assertIn(fragment, self.update)

    def test_swap_and_rollback_restore_previous_code_env_unit_and_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            bin_dir = root / "bin"
            stage = app_dir / ".deploy-stage.test"
            app_dir.mkdir()
            bin_dir.mkdir()
            stage.mkdir()
            (app_dir / "code-state").write_text("new")
            (app_dir / "venv").mkdir()
            (app_dir / "venv" / "identity").write_text("old-env")
            (stage / "venv").mkdir()
            (stage / "venv" / "identity").write_text("new-env")
            unit_path = root / "pinhaoke.service"
            unit_path.write_text("new-unit")
            unit_backup = stage / "old-unit"
            unit_backup.write_text("old-unit")
            systemctl_log = root / "systemctl.log"

            (bin_dir / "git").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == reset ]]; then printf '%s' \"$3\" >\"$APP_DIR/code-state\"; fi\n"
                "exit 0\n"
            )
            (bin_dir / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                "exit 0\n"
            )
            for stub in bin_dir.iterdir():
                stub.chmod(0o755)

            harness = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export APP_DIR={app_dir!s}
                export LIVE_VENV="$APP_DIR/venv"
                export UNIT_PATH={unit_path!s}
                export SYSTEMCTL_LOG={systemctl_log!s}
                export PATH={bin_dir!s}:$PATH
                source {ROOT / 'deploy/update.sh'}
                STAGE_DIR={stage!s}
                CANDIDATE_VENV="$STAGE_DIR/venv"
                TARGET_COMMIT=new
                PREVIOUS_COMMIT=old
                PREVIOUS_SERVICE_ACTIVE=1
                PREVIOUS_USES_LFS=0
                UNIT_PREVIOUSLY_EXISTED=1
                UNIT_BACKUP={unit_backup!s}
                ACTIVATION_STARTED=1
                apply_release_permissions() {{ :; }}
                swap_candidate_venv
                [[ $(<"$LIVE_VENV/identity") == new-env ]]
                rollback_activation test-failure
                [[ $(<"$APP_DIR/code-state") == old ]]
                [[ $(<"$LIVE_VENV/identity") == old-env ]]
                [[ $(<"$UNIT_PATH") == old-unit ]]
                find "$APP_DIR" -maxdepth 1 -type d -name '.venv-failed-*' | grep -q .
                grep -q '^stop pinhaoke$' "$SYSTEMCTL_LOG"
                grep -q '^daemon-reload$' "$SYSTEMCTL_LOG"
                grep -q '^start pinhaoke$' "$SYSTEMCTL_LOG"
                grep -q '^is-active --quiet pinhaoke$' "$SYSTEMCTL_LOG"
                """
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hup_before_venv_swap_restores_release_without_moving_live_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            bin_dir = root / "bin"
            stage = app_dir / ".deploy-stage.test"
            app_dir.mkdir()
            bin_dir.mkdir()
            stage.mkdir()
            (app_dir / "code-state").write_text("new")
            (app_dir / "venv").mkdir()
            (app_dir / "venv" / "identity").write_text("old-env")
            unit_path = root / "pinhaoke.service"
            unit_path.write_text("new-unit")
            unit_backup = stage / "old-unit"
            unit_backup.write_text("old-unit")
            systemctl_log = root / "systemctl.log"

            (bin_dir / "git").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == reset ]]; then printf '%s' \"$3\" >\"$APP_DIR/code-state\"; fi\n"
                "exit 0\n"
            )
            (bin_dir / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                "exit 0\n"
            )
            for stub in bin_dir.iterdir():
                stub.chmod(0o755)

            harness = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export APP_DIR={app_dir!s}
                export LIVE_VENV="$APP_DIR/venv"
                export UNIT_PATH={unit_path!s}
                export SYSTEMCTL_LOG={systemctl_log!s}
                export PATH={bin_dir!s}:$PATH
                source {ROOT / 'deploy/update.sh'}
                STAGE_DIR={stage!s}
                TARGET_COMMIT=new
                PREVIOUS_COMMIT=old
                PREVIOUS_SERVICE_ACTIVE=1
                UNIT_PREVIOUSLY_EXISTED=1
                UNIT_BACKUP={unit_backup!s}
                ACTIVATION_STARTED=1
                apply_release_permissions() {{ :; }}
                trap cleanup EXIT
                trap 'on_error "$LINENO"' ERR
                trap 'on_signal INT' INT
                trap 'on_signal TERM' TERM
                trap 'on_signal HUP' HUP
                kill -HUP $$
                """
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 129, result.stderr)
            self.assertEqual((app_dir / "venv" / "identity").read_text(), "old-env")
            self.assertEqual((app_dir / "code-state").read_text(), "old")
            self.assertEqual(unit_path.read_text(), "old-unit")
            self.assertFalse(list(app_dir.glob(".venv-failed-*")))
            calls = systemctl_log.read_text().splitlines()
            self.assertIn("start pinhaoke", calls)
            self.assertIn("is-active --quiet pinhaoke", calls)

    def test_unexpected_exit_after_activation_runs_exit_trap_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            bin_dir = root / "bin"
            stage = app_dir / ".deploy-stage.test"
            app_dir.mkdir()
            bin_dir.mkdir()
            stage.mkdir()
            (app_dir / "code-state").write_text("new")
            (app_dir / "venv").mkdir()
            (app_dir / "venv" / "identity").write_text("old-env")
            unit_path = root / "pinhaoke.service"
            unit_path.write_text("new-unit")
            unit_backup = stage / "old-unit"
            unit_backup.write_text("old-unit")
            systemctl_log = root / "systemctl.log"

            (bin_dir / "git").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == reset ]]; then printf '%s' \"$3\" >\"$APP_DIR/code-state\"; fi\n"
                "exit 0\n"
            )
            (bin_dir / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                "exit 0\n"
            )
            for stub in bin_dir.iterdir():
                stub.chmod(0o755)

            harness = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export APP_DIR={app_dir!s}
                export LIVE_VENV="$APP_DIR/venv"
                export UNIT_PATH={unit_path!s}
                export SYSTEMCTL_LOG={systemctl_log!s}
                export PATH={bin_dir!s}:$PATH
                source {ROOT / 'deploy/update.sh'}
                STAGE_DIR={stage!s}
                TARGET_COMMIT=new
                PREVIOUS_COMMIT=old
                PREVIOUS_SERVICE_ACTIVE=1
                UNIT_PREVIOUSLY_EXISTED=1
                UNIT_BACKUP={unit_backup!s}
                ACTIVATION_STARTED=1
                apply_release_permissions() {{ :; }}
                trap cleanup EXIT
                exit 42
                """
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertEqual((app_dir / "venv" / "identity").read_text(), "old-env")
            self.assertEqual((app_dir / "code-state").read_text(), "old")
            self.assertEqual(unit_path.read_text(), "old-unit")
            self.assertIn("start pinhaoke", systemctl_log.read_text().splitlines())

    def test_abandoned_artifact_cleanup_uses_separate_retention_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            old_stage = app_dir / ".deploy-stage.old"
            fresh_stage = app_dir / ".deploy-stage.fresh"
            old_failed = app_dir / ".venv-failed-old"
            fresh_failed = app_dir / ".venv-failed-fresh"
            for path in (old_stage, fresh_stage, old_failed, fresh_failed):
                path.mkdir()
            now = time.time()
            os.utime(old_stage, (now - 2 * 86400, now - 2 * 86400))
            os.utime(fresh_stage, (now, now))
            os.utime(old_failed, (now - 8 * 86400, now - 8 * 86400))
            os.utime(fresh_failed, (now, now))

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"APP_DIR={app_dir!s}; source {ROOT / 'deploy/update.sh'}; "
                    "cleanup_abandoned_artifacts",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(old_stage.exists())
            self.assertTrue(fresh_stage.exists())
            self.assertFalse(old_failed.exists())
            self.assertTrue(fresh_failed.exists())

    def test_service_runs_as_www_data_with_read_only_sandbox(self):
        for setting in (
            "User=www-data",
            "Group=www-data",
            "Environment=PYTHONDONTWRITEBYTECODE=1",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "RestrictSUIDSGID=true",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "UMask=0027",
            "ReadOnlyPaths=/opt/pinhaoke",
            "StateDirectory=pinhaoke",
            "StateDirectoryMode=0750",
            "Environment=PINHAOKE_MESSAGES_DB=/var/lib/pinhaoke/留言板.db",
            "Environment=PINHAOKE_STATS_DB=/var/lib/pinhaoke/访问统计.db",
            "Environment=PINHAOKE_ACCOUNTS_DB=/var/lib/pinhaoke/账户.db",
        ):
            self.assertIn(setting, self.service)
        self.assertNotIn("ReadWritePaths=/opt/pinhaoke", self.service)
        self.assertIn("127.0.0.1", self.service)

    def test_nginx_has_canonical_https_and_exact_certbot_paths(self):
        self.assertRegex(self.nginx, r"listen 80;")
        self.assertRegex(self.nginx, r"return 30[18] https://www\.pinhaoke\.love\$request_uri;")
        self.assertIn("listen 443 ssl", self.nginx)
        self.assertIn(
            "ssl_certificate /etc/letsencrypt/live/pinhaoke.love/fullchain.pem;",
            self.nginx,
        )
        self.assertIn(
            "ssl_certificate_key /etc/letsencrypt/live/pinhaoke.love/privkey.pem;",
            self.nginx,
        )

    def test_nginx_images_cache_and_gzip_are_safe(self):
        self.assertIn("alias /opt/pinhaoke/Images/;", self.nginx)
        self.assertIn('Cache-Control "public, max-age=2592000"', self.nginx)
        self.assertNotIn("immutable", self.nginx.lower())
        gzip_types = re.search(r"gzip_types\s+([^;]+);", self.nginx, re.DOTALL)
        self.assertIsNotNone(gzip_types)
        self.assertNotIn("text/html", gzip_types.group(1).split())

    def test_nginx_proxies_scheme_and_sets_security_headers(self):
        for setting in (
            "proxy_set_header X-Forwarded-Proto $scheme;",
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            'X-Content-Type-Options "nosniff"',
            'Referrer-Policy "strict-origin-when-cross-origin"',
            'X-Frame-Options "SAMEORIGIN"',
            'Strict-Transport-Security "max-age=',
        ):
            self.assertIn(setting, self.nginx)

    def test_deploy_readme_keeps_nginx_manual_and_documents_rollback(self):
        for text in (
            "nginx -t",
            "Certbot",
            "回滚",
            "deploy/update.sh",
            "不会",
        ):
            self.assertIn(text, self.deploy_readme)
        self.assertRegex(self.deploy_readme, r"手[动工]")
        self.assertRegex(self.deploy_readme, r"systemctl\s+(status|is-active)")

    def test_backup_units_are_hardened_and_root_scoped(self):
        service = self.backup_service
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=root", service)
        self.assertIn("Group=root", service)
        self.assertIn("SupplementaryGroups=www-data", service)
        # SQLite 只读连接也可能需要创建 sidecar；用服务身份创建，副本仍为 root-only。
        self.assertIn("ReadWritePaths=/var/backups/pinhaoke /var/lib/pinhaoke", service)
        self.assertNotIn("ReadOnlyPaths=/var/lib/pinhaoke", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertEqual(
            [line for line in service.splitlines() if line.startswith("CapabilityBoundingSet=")],
            ["CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_KILL"],
        )
        self.assertIn("AmbientCapabilities=\n", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("TimeoutStartSec=7min", service)
        self.assertIn(
            "ExecStart=/opt/pinhaoke/venv/bin/python /opt/pinhaoke/deploy/backup_databases.py",
            service,
        )
        for env in (
            "PINHAOKE_ACCOUNTS_DB=/var/lib/pinhaoke/账户.db",
            "PINHAOKE_MESSAGES_DB=/var/lib/pinhaoke/留言板.db",
            "PINHAOKE_STATS_DB=/var/lib/pinhaoke/访问统计.db",
            "PINHAOKE_BACKUP_DIR=/var/backups/pinhaoke",
        ):
            self.assertIn(env, service)
        timer = self.backup_timer
        self.assertIn("OnCalendar=", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_backup_installation_precreates_private_directory_and_requires_unit_upgrade(self):
        create = "sudo install -d -o root -g root -m 0700 /var/backups/pinhaoke"
        start = "sudo systemctl start pinhaoke-backup.service"
        self.assertIn(create, self.deploy_readme)
        self.assertLess(self.deploy_readme.index(create), self.deploy_readme.index(start))
        for fact in ("旧版备份单元", "不能只更新脚本", "只读核实", "获得发布授权",
                     "CAP_SETUID CAP_SETGID CAP_KILL", "SupplementaryGroups=www-data",
                     "清空子进程附加组", "mode=ro", "immutable=1"):
            self.assertIn(fact, self.deploy_readme)

    def test_backup_units_stay_out_of_update_script(self):
        # 备份单元一次性手工安装，更新脚本不得触碰它们，回滚路径零风险。
        self.assertNotIn("pinhaoke-backup", self.update)


if __name__ == "__main__":
    unittest.main()
