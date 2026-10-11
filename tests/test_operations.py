import io
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from rag.operations import backup, restore
from rag.resources import ModelOwnerLock, ResourceBusy


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.root=self.base/'source'
        (self.root/'data/topic').mkdir(parents=True)
        (self.root/'data/topic/a.txt').write_text('42',encoding='utf-8')
        (self.root/'prepared_tables').mkdir()
        with sqlite3.connect(self.root/'prepared_tables/a.sqlite') as db:
            db.execute('CREATE TABLE example(value INTEGER)'); db.execute('INSERT INTO example VALUES (42)')
        db.close()
        (self.root/'.env').write_text('API_KEY=never-back-up')
        self.archive=self.base/'backup.tar.gz'

    def test_round_trip_verifies_sqlite_and_excludes_secrets(self):
        backup(self.root,self.archive)
        target=self.base/'restored'
        restore(self.archive,target)
        self.assertEqual((target/'data/topic/a.txt').read_text(),'42')
        self.assertFalse((target/'.env').exists())

    def test_backup_requires_drained_owner(self):
        with ModelOwnerLock(self.root):
            with self.assertRaises(ResourceBusy): backup(self.root,self.archive)
        self.assertFalse(self.archive.exists())

    def test_existing_destination_is_never_overwritten(self):
        backup(self.root,self.archive)
        with self.assertRaises(FileExistsError): restore(self.archive,self.root)
        self.assertEqual((self.root/'data/topic/a.txt').read_text(),'42')

    def test_unsafe_archive_members_are_rejected(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),
                          ('data/link',tarfile.SYMTYPE),('C:/outside',tarfile.REGTYPE)]:
            with tarfile.open(self.archive,'w:gz') as archive:
                member=tarfile.TarInfo(name); member.type=kind; member.linkname='../../outside'
                archive.addfile(member)
            with self.assertRaises(ValueError): restore(self.archive,self.base/'restored')
            self.assertFalse((self.base/'restored').exists())

    def test_checksum_change_blocks_activation(self):
        backup(self.root,self.archive)
        changed=self.base/'changed.tar.gz'
        with tarfile.open(self.archive,'r:gz') as source, tarfile.open(changed,'w:gz') as output:
            for member in source:
                data=source.extractfile(member).read()
                if member.name=='data/topic/a.txt': data=b'99'
                member.size=len(data); output.addfile(member,io.BytesIO(data))
        with self.assertRaises(ValueError): restore(changed,self.base/'restored')
        self.assertFalse((self.base/'restored').exists())

    def test_invalid_sqlite_with_valid_checksum_blocks_activation(self):
        content=b'not a SQLite database'
        manifest={'schema_version':1,'files':{'prepared_tables/a.sqlite':{
            'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}}}
        with tarfile.open(self.archive,'w:gz') as output:
            for name,data in [('prepared_tables/a.sqlite',content),('manifest.json',json.dumps(manifest).encode())]:
                member=tarfile.TarInfo(name); member.size=len(data)
                output.addfile(member,io.BytesIO(data))
        with self.assertRaises(sqlite3.DatabaseError): restore(self.archive,self.base/'restored')
        self.assertFalse((self.base/'restored').exists())

    def test_restore_real_faiss_retrieval_without_model(self):
        from rag.embeddings import local_profile
        from rag.index_store import build_version,activate_bundle,current_bundle,load_bundle
        class Embedding:
            profile=local_profile('a'*40,{})
            def embed_documents(self,texts): return [[1.0]+[0.0]*383 for _ in texts]
        bundle=build_version(self.root,'topic',Embedding())
        activate_bundle(self.root,'topic',bundle)
        backup(self.root,self.archive)
        target=self.base/'restored'
        restore(self.archive,target)
        self.assertEqual(current_bundle(target,'topic'),bundle)
        index,rows=load_bundle(target,'topic',bundle)
        self.assertEqual(index.ntotal,len(rows))

    def test_load_report_has_counts_and_no_question_or_answer(self):
        from scripts.benchmark_rag import measure
        def call(number):
            if number==0: raise RuntimeError('private failure text')
            return {'index_version':'v1','answer':'private answer','question':'private question'}
        result=measure(call,30,2)
        self.assertEqual((result['completed'],result['errors']),(29,1))
        self.assertNotIn('private',json.dumps(result))
        self.assertFalse(result['quality_evaluated'])

    def test_stage_summary_uses_actual_usage_without_private_fields(self):
        from scripts.collect_metrics import summarize_stages
        path=self.base/'stages.jsonl'
        events=[{'stage':'generation','status':'ok','duration_ms':n,'input_tokens':None,
                 'output_tokens':3,'question':'private'} for n in range(1,31)]
        path.write_text('\n'.join(json.dumps(row) for row in events))
        result=summarize_stages(path)
        self.assertEqual(result['generation']['p95_ms'],29)
        self.assertEqual(result['generation']['output_tokens_observed'],90)
        self.assertIsNone(result['generation']['input_tokens_observed'])
        self.assertNotIn('private',json.dumps(result))

    def test_resource_snapshot_includes_index_disk_size(self):
        from rag.operations import resource_snapshot
        (self.root/'faiss_indexes').mkdir()
        (self.root/'faiss_indexes/index.faiss').write_bytes(b'12345')
        self.assertEqual(resource_snapshot(self.root)['index_bytes'],5)

    def test_metrics_rotate_at_three_files_and_missing_linux_counters_are_null(self):
        from scripts.collect_metrics import append_event
        from rag.operations import resource_snapshot
        from unittest.mock import patch
        log=self.base/'metrics.jsonl'
        for _ in range(4):
            log.write_bytes(b' '*(10*1024*1024))
            append_event(log,{'ready':False})
        self.assertEqual(len(list(self.base.glob('metrics.jsonl*'))),3)
        with patch.object(Path,'read_text',side_effect=FileNotFoundError): result=resource_snapshot(self.base)
        self.assertIsNone(result['cpu_steal_ticks'])
        self.assertIsNone(result['rss_bytes'])
