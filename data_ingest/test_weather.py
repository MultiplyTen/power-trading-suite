import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from database import weather as wx

class WeatherTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'master.db'
  self.locs=pd.DataFrame([dict(location_id='guangzhou',city_name='广州',region='珠三角',latitude=23.1291,longitude=113.2644)])
  self.start=pd.Timestamp('2026-09-17');self.end=pd.Timestamp('2026-09-25');idx=pd.date_range(self.start,self.end+pd.Timedelta(hours=23),freq='h')
  self.item=dict(latitude=23.13,longitude=113.26,utc_offset_seconds=28800,hourly_units=wx.UNITS,hourly={'time':[str(x) for x in idx]})
  for k,v in zip(wx.VARS,[28,70,0,500,3,40,1]):self.item['hourly'][k]=[v]*len(idx)
  self.raw=json.dumps([self.item]).encode();self.f=wx.validate(self.raw,self.locs,self.start,self.end)
  d=sqlite3.connect(self.path);wx.ensure(d);d.commit();d.close()
 def tearDown(self):self.tmp.cleanup()
 def insert(self,received='2026-09-17 08:00:00',bid='one'):
  d=sqlite3.connect(self.path);d.execute('INSERT INTO weather_batches VALUES(?,?,?,?,?,?,?,?,?)',(bid,received,'url','sha',self.raw,str(self.start),str(self.end),'["guangzhou"]',len(self.f)))
  rows=[(bid,r.location_id,r.city_name,r.region,str(r.valid_time),*[getattr(r,v) for v in wx.VARS]) for r in self.f.itertuples()];d.executemany('INSERT INTO weather_points VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',rows);d.commit();d.close()
 def test_valid_coverage(self):self.assertEqual(len(self.f),216)
 def test_units(self):
  item=copy.deepcopy(self.item);item['hourly_units']['wind_speed_100m']='km/h'
  with self.assertRaises(ValueError):wx.validate(json.dumps(item),self.locs,self.start,self.end)
 def test_missing_hour(self):
  item=copy.deepcopy(self.item)
  for k in item['hourly']:item['hourly'][k].pop()
  with self.assertRaises(ValueError):wx.validate(json.dumps(item),self.locs,self.start,self.end)
 def test_null(self):
  item=copy.deepcopy(self.item);item['hourly']['temperature_2m'][0]=None
  with self.assertRaises(ValueError):wx.validate(json.dumps(item),self.locs,self.start,self.end)
 def test_wrong_city(self):
  item=copy.deepcopy(self.item);item['latitude']=40
  with self.assertRaises(ValueError):wx.validate(json.dumps(item),self.locs,self.start,self.end)
 def test_no_future_receipt(self):
  self.insert();self.assertTrue(wx.select(self.path,'2026-09-17 07:59','2026-09-18','2026-09-18 23:00').empty)
  self.assertEqual(len(wx.select(self.path,'2026-09-17 09:00','2026-09-18','2026-09-18 23:00')),24)
 def test_expired(self):
  self.insert();self.assertTrue(wx.select(self.path,'2026-09-18 09:00','2026-09-19','2026-09-19 23:00').empty)
 def test_old_version_selected(self):
  self.insert();self.insert('2026-09-17 10:00:00','late')
  f=wx.select(self.path,'2026-09-17 09:00','2026-09-18','2026-09-18 23:00');self.assertTrue(f.retrieved_at.eq(pd.Timestamp('2026-09-17 08:00')).all())
 def test_missing_city(self):
  self.insert();self.assertTrue(wx.select(self.path,'2026-09-17 09:00','2026-09-18','2026-09-18 23:00',required_ids=['shenzhen']).empty)
 def test_failure_does_not_delete(self):
  self.insert()
  with patch.object(wx,'locations',return_value=self.locs),patch.object(wx,'now',return_value=pd.Timestamp('2026-09-17 09:00')):
   r=wx.refresh(self.path,{'weather_auto':{'retries':1}},force=True,fetcher=lambda *_:(_ for _ in ()).throw(OSError('offline')))
  self.assertEqual(r['status'],'cached_after_failure')
  d=sqlite3.connect(self.path);self.assertEqual(d.execute('select count(*) from weather_batches').fetchone()[0],1);d.close()
 def test_failure_without_cache(self):
  with patch.object(wx,'locations',return_value=self.locs),patch.object(wx,'now',return_value=pd.Timestamp('2026-09-17 09:00')):
   r=wx.refresh(self.path,{'weather_auto':{'retries':1}},force=True,fetcher=lambda *_:b'bad json')
  self.assertEqual(r['status'],'failed')
 def test_recent_cache_no_network(self):
  self.insert()
  with patch.object(wx,'locations',return_value=self.locs),patch.object(wx,'now',return_value=pd.Timestamp('2026-09-17 08:30')):
   r=wx.refresh(self.path,{},fetcher=lambda *_:self.fail('cache should avoid network'))
  self.assertEqual(r['status'],'cached')
 def test_save_rollback(self):
  with patch.object(wx,'backup'),patch.object(wx,'import_one',side_effect=ValueError('failure')):
   with self.assertRaises(ValueError):wx.save(self.path,self.raw,self.f,'url',pd.Timestamp('2026-09-17 08:00'),self.start,self.end,self.locs)
  d=sqlite3.connect(self.path);self.assertEqual(d.execute('select count(*) from weather_batches').fetchone()[0],0);self.assertEqual(d.execute('select count(*) from weather_points').fetchone()[0],0);d.close()
if __name__=='__main__':unittest.main()
