# -*- coding: utf-8 -*-
"""24 小时联合系数优化。

两条求解线：
1) optimize()      五起点网格坐标搜索（历史行为，作对拍与兜底）；
2) optimize_exact() Rockafellar-Uryasev 线性化的精确解：加权均值-CVaR 为 LP，
   超限概率约束（场景化机会约束）加 0-1 指示变量后为 MILP，HiGHS 求解。

收益函数对单小时系数 k 为分段线性凹函数（段边界 0.8l/f、l/f、1.2l/f），
凹函数=各段支撑直线的 min 包络，因此 t<=a_j·k+b_j 的超图约束给出精确表达。
整日 CVaR / 超限约束基于 24 小时求和，保留跨小时与场景依赖。
场景可选近期指数加权（scenario_weight_halflife_days）。
CVaR 一律截非负（LP 中 z>=0），与 risk()/objective() 口径一致。
"""
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
import scipy.sparse as sp

def cvar(loss,alpha=.9):
    loss=np.asarray(loss,dtype=float)
    if not len(loss) or not np.isfinite(loss).all():raise ValueError('CVaR 必须使用有限日损失')
    # 离散经验 CVaR 的 Rockafellar-Uryasev 表达式，边界分位数只计部分质量。
    z=np.quantile(loss,alpha)
    return float(z+np.maximum(loss-z,0).mean()/(1-alpha))

def weighted_cvar(loss,alpha=.9,p=None):
    """加权 RU-CVaR：min_z z + Σ p_d·(L_d−z)^+/(1−α)。p=None 等权，结果与 cvar() 一致；
    有限支撑下最优 z 必落在某个损失值处，逐候选精确取最小。不截非负（调用方按需截断）。"""
    loss=np.asarray(loss,dtype=float)
    if not len(loss) or not np.isfinite(loss).all():raise ValueError('CVaR 必须使用有限日损失')
    if p is None:return cvar(loss,alpha)
    p=np.asarray(p,dtype=float)
    if not np.isfinite(p).all() or p.min()<0 or abs(p.sum()-1)>1e-9:raise ValueError('场景权重必须非负且和为1')
    z=np.unique(loss)
    excess=np.maximum(loss[None,:]-z[:,None],0)@p
    return float((z+excess/(1-alpha)).min())

def haircut(spread,std,shrink):
    return np.sign(spread)*np.maximum(abs(spread)-shrink*std,0)

def recovery_fee(q,l,spread,c):
    """偏差考核（spread=日前−实时）。profit_recovery：超出上下带部分按方向价差回收收益；
    fixed_fee：旧固定费率口径。历史记录无 deviation_rule 键时保持 fixed_fee 以复现旧结果。"""
    upper=np.maximum(q-c['deviation_upper_ratio']*l,0);lower=np.maximum(c['deviation_lower_ratio']*l-q,0)
    if c.get('deviation_rule','fixed_fee')=='profit_recovery':
        # 多报超带且实时更贵(spread<0)：多买赚的日前便宜价被回收upper*|spread|；
        # 少报超带且日前更贵(spread>0)：少买省的日前高价被回收lower*spread。
        return upper*np.maximum(-spread,0)+lower*np.maximum(spread,0)
    return (upper+lower)*c.get('deviation_cost_per_mwh',0.0)

def pnl(k,f,l,spread,c,std=None):
    """基准成本为实际负荷全在日前购电：收益=(Q-L)*(RT-DA)-偏差考核回收。"""
    q=f*np.asarray(k);exposure=q-l
    hourly=-exposure*spread-recovery_fee(q,l,spread,c)
    # 对实际持仓方向取不利价差，负价差不会得到更激进的“折减”。
    if std is not None:hourly-=abs(exposure)*c['error_shrink']*std
    return hourly.sum(axis=1)

def objective(values,c,p=None):
    """效用=加权日均收益−λ·CVaR(日损失，截非负)。与 risk() 的 objective 同口径。"""
    values=np.asarray(values,dtype=float)
    mean=float(values.mean() if p is None else values@np.asarray(p,dtype=float))
    tail=max(weighted_cvar(-values,c['cvar_alpha'],p),0.0)
    return mean-c['lambda_risk']*tail

