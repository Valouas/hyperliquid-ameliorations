"""Copy simulation (target-position replay) with the paper config: sizing S, margin M, 3x, HIP-3 excluded by default,
10$ min with accumulation, IOC (2 bps spread + latency drift), only positions opened inside the 30d window
(a coin is followed once leader is flat on it, or flips through zero).

HIP-3 (2026-10-20): run(..., hip3=H) where H is a dict built by hip3_market_info() includes builder-deployed perps
('dex:COIN') that are eligible NOW (live, dex allowed, 24h volume >= min, book measured) with their real fee
(base taker x deployer scale x growth mode, docs: hyperliquid-docs/trading/fees#fee-formula-for-developers) and a
per-coin slippage in bps (measured half-spread + impact). With hip3=None the result is identical to the previous
version (same fills, same arithmetic). Extra outputs (pnl_hip3, hip3_orders, coins) only appear when hip3 is given."""
import bisect,json,os,time,gzip
from .net import info
DAY=86400000
class Px:
    def __init__(self,s): self.t=[a for a,b in s]; self.v=[b for a,b in s]
    def at(self,t):
        if not self.t: return None
        i=bisect.bisect_right(self.t,t)
        if i==0: return self.v[0]
        if i>=len(self.t): return self.v[-1]
        t0,t1=self.t[i-1],self.t[i]; v0,v1=self.v[i-1],self.v[i]
        return v0+(v1-v0)*(t-t0)/max(t1-t0,1)
def candles(cdir,coin,start,end):
    """15m candles over the window (cheap: ~70 weight/coin), cached per coin for 6h."""
    if coin.startswith('#'): return Px([])   # outcome-market tickers: candleSnapshot always answers HTTP 500
    p=os.path.join(cdir,coin.replace(':','_').replace('/','_')+'.json')
    if os.path.exists(p):
        d=json.load(open(p))
        if d['start']<=start+DAY and d['end']>=end-6*3600000 and time.time()-os.path.getmtime(p)<6*3600: return Px(d['pts'])
    d=info({'type':'candleSnapshot','req':{'coin':coin,'interval':'15m','startTime':start,'endTime':end}})
    if d is None: return Px([])   # fetch failed: do not cache an empty file for 6h, retry on the next analysis
    pts={}
    for c in d: pts[c['t']]=float(c['o']); pts[c['T']]=float(c['c'])
    s=sorted(pts.items()); json.dump({'start':start,'end':end,'pts':s},open(p,'w')); return Px(s)
def av_lookup(port):
    pts={}
    for k in ('allTime','month','week','day'):
        for t,v in (port.get(k) or {}).get('accountValueHistory',[]): pts[t]=float(v)
    ts=sorted(pts); return ts,[pts[t] for t in ts]
def is_hip3(c): return ':' in c
def hip3_fee(meta,base_taker=0.00045):
    """All-in taker rate of a HIP-3 asset for a base-tier user (official fee formula, no staking/referral)."""
    s=float(meta.get('deployerFeeScale') or 1.0)
    mult=s+1 if s<1 else 2*s
    return base_taker*mult*(0.1 if meta.get('growthMode')=='enabled' else 1.0)
def hip3_market_info(dex_meta,books=None,allowed_dexes=('xyz',),min_vol=2e6,min_slip_bps=2.0,base_taker=0.00045):
    """dex_meta: {dex: [meta, ctxs]} from metaAndAssetCtxs(dex). books: {coin: slippage_bps} measured (None = unknown).
    Returns {coin: {'fee','slip_bps','max_lev'}} for HIP-3 coins copyable NOW; anything else stays excluded."""
    out={}
    for d,(m,ctx) in dex_meta.items():
        if d not in allowed_dexes: continue
        for a,x in zip(m['universe'],ctx):
            c=a['name']
            if a.get('isDelisted') or float(x['dayNtlVlm'])<min_vol: continue
            if books is not None and books.get(c) is None: continue
            slip=max(min_slip_bps,books[c]) if books is not None else min_slip_bps
            out[c]={'fee':hip3_fee(a,base_taker),'slip_bps':slip,'max_lev':a['maxLeverage']}
    return out
def in_weekend(t_ms):
    """xyz internal-pricing session Fri 20:00 -> Sun 20:05 America/New_York (same rule as the engine option)."""
    import datetime
    from zoneinfo import ZoneInfo
    t=datetime.datetime.fromtimestamp(t_ms/1000,ZoneInfo('America/New_York')); m=t.hour*60+t.minute
    return (t.weekday()==4 and m>=1200) or t.weekday()==5 or (t.weekday()==6 and m<1205)
