# -*- coding: utf-8 -*-
"""单文件离线报告，不依赖 CDN。"""
import html,json
import numpy as np

def line(values,title,color='#147d92',marks=None):
    a=np.asarray(values,dtype=float);a=a[np.isfinite(a)]
    if not len(a):return ''
    lo,hi=(min(a.min(),.8),max(a.max(),1.2)) if marks is not None else (min(a.min(),0),max(a.max(),0));span=max(hi-lo,1e-9)
    pts=' '.join(f'{55+i*830/max(len(a)-1,1):.1f},{180-(v-lo)/span*140:.1f}' for i,v in enumerate(a))
    dots='' if marks is None else ''.join(f'<circle cx="{55+i*830/max(len(a)-1,1):.1f}" cy="{180-(a[i]-lo)/span*140:.1f}" r="4" fill="#cf7536"><title>{i}:00 约束触达</title></circle>' for i,m in enumerate(marks) if m)
    return f'<section><h3>{html.escape(title)}</h3><svg viewBox="0 0 920 220"><path d="M55 30V180H890" fill="none" stroke="#9daeb6"/><polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>{dots}<text x="0" y="40">{hi:.2f}</text><text x="0" y="180">{lo:.2f}</text><text x="55" y="205">第1点</text><text x="810" y="205">第{len(a)}点</text></svg></section>'

def histogram(values,title):
    a=np.asarray(values);a=a[np.isfinite(a)];counts,edges=np.histogram(a,bins=20)
    bars=''.join(f'<rect x="{55+i*41}" y="{180-v/max(counts.max(),1)*140}" width="38" height="{v/max(counts.max(),1)*140}" fill="#147d92"><title>{edges[i]:.2f}~{edges[i+1]:.2f}: {v}</title></rect>' for i,v in enumerate(counts))
    return f'<section><h3>{title}</h3><svg viewBox="0 0 920 220">{bars}<text x="55" y="205">{edges[0]:.2f}</text><text x="800" y="205">{edges[-1]:.2f}</text></svg></section>'

def write_html(path,coeff,daily,metrics,errors,meta,sensitivity):
    metrics=metrics.copy();metrics['方向准确率']=metrics['方向准确率'].map(lambda v:f'{v:.2%}');sensitivity=sensitivity.rename(columns={'mean':'日均收益_元','cvar':'CVaR_元','exceed_probability':'超限概率','objective':'风险调整目标值','feasible':'满足概率约束'})
    body='<h1>日前／实时申报系数决策</h1><p class="notice">研究示例：系数为历史数据搜索所得解，不保证未来收益。数据发布时间、价格口径与负荷单位未完成认证；实际采用系数保持 1。</p>'
    body+='<p><strong>目标日：'+meta['目标日']+'；当前搜索'+('找到场景可行解' if meta['最终场景风险']['feasible'] else '未找到满足风控约束的解，不可申报')+'</strong></p>'
    body+='<p>前段选择、后段冻结验证。真实盈亏按实际价格结算；优化场景额外扣除持仓方向不利的误差成本。CVaR 为整日损失的经验尾部均值，图中损失单位为元（取决于 MWh 单位确认）。</p>'
    body+=line(coeff['研究系数'], '24 小时研究系数（橙点为边界或平滑触达，点1对应00:00）',marks=(coeff['边界触达']|coeff['平滑触达']).to_numpy())
    body+=coeff.to_html(index=False,border=0,na_rep='—',float_format=lambda v:f'{v:,.3f}')
    body+=line(daily['累计收益'],'独立验证期累计收益／元')+line(daily['回撤'],'独立验证期回撤／元','#c26a38')
    body+=histogram(-daily['实际收益'],'独立验证期单日亏损分布／元')
    for col in ['da_error','rt_error','spread_error']:body+=histogram(errors[col],col+' 历史误差分布／元每MWh')
    body+='<h2>独立验证期价格准确率</h2>'+metrics.to_html(index=False,border=0,na_rep='—',float_format=lambda v:f'{v:,.3f}')+'<h2>系数敏感性：固定五档，验证期实际价格</h2>'+sensitivity.to_html(index=False,border=0,na_rep='—',float_format=lambda v:f'{v:,.3f}')
    body+='<h2>模型、参数、来源与风控</h2><pre>'+html.escape(json.dumps(meta,ensure_ascii=False,indent=2,default=str))+'</pre>'
    path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>申报系数优化报告</title><style>body{font:15px system-ui;color:#163745;background:#f4f7f8;max-width:1440px;margin:40px auto;padding:0 24px}h1{font-size:34px}section{background:white;padding:16px;border-radius:12px;margin:24px 0}svg{width:100%;max-height:260px}table{display:block;overflow:auto;border-collapse:collapse;background:white;font-size:13px}th,td{padding:10px;border-bottom:1px solid #dce5e9;white-space:nowrap;text-align:right}th{background:#163745;color:white}.notice{padding:18px;background:#fff0dc;border-left:5px solid #bf7b24}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px}</style>'+body+'</html>',encoding='utf-8')