def risk(values,c,limit,p=None):
    values=np.asarray(values,dtype=float)
    losses=-values
    tail=max(weighted_cvar(losses,c['cvar_alpha'],p),0.0)
    if p is None:exceed=float((losses>limit+1e-8).mean())
    else:
        p=np.asarray(p,dtype=float);exceed=float(p[losses>limit+1e-8].sum())
    return {'mean':float(values.mean() if p is None else values@p),'cvar':tail,'exceed_probability':exceed,'objective':objective(values,c,p),'feasible':exceed<=1-c['confidence']+1e-10}

def scenario_weights(dates,halflife_days):
    """场景日按半衰期指数加权（最近一天最大），归一化为概率。halflife_days 为空/0 返回 None（等权）。"""
    if not halflife_days:return None
    d=np.asarray(dates,dtype='datetime64[D]').astype(float)
    if not len(d) or not np.isfinite(d).all():raise ValueError('场景日期无效')
    w=0.5**(np.maximum(d.max()-d,0)/float(halflife_days))
    total=w.sum()
    if total<=0:raise ValueError('场景权重全为零')
    return w/total

def _segment_coeffs(f,l,spread,std,c):
    """逐场景逐小时分段线性凹收益的段直线系数 a,b：形状 (D,24,4)，
    满足 v_{d,h}(k)=min_j(a_j·k+b_j) 对任意 k 成立（凹分段线性=各段支撑直线下确界）。"""
    f=np.asarray(f,dtype=float);l=np.asarray(l,dtype=float);s=np.asarray(spread,dtype=float)
    kappa=c['error_shrink']*np.asarray(std,dtype=float)[None,:]
    slopes_q=np.stack([-s+kappa+np.maximum(s,0),-s+kappa,-s-kappa,-s-kappa-np.maximum(-s,0)],axis=-1)
    a=slopes_q*f[...,None]
    safe=np.where(f>0,f,1.0)
    rep=np.stack([c['deviation_lower_ratio']*l/safe,c['deviation_lower_ratio']*l/safe,
                  c['deviation_upper_ratio']*l/safe,c['deviation_upper_ratio']*l/safe],axis=-1)
    rep=np.where(f[...,None]>0,rep,1.0)
    q=f[...,None]*rep
    lb_=np.broadcast_to(l[...,None],q.shape);sb=np.broadcast_to(s[...,None],q.shape);kb=np.broadcast_to(kappa[...,None],q.shape)
    v=-(q-lb_)*sb-np.abs(q-lb_)*kb-recovery_fee(q,lb_,sb,c)
    b=v-a*rep
    return a,b

