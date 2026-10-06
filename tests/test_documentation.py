import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MARKDOWN = {
    "README.md",
    "CLAUDE.md",
    "deploy/README.md",
    "北京大学选课网数据抓取/README.md",
    "北京大学课程数据翻译/README.md",
    "课程数据/数据说明.md",
    "归档/README.md",
}


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class DocumentationTests(unittest.TestCase):
    def test_only_required_tracked_markdown_files_remain(self):
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        tracked = {
            item.decode("utf-8")
            for item in result.stdout.split(b"\0")
            if item
        }
        self.assertEqual(tracked, EXPECTED_MARKDOWN)

    def test_local_markdown_links_resolve(self):
        link_pattern = re.compile(r"\[[^\]]+\]\((?!https?://)([^)#]+)(?:#[^)]+)?\)")
        for relative_path in EXPECTED_MARKDOWN:
            source = ROOT / relative_path
            for target in link_pattern.findall(read(relative_path)):
                destination = (source.parent / target).resolve()
                self.assertTrue(
                    destination.exists(),
                    f"broken link in {relative_path}: {target}",
                )

    def test_claude_is_the_engineering_contract(self):
        text = read("CLAUDE.md")
        self.assertIn(
            "This file provides guidance to Claude Code (claude.ai/code)", text
        )
        self.assertIn("README.md", text)
        self.assertIn("FastAPI", text)
        self.assertIn("无构建步骤", text)
        self.assertIn("五个课程 SQLite 数据库和一个树洞评测数据库", text)
        self.assertIn("## API 契约", text)
        self.assertIn("## 文档所有权", text)
        self.assertIn("七份 tracked Markdown", text)
        self.assertNotIn("AGENTS.md", text)

    def test_readme_has_https_terms_and_no_sponsor_display(self):
        text = read("README.md")
        self.assertIn("https://www.pinhaoke.love", text)
        self.assertIn("### 留言板", text)
        for fact in ("POST /api/messages/{message_id}/replies", "GET /api/changelog",
                     "POST /api/account/nickname", "路过的 PKUer", "历史发言", "changelog.json"):
            self.assertIn(fact, text)
        self.assertIn("公开留言", text)
        self.assertIn("### 访问统计", text)
        self.assertIn("### 收藏与账号", text)
        self.assertIn("多端同步", text)
        self.assertIn("个人中心", text)
        self.assertIn("多个收藏夹", text)
        self.assertIn("默认收藏夹", text)
        self.assertIn("POST /api/auth/register", text)
        self.assertIn("POST /api/favorites/remove", text)
        self.assertIn("POST /api/favorites/set-collections", text)
        self.assertIn("POST /api/collections", text)
        self.assertIn("### 关于项目", text)
        self.assertIn("## 反馈与联系", text)
        self.assertLess(text.index("2026 春季"), text.index("2026 暑期"))
        self.assertLess(text.index("2026 暑期"), text.index("2026 秋季"))
        self.assertIn("秋季为默认学期", text)
        self.assertIn("树洞课程评测", text)
        self.assertIn("上课节时", text)
        self.assertIn("独立入口", text)
        self.assertIn("手机端", text)
        self.assertIn("三个学期入口每行两个", text)
        self.assertIn("独立入口暂时隐藏", text)
        self.assertIn("关于本项目入口及面板暂时隐藏", text)
        self.assertIn("所有学期均关闭翻译", text)
        self.assertIn("课程名、教师名", text)
        self.assertIn("教师姓名拼音首字母都会加粗彩色高亮", text)
        self.assertIn("热度最高的 `24` 门课程", text)
        self.assertIn("“热门课程”", text)
        self.assertIn("不显示输入联想", text)
        self.assertIn("数据范围 `2022-12-21` 至 `2026-07-13`", text)
        self.assertIn("评测数据量 `90880`", text)
        self.assertIn("`47843` 个主题加 `43037` 条相关回复", text)
        self.assertIn("点击卡片", text)
        self.assertIn("210570", text)
        self.assertIn("## 技术架构", text)
        self.assertIn("## 网页设计", text)
        self.assertIn("无构建步骤", text)
        self.assertIn("最大内容宽度为 `1120px`", text)
        self.assertIn("prefers-reduced-motion", text)
        self.assertIn("AbortController", text)
        self.assertIn("完整树洞仅指输入快照", text)
        self.assertNotIn("赞助", text)
        self.assertNotIn("sponsor", text)

    def test_claude_matches_engineering_contracts(self):
        text = read("CLAUDE.md")
        for fact in (
            "GET /api/health",
            "mode=ro",
            "PRAGMA query_only = ON",
            "a<id>",
            "r<id>",
            "u<id>",
            "g<id>",
            "s<id>",
            "fall=4529",
            "spring=3701",
            "summer=160",
            "GET /api/reviews",
            "GET /api/reviews/{pid}",
            "GET /api/review-courses",
            "GET /api/messages",
            "POST /api/messages",
            "GET /api/messages/{message_id}/replies",
            "POST /api/messages/{message_id}/replies",
            "GET /api/changelog",
            "POST /api/account/nickname",
            "NICKNAME_MAX_LENGTH",
            "账户库版本 4",
            "GET /api/stats",
            "赞助面板",
            "tip-pinned",
            "`periods`",
            "`period`",
            "上课节时",
            "星期几",
            "两节课的区间",
            "PINHAOKE_MESSAGES_DB",
            "<库名>.init.lock",
            "flock",
            "等待文件锁时不持有 SQLite 读锁",
            "PINHAOKE_STATS_DB",
            "PINHAOKE_ACCOUNTS_DB",
            "StateDirectory",
            "StateDirectoryMode=0750",
            "GET /api/account",
            "POST /api/auth/register",
            "POST /api/auth/login",
            "POST /api/auth/reset/questions",
            "POST /api/auth/reset",
            "POST /api/auth/delete",
            "GET /api/favorites",
            "POST /api/favorites/remove",
            "hashlib.scrypt",
            "pinhaoke_session",
            "pinhaoke_fav_mode",
            "_require_trusted_origin",
            "FAVORITES_MAX = 300",
            "COLLECTIONS_MAX",
            "favorite_collections",
            "collections",
            "POST /api/collections",
            "POST /api/favorites/set-collections",
            "accountView",
            "个人",
            "is_default",
            "默认收藏夹",
            "账号与收藏契约",
            "renderFavoritesPanel()",
            "热门课程",
            "树洞课程评测.db",
            "47843",
            "90880",
            "210570",
            "thread_replies",
            "--enrich-thread-replies",
            "entry_highlights",
            "entity_aliases",
            "135241",
            "53518",
            "56168",
            "27962",
            "## 前端与网页设计契约",
            "1120px",
            "1.5px",
            "不使用左侧彩条",
            "AbortController",
            "lru_cache(maxsize=32)",
            "_filter_options",
            "lru_cache(maxsize=6)",
            "无筛选时全部非空分组已命中",
            "_database_revision",
            "_grouped_course_ctes",
            "_valid_text",
            "2**63 - 1",
            "重试同页",
            "逐次应用成功结果",
            "textContent",
            "inert",
            "1440px",
            "390px",
            "320px",
            "python3 -m unittest discover -s tests -v",
            "atomic_database",
            "addToPlan.do",
            "deploy/update.sh",
            "pinhaoke-backup.timer",
            "/var/backups/pinhaoke",
        ):
            self.assertIn(fact, text)
        self.assertNotIn("There is no test suite", text)

    def test_scrape_guide_covers_safe_current_chrome_workflow(self):
        text = read("北京大学选课网数据抓取/README.md")
        for fact in (
            "已登录的 Chrome 当前页面",
            "一次性 token",
            "本地网络访问",
            "开课单位=ALL",
            "翻页",
            "课程详情",
            "严格校验",
            "addToPlan.do",
            "加入选课计划",
            "build_summer_db.py",
            "build_undergrad_2627_fall_db.py",
            "build_graduate_2627_fall_db.py",
        ):
            self.assertIn(fact, text)

    def test_translation_guide_has_matrix_counts_and_selectors(self):
        text = read("北京大学课程数据翻译/README.md")
        for fact in (
            "100156",
            "39445",
            "10010",
            "159138",
            "52430",
            "--only",
            "--db",
            "--phase",
            "数据库锁",
            "回退原始中文",
            "绝不自动运行付费 API",
        ):
            self.assertIn(fact, text)

    def test_data_guide_has_raw_and_merged_counts(self):
        text = read("课程数据/数据说明.md")
        for fact in (
            "2465",
            "1379",
            "194",
            "3032",
            "3152",
            "20260907合并",
            "1611",
            "春季 | 3701",
            "暑期 | 160",
            "秋季 | 4529",
            "basic_info",
            "detail_info",
            "translations",
            "3561472",
            "15245822",
            "95.24%",
            "47843",
            "90880",
            "210570",
            "thread_replies",
            "--enrich-thread-replies",
            "course_catalog",
            "entry_highlights",
            "entity_aliases",
            "135241",
            "53518",
            "56168",
            "27962",
            "## 页面呈现口径",
            "GET /api/reviews/{pid}",
            "不加入页面评测数据量",
        ):
            self.assertIn(fact, text)

    def test_deploy_and_archive_boundaries_are_explicit(self):
        deploy = read("deploy/README.md")
        for fact in (
            "https://www.pinhaoke.love",
            "sudo bash /opt/pinhaoke/deploy/update.sh",
            "停服前",
            "自动回滚",
            "journalctl -u pinhaoke",
            "/api/health",
            "/api/reviews?page_size=1",
            "/api/reviews/{pid}",
            "StateDirectory=pinhaoke",
            "PINHAOKE_MESSAGES_DB",
            "PINHAOKE_ACCOUNTS_DB",
            "StateDirectoryMode=0750",
            "五类本机 API 契约",
            "review-detail 五类烟测",
            "## 数据备份",
            "pinhaoke-backup.timer",
            "backup_databases.py",
        ):
            self.assertIn(fact, deploy)
        self.assertNotIn("本次任务未部署生产", deploy)
        self.assertNotIn("也没有 push", deploy)
        archive = read("归档/README.md")
        self.assertIn("V1", archive)
        self.assertIn("只读参考", archive)
        self.assertIn("严禁用于生产", archive)

    def test_gitignore_excludes_all_writable_databases(self):
        lines = read(".gitignore").splitlines()
        for pattern in ("留言板.db*", "访问统计.db*", "账户.db*"):
            self.assertIn(pattern, lines)
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.db"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        tracked = {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
        writable = {"留言板.db", "访问统计.db", "账户.db"}
        self.assertFalse({Path(path).name for path in tracked} & writable, tracked)

    def test_documents_do_not_use_relative_time_words(self):
        forbidden = re.compile(
            r"今天|昨天|刚刚|最近|上周|下周|目前|近期|today|yesterday|recently",
            re.IGNORECASE,
        )
        for relative_path in EXPECTED_MARKDOWN:
            self.assertIsNone(
                forbidden.search(read(relative_path)),
                f"relative time found in {relative_path}",
            )


if __name__ == "__main__":
    unittest.main()
