import html,json
import numpy as np
from reports.excel import LABELS

def svg_chart(frame,cols,title):
    f=frame[cols].replace([np.inf,-np.inf],np.nan);v=f.to_numpy();finite=v[np.isfinite(v)]
    if not len(finite):return '<p>该图暂无有效数据</p>'
    low=float(finite.min());high=float(finite.max());span=max(high-low,1);width=1050;height=260
    parts=[f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}"><text x="48" y="24">{html.escape(title)}</text>']
    colors=['#136F8A','#C45D26','#617A31','#865BA7']
    for j,col in enumerate(cols):
        seg=[]
        for i,value in enumerate(f[col]):
            if np.isfinite(value):seg.append(f'{48+960*i/max(1,len(f)-1):.1f},{210-150*(value-low)/span:.1f}')
            elif seg:parts.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{colors[j%4]}" stroke-width="2"/>');seg=[]
        if seg:parts.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{colors[j%4]}" stroke-width="2"/>')
        parts.append(f'<text x="{48+j*235}" y="248" fill="{colors[j%4]}">{html.escape(LABELS.get(col,col))}</text>')
    for i in sorted(set([0,len(f)//4,len(f)//2,3*len(f)//4,len(f)-1])):
        label=f.index[i].strftime('%m-%d %H:%M') if hasattr(f.index[i],'strftime') else str(f.index[i])
        parts.append(f'<text x="{48+960*i/max(1,len(f)-1):.1f}" y="228" text-anchor="middle" font-size="11">{html.escape(label)}</text>')
    parts.extend([f'<text x="2" y="64">{high:.1f}</text>',f'<text x="2" y="213">{low:.1f}</text>','</svg>']);return ''.join(parts)

def render(path,overview,metrics,quality,parameters,reference=None):
    charts=''.join(svg_chart(overview,cols,title) for cols,title in [(['load_hat','load_baseline','load_p10','load_p90'],'客户组合负荷及区间 · MWh'),(['system_load_hat'],'全省统调负荷 · MW'),(['da_hat','rt_hat'],'日前与实时价格 · 元/MWh'),(['prob_rt_above_da'],'实时价格高于日前的概率'),(['research_k','adopted_k'],'填报系数')]) if len(overview) else ''
    table=overview.reset_index().rename(columns=LABELS).to_html(index=False,escape=True,na_rep='缺失',float_format=lambda x:f'{x:,.3f}')
    document='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>负荷、价格与填报系数</title><style>body{font:16px system-ui,sans-serif;color:#173745;background:#eef3f5;margin:0}main{max-width:1160px;margin:auto;padding:36px}h1{font-size:30px}section,svg{background:white;border-radius:10px;padding:18px;box-sizing:border-box;width:100%;margin:12px 0}.notice{background:#fff0cd;border-left:5px solid #b76a12;padding:20px}table{border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{padding:10px;border-bottom:1px solid #dbe3e8;text-align:right}th{background:#153d53;color:white}pre{white-space:pre-wrap;overflow-wrap:anywhere}.scroll{overflow:auto}a{color:#136f8a}</style><main><h1>负荷、价格与填报系数</h1><p>四模块联动 · 可追溯研究输出</p><div class="notice">数据缺口、价格口径和预测时点决定结果可用性。研究系数与采用系数分别保留；历史研究不能作为实盘业绩。</div>'+charts+'<section><h2>逐小时结果</h2><div class="scroll">'+table+'</div></section><section><h2>独立测试期指标</h2><div class="scroll">'+metrics.to_html(index=False,escape=True,na_rep='—')+'</div></section><section><h2>数据状态与来源</h2><pre>'+html.escape(json.dumps(quality,ensure_ascii=False,indent=2,default=str))+'</pre><p>天气说明：<a href="https://open-meteo.com/en/docs/previous-runs-api">Open-Meteo Previous Runs</a>；<a href="https://open-meteo.com/en/docs">Forecast API</a>。图表离线可用。</p></section><section><h2>参数</h2><pre>'+html.escape(json.dumps(parameters,ensure_ascii=False,indent=2,default=str))+'</pre></section></main></html>'
    if len(overview):document=document.replace('<p>四模块联动 · 可追溯研究输出</p>','<p>四模块联动 · 可追溯研究输出 · '+html.escape(str(overview.index.min())+' 至 '+str(overview.index.max()))+'</p>')
    path.write_text(document,encoding='utf-8')
