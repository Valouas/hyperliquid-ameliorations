LAST_OPEN={}
import json,os,bisect,statistics as S
DAY=86400000
def av_lookup(port):
    # merge allTime+month+week accountValueHistory for interpolation
    pts={}
    for k in ('allTime','month','week','day'):
        for t,v in port.get(k,{}).get('accountValueHistory',[]): pts[t]=float(v)
    ts=sorted(pts); return ts,[pts[t] for t in ts]
def av_at(ts,vs,t):
    i=bisect.bisect_right(ts,t)-1
    return vs[max(i,0)] if ts else None
def compute(a,fd,port,now):
    fills=[f for f in fd['fills'] if f['dir'] not in ('Buy','Sell') and not f['coin'].startswith('@') and '/' not in f['coin']]
    spot=len(fd['fills'])-len(fills)
    ts,vs=av_lookup(port)
    pos={};open_={};trades=[];gpos={};gpx={};levs=[];lastt=None;lev_tw=0;tw=0;levmax=0
    for f in fills:
        _c=f['coin'];_ep=float(f['startPosition'])+(1 if f['side']=='B' else -1)*float(f['sz'])
        if lastt is not None and levs:
            dt=f['time']-lastt; lev_tw+=levs[-1]*dt; tw+=dt
        gpos[_c]=_ep; gpx[_c]=float(f['px'])
        _av=av_at(ts,vs,f['time'])
        if _av and _av>0:
            L=sum(abs(q)*gpx[k] for k,q in gpos.items())/_av; levs.append(L); levmax=max(levmax,L)
        lastt=f['time']
        c=f['coin'];sz=float(f['sz']);px=float(f['px']);sp=float(f['startPosition'])
        sgn=1 if f['side']=='B' else -1
        endp=sp+sgn*sz
        net=float(f['closedPnl'])-float(f['fee'])
        tr=open_.get(c)
        if tr is None and abs(sp)>1e-12:
            # position opened before window: start partial trade (flag)
            tr={'coin':c,'open':f['time'],'pnl':0,'fees':0,'peak':abs(sp)*px,'nf':0,'partial':True,'side':'L' if sp>0 else 'S'}
            open_[c]=tr
        if tr is None:
            tr={'coin':c,'open':f['time'],'pnl':0,'fees':0,'peak':0,'nf':0,'partial':False,'side':'L' if endp>0 else 'S'}
            open_[c]=tr
        flip=(sp>0 and endp<0) or (sp<0 and endp>0)
        if flip:
            # split fee proportionally
            frac=abs(sp)/sz; fee=float(f['fee'])
            tr['pnl']+=float(f['closedPnl'])-fee*frac; tr['fees']+=fee*frac; tr['nf']+=1
            tr['close']=f['time']; trades.append(tr)
            tr={'coin':c,'open':f['time'],'pnl':-fee*(1-frac),'fees':fee*(1-frac),'peak':abs(endp)*px,'nf':1,'partial':False,'side':'L' if endp>0 else 'S'}
            open_[c]=tr
        else:
            tr['pnl']+=net; tr['fees']+=float(f['fee']); tr['nf']+=1
            tr['peak']=max(tr['peak'],abs(endp)*px)
            if abs(endp)<1e-9:
                tr['close']=f['time']; trades.append(tr); del open_[c]
    for tr in trades:
        tr['hold_min']=(tr['close']-tr['open'])/60000
        avo=av_at(ts,vs,tr['open']); tr['lev']=tr['peak']/avo if avo and avo>0 else None
    full=[t for t in trades if not t['partial']]
    def win(ws):
        return [f for f in fills if f['time']>=now-ws]
    r={'addr':a,'fills_total':len(fd['fills']),'perp_fills':len(fills),'spot_fills':spot,
       'window_start':min([f['time'] for f in fills],default=None),'capped_10k':len(fd['fills'])>=10000}
    for lab,ws in (('7d',7*DAY),('30d',30*DAY),('90d',90*DAY)):
        ff=win(ws); r['real_pnl_'+lab]=sum(float(f['closedPnl'])-float(f['fee']) for f in ff); r['fills_'+lab]=len(ff)
    T=full
    r['trades']=len(T); r['trades_30d']=sum(1 for t in T if t['close']>=now-30*DAY)
    wins=[t['pnl'] for t in T if t['pnl']>0]; losses=[t['pnl'] for t in T if t['pnl']<=0]
    r['win_rate']=len(wins)/len(T) if T else None
    r['profit_factor']=(sum(wins)/-sum(losses)) if losses and sum(losses)<0 else (float('inf') if wins else None)
    T30=[t for t in T if t['close']>=now-30*DAY]
    w30=[t['pnl'] for t in T30 if t['pnl']>0]; l30=[t['pnl'] for t in T30 if t['pnl']<=0]
    r['win_rate_30d']=len(w30)/len(T30) if T30 else None
    r['pf_30d']=(sum(w30)/-sum(l30)) if l30 and sum(l30)<0 else (float('inf') if w30 else None)
    tot=sum(t['pnl'] for t in T)
    r['trade_pnl_total']=tot
    best=max(T,key=lambda t:t['pnl']) if T else None
    r['best_trade_pnl']=best['pnl'] if best else None; r['best_trade_coin']=best['coin'] if best else None
    r['best_share']=best['pnl']/tot if best and tot>0 else None
    hs=[t['hold_min'] for t in T]
    r['hold_mean_min']=S.mean(hs) if hs else None; r['hold_median_min']=S.median(hs) if hs else None
    r['share_trades_hold_lt5m']=sum(h<5 for h in hs)/len(hs) if hs else None
    lv=[t['lev'] for t in T if t['lev'] is not None]
    r['lev_mean']=S.mean(lv) if lv else None; r['lev_median']=S.median(lv) if lv else None; r['lev_p90']=sorted(lv)[int(.9*len(lv))-1] if len(lv)>=10 else (max(lv) if lv else None)
    r['acct_lev_twavg']=lev_tw/tw if tw else None; r['acct_lev_max']=levmax if levs else None
    r['maker_share']=sum(1 for f in fills if not f['crossed'])/len(fills) if fills else None
    r['fills_per_trade']=len(fills)/len(T) if T else None
    from collections import Counter
    cc=Counter(f['coin'] for f in fills); r['coins_n']=len(cc); r['coins_top']=';'.join(f'{k}:{v}' for k,v in cc.most_common(6))
    r['hip3_share']=sum(v for k,v in cc.items() if ':' in k)/len(fills) if fills else None
    r['last_fill']=max([f['time'] for f in fd['fills']],default=None)
    r['open_trades_now']=len(open_)
    r['long_share']=sum(1 for t in T if t['side']=='L')/len(T) if T else None
    # daily consistency over 30d
    days={}
    for f in win(30*DAY): d=f['time']//DAY; days[d]=days.get(d,0)+float(f['closedPnl'])-float(f['fee'])
    r['days_active_30d']=len(days); r['pos_days_30d']=sum(v>0 for v in days.values())
    global LAST_OPEN
    LAST_OPEN=dict(open_)
    return r,trades
