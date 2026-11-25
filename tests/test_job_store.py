import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from cctv_archive_manager.job_schema import initialize_job_database, connect_job_database
from cctv_archive_manager.job_store import JobStore, JobStateError
from cctv_archive_manager.merge_planner import MergeGroup
from cctv_archive_manager.daily_resume import _signature


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=self.root/'jobs.sqlite3'
        initialize_job_database(self.db)
        self.store=JobStore(self.db)
        self.source=self.root/'source'
        self.source.mkdir()
        files=[]
        for i in range(4):
            p=self.source/f'{i}.mkv'
            p.write_bytes(b'test')
            files.append(p)
        self.groups=[MergeGroup('2026-09-03',1,tuple(files[:2])),MergeGroup('2026-09-03',2,tuple(files[2:]))]

    def register(self,groups=None,**kw):
        return self.store.register(self.groups if groups is None else groups,source_root=self.source,catalog_path=self.root/'catalog.sqlite3',output_root=self.root/'output',parts_per_day=2,**kw)

    def active(self):
        j=self.register(); p,q=[r['part_id'] for r in self.store.parts(j)]
        self.store.start(j)
        return j,p,q

    def complete(self,j,p):
        self.store.start_part(j,p); self.store.verifying(j,p)
        self.store.complete_verified_part(j,p,output_bytes=10,output_modified_ns=123,output_sha256='a'*64,integrity_method='identify+sha256')

    def test_registration_persists_order_and_signature(self):
        j=self.register(); s=JobStore(self.db)
        self.assertEqual(s.unfinished()[0]['plan_signature'],_signature(self.groups))
        self.assertEqual([r['source_path'] for r in s.files(j)],[str(p.resolve()) for g in self.groups for p in g.files])
        self.assertEqual(len(s.events(j)),1)
        self.assertFalse((self.root/'output').exists())

    def test_completion_and_derived_file_status(self):
        j,p,q=self.active(); self.complete(j,p)
        self.assertEqual([r['part_status'] for r in self.store.files(j)],['COMPLETED']*2+['PENDING']*2)
        self.complete(j,q)
        self.assertEqual(self.store.unfinished(),[])
        self.assertEqual(self.store.events(j)[-1]['event_type'],'JOB_COMPLETED')

    def test_out_of_order_and_double_start(self):
        j,p,q=self.active()
        with self.assertRaises(JobStateError): self.store.start_part(j,q)
        self.store.start_part(j,p)
        with self.assertRaises(JobStateError): self.store.start_part(j,p)
        with self.assertRaises(JobStateError): self.store.start(j)
        self.assertEqual(self.store.parts(j)[0]['attempt_count'],1)

    def test_invalid_receipt(self):
        j,p,q=self.active(); self.store.start_part(j,p); self.store.verifying(j,p)
        with self.assertRaises(ValueError): self.store.complete_verified_part(j,p,output_bytes=10,output_modified_ns=123,output_sha256='z'*64,integrity_method='identify+sha256')
        self.assertEqual(self.store.parts(j)[0]['status'],'VERIFYING')

    def test_cannot_complete_without_verifying(self):
        j,p,q=self.active(); self.store.start_part(j,p)
        with self.assertRaises(JobStateError): self.store.complete_verified_part(j,p,output_bytes=10,output_modified_ns=123,output_sha256='a'*64,integrity_method='identify+sha256')

    def test_interruption_preserves_completed(self):
        j,p,q=self.active(); self.complete(j,p); self.store.start_part(j,q)
        self.assertEqual(JobStore(self.db).unfinished()[0]['status'],'RUNNING')
        self.store.stop(j,status='INTERRUPTED',message='owner confirmed stopped')
        self.assertEqual([r['status'] for r in self.store.parts(j)],['COMPLETED','INTERRUPTED'])
        self.assertEqual(self.store.unfinished()[0]['status'],'INTERRUPTED')

    def test_duplicate_source_rejected(self):
        with self.assertRaises(ValueError): self.register([self.groups[0],MergeGroup('2026-09-03',2,self.groups[0].files)])
        self.assertEqual(self.store.unfinished(),[])

    def test_invalid_job_type_rolls_back(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.register(job_type='bad')
        self.assertEqual(self.store.unfinished(),[])

    def test_cross_job_part_rejected(self):
        j,p,q=self.active(); other=self.register()
        with self.assertRaises(JobStateError): self.store.start_part(other,p)

    def test_event_failure_rolls_back_state(self):
        from unittest.mock import patch
        j,p,q=self.active()
        with patch.object(JobStore,'_event',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError): self.store.start_part(j,p)
        self.assertEqual(self.store.parts(j)[0]['status'],'PENDING')
        self.assertEqual(self.store.parts(j)[0]['attempt_count'],0)
