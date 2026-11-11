# -*- coding: utf-8 -*-
"""Reconcile every SQL cell against its archived source version, not just counts."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from database.store import ROOT,connect,check,parse,normalize,quote,suite_lock

def main():
    with suite_lock(),connect() as db:
        result=check(db);verified_rows=0
        for version,path,payload in db.execute("SELECT v.id,v.path,v.payload FROM input_versions v JOIN input_files f ON v.path=f.path WHERE f.role='input'"):
            for sheet,frame in parse(path,payload).items():
                name=db.execute('SELECT name FROM input_tables WHERE path=? AND sheet=?',(path,sheet)).fetchone()[0]
                expected=pd.DataFrame([[normalize(v) for v in row] for row in frame.itertuples(index=False,name=None)],columns=frame.columns)
                actual=pd.read_sql_query('SELECT '+','.join(quote(c) for c in frame.columns)+' FROM '+quote(name)+' WHERE _version=? ORDER BY _row',db,params=[version])
                pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False,rtol=1e-12,atol=1e-12)
                positions=[v[0] for v in db.execute('SELECT _row FROM '+quote(name)+' WHERE _version=? ORDER BY _row',(version,))]
                assert positions==list(range(2,len(frame)+2))
                verified_rows+=len(frame)
        result.pop('tables');result['all_structured_cells_match_archived_sources']=True;result['verified_version_rows']=verified_rows
        result['review_counts']={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['runs','predictions','actuals','audit_events']}
    (ROOT/'database/verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