def run(fd,port,cdir,S=125.0,M=100.0,delay=5,min_ntl=10.0,taker=0.00045,spread=2.0,hip3=None,slip_mult=1.0,hip3_weekend_pause=False):
    now=fd['fetched_at']; st=now-30*DAY
    ts,vs=av_lookup(port)
    def avf(t):
        i=bisect.bisect_right(ts,t)-1; return vs[max(i,0)] if ts else None
    H=hip3 or {}
    fills=[f for f in fd['fills'] if f['time']>=st and f['dir'] not in ('Buy','Sell') and not f['coin'].startswith('@') and '/' not in f['coin'] and (not is_hip3(f['coin']) or f['coin'] in H) and not f['coin'].startswith('#')]
    coins=sorted(set(f['coin'] for f in fills)); pxs={c:candles(cdir,c,st,now) for c in coins}
    coin_cap=min(1.5*S,3*M); tot_cap=3*M
    cash=0;pos={};tr={};lp={};curve=[];orders=0;skipped=0
    cc={};ho=0
    for f in fills:
        c=f['coin'];sz=float(f['sz']);sg=1 if f['side']=='B' else -1;sp=float(f['startPosition']);ep=sp+sg*sz;lpx=float(f['px'])
        if c not in tr: tr[c]=abs(sp)<1e-12
        if not tr[c]:
            if abs(ep)<1e-12: tr[c]=True; continue
            if sp*ep<0: tr[c]=True
            else: continue
        av=avf(f['time'])
        if not av or av<=0: continue
        tgt=ep*S/av
        p0=pxs[c].at(f['time']);p1=pxs[c].at(f['time']+delay*1000);px=lpx+((p1-p0) if p0 and p1 else 0)
        ccap=coin_cap if c not in H else min(coin_cap,min(3,H[c]['max_lev'])*M)
        if abs(tgt)*px>ccap: tgt=ccap/px*(1 if tgt>0 else -1)
        cur=pos.get(c,0);d=tgt-cur
        if abs(ep)<1e-12: d=-cur
        else:
            other=sum(abs(q)*lp.get(k,px) for k,q in pos.items() if k!=c);room=max(tot_cap-other,0)/px
            if abs(cur+d)>room and abs(cur+d)>abs(cur): d=room*(1 if tgt>0 else -1)-cur
        if abs(d)<1e-15: continue
        if c in H and hip3_weekend_pause and abs(cur+d)>abs(cur) and in_weekend(f['time']):
            # engine: no new HIP-3 entry during the weekend session; a cycle opened then is not followed
            if abs(cur)<1e-12: tr[c]=False
            skipped+=1; continue
        if c in H:
            sl=H[c]['slip_bps']*slip_mult; fee=H[c]['fee']
        else:
            sl=spread*slip_mult if hip3 is not None else spread; fee=taker
        px*=(1+sl/1e4) if d>0 else (1-sl/1e4)
        if abs(d)*px<min_ntl and abs(ep)>1e-12: skipped+=1; continue
        cash-=d*px+abs(d)*px*fee;pos[c]=cur+d;lp[c]=px;orders+=1
        if hip3 is not None:
            cc[c]=cc.get(c,0)-(d*px+abs(d)*px*fee)
            if c in H: ho+=1
        curve.append((f['time'],cash+sum(q*lp[k] for k,q in pos.items()),sum(abs(q)*lp[k] for k,q in pos.items())))
    mtm=sum(q*(pxs[k].v[-1] if pxs[k].v else lp[k]) for k,q in pos.items()); pnl=cash+mtm
    pk=-1e18;mdd=0;peak=0
    for t,e,n in curve: pk=max(pk,e);mdd=max(mdd,(pk-e)/M);peak=max(peak,n)
    qs=[]
    if curve:
        T=[x[0] for x in curve];E=[x[1] for x in curve];prev=0.0
        for i in range(1,4):
            j=bisect.bisect_right(T,st+i*7.5*DAY)-1; v=E[j] if j>=0 else 0.0; qs.append(round(v-prev,1)); prev=v
        qs.append(round(pnl-prev,1))
    r=dict(pnl=round(pnl,2),orders=orders,skipped=skipped,mdd_pct_margin=round(mdd*100,1),peak_ntl=round(peak),quarters=qs)
    if hip3 is not None:
        per={k:v+pos.get(k,0)*(pxs[k].v[-1] if pxs[k].v else lp[k]) for k,v in cc.items()}
        r['pnl_hip3']=round(sum(v for k,v in per.items() if is_hip3(k)),2); r['hip3_orders']=ho
        r['hip3_coins']={k:round(v,2) for k,v in sorted(per.items(),key=lambda kv:kv[1]) if is_hip3(k)}
    return r
