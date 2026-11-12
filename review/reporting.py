# -*- coding: utf-8 -*-
import html,json
from pathlib import Path
import numpy as np
import pandas as pd
from excel_report import write_book
from metrics import analyze
from common import resolve,get_path

def chart(values,title):
 a=np.asarray(values,dtype=float);a=a[np.isfinite(a)]
 if not len(a):return ''
 lo=min(0,a.min());hi=max(0,a.max());pts=' '.join(f'{50+i*820/max(1,len(a)-1):.1f},{180-(v-lo)/max(hi-lo,1e-8)*140:.1f}' for i,v in enumerate(a))
 return f'<h3>{title}</h3><svg viewBox="0 0 920 210"><polyline points="{pts}" fill="none" stroke="#16788c" stroke-width="3"/><text x="0" y="35">{hi:.1f}</text><text x="0" y="180">{lo:.1f}</text><text x="50" y="205">期间起点</text><text x="815" y="205">期间终点</text></svg>'

def tracking(c,done):
 root=resolve(c,c['output_dir'])/'adjustments';rows=[]
 for path in sorted((root/'transactions').glob('*.json')):
  t=json.loads(path.read_text(encoding='utf-8'));time=pd.Timestamp(t['created_at']);after=done[(done.published_at>=time)&(~done.late_publication)] if len(done) else done
  perf=analyze(after)['summary'];perf={} if perf.empty else perf.set_index('指标')['值'].to_dict()
  for item in t['items']:rows.append({'调整时间':t['created_at'],'状态':t['status'],'项目':item['project'],'参数':item['parameter'],'新值':str(item['new_value']),'预计提升':item['improvement'],'调整后有效样本小时':len(after),'表现范围':'正式或及时发布的研究预测，见原记录状态','实际负荷WMAPE':perf.get('负荷WMAPE'),'实际负荷MAPE':perf.get('负荷MAPE'),'实际日前MAE':perf.get('日前MAE'),'实际实时MAE':perf.get('实时MAE'),'实际超额收益':perf.get('累计超额收益'),'跟踪状态':'待新预测及D+滞后实际值齐全' if after.empty else '有后续样本，需单独归因避免并发变化'})
 return pd.DataFrame(rows)

