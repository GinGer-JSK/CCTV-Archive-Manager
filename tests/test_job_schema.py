import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cctv_archive_manager.job_schema import (
    APPLICATION_ID, SCHEMA_VERSION, JobSchemaError,
    initialize_job_database, connect_job_database,
)


class JobSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'jobs.sqlite3'

    def open(self):
        return closing(connect_job_database(self.db))

    def seed(self, conn, job='j1'):
        conn.execute('''INSERT INTO merge_jobs
            (job_id,job_type,source_root,catalog_path,output_root,start_date,end_date,
             split_method,parts_per_day,plan_signature,status,created_at,updated_at)
            VALUES (?, 'daily','source','catalog','output','2026-09-03','2026-09-03',
                    'count',4,?,'PENDING','now','now')''', (job, 'a'*64))
        return conn.execute('''INSERT INTO merge_parts
            (job_id,record_date,part_number,sequence_no,output_name,status)
            VALUES (?, '2026-09-03',1,1,'part01.mkv','PENDING')''', (job,)).lastrowid

    def test_initialize_and_reopen(self):
        initialize_job_database(self.db)
        initialize_job_database(self.db)
        with self.open() as conn:
            self.assertEqual(conn.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table'").fetchone()[0], 4)

    def test_catalog_rejected_without_mutation(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE recordings(path TEXT PRIMARY KEY)')
            conn.execute("INSERT INTO recordings VALUES ('a')")
            conn.commit()
        original = self.db.read_bytes()
        with self.assertRaises(JobSchemaError):
            initialize_job_database(self.db)
        self.assertEqual(self.db.read_bytes(), original)

    def test_future_version_rejected(self):
        initialize_job_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA user_version = 99')
        with self.assertRaises(JobSchemaError):
            initialize_job_database(self.db)

    def test_missing_db_not_created_by_connect(self):
        with self.assertRaises(sqlite3.OperationalError):
            connect_job_database(self.db)
        self.assertFalse(self.db.exists())

    def test_schema_drift_rejected(self):
        initialize_job_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP INDEX idx_merge_jobs_status')
        with self.assertRaises(JobSchemaError):
            connect_job_database(self.db)

    def test_completed_requires_integrity_metadata(self):
        initialize_job_database(self.db)
        with self.open() as conn:
            part = self.seed(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE merge_parts SET status='COMPLETED' WHERE part_id=?", (part,))
            conn.execute('''UPDATE merge_parts SET status='COMPLETED', completed_at='now',
                output_bytes=100, output_modified_ns=123, output_sha256=?,
                integrity_method='identify+sha256' WHERE part_id=?''', ('a'*64, part))

    def test_invalid_status_rejected(self):
        initialize_job_database(self.db)
        with self.open() as conn:
            self.seed(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE merge_jobs SET status='PASS'")

    def test_file_and_event_foreign_keys(self):
        initialize_job_database(self.db)
        with self.open() as conn:
            part = self.seed(conn)
            other = self.seed(conn, 'j2')
            conn.execute('INSERT INTO merge_part_files(part_id,sequence_no,source_path,size_bytes,modified_ns) VALUES (?,1,?,100,1)', (part,'a.mkv'))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO merge_part_files(part_id,sequence_no,source_path,size_bytes,modified_ns) VALUES (999,1,'a',100,1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO merge_job_events(job_id,part_id,event_type,created_at) VALUES ('j1',?,'test','now')", (other,))
            conn.execute("INSERT INTO merge_job_events(job_id,part_id,event_type,created_at) VALUES ('j1',?,'test','now')", (part,))
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_duplicate_output_case_insensitive(self):
        initialize_job_database(self.db)
        with self.open() as conn:
            self.seed(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO merge_parts(job_id,record_date,part_number,sequence_no,output_name,status) VALUES ('j1','2026-09-03',2,2,'PART01.MKV','PENDING')")

    def test_rollback_preserves_atomicity(self):
        initialize_job_database(self.db)
        with self.open() as conn:
            with self.assertRaises(RuntimeError):
                with conn:
                    self.seed(conn)
                    raise RuntimeError('simulated failure')
            self.assertEqual(conn.execute('SELECT count(*) FROM merge_jobs').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM merge_parts').fetchone()[0], 0)