def _exact_program(f,l,spread,c,std):
    """组装 LP/MILP 公共部分：变量 [k(24), t(D×24), u(D), z(1)]。
    返回 (A, lb, ub, var_lb, var_ub, D)。目标系数由调用方按场景权重组装。"""
    f=np.asarray(f,dtype=float);l=np.asarray(l,dtype=float);spread=np.asarray(spread,dtype=float)
    D=len(f);n_t=D*24;nk=24
    n=nk+n_t+D+1
    i_z=nk+n_t+D
    a,b=_segment_coeffs(f,l,spread,std,c)
    rows=[];lbs=[];ubs=[]
    # 超图：t_{d,h} <= a_j·k_h + b_j  ⇔  −a_j·k_h + t_{d,h} <= b_j（每段一条，凹包络恒等式）
    hh=np.tile(np.arange(24),D);rr=np.arange(n_t)
    for j in range(4):
        cols=np.concatenate([hh,nk+rr])
        data=np.concatenate([-a[:,:,j].ravel(),np.full(n_t,1.0)])
        rows.append(sp.csr_matrix((data,(np.concatenate([rr,rr]),cols)),shape=(n_t,n)))
        lbs.append(np.full(n_t,-np.inf));ubs.append(b[:,:,j].ravel())
    # u 链：u_d + Σ_h t_{d,h} + z >= 0  ⇔  −(u_d+Σt+z) <= 0
    for d in range(D):
        cols=np.concatenate([[nk+n_t+d],nk+d*24+np.arange(24),[i_z]])
        data=np.concatenate([[-1.0],np.full(24,-1.0),[-1.0]])
        rows.append(sp.csr_matrix((data,([0]*26,cols)),shape=(1,n)))
        lbs.append(np.array([-np.inf]));ubs.append(np.array([0.]))
    # 平滑：|k_{h+1}−k_h| <= smooth_limit
    for h in range(23):
        for sign in (1.,-1.):
            row=np.zeros(n);row[h]=-sign;row[h+1]=sign
            rows.append(sp.csr_matrix(row.reshape(1,-1)))
            lbs.append(np.array([-np.inf]));ubs.append(np.array([c['smooth_limit']+1e-12]))
    var_lb=np.concatenate([np.full(nk,c['k_min']),np.full(n_t,-np.inf),np.zeros(D),[0.]])
    var_ub=np.concatenate([np.full(nk,c['k_max']),np.full(n_t,np.inf),np.full(D,np.inf),[np.inf]])
    return sp.vstack(rows,format='csr'),np.concatenate(lbs),np.concatenate(ubs),var_lb,var_ub,D

