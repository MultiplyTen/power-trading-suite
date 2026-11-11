# -*- coding: utf-8 -*-
import hashlib,json
from pathlib import Path
import pandas as pd
import yaml
ROOT=Path(__file__).resolve().parent

def read_config(root=ROOT):
 c=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'));c['_root']=str(root.resolve())
 if c['actual_lag_days']<0 or c['suggest_window_days']<15 or not 0<c['improve_threshold']<1:raise ValueError('滞后天数、建议窗口或改善阈值无效')
 return c

def resolve(c,p):return (Path(c['_root'])/p).resolve()
def stamp():return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).isoformat()
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def dump(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
def get_path(c,path):
 for k in path.split('.'):c=c[k]
 return c
def set_path(c,path,value):
 keys=path.split('.')
 for k in keys[:-1]:
  if k not in c or not isinstance(c[k],dict):raise ValueError('参数路径不存在：'+path)
  c=c[k]
 if keys[-1] not in c:raise ValueError('参数不存在：'+path)
 c[keys[-1]]=value
