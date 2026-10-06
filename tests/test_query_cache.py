"""Read-only query caching must not cache mutable state or survive data updates."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import tests.test_app as app_tests


class QueryCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.database = self.root / '课程 #?%.db'
        shutil.copyfile(app.SUMMER_DB, self.database)
        patcher = patch.dict(app.TERM_DBS, {'summer': [('main', self.database, 's')]})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(app, 'MESSAGES_DB_PATH', self.root / 'messages.db')
        patcher.start(); self.addCleanup(patcher.stop)
        for cache in (app._course_page_rows, app._review_page, app._filter_options):
            cache.cache_clear()
            self.addCleanup(cache.cache_clear)

    def courses(self, **kwargs):
        return app_tests.CourseListTests.call(self, term='summer', page_size=5, **kwargs)

    def test_cache_reuses_source_rows_but_messages_and_response_lists_stay_fresh(self):
        with patch.object(app, 'get_db', wraps=app.get_db) as source:
            first = self.courses()
            self.assertEqual(source.call_count, 1)
            again = self.courses()
            self.assertEqual(source.call_count, 1)
        self.assertEqual(first, again)
        target = first['courses'][0]
        key = app._course_message_key(target['id'])
        app._insert_message('补充信息', 'test', course_key=key)
        target['course_type'].clear()
        target['course_name'] = '不能污染其他请求'
        with patch.object(app, 'get_db', side_effect=AssertionError('unexpected source read')):
            updated = self.courses()
        self.assertTrue(updated['courses'][0]['has_course_corrections'])
        self.assertEqual(updated['courses'][0]['course_type'], again['courses'][0]['course_type'])
        self.assertEqual(updated['courses'][0]['course_name'], again['courses'][0]['course_name'])

    def test_cache_invalidates_for_in_place_wal_and_atomic_replacement(self):
        self.courses()
        with sqlite3.connect(self.database) as writer:
            writer.execute("UPDATE basic_info SET notes='原位更新'")
        self.assertTrue(all(c['notes'] == '原位更新' for c in self.courses()['courses']))
        writer = sqlite3.connect(self.database)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE basic_info SET notes='WAL 更新'")
            writer.commit()
            self.assertTrue(all(c['notes'] == 'WAL 更新' for c in self.courses()['courses']))
        finally:
            writer.close()
        replacement = self.root / 'replacement.db'
        shutil.copyfile(app.SUMMER_DB, replacement)
        with sqlite3.connect(replacement) as writer:
            writer.execute("UPDATE basic_info SET notes='原子替换'")
        os.replace(replacement, self.database)
        self.assertTrue(all(c['notes'] == '原子替换' for c in self.courses()['courses']))
        self.assertEqual(app._course_page_rows.cache_info().misses, 4)

    def test_filter_cache_reuses_options_and_returns_independent_responses(self):
        with patch.object(app, 'get_db', wraps=app.get_db) as source:
            first = app.get_filters('summer')
            expected = json.loads(first.body)
            decoded = json.loads(first.body)
            decoded['course_types'].clear()
            again = app.get_filters('summer')
            self.assertEqual(json.loads(again.body), expected)
            self.assertEqual(first.headers['cache-control'], 'public, max-age=3600')
            self.assertEqual(source.call_count, 1)
            app.get_filters('fall')
            self.assertEqual(source.call_count, 2)
            self.assertEqual(json.loads(app.get_filters('summer').body), expected)
            self.assertEqual(source.call_count, 2)
        self.assertEqual(app._filter_options.cache_info().maxsize, 6)

    def test_filter_cache_invalidates_for_in_place_wal_and_atomic_replacement(self):
        app.get_filters('summer')
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute("UPDATE basic_info SET department='原位院系', schedule='每周周二13~14节'")
        payload = json.loads(app.get_filters('summer').body)
        self.assertEqual(payload['departments'], ['原位院系'])
        self.assertEqual(payload['periods'], ['13-14'])
        writer = sqlite3.connect(self.database)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE basic_info SET department='WAL院系', schedule='每周周三1~14节'")
            writer.commit()
            payload = json.loads(app.get_filters('summer').body)
            self.assertEqual(payload['departments'], ['WAL院系'])
            self.assertEqual(payload['periods'], ['1-14'])
        finally:
            writer.close()
        replacement = self.root / 'replacement.db'
        shutil.copyfile(app.SUMMER_DB, replacement)
        with closing(sqlite3.connect(replacement)) as writer, writer:
            writer.execute("UPDATE basic_info SET department='替换院系', schedule='每周周五7~8节'")
        os.replace(replacement, self.database)
        payload = json.loads(app.get_filters('summer').body)
        self.assertEqual(payload['departments'], ['替换院系'])
        self.assertEqual(payload['periods'], ['7-8'])
        self.assertEqual(app._filter_options.cache_info().misses, 4)

    def test_public_caches_follow_passive_and_truncate_wal_checkpoints(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE basic_info SET department='WAL院系'")
            writer.commit()
            filters = json.loads(app.get_filters('summer').body)
            courses = self.courses()
            revision = app._database_revision((self.database,))
            inode = self.database.stat().st_ino
            wal = Path(str(self.database) + '-wal')
            wal_size = wal.stat().st_size
            checkpoint = writer.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            self.assertEqual(checkpoint[0], 0)
            self.assertEqual(checkpoint[1], checkpoint[2])
            self.assertEqual(wal.stat().st_size, wal_size)
            self.assertNotEqual(app._database_revision((self.database,)), revision)
            self.assertEqual(json.loads(app.get_filters('summer').body), filters)
            self.assertEqual(self.courses(), courses)
            self.assertEqual(app._filter_options.cache_info().misses, 2)
            self.assertEqual(app._course_page_rows.cache_info().misses, 2)

            writer.execute("UPDATE basic_info SET department='检查点后院系'")
            writer.commit()
            filters = json.loads(app.get_filters('summer').body)
            courses = self.courses()
            self.assertEqual(filters['departments'], ['检查点后院系'])
            self.assertTrue(all(course['department'] == '检查点后院系' for course in courses['courses']))
            revision = app._database_revision((self.database,))
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
            self.assertEqual(wal.stat().st_size, 0)
            self.assertEqual(self.database.stat().st_ino, inode)
            self.assertNotEqual(app._database_revision((self.database,)), revision)
            self.assertEqual(json.loads(app.get_filters('summer').body), filters)
            self.assertEqual(self.courses(), courses)
            self.assertEqual(app._filter_options.cache_info().misses, 4)
            self.assertEqual(app._course_page_rows.cache_info().misses, 4)

    def test_late_filter_read_cannot_replace_a_newer_revision_cached_concurrently(self):
        ready = threading.Event()
        release = threading.Event()
        get_db = app.get_db

        @contextmanager
        def delayed_old_read(term):
            with get_db(term) as conn:
                yield conn
            if threading.current_thread().name.startswith('old-filter-read'):
                ready.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release old filter read')

        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE basic_info SET department='旧版院系'")
            writer.commit()
            with patch.object(app, 'get_db', side_effect=delayed_old_read) as source, \
                 ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-filter-read') as executor:
                pending = executor.submit(app.get_filters, 'summer')
                try:
                    self.assertTrue(ready.wait(5), 'old read did not reach the test gate')
                    writer.execute("UPDATE basic_info SET department='新版院系'")
                    writer.commit()
                    newer = app.get_filters('summer')
                    self.assertEqual(json.loads(newer.body)['departments'], ['新版院系'])
                finally:
                    release.set()
                self.assertEqual(json.loads(pending.result(timeout=5).body)['departments'], ['旧版院系'])
                self.assertEqual(app.get_filters('summer').body, newer.body)
                self.assertEqual(source.call_count, 2)
            self.assertEqual(app._filter_options.cache_info().misses, 2)
            self.assertEqual(app._filter_options.cache_info().hits, 1)

    def test_unfiltered_grouping_matches_selecting_every_group_and_excludes_null_keys(self):
        source, params, _ = app._build_course_query('summer', 'zh', {})
        # 模拟空分组键，确保跳过命中组回联后仍保留原来的 NULL 排除语义。
        source = source.replace('AS group_key', 'AS original_group_key')
        source = (
            f"SELECT s.*, CASE WHEN s.id='s1' THEN NULL ELSE s.original_group_key END AS group_key"
            f" FROM ({source}) s"
        )
        with app.get_db('summer') as conn:
            optimized = conn.execute(
                app._grouped_course_ctes(source, '') + ' SELECT * FROM grouped ORDER BY id', params,
            ).fetchall()
            all_selected = conn.execute(
                app._grouped_course_ctes(source, ' WHERE 1') + ' SELECT * FROM grouped ORDER BY id', params,
            ).fetchall()
        self.assertTrue(optimized)
        self.assertEqual([dict(row) for row in optimized], [dict(row) for row in all_selected])
        self.assertNotIn('s1', {row['id'] for row in optimized})

    def test_queries_and_pages_have_independent_entries(self):
        first = self.courses()
        second = self.courses(page=2)
        self.assertFalse({c['id'] for c in first['courses']} & {c['id'] for c in second['courses']})
        filtered = self.courses(q=first['courses'][0]['course_code'])
        self.assertTrue(all(c['course_code'] == first['courses'][0]['course_code'] for c in filtered['courses']))
        self.assertEqual(self.courses(), first)
        self.assertEqual(app._course_page_rows.cache_info().hits, 1)

    def test_review_cache_preserves_nested_data_and_tracks_revision(self):
        with patch.object(app, '_database_revision', return_value=('revision1',)), \
             patch.object(app, 'get_reviews_db', wraps=app.get_reviews_db) as source:
            first = app.list_reviews('高数', 1, 2)
            expected = json.loads(json.dumps(first))
            first['threads'][0]['entries'].clear()
            first['threads'][0]['highlights'].clear()
            self.assertEqual(app.list_reviews('高数', 1, 2), expected)
            self.assertEqual(source.call_count, 1)
        with patch.object(app, '_database_revision', return_value=('revision2',)), \
             patch.object(app, 'get_reviews_db', wraps=app.get_reviews_db) as source:
            self.assertEqual(app.list_reviews('高数', 1, 2), expected)
            self.assertEqual(source.call_count, 1)

    def test_sqlite_uri_escapes_paths_and_stays_read_only(self):
        with app.get_db('summer') as conn:
            self.assertGreater(conn.execute('SELECT count(*) FROM basic_info').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('CREATE TABLE forbidden(id)')

    def test_oversized_course_and_review_ids_never_reach_sqlite(self):
        for course_id in ('a' + str(2**63), 'u' + '9' * 5000):
            with patch.object(app, 'get_db') as source:
                with self.assertRaises(app.HTTPException) as caught:
                    app.get_course_detail(course_id, 'zh')
                self.assertEqual(caught.exception.status_code, 404)
                source.assert_not_called()
        with patch.object(app, 'get_reviews_db') as source:
            with self.assertRaises(app.HTTPException) as caught:
                app.get_review_thread(2**63)
            self.assertEqual(caught.exception.status_code, 422)
            source.assert_not_called()

    def test_invalid_unicode_is_rejected_before_search_or_message_storage(self):
        for call in (lambda: self.courses(q='\ud800'),
                     lambda: app.list_reviews('\ud800', 1, 20),
                     lambda: app._validate_message_content({'content': '\ud800'})):
            with self.assertRaises(app.HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 422)