def optimize_exact(f,l,spread,c,std,limit,weights=None,time_limit=120.0):
    """精确解：先解 LP（加权均值-CVaR+box+平滑）；若超限概率违反再解 MILP（场景化机会约束）。
    MILP 不可行/超时失败时回退 LP 解并如实标注（exceed_enforced=False）。
    返回 (k, info)；LP 本身失败返回 (None, info)。"""
    f=np.asarray(f,dtype=float);l=np.asarray(l,dtype=float);spread=np.asarray(spread,dtype=float)
    D=len(f)
    if f.shape[1]!=24 or not(f.shape==l.shape==spread.shape):raise ValueError('场景必须为完整的 天数×24 矩阵')
    if not all(np.isfinite(x).all() for x in [f,l,spread,std]) or (f<0).any() or (l<0).any():raise ValueError('优化场景包含缺失、非有限值或负负荷')
    p=None if weights is None else np.asarray(weights,dtype=float)
    if p is not None and (len(p)!=D or abs(p.sum()-1)>1e-9):raise ValueError('场景权重长度必须等于场景天数且和为1')
    pw=np.full(D,1.0/D) if p is None else p
    A,lb,ub,var_lb,var_ub,_=_exact_program(f,l,spread,c,std)
    nk=24;n_t=D*24;i_z=nk+n_t+D;n=nk+n_t+D+1
    coef=np.zeros(n)
    coef[nk:nk+n_t]=-np.repeat(pw,24)
    coef[nk+n_t:i_z]=c['lambda_risk']*pw/(1-c['cvar_alpha'])
    coef[i_z]=c['lambda_risk']
    def evaluate(k):return risk(pnl(k,f,l,spread,c,std),c,limit,p)
    res=milp(c=coef,constraints=LinearConstraint(A,lb,ub),bounds=Bounds(var_lb,var_ub),
             integrality=np.zeros(n),options={'time_limit':time_limit,'presolve':True})
    if res.x is None:
        return None,{'solver':'LP','status':'lp_failed','message':str(res.message),'scenario_days':D,'daily_loss_limit':limit,'exceed_enforced':False}
    k_lp=res.x[:nk];info_lp=evaluate(k_lp)
    info_lp.update(solver='LP（HiGHS，加权均值-CVaR精确解）',status='optimal',scenario_days=D,daily_loss_limit=limit,exceed_enforced=False,raw_status=int(res.status))
    if info_lp['feasible']:return k_lp,info_lp
    # LP 解超限违反 → MILP：w_d∈{0,1}，L_d − M_d·w_d <= limit（w_d=0 强制该日不超限），
    # Σ p_d·w_d <= 1−置信度（加权超限比例约束）。
    # M_d 必须覆盖任意k下的损失上界：凹收益在k端点取最小，取两端整日损失的最大值作严格上界。
    v_lo=pnl(np.full(24,c['k_min']),f,l,spread,c,std);v_hi=pnl(np.full(24,c['k_max']),f,l,spread,c,std)
    loss_ub=np.maximum(-np.minimum(v_lo,v_hi),0.0)
    M=np.maximum(loss_ub-limit,0.0)+1.0
    i_w=lambda d:n+d
    rows2=[];lbs2=[];ubs2=[]
    for d in range(D):
        cols=np.concatenate([nk+d*24+np.arange(24),[i_w(d)]])
        data=np.concatenate([np.full(24,-1.0),[-M[d]]])
        rows2.append(sp.csr_matrix((data,([0]*25,cols)),shape=(1,n+D)))
        lbs2.append(-np.inf);ubs2.append(limit)
    rows2.append(sp.csr_matrix((pw,([0]*D,[i_w(d) for d in range(D)])),shape=(1,n+D)))
    lbs2.append(-np.inf);ubs2.append(float(1-c['confidence'])+1e-12)
    rows_ex=rows2[:D]  # 每场景超限指示约束（L_d − M_d·w_d <= limit）
    rows_budget=rows2[D:]  # 加权超限预算约束
    coef2=np.concatenate([coef,np.zeros(D)])
    vl2=np.concatenate([var_lb,np.zeros(D)]);vu2=np.concatenate([var_ub,np.ones(D)])
    A_ext=sp.hstack([A,sp.csr_matrix((A.shape[0],D))],format='csr')  # 基础约束补 w 零列
    res=milp(c=coef2,constraints=LinearConstraint(sp.vstack([A_ext]+rows2,format='csr'),
             np.concatenate([lb,lbs2]),np.concatenate([ub,ubs2])),
             bounds=Bounds(vl2,vu2),integrality=np.concatenate([np.zeros(n),np.ones(D)]),
             options={'time_limit':time_limit,'presolve':True})
    if res.x is not None:
        k_m=res.x[:nk];info_m=evaluate(k_m)
        if res.status==0 or info_m['feasible']:
            info_m.update(solver='MILP（HiGHS，超限机会约束精确解）',
                          status='optimal' if res.status==0 else 'milp_time_limit_best_found',
                          scenario_days=D,daily_loss_limit=limit,exceed_enforced=True,raw_status=int(res.status))
            return k_m,info_m
    # MILP 不可行/失败：解 min Σp·w 辅助MILP（去掉预算行，必可行）给出最小可达加权超限参考，
    # 量化"距预算多远"；再回退LP解如实标注超限未强制。
    min_exceed=None
    try:
        coef_min=np.concatenate([np.zeros(n),pw])
        res_min=milp(c=coef_min,constraints=LinearConstraint(sp.vstack([A_ext]+rows_ex,format='csr'),
                     np.concatenate([lb,lbs2[:D]]),np.concatenate([ub,ubs2[:D]])),
                     bounds=Bounds(vl2,vu2),integrality=np.concatenate([np.zeros(n),np.ones(D)]),
                     options={'time_limit':min(time_limit,60.0),'presolve':True})
        if res_min.x is not None:min_exceed=float(pw@res_min.x[n:])
    except Exception:pass
    info_lp['solver']='LP（MILP超限约束不可行或超时，回退LP解；超限未强制）'
    info_lp['milp_status']=str(res.message)
    info_lp['min_exceed_probability']=min_exceed
    return k_lp,info_lp

