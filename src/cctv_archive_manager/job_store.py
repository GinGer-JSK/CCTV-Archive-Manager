"""Transactional job ledger. No merging, lock recovery or GUI side effects.

The execution coordinator must own the output lock before changing execution
state. A RUNNING row alone is never evidence of an abandoned process.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .job_schema import connect_job_database
from .merge_planner import MergeGroup


class JobStateError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database):
        self.database = Path(database).resolve()

    @contextmanager
    def _transaction(self):
        with closing(connect_job_database(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _event(conn, job_id, part_id, kind, old, new, message=None):
        conn.execute('''INSERT INTO merge_job_events
            (job_id,part_id,event_type,previous_status,new_status,message,created_at)
            VALUES (?,?,?,?,?,?,?)''', (job_id,part_id,kind,old,new,message,_now()))

    def register(self, groups, *, source_root, catalog_path, output_root,
                 job_type='daily', split_method='count', parts_per_day=1):
        """Persist ordered source metadata and plan, without touching outputs."""
        groups = tuple(groups)
        if not groups or type(parts_per_day) is not int or parts_per_day < 1:
            raise ValueError('invalid plan')
        source = Path(source_root).resolve()
        output = Path(output_root).resolve()
        seen, names, data, snapshots = set(), set(), [], []
        for group in groups:
            if not isinstance(group, MergeGroup) or len(group.files) < 2:
                raise ValueError('invalid group')
            if date.fromisoformat(group.date).isoformat() != group.date or type(group.part_number) is not int or group.part_number < 1:
                raise ValueError('invalid group date or number')
            name = group.output_name
            if name.casefold() in names:
                raise ValueError('duplicate output')
            names.add(name.casefold())
            inputs, rows = [], []
            for item in group.files:
                p = Path(item).resolve(strict=True)
                key = str(p).casefold()
                if key in seen or not p.is_relative_to(source) or not p.is_file() or p.suffix.lower() != '.mkv':
                    raise ValueError('invalid or duplicate source')
                seen.add(key)
                st = p.stat()
                if st.st_size <= 0 or key == str(output / name).casefold():
                    raise ValueError('empty source or output collision')
                inputs.append([key, st.st_size, st.st_mtime_ns])
                rows.append((str(p), st.st_size, st.st_mtime_ns))
            data.append([group.date, group.part_number, name, inputs])
            snapshots.append(rows)
        if seen.intersection(str(output / g.output_name).casefold() for g in groups):
            raise ValueError('output collides with source')
        signature = hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        job_id, now = uuid4().hex, _now()
        with self._transaction() as c:
            c.execute('''INSERT INTO merge_jobs
                (job_id,job_type,source_root,catalog_path,output_root,start_date,end_date,
                 split_method,parts_per_day,plan_signature,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)''',
                (job_id,job_type,str(source),str(Path(catalog_path).resolve()),str(output),
                 min(g.date for g in groups),max(g.date for g in groups),split_method,parts_per_day,signature,now,now))
            for seq,(g,rows) in enumerate(zip(groups,snapshots),1):
                part = c.execute('''INSERT INTO merge_parts
                    (job_id,record_date,part_number,sequence_no,output_name,status)
                    VALUES (?,?,?,?,?,'PENDING')''',(job_id,g.date,g.part_number,seq,g.output_name)).lastrowid
                c.executemany('''INSERT INTO merge_part_files
                    (part_id,sequence_no,source_path,size_bytes,modified_ns) VALUES (?,?,?,?,?)''',
                    [(part,i,*row) for i,row in enumerate(rows,1)])
            self._event(c,job_id,None,'JOB_REGISTERED',None,'PENDING')
        return job_id

    def _job(self,c,job_id):
        row = c.execute('SELECT * FROM merge_jobs WHERE job_id=?',(job_id,)).fetchone()
        if row is None:
            raise JobStateError('unknown job')
        return row

    def _part(self,c,job_id,part_id):
        row = c.execute('SELECT * FROM merge_parts WHERE job_id=? AND part_id=?',(job_id,part_id)).fetchone()
        if row is None:
            raise JobStateError('unknown part for job')
        return row

    def start(self,job_id):
        """Initial execution only; recovery authorization is a later layer."""
        with self._transaction() as c:
            j=self._job(c,job_id)
            if j['status']!='PENDING':
                raise JobStateError('job is not pending')
            now=_now()
            c.execute("UPDATE merge_jobs SET status='RUNNING',started_at=?,updated_at=? WHERE job_id=?",(now,now,job_id))
            self._event(c,job_id,None,'JOB_STARTED','PENDING','RUNNING')

    def start_part(self,job_id,part_id):
        with self._transaction() as c:
            j,p=self._job(c,job_id),self._part(c,job_id,part_id)
            first=c.execute("SELECT part_id FROM merge_parts WHERE job_id=? AND status!='COMPLETED' ORDER BY sequence_no LIMIT 1",(job_id,)).fetchone()
            if j['status']!='RUNNING' or p['status']!='PENDING' or first is None or first[0]!=part_id:
                raise JobStateError('part cannot start')
            now=_now()
            c.execute("UPDATE merge_parts SET status='RUNNING',attempt_count=attempt_count+1,started_at=? WHERE part_id=?",(now,part_id))
            c.execute('UPDATE merge_jobs SET updated_at=? WHERE job_id=?',(now,job_id))
            self._event(c,job_id,part_id,'PART_STARTED','PENDING','RUNNING')

    def verifying(self,job_id,part_id):
        with self._transaction() as c:
            j,p=self._job(c,job_id),self._part(c,job_id,part_id)
            if j['status']!='RUNNING' or p['status']!='RUNNING':
                raise JobStateError('part is not running')
            c.execute("UPDATE merge_parts SET status='VERIFYING' WHERE part_id=?",(part_id,))
            c.execute("UPDATE merge_jobs SET status='VERIFYING',updated_at=? WHERE job_id=?",(_now(),job_id))
            self._event(c,job_id,part_id,'PART_VERIFYING','RUNNING','VERIFYING')
            self._event(c,job_id,None,'JOB_VERIFYING','RUNNING','VERIFYING')

    def complete_verified_part(self,job_id,part_id,*,output_bytes,output_modified_ns,output_sha256,integrity_method):
        """Record trusted engine verification receipt, not verify media itself."""
        if type(output_bytes) is not int or output_bytes<=0 or type(output_modified_ns) is not int or not isinstance(output_sha256,str) or not re.fullmatch('[0-9a-f]{64}',output_sha256) or integrity_method not in ('identify+sha256','decode+sha256'):
            raise ValueError('invalid verification receipt')
        with self._transaction() as c:
            j,p=self._job(c,job_id),self._part(c,job_id,part_id)
            if j['status']!='VERIFYING' or p['status']!='VERIFYING':
                raise JobStateError('part is not verifying')
            now=_now()
            c.execute("""UPDATE merge_parts SET status='COMPLETED',completed_at=?,output_bytes=?,
                output_modified_ns=?,output_sha256=?,integrity_method=? WHERE part_id=?""",
                (now,output_bytes,output_modified_ns,output_sha256,integrity_method,part_id))
            self._event(c,job_id,part_id,'PART_COMPLETED','VERIFYING','COMPLETED')
            remaining=c.execute("SELECT COUNT(*) FROM merge_parts WHERE job_id=? AND status!='COMPLETED'",(job_id,)).fetchone()[0]
            state='RUNNING' if remaining else 'COMPLETED'
            c.execute('UPDATE merge_jobs SET status=?,updated_at=?,completed_at=? WHERE job_id=?',(state,now,None if remaining else now,job_id))
            self._event(c,job_id,None,'JOB_CONTINUED' if remaining else 'JOB_COMPLETED','VERIFYING',state)

    def stop(self,job_id,*,status,message):
        """Caller must establish execution stopped (or ownership) first."""
        if status not in ('PAUSED','FAILED','INTERRUPTED','BLOCKED'):
            raise ValueError('invalid stop status')
        with self._transaction() as c:
            j=self._job(c,job_id)
            if j['status'] not in ('RUNNING','VERIFYING'):
                raise JobStateError('job is not active')
            for p in c.execute("SELECT part_id,status FROM merge_parts WHERE job_id=? AND status IN ('RUNNING','VERIFYING')",(job_id,)).fetchall():
                c.execute('UPDATE merge_parts SET status=?,last_error=? WHERE part_id=?',(status,message,p[0]))
                self._event(c,job_id,p[0],'PART_STOPPED',p[1],status,message)
            c.execute('UPDATE merge_jobs SET status=?,updated_at=?,last_error=? WHERE job_id=?',(status,_now(),message,job_id))
            self._event(c,job_id,None,'JOB_STOPPED',j['status'],status,message)

    def _read(self,sql,args=()):
        with closing(connect_job_database(self.database)) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute(sql,args)]

    def unfinished(self):
        return self._read("SELECT * FROM merge_jobs WHERE status NOT IN ('COMPLETED','CANCELLED') ORDER BY created_at,job_id")

    def parts(self,job_id):
        return self._read('SELECT * FROM merge_parts WHERE job_id=? ORDER BY sequence_no',(job_id,))

    def files(self,job_id):
        return self._read('''SELECT f.*,p.status AS part_status FROM merge_part_files f
            JOIN merge_parts p ON p.part_id=f.part_id WHERE p.job_id=? ORDER BY p.sequence_no,f.sequence_no''',(job_id,))

    def events(self,job_id):
        return self._read('SELECT * FROM merge_job_events WHERE job_id=? ORDER BY event_id',(job_id,))
