# -*- coding: utf-8 -*-
import unittest,tempfile,sqlite3,json,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.store import SCHEMA,publish,connect,activate,materialize,check,suite_lock,shared_consistency,input_fingerprint

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'database').mkdir();self.db=self.root/'database/master.db'
        with sqlite3.connect(self.db) as d:d.executescript(SCHEMA)
        self.file=self.root/'new.csv';self.dataset='load_forecast/data/portfolio.csv'
        self.base='entity_id,date,hour,value,unit,available_time,version\nportfolio,2026-08-01,0,1.2,MWh,,original\n'
        self.file.write_text(self.base)
        self.item={'dataset':self.dataset,'file':str(self.file)}
        self.original=publish([self.item],'initial',self.db,bootstrap=True)['items'][0]['version']
    def tearDown(self):self.tmp.cleanup()
    def count(self):
        with connect(self.db) as d:return d.execute('SELECT count(*) FROM input_versions').fetchone()[0]
    def test_duplicate_import_is_idempotent(self):
        self.assertEqual(publish([self.item],'repeat',self.db)['items'][0]['status'],'unchanged');self.assertEqual(self.count(),1)
    def test_updated_data_retains_version_and_backup(self):
        self.file.write_text(self.base.replace('1.2','2.3'));r=publish([self.item],'correction',self.db)
        self.assertTrue(Path(r['backup']).exists());self.assertEqual(self.count(),2)
        activate({self.dataset:self.original},'rollback',self.db)
        materialize(self.root);self.assertEqual((self.root/self.dataset).read_text(),self.base)
    def test_duplicate_keys_rollback(self):
        self.file.write_text(self.base+self.base.splitlines()[1]+'\n')
        with self.assertRaises(ValueError):publish([self.item],'bad duplicate',self.db)
        self.assertEqual(self.count(),1)
    def test_schema_change_rejected(self):
        self.file.write_text(self.base.replace('value','changed'))
        with self.assertRaises(ValueError):publish([self.item],'schema',self.db)
        self.assertEqual(self.count(),1)
    def test_wrong_unit_rejected(self):
        self.file.write_text(self.base.replace('MWh','kWh'))
        with self.assertRaises(ValueError):publish([self.item],'unit',self.db)
    def test_batch_is_atomic(self):
        self.file.write_text(self.base.replace('1.2','2.3'))
        with self.assertRaises(ValueError):publish([self.item,{'dataset':'unknown/file.csv','file':str(self.file)}],'bad batch',self.db)
        self.assertEqual(self.count(),1)
    def test_edited_or_deleted_cache_recovers_exact_bytes(self):
        materialize(self.root);p=self.root/self.dataset;p.write_text('tampered')
        materialize(self.root);self.assertEqual(p.read_text(),self.base)
        p.unlink();materialize(self.root);self.assertEqual(p.read_text(),self.base)
    def test_available_time_can_be_added_to_initially_blank_column(self):
        self.file.write_text(self.base.replace('MWh,,','MWh,2026-08-06 00:00:00,'))
        publish([self.item],'actual receipt',self.db)
        with connect(self.db) as d:
            self.assertEqual(d.execute('SELECT available_time FROM current_load_portfolio').fetchone()[0],'2026-08-06 00:00:00')
    def test_content_corruption_detected(self):
        with connect(self.db) as d:d.execute("UPDATE input_versions SET payload=X'00'")
        with connect(self.db) as d:
            with self.assertRaises(ValueError):check(d)
    def test_cross_project_conflict_rejects_batch(self):
        source=self.root/'actual.csv';source.write_text('timestamp,actual_load\n2026-08-01 00:00:00,1.2\n')
        publish([{'dataset':'coefficient_opt/data/actual_load.csv','file':str(source)}],'add alias',self.db,bootstrap=True)
        self.file.write_text(self.base.replace('1.2','2.3'))
        with self.assertRaises(ValueError):publish([self.item],'unsynced',self.db)
        self.assertEqual(self.count(),2)
    def test_wrong_version_cannot_be_activated(self):
        with self.assertRaises(ValueError):activate({self.dataset:999},'bad',self.db)
    def test_non_owner_cannot_enter_lock(self):
        with suite_lock(self.root):
            token=os.environ.pop('POWER_SUITE_LOCK_TOKEN')
            try:
                with self.assertRaises(ValueError):
                    with suite_lock(self.root):pass
            finally:os.environ['POWER_SUITE_LOCK_TOKEN']=token
    def test_non_hourly_actual_timestamp_rejected(self):
        source=self.root/'actual.csv';source.write_text('timestamp,actual_load\n2026-08-01 00:15:00,1.2\n')
        with self.assertRaises(ValueError):publish([{'dataset':'coefficient_opt/data/actual_load.csv','file':str(source)}],'bad time',self.db,bootstrap=True)
        self.assertEqual(self.count(),1)
    def test_negative_reconciliation_load_rejected(self):
        source=self.root/'actual.csv';source.write_text('timestamp,actual_load\n2026-08-01 00:00:00,-1.2\n')
        with self.assertRaises(ValueError):publish([{'dataset':'coefficient_opt/data/actual_load.csv','file':str(source)}],'bad load',self.db,bootstrap=True)
        self.assertEqual(self.count(),1)
    def test_shared_input_update_invalidates_tuning_cache(self):
        before=input_fingerprint(self.db)
        self.file.write_text(self.base.replace('1.2','2.3'));publish([self.item],'changed input',self.db)
        self.assertNotEqual(before,input_fingerprint(self.db))
        activate({self.dataset:self.original},'restore input',self.db)
        self.assertEqual(before,input_fingerprint(self.db))

if __name__=='__main__':unittest.main(verbosity=2)