def optimize(f,l,spread,c,std,limit=None,p=None):
    if not (f.shape==l.shape==spread.shape) or f.shape[1]!=24:raise ValueError('场景必须为完整的 天数×24 矩阵')
    if not all(np.isfinite(x).all() for x in [f,l,spread,std]) or (f<0).any() or (l<0).any():raise ValueError('优化场景包含缺失、非有限值或负负荷')
    if p is not None and (len(p)!=len(f) or abs(np.asarray(p).sum()-1)>1e-9):raise ValueError('场景权重长度必须等于场景天数且和为1')
    reference=pnl(np.ones(24),f,l,spread,c)
    if limit is None:
        if c['daily_loss_limit']=='auto':
            # auto限额=max(2×|k=1日均收益|, k=1日损失的CVaR_{1-alpha})。
            # 修复：基准日均收益≈0时旧口径限额≈0，所有解被误判不可行；
            # CVaR锚保证限额不小于中性策略自身的尾部损失水平，不由候选解反推。
            limit=max(2*abs(float(reference.mean())),cvar(-reference,c['cvar_alpha']))
        else:limit=float(c['daily_loss_limit'])
    grid=np.round(np.arange(c['k_min'],c['k_max']+c['k_step']/2,c['k_step']),10)
    # 预计算每个小时/网格/完整日场景；联合风险始终基于整日求和。
    cube=np.stack([-(f[:,h,None]*grid-l[:,h,None])*spread[:,h,None]-abs(f[:,h,None]*grid-l[:,h,None])*c['error_shrink']*np.broadcast_to(std,f.shape)[:,h,None]-recovery_fee(f[:,h,None]*grid,l[:,h,None],spread[:,h,None],c) for h in range(24)])
    best=None
    # 均匀多起点 + 邻域逐坐标搜索；每次变更直接满足平滑，不事后投影破坏风险约束。
    for start in np.unique(np.round(np.linspace(0,len(grid)-1,5)).astype(int)):
        inds=np.full(24,start,dtype=int);values=cube[np.arange(24),:,inds].sum(axis=0);r=risk(values,c,limit,p)
        def rank(a):return (int(a['feasible']), -a['exceed_probability'] if not a['feasible'] else 0,a['objective'])
        for _ in range(8):
            changed=False
            for h in range(24):
                chosen=inds[h];score=rank(r)
                for j,g in enumerate(grid):
                    if h and abs(g-grid[inds[h-1]])>c['smooth_limit']+1e-9:continue
                    if h<23 and abs(g-grid[inds[h+1]])>c['smooth_limit']+1e-9:continue
                    candidate=values-cube[h,:,inds[h]]+cube[h,:,j];rr=risk(candidate,c,limit,p)
                    if rank(rr)>score:chosen=j;score=rank(rr)
                if chosen!=inds[h]:
                    values=values-cube[h,:,inds[h]]+cube[h,:,chosen];inds[h]=chosen;r=risk(values,c,limit,p);changed=True
            if not changed:break
        if best is None or rank(r)>rank(best[1]):best=(grid[inds].copy(),r)
    k,r=best
    check=risk(pnl(k,f,l,spread,c,std),c,limit,p)
    assert abs(check['objective']-r['objective'])<1e-6
    assert np.max(abs(np.diff(k)))<=c['smooth_limit']+1e-8
    return k,{**check,'daily_loss_limit':limit,'solver':'五起点离散坐标搜索；仅为搜索所得解，不保证全局最优','scenario_days':len(f)}

def neighborhood(k,f,l,spread,c,std,weights=None,step=None):
    """最优解邻域稳健度：逐小时 ±1 网格扰动的加权效用退化（正值=扰动后变差）。
    扰动后违反平滑约束或超出系数域的方向记 NaN（该指标只定义在可行邻域上）；
    在可行邻域内若出现负退化，说明解未达可行域内最优。"""
    step=c['k_step'] if step is None else step
    k=np.asarray(k,dtype=float)
    base=objective(pnl(k,f,l,spread,c,std),c,weights)
    up=np.full(24,np.nan);down=np.full(24,np.nan)
    for h in range(24):
        for delta,out in ((step,up),(-step,down)):
            kk=k.copy();kk[h]=round(k[h]+delta,10)
            if c['k_min']-1e-12<=kk[h]<=c['k_max']+1e-12 and np.abs(np.diff(kk)).max()<=c['smooth_limit']+1e-9:
                out[h]=base-objective(pnl(kk,f,l,spread,c,std),c,weights)
    return up,down
