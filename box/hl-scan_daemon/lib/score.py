import json,math,statistics as S,datetime
from zoneinfo import ZoneInfo
from . import metrics,robust
from .metrics import compute
Z=ZoneInfo('Europe/Zurich'); DAY=86400000
MIN_ACCT_AGE_DAYS=30   # minimum account age (portfolio allTime series start); was 180, set to 30 on 2026-10-31 (Valou)
MAX_INACTIVE_HOURS=96  # exclude wallets with no fill in more than this many hours
def ddcalc(pnl,avh):
    av=dict((t,float(v)) for t,v in avh); pk=None;pt=None;m=0
    for t,v in pnl:
        v=float(v)
        if pk is None or v>pk: pk=v;pt=t
        base=max(av.get(pt,0),av.get(t,0)+(pk-v))
        if base>0: m=max(m,(pk-v)/base)
    return m
def med(x): return S.median(x) if x else None
def score(a,fd,port,st,vaults,port_time=None,bench=None):
    r,trades=compute(a,fd,port,fd['fetched_at']); op=metrics.LAST_OPEN
    o={'addr':a,'fills_src':'fills','fills_asof':datetime.datetime.fromtimestamp(fd['fetched_at']/1000,Z).strftime('%m-%d %H:%M'),
       'port_asof':datetime.datetime.fromtimestamp((port_time or fd['fetched_at'])/1000,Z).strftime('%m-%d %H:%M')}
    o['gap_possible']=bool(fd.get('incremental_from')) and len([f for f in fd['fills'] if f['time']>fd['incremental_from']])>=9000
    fills=[f for f in fd['fills'] if f['dir'] not in ('Buy','Sell') and not f['coin'].startswith('@') and '/' not in f['coin']]
    for lab,k in (('7d','week'),('30d','month')):
        s=port.get(k) or {}; ph=s.get('pnlHistory',[])
        if len(ph)>=2:
            t0,t1=ph[0][0],ph[-1][0]; tot=float(ph[-1][1])-float(ph[0][1])
            real=sum(float(f['closedPnl'])-float(f['fee']) for f in fills if t0<=f['time']<=t1)
            cov=fd['fetched_at']>=t1-3600000   # fills cover portfolio end
            o['total_'+lab]=tot; o['realized_'+lab]=real; o['unreal_funding_delta_'+lab]=tot-real if cov else None
            o['realized_cover_ok_'+lab]=cov
        else: o['total_'+lab]=o['realized_'+lab]=o['unreal_funding_delta_'+lab]=None
        o['dd_mtm_'+lab]=ddcalc(ph,s.get('accountValueHistory',[])) if ph else None
    avh=(port.get('month') or {}).get('accountValueHistory',[])
    av_port=float(avh[-1][1]) if avh else None
    av_state=float(st['marginSummary']['accountValue']) if st else None
    av=max([x for x in (av_port,av_state) if x is not None],default=None); o['av']=av; o['av_port']=av_port; o['av_state_perp']=av_state
    now=st['time'] if st and st.get('time') else fd['fetched_at']
    # open positions
    pos=[]
    if st:
        for p in st['assetPositions']:
            q=p['position']; c=q['coin']; u=float(q['unrealizedPnl']); tr=op.get(c)
            if tr and not tr['partial']: age=(now-tr['open'])/DAY; agelb=False; rpart=tr['pnl']
            else: age=(now-r['window_start'])/DAY if r['window_start'] else None; agelb=True; rpart=tr['pnl'] if tr else 0.0
            pos.append(dict(coin=c,u=u,val=float(q['positionValue']),lev=q['leverage']['value'],age=age,agelb=agelb,rpart=rpart,szi=float(q['szi'])))
    o['n_open']=len(pos)
    up=sum(p['u'] for p in pos if p['u']>0); un=sum(p['u'] for p in pos if p['u']<0)
    o['unreal_now']=up+un; o['unreal_pos']=up; o['unreal_neg']=un
    o['unreal_pos_share']=up/(up-un) if (up-un)>0 else None
    o['unreal_loss_pct_av']=-un/av if av else None
    o['open_ntl']=sum(p['val'] for p in pos); o['cur_lev']=o['open_ntl']/av if av else None
    o['max_pos_lev_setting']=max([p['lev'] for p in pos],default=None)
    losers=[p for p in pos if p['u']<0]
    if losers:
        b=min(losers,key=lambda p:p['u']); o['big_open_loser']=f"{b['coin']} {b['u']:,.0f}$ age {'>' if b['agelb'] else ''}{(b['age'] if b['age'] is not None else float('nan')):.1f}j"
        o['big_open_loser_age_d']=b['age']; o['big_open_loser_pct_av']=-b['u']/av if av else None
    o['open_loser_age_med_d']=med([p['age'] for p in losers if p['age'] is not None])
    o['open_winner_age_med_d']=med([p['age'] for p in pos if p['u']>0 and p['age'] is not None])
    winners=[p for p in pos if p['u']>0]
    if winners:
        b=max(winners,key=lambda p:p['u']); o['big_open_winner']=f"{b['coin']} +{b['u']:,.0f}$ age {'>' if b['agelb'] else ''}{(b['age'] if b['age'] is not None else float('nan')):.1f}j"
    # positions opened before 30d window (uncopyable)
    old=[p for p in pos if p['age'] is not None and p['age']>30]
    o['unreal_from_pos_older_30d']=sum(p['u'] for p in old)
    # closed trades
    T=[t for t in trades if not t['partial']]
    o['closed_trades']=len(T); o['realized_trades_pnl']=sum(t['pnl'] for t in T)
    W=[t for t in T if t['pnl']>0]; L=[t for t in T if t['pnl']<=0]
    o['wr_closed']=len(W)/len(T) if T else None
    o['pf_closed']=(sum(t['pnl'] for t in W)/-sum(t['pnl'] for t in L)) if L and sum(t['pnl'] for t in L)<0 else (math.inf if W else None)
    o['hold_med_win_h']=med([t['hold_min']/60 for t in W]); o['hold_med_loss_h']=med([t['hold_min']/60 for t in L])
    o['hold_mean_win_h']=S.mean([t['hold_min']/60 for t in W]) if W else None; o['hold_mean_loss_h']=S.mean([t['hold_min']/60 for t in L]) if L else None
    o['n_losers_closed']=len(L)
    o['disposition_ratio']=(o['hold_med_loss_h']/o['hold_med_win_h']) if o['hold_med_win_h'] and o['hold_med_loss_h'] is not None and len(L)>=5 else None
    # MTM trade set
    mt=[t['pnl'] for t in T]+[p['rpart']+p['u'] for p in pos]
    mw=[x for x in mt if x>0]; ml=[x for x in mt if x<=0]
    o['n_positions_mtm']=len(mt)
    o['wr_mtm']=len(mw)/len(mt) if mt else None
    o['pf_mtm']=(sum(mw)/-sum(ml)) if ml and sum(ml)<0 else (math.inf if mw else None)
    totm=sum(mt); o['pnl_mtm_trades']=totm
    o['best_share_mtm']=max(mt)/totm if mt and totm>0 else None
    o['best_share_closed']=r['best_share']
    for k in ('hold_median_min','hold_mean_min','acct_lev_twavg','acct_lev_max','maker_share','hip3_share','share_trades_hold_lt5m','coins_n','coins_top','last_fill','window_start','capped_10k','fills_total','fills_30d','spot_fills','long_share','real_pnl_90d'):
        o[k]=r.get(k)
    o['hist_days']=(now-r['window_start'])/DAY if r['window_start'] else None
    o['days_since_last']=(now-r['last_fill'])/DAY if r['last_fill'] else None
    # Account age from the portfolio allTime series (fills cover only 90 d, so hist_days <= 90 cannot test "180 days")
    stamps=[float(x[0]) for k in ('allTime','perpAllTime') for x in ((port.get(k) or {}).get('accountValueHistory') or [])[:1]]
    o['acct_age_days']=(now-min(stamps))/DAY if stamps else None
    o['hours_since_last_fill']=(now-r['last_fill'])/3600000 if r['last_fill'] else None
    o['is_vault']=a in vaults
    # filters
    f=[]
    dd=max([x for x in (o['dd_mtm_7d'],o['dd_mtm_30d']) if x is not None],default=None)
    o['dd_mtm']=dd
    if dd is None or dd>0.20: f.append(f"DD_MTM>20%({None if dd is None else round(dd*100)})")
    strict=o['closed_trades']>=80
    relaxed=(o['closed_trades']>=25 and o['n_positions_mtm']>=30 and (o['hist_days'] or 0)>=60 and (o['hold_med_win_h'] or 0)>=12)
    o['trades_rule']='strict' if strict else ('relaxed' if relaxed else 'fail')
    if not (strict or relaxed): f.append(f"trades({o['closed_trades']}c+{o['n_open']}o)")
    if o['pf_mtm'] is None or o['pf_mtm']<1.5: f.append(f"PF_MTM<1.5({None if o['pf_mtm'] is None else round(o['pf_mtm'],2)})")
    if o['total_30d'] is None or o['total_30d']<=0: f.append('total30d<=0')
    # 2026-11-10: un 7 jours négatif n'est plus un échec (bruit + entrée au sommet) mais un drapeau, voir robust.judge
    if o['total_7d'] is None: f.append('total7d_nd')
    if r['hold_median_min'] is None or r['hold_median_min']<30: f.append('medHold<30m')
    if r['acct_lev_twavg'] is None or r['acct_lev_twavg']>8: f.append('avgLev>8')
    if o['best_share_mtm'] is None or o['best_share_mtm']>0.5: f.append(f"bestPos>50%({None if o['best_share_mtm'] is None else round(o['best_share_mtm'],2)})")
    if o['acct_age_days'] is None or o['acct_age_days']<MIN_ACCT_AGE_DAYS: f.append(f"hist<{MIN_ACCT_AGE_DAYS}d({None if o['acct_age_days'] is None else round(o['acct_age_days'])})")
    if o['hours_since_last_fill'] is None or o['hours_since_last_fill']>MAX_INACTIVE_HOURS: f.append('inactive96h')
    if av is None or not 20e3<=av<=2e6: f.append('AV')
    if o['is_vault']: f.append('VAULT')
    if (o['unreal_loss_pct_av'] or 0)>0.15: f.append(f"HOLDS_LOSERS:latent-{o['unreal_loss_pct_av']*100:.0f}%AV")
    disp=o['disposition_ratio']
    if disp is not None and disp>=3: f.append(f"HOLDS_LOSERS:disp{disp:.1f}")
    if losers and o['hold_med_win_h'] and (o.get('big_open_loser_pct_av') or 0)>0.05 and (o['big_open_loser_age_d'] or 0)*24>3*o['hold_med_win_h']: f.append('HOLDS_LOSERS:old_open_loser')
    if losers and o['hold_med_win_h'] and (o['unreal_loss_pct_av'] or 0)>0.05 and (o['open_loser_age_med_d'] or 0)*24>3*o['hold_med_win_h']: f.append('HOLDS_LOSERS:open_losers_aged')
    # 2026-11-10: rendements journaliers, régularité hebdomadaire, alpha contre beta (bench = prix BTC, None = non jugé)
    o.update(robust.metrics(fills,T,now,bench)); rf,rfl=robust.judge(o); f+=rf
    o['fails']='|'.join(f); o['n_fails']=len(f)
    fl=[]
    if disp is not None and 1.5<=disp<3: fl.append(f"disposition {disp:.1f}")
    if (o['unreal_loss_pct_av'] or 0)>0.05: fl.append(f"latent négatif {o['unreal_loss_pct_av']*100:.0f}% AV")
    if (o['cur_lev'] or 0)>5: fl.append(f"levier actuel {o['cur_lev']:.1f}x")
    if (o['maker_share'] or 0)>0.7: fl.append(f"maker {o['maker_share']:.0%}")
    if (o['hip3_share'] or 0)>0.3: fl.append(f"HIP-3 {o['hip3_share']:.0%}")
    if (o['wr_closed'] or 0)>0.9: fl.append('WR>90% (grid?)')
    if o['unreal_now'] and o['unreal_from_pos_older_30d'] and o['unreal_now']>0 and o['unreal_from_pos_older_30d']/o['unreal_now']>0.5: fl.append('latent surtout sur positions >30j (non copiables)')
    if o.get('gap_possible'): fl.append('trou possible dans les fills (>10k récents)')
    elif (o.get('hist_days') or 99)<30: fl.append(f"historique fills {o['hist_days']:.0f}j (<30j)")
    if o['trades_rule']=='relaxed': fl.append('règle trades assouplie')
    fl+=rfl
    o['flags']='; '.join(fl)
    return o