def reports(c,done,pending,cutoff,candidates,suggestions,load_risk=None):
 out=resolve(c,c['output_dir']);day=pd.Timestamp(c['review_date']) if c['review_date']!='today' else pd.Timestamp.now().normalize();history=tracking(c,done)
 scopes=[('daily',day.strftime('%Y%m%d'),None)]
 # 每个有对账样本的自然周/月均生成，跨期不混入同一指标。
 if len(done):
  scopes += [('weekly',str(k),g) for k,g in done.groupby(done.target.dt.strftime('%G-%V'))]
  scopes += [('monthly',str(k),g) for k,g in done.groupby(done.target.dt.strftime('%Y-%m'))]
 else:scopes += [('weekly',day.strftime('%G-%V'),None),('monthly',day.strftime('%Y-%m'),None)]
 outputs=[]
 for period,label,subset in scopes:
  f=done if subset is None else subset
  research=f[~f.status.eq('production')|f.late_publication] if len(f) else f;production=f[f.status.eq('production')&~f.late_publication] if len(f) else f
  a=analyze(research);live=analyze(production)
  headline=f'{dict(daily="每日简报",weekly="周度复盘",monthly="月度复盘")[period]} · {label}';body=f'<h1>{headline}</h1><p class="notice">历史表现不代表未来收益；调参需人工执行 apply 后生效。研究回放与正式发布分别统计，研究中性系数不代表历史真实执行策略。</p><div class="cards"><article>允许复盘至<strong>{cutoff.date()}</strong></article><article>正式已对账小时<strong>{len(production)}</strong></article><article>研究已对账小时<strong>{len(research)}</strong></article><article>待复盘记录<strong>{len(pending)}</strong></article></div>'
  body+='<p>本报告已对账样本最新日期：'+(str(f.target.max().date()) if len(f) else '暂无')+'；负荷与价格覆盖可能不同，各自按有效样本计算。</p>'
  if load_risk is not None:
   body+='<h2>原始事前负荷预测误差诊断</h2><p>以下为独立的滚动诊断窗口，不随本页周/月子集重算。只使用申报截点前原始发布与诊断时已可得实际值，每目标小时唯一配对；排除研究回放。偏差=实际−预测。偏差修正候选与去均值残差分位数分开，均不自动回写。k=1仍有预测负荷与实际用电偏差敞口；本表不推断资金预算或未来概率。</p>'
   body+='<p>诊断状态：'+html.escape(load_risk['status'])+'；样本小时 '+str(load_risk['summary']['sample_hours'])+'；完整日 '+str(load_risk['summary']['complete_days'])+'。</p><p>'
   body+=' / '.join('<a href="'+html.escape(name,quote=True)+'">'+html.escape(label)+'</a>' for label,name in load_risk['files'].items())+'</p>'
   body+=pd.DataFrame([load_risk['summary']]).to_html(index=False,border=0,na_rep='不足/未评估')
  body+='<h2>短板环节</h2><p>'+html.escape(a['weakness'])+'</p><p>归因方法：三因素Shapley值（系数选择/负荷误差/价格误差），对8个因素组合的收益取全排列平均边际贡献；效率性保证三因素之和=实际收益−中性预测收益，消除旧版路径分解的替换顺序偏差；仍不等同因果识别，系数项不是事后最优收益。</p>'
  if len(research):
   l=research[research.kind.eq('load')&research.actual_load.ne(0)].copy();q=research[research.kind.eq('coefficient')].copy()
   if len(l):body+=chart((abs(l.load-l.actual_load)/l.actual_load.abs()).groupby(l.target.dt.normalize()).mean(),'研究负荷每日MAPE（比例）')
   if len(q):body+=chart((np.sign(q.da-q.rt)==np.sign(q.actual_da-q.actual_rt)).groupby(q.target.dt.normalize()).mean(),'研究价差每日方向准确率（比例）')
  if len(a['daily']):body+=chart(a['daily']['累计收益'],'研究复盘累计收益')+chart(a['daily']['回撤'],'研究复盘回撤')
  tables={'正式复盘指标':live['summary'],'研究复盘指标':a['summary'],'分小时指标':a['hourly'],'工作日节假日':a['calendar'],'逐日收益与归因':a['daily'],'逐小时归因':a['attribution'],'五档系数对照':a['sensitivity'],'待复盘清单':pending.drop(columns=['config_json'],errors='ignore'),'调参候选比较':candidates,'建议清单':pd.DataFrame(suggestions.get('suggestions',[])),'调整后跟踪':history,'研究预测实际明细':research.drop(columns=['config_json'],errors='ignore')}
  if load_risk is not None:
   tables['事前负荷误差诊断']=pd.DataFrame([load_risk['summary']])
  if period=='daily':tables['当日应复盘日期明细']=f[f.target.dt.normalize().eq(cutoff)].drop(columns=['config_json'],errors='ignore') if len(f) else pd.DataFrame()
  for name,frame in tables.items():
   body+='<h2>'+name+'</h2>'+(frame.head(100).to_html(index=False,border=0,na_rep='—',float_format=lambda v:f'{v:,.4f}') if len(frame) else '<p>暂无符合条件的记录。</p>')
  body+='<p>HTML每表最多展示100行；完整数据见同名Excel。价差基准为对账窗口内实际价差的无条件多数基准准确率（自动计算），不是模型发布时已证实的准确率；负荷主指标为WMAPE，MAPE仅作对照。</p><pre>'+html.escape(json.dumps(suggestions,ensure_ascii=False,indent=2,default=str))+'</pre>'
  stem=f'review_{period}_{label}';path=out/(stem+'.html');path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+headline+'</title><style>body{font:15px system-ui;max-width:1440px;margin:36px auto;padding:0 24px;background:#f2f6f7;color:#183846}.notice{background:#fff0d6;padding:18px}.cards{display:flex;gap:16px}.cards article{background:white;border-radius:10px;padding:20px;flex:1}strong{display:block;font-size:28px;color:#15778b;margin-top:10px}table{display:block;overflow:auto;border-collapse:collapse;background:white}td,th{padding:10px;border-bottom:1px solid #dfe8eb;white-space:nowrap}th{background:#183846;color:white}svg{width:100%;max-height:260px;background:white}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'+body+'</html>',encoding='utf-8')
  write_book(out/(stem+'.xlsx'),tables);outputs.append(stem)
 return outputs
