#!/usr/bin/env python3
"""Hyperliquid copy-candidate scanner daemon (no AI, no keys, read-only public endpoints, never trades/signs).
Loop: discovery (leaderboard/HyperStats/HyperDash/HypurrScan/hypeRank probe) -> sqlite queue -> portfolio prefilter
-> 90d fills (fixed pagination) -> MTM score -> copy sim (S=125,M=100,3x) -> found_wallets.csv/md + book alerts."""
import os,sys,json,time,gzip,sqlite3,logging,logging.handlers,traceback,datetime,bisect,statistics,shutil,signal,argparse,csv
BASE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,BASE)
from lib import net,score as SC,sim as SIM
from lib import bench as BN,history as HI,exits as EX,book as BK,criteria as CR   # 2026-11-10: robustesse, historique, sorties
from zoneinfo import ZoneInfo
Z=ZoneInfo('Europe/Zurich'); DAY=86400000; H=3600
DATA=os.path.join(BASE,'data'); FD=os.path.join(DATA,'fills'); CD=os.path.join(DATA,'candles')
for d in (DATA,FD,CD): os.makedirs(d,exist_ok=True)
DBP=os.path.join(BASE,'state.db'); PID=os.path.join(BASE,'daemon.pid'); HB=os.path.join(BASE,'heartbeat.json')
DISK_CAP=3*1024**3          # data/ cap (fills+candles)
S_SIZ,M_MAR=125.0,100.0
# HIP-3 (2026-10-20): also simulate builder-deployed perps (fees: official formula, slippage: impactPxs proxy) and let
# HIP-3 trades count in the verdict when they are profitable after fees. File flag 'hip3.disabled' turns it off.
SIM_HIP3=True  # default on: 30d sim of 78 HIP-3 wallets, Δ>0 for 57 (see botreview/hip3)
HIP3_DEXES=('xyz','io')
HIP3_MIN_VOL=2e6
_H3={'t':0,'info':None}
def hip3_enabled(): return SIM_HIP3 and not os.path.exists(os.path.join(BASE,'hip3.disabled'))
def hip3_info():
    """{coin: fee/slip/max_lev} of HIP-3 coins copyable now (live, 24h vol>=2M, impact prices present); cached 1h."""
    if _H3['info'] is not None and time.time()-_H3['t']<3600: return _H3['info']
    meta={}; slip={}
    for d in HIP3_DEXES:
        r=net.info({'type':'metaAndAssetCtxs','dex':d})
        if not r: continue
        meta[d]=r
        for a,x in zip(r[0]['universe'],r[1]):
            ip=x.get('impactPxs'); mid=float(x.get('midPx') or x.get('markPx') or 0)
            slip[a['name']]=((float(ip[1])-float(ip[0]))/2/mid*1e4) if ip and len(ip)==2 and mid>0 else None
    _H3['info']=SIM.hip3_market_info(meta,slip,allowed_dexes=HIP3_DEXES,min_vol=HIP3_MIN_VOL); _H3['t']=time.time()
    return _H3['info']
RECHECK={'PASS':24*H,'WATCH_AV':24*H,'NEAR':60*H,'OTHER':7*24*H,'ERROR':6*H}
DISC_EVERY={'leaderboard':6*H,'hyperstats':12*H,'hyperdash':12*H,'hypurrscan':24*H,'hyperank':24*H,'vaults':24*H}
BOOK_DEFAULT={'main':{'a2e3':'0xa2e366c4e2d453cab49cc9c64f0670fd2fd060a3','05c6':'0x05c6959d997cde99d2f967367e5e9f1959ba9706',
              'b989':'0xb98909dd9cf5a95fe7079ee5bb4698cdad72b05e','0f4f':'0x0f4fbea1aaecd66967af7ef02c7fa4693999834a',
              '3de9':'0x3de9a354243a94e10060a198c7179b8e5393b9a1'},
      'test':{'f838':'0xf83858e57d9f804f5ca1603bce82558119aeac7b','dc52':'0xdc5289909be4f3ce2604806bdd1448fd307f19f5',
              '2d42':'0x2d4299e0ed63ea67f9aa0fbcdc14383f6c574f62'}}
log=logging.getLogger('scanner')
BOOK_FILE=os.path.join(BASE,'book.json'); EXITS_FILE=os.path.join(BASE,'book_exits.json')
def load_book():
    """Wallets suivis: book.json (export PC de policies_multi, tools/export_book.py) sinon les 8 adresses d'origine."""
    try: return BK.load(BOOK_FILE,BOOK_DEFAULT)
    except (OSError,ValueError) as e:
        log.warning('book.json illisible (%s): liste par défaut utilisée',e); return BOOK_DEFAULT
BOOK=load_book()
BOOK_ADDR={a:(b,t) for b,d in BOOK.items() for t,a in d.items()}
BOOK_EVERY=(1 if len(BOOK_ADDR)<=20 else 3)*H   # plus de 20 wallets suivis: vérification toutes les 3 h
def now_ms(): return int(time.time()*1000)
def zt(ts): return datetime.datetime.fromtimestamp(ts,Z).strftime('%Y-%m-%d %H:%M') if ts else ''
STOP=False
_C=None
def _stop(*a):
    global STOP; STOP=True; log.info('signal reçu, arrêt propre après l’étape en cours')
# ---------------- DB
def db():
    c=sqlite3.connect(DBP,timeout=60); c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS wallets(addr TEXT PRIMARY KEY, sources TEXT DEFAULT '', first_seen REAL, last_analyzed REAL,
      next_due REAL, status TEXT DEFAULT 'NEW', n_fails INTEGER, fails TEXT, result TEXT, sim TEXT, ext TEXT DEFAULT '{}',
      first_pass REAL, last_pass REAL, n_analyses INTEGER DEFAULT 0, last_error TEXT);
      CREATE INDEX IF NOT EXISTS ix_due ON wallets(next_due);
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS runs(t REAL, kind TEXT, info TEXT);
      CREATE TABLE IF NOT EXISTS book(addr TEXT PRIMARY KEY, t REAL, result TEXT, alerts TEXT);''')
    HI.ensure(c)
    return c
def meta_get(c,k,d=None):
    r=c.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone(); return json.loads(r[0]) if r else d
def meta_set(c,k,v): c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,json.dumps(v))); c.commit()
def add_addr(c,a,src,ext=None):
    a=a.lower()
    if not (a.startswith('0x') and len(a)==42): return False
    r=c.execute('SELECT sources,ext FROM wallets WHERE addr=?',(a,)).fetchone(); t=time.time()
    if r is None:
        c.execute('INSERT INTO wallets(addr,sources,first_seen,next_due,status,ext) VALUES(?,?,?,?,?,?)',(a,src,t,0,'NEW',json.dumps(ext or {}))); return True
    s=set(filter(None,r['sources'].split(';'))); e=json.loads(r['ext'] or '{}')
    if src not in s or ext:
        s.add(src); e.update(ext or {})
        c.execute('UPDATE wallets SET sources=?,ext=? WHERE addr=?',(';'.join(sorted(s)),json.dumps(e),a))
    return False
# ---------------- seeding from earlier batch scans (so they are rescheduled, not redone at once)
def seed(c):
    if meta_get(c,'seeded'): return
    root=os.path.dirname(BASE); n=0
    srcs={}
    try:
        for r in csv.DictReader(open(f'{root}/scan2/candidates_sources.csv')): srcs[r['addr'].lower()]=r['sources']
    except Exception: pass
    for p,t0 in ((f'{root}/mtm/pass3.json',None),(f'{root}/scan2/scan2_results.json',None)):
        try: L=json.load(open(p)); mt=os.path.getmtime(p)
        except Exception: continue
        for o in L:
            a=o['addr'].lower(); add_addr(c,a,srcs.get(a,'scan_initial_765'))
            st=o.get('status') or ('PASS' if not o.get('fails') else 'FAIL')
            nf=o.get('n_fails') or 0
            cls='PASS' if st in('PASS','WATCH_AV') else ('NEAR' if nf<=2 or st=='WATCH_AV' else 'OTHER')
            if a in BOOK_ADDR: cls='PASS'
            c.execute('UPDATE wallets SET status=?,n_fails=?,fails=?,last_analyzed=?,next_due=? WHERE addr=?',
                      ('SEED_'+st,nf,o.get('fails'),mt,mt+RECHECK[cls],a)); n+=1
    for a in srcs: add_addr(c,a,srcs[a])
    c.commit(); meta_set(c,'seeded',time.time()); log.info('seed: %d résultats antérieurs importés',n)
# ---------------- discovery
VAULTS=set()
def load_vaults(c,force=False):
    global VAULTS
    p=os.path.join(DATA,'vaults.json')
    if force or not os.path.exists(p):
        st,b=net.http('https://stats-data.hyperliquid.xyz/Mainnet/vaults',timeout=180)
        if st==200 and b:
            try:
                VAULTS={v['summary']['vaultAddress'].lower() for v in json.loads(b)}; json.dump(sorted(VAULTS),open(p,'w')); return
            except Exception as e: log.warning('vaults parse %s',e)
    if os.path.exists(p): VAULTS=set(json.load(open(p)))
    elif os.path.exists(os.path.join(os.path.dirname(BASE),'raw/vaults.json')):
        VAULTS={v['summary']['vaultAddress'].lower() for v in json.load(open(os.path.join(os.path.dirname(BASE),'raw/vaults.json')))}
def disc_leaderboard(c):
    st,b=net.http('https://stats-data.hyperliquid.xyz/Mainnet/leaderboard',timeout=300)
    if st!=200: return f'HTTP {st}'
    rows=json.loads(b)['leaderboardRows']; del b
    P=[]
    for r in rows:
        w={k:v for k,v in r['windowPerformances']}; av=float(r['accountValue'])
        P.append((r['ethAddress'].lower(),av,{k:(float(w[k]['pnl']),float(w[k]['roi']),float(w[k]['vlm'])) for k in w}))
    lb={a:(av,w) for a,av,w in P}; meta_set(c,'lb_snapshot_n',len(P))
    json.dump({a:[av,w.get('week',(0,0,0))[0],w.get('month',(0,0,0))[0]] for a,av,w in P},open(os.path.join(DATA,'lb_light.json'),'w'))
    new=0
    def ok(av,w):
        try: return 10e3<=av<=5e6 and w['week'][0]>0 and w['month'][0]>0 and w['allTime'][0]>0 and w['month'][2]/max(av,1)<400 and w['month'][1]<=3
        except KeyError: return False
    for win in ('month','week','allTime'):
        for k,idx in (('pnl',0),('roi',1)):
            cand=[x for x in P if ok(x[1],x[2])]
            cand.sort(key=lambda x:-x[2][win][idx])
            for a,av,w in cand[:400]: new+=add_addr(c,a,f'lb_{win}_{k}',{'lb_av':av})
    c.commit(); return f'{len(P)} lignes, {new} nouvelles'
def disc_hyperstats(c):
    H_={'Origin':'https://hyperstats.org','Referer':'https://hyperstats.org/'}; new=0; seen=0
    for g in ('S+','S','A+'):
        for off in range(0,3000,100):
            st,b=net.http(f'https://v2-api.hyperstats.org/api/traders/top?limit=100&offset={off}&grade={g.replace("+","%2B")}&sortBy=rank&order=asc',headers=H_)
            if st!=200: break
            T=json.loads(b).get('traders') or []
            for t in T:
                seen+=1; new+=add_addr(c,t['address'],'hyperstats_'+g,{'hs_grade':t.get('grade'),'hs_pv':t.get('portfolioValue')})
            if len(T)<100: break
            time.sleep(1.5)
    c.commit(); return f'{seen} vus, {new} nouvelles'
def disc_hyperdash(c):
    H_={'Origin':'https://hyperdash.com','Referer':'https://hyperdash.com/'}; new=0; seen=0
    Q='query($p:Int,$ps:Int,$tf:TraderTimeframe!,$s:TraderSortInput){ exploreTraders(page:$p,pageSize:$ps,timeframe:$tf,sortBy:$s){ data{ address label pnl perpsEquity winrate totalTrades drawdown copyScore: copyScoreV2 tag } pagination{ totalItems totalPages } } }'
    for field,tf,pages in (('copyScoreV2','thirty_days',15),('pnl','thirty_days',10),('pnl','seven_days',6),('copyScoreV2','seven_days',5)):
        for p in range(1,pages+1):
            st,b=net.http('https://api.hyperdash.com/graphql',data={'query':Q,'variables':{'p':p,'ps':100,'tf':tf,'s':{'field':field,'order':'desc'}}},headers=H_)
            if st!=200: break
            d=json.loads(b)
            if 'errors' in d: log.info('hyperdash %s %s: %s',field,tf,d['errors'][0].get('message','')[:120]); break
            D=d['data']['exploreTraders']['data']
            for t in D:
                seen+=1; new+=add_addr(c,t['address'],f'hyperdash_{field}_{tf}',{'hd_copyscore':t.get('copyScore'),'hd_tag':t.get('tag'),'hd_label':t.get('label')})
            if len(D)<100: break
            time.sleep(1.5)
    c.commit(); return f'{seen} vus, {new} nouvelles'
def disc_hypurrscan(c):
    st,b=net.http('https://api.hypurrscan.io/globalAliases')
    if st!=200: return f'HTTP {st}'
    d=json.loads(b); json.dump({k.lower():v for k,v in d.items()},open(os.path.join(DATA,'hypurrscan_aliases.json'),'w'))
    return f'{len(d)} labels (flag seulement)'
def disc_hyperank(c):
    st,b=net.http('https://hyperank.io',timeout=30,retries=1)
    return f'HTTP {st} (pas d’endpoint JSON public connu; source non exploitée)' if st==200 else f'indisponible (HTTP {st})'
DISC={'vaults':lambda c:(load_vaults(c,True),f'{len(VAULTS)} vaults')[1],'leaderboard':disc_leaderboard,'hyperstats':disc_hyperstats,
      'hyperdash':disc_hyperdash,'hypurrscan':disc_hypurrscan,'hyperank':disc_hyperank}
import discovery_ext as DX
DX.register(sys.modules[__name__])
def discovery(c,force=False):
    last=meta_get(c,'disc_last',{}); stat=meta_get(c,'disc_status',{})
    for k,f in DISC.items():
        if STOP: return
        if not force and time.time()-last.get(k,0)<DISC_EVERY[k]: continue
        try: r=f(c)
        except Exception as e: r=f'ERREUR {e}'; log.warning('discovery %s: %s',k,traceback.format_exc()[-400:])
        last[k]=time.time()
        if isinstance(r,str) and (r.startswith('fenêtre trop courte') or r.startswith('pas encore')): last[k]=time.time()-DISC_EVERY[k]+900   # retry in 15 min
        stat[k]={'t':time.time(),'res':r}; meta_set(c,'disc_last',last); meta_set(c,'disc_status',stat)
        log.info('discovery %s: %s',k,r); heartbeat(c,f'discovery {k}')
ALIASES={}
def aliases():
    global ALIASES
    p=os.path.join(DATA,'hypurrscan_aliases.json')
    if os.path.exists(p) and not ALIASES: ALIASES=json.load(open(p))
    return ALIASES
# ---------------- data fetch
def fills_path(a): return os.path.join(FD,a+'.json.gz')
def load_fills(a):
    p=fills_path(a)
    if os.path.exists(p):
        try: return json.load(gzip.open(p,'rt'))
        except Exception: os.remove(p)
    return None
def get_fills(a,days=90):
    """Fixed pagination: walk forward from start until a page < 2000 (no stop at 10k). Incremental when cached."""
    now=now_ms(); start=now-days*DAY; old=load_fills(a); allf=[]; inc=None
    if old and old['fills'] and old['start']<=start+2*DAY:
        allf=[f for f in old['fills'] if f['time']>=start]; inc=max(f['time'] for f in old['fills'])-60000; s=inc
    else: s=start
    seen={(f['tid'],f['oid'],f['time']) for f in allf}; n=0
    while True:
        d=net.info({'type':'userFillsByTime','user':a,'startTime':s,'aggregateByTime':False})
        if d is None: return None
        new=[f for f in d if (f['tid'],f['oid'],f['time']) not in seen]
        for f in new: seen.add((f['tid'],f['oid'],f['time']))
        allf+=new; n+=1
        if _C is not None and n%5==0: heartbeat(_C,f'fills {a} page {n}')   # long histories: keep watchdog informed
        if len(d)<2000 or not new or n>60: break
        s=max(f['time'] for f in d)
    allf.sort(key=lambda f:f['time'])
    fd={'fetched_at':now,'start':start,'fills':allf}
    if inc: fd['incremental_from']=inc
    return fd
def save_fills(a,fd): json.dump(fd,gzip.open(fills_path(a),'wt'))
def prefilter(port,st):
    r={}
    for k in ('week','month'):
        s=port.get(k) or {}; ph=s.get('pnlHistory',[])
        if len(ph)<2: return r,'nohist'
        r['tot_'+k]=float(ph[-1][1])-float(ph[0][1]); r['dd_'+k]=SC.ddcalc(ph,s.get('accountValueHistory',[]))
    avh=(port.get('day') or {}).get('accountValueHistory') or port['month']['accountValueHistory']
    av=float(avh[-1][1]) if avh else 0
    if st: av=max(av,float(st['marginSummary']['accountValue']))
    r['av']=av; why=[]
    # 2026-11-10: un 7 jours négatif ne rejette plus au préfiltre (drapeau '7j<=0' dans le score)
    if r['tot_month']<=0: why.append('total30d<=0')
    if max(r['dd_week'],r['dd_month'])>0.20: why.append('DD_MTM>20%')
    if not (10e3<=av<=5e6): why.append('AV')
    return r,'|'.join(why)
def opens(fd,days=30):
    st=fd['fetched_at']-days*DAY
    return [(f['time'],f['coin'],f['side']) for f in fd['fills'] if f['time']>=st and abs(float(f['startPosition']))<1e-12 and f['dir'].startswith('Open')]
def linked(oa,ob):
    if len(oa)<10 or len(ob)<10: return None
    idx={}
    for t,c,s in ob: idx.setdefault((c,s),[]).append(t)
    for v in idx.values(): v.sort()
    m=0;lags=[]
    for t,c,s in oa:
        L=idx.get((c,s))
        if not L: continue
        i=bisect.bisect_left(L,t-120000)
        if i<len(L) and L[i]<=t+120000: m+=1; lags.append((L[i]-t)/1000)
    return m/len(oa),(statistics.median(lags) if lags else None)
# ---------------- analysis of one address
def classify(o):
    fl=o.get('fails','').split('|') if o.get('fails') else []
    if not fl: return 'PASS'
    if fl==['AV'] and o.get('av') and 10e3<=o['av']<=5e6: return 'WATCH_AV'
    return 'FAIL'
def analyze(a,keep=False,keep_fills=False):
    port=net.info({'type':'portfolio','user':a}); st=net.info({'type':'clearinghouseState','user':a})
    if port is None: raise RuntimeError('portfolio indisponible')
    port=dict(port); pre,why=prefilter(port,st)
    if why: return {'status':'PREFILTER_FAIL','fails':why,'n_fails':len(why.split('|')),'result':dict(pre,addr=a)},None,port
    # prix BTC 1h (1 requête/heure, en cache): témoin alpha contre beta. Une donnée manquante ne devient pas un verdict
    # favorable: sans référence, l'analyse est reportée (ERROR -> nouvel essai, statut précédent conservé).
    bq=BN.load(CD,net.info,now_ms())
    if not bq.times: raise RuntimeError('référence BTC indisponible')
    fd=get_fills(a)
    if fd is None: raise RuntimeError('fills indisponibles')
    o=SC.score(a,fd,port,st,VAULTS,port_time=now_ms(),bench=bq)
    if (o.get('maker_share') or 0)>0.8: o['flags']=(o.get('flags') or '')+'; MM? maker>80%'
    o['status']=classify(o)
    if st:
        o['open_coins']=sorted({p['position']['coin'] for p in st.get('assetPositions',[]) if float(p['position']['szi'])!=0})
        o['lev_by_coin']={p['position']['coin']:p['position']['leverage'].get('value') for p in st.get('assetPositions',[]) if float(p['position']['szi'])!=0}
    simr=None
    if o['status'] in ('PASS','WATCH_AV') or keep:
        try:
            d5=SIM.run(fd,port,CD,S_SIZ,M_MAR,delay=5); d30=SIM.run(fd,port,CD,S_SIZ,M_MAR,delay=30); simr={'d5':d5,'d30':d30}
            if hip3_enabled() and (o.get('hip3_share') or 0)>0:
                H=hip3_info()
                simr['d5h']=SIM.run(fd,port,CD,S_SIZ,M_MAR,delay=5,hip3=H); simr['d30h']=SIM.run(fd,port,CD,S_SIZ,M_MAR,delay=30,hip3=H)
        except Exception as e: log.warning('sim %s: %s',a,e); simr={'error':str(e)}
    nf=o.get('n_fails') or 0
    if o['status'] in ('PASS','WATCH_AV') or nf<=2 or keep or keep_fills: save_fills(a,fd)
    elif os.path.exists(fills_path(a)): os.remove(fills_path(a))
    return {'status':o['status'],'fails':o.get('fails',''),'n_fails':nf,'result':o},simr,port
def schedule(status,nf,fails=''):
    if status in ('PASS','WATCH_AV'): return RECHECK['PASS']
    if status=='PREFILTER_FAIL': return RECHECK['NEAR'] if fails=='AV' else RECHECK['OTHER']
    if nf<=2: return RECHECK['NEAR']
    return RECHECK['OTHER']
def process(c,a):
    r0=c.execute('SELECT * FROM wallets WHERE addr=?',(a,)).fetchone(); t=time.time()
    # un wallet qui a passé récemment garde ses fills même s'il échoue ensuite: sans eux, pas de validation walk-forward
    recent=bool(r0['last_pass'] and t-r0['last_pass']<CR.KEEP_FILLS_DAYS_AFTER_PASS*86400)
    try:
        r,simr,_=analyze(a,keep_fills=recent)
    except Exception as e:
        c.execute('UPDATE wallets SET next_due=?,last_error=?,status=CASE WHEN status="NEW" THEN "ERROR" ELSE status END WHERE addr=?',(t+RECHECK['ERROR'],str(e)[:300],a)); c.commit()
        log.warning('analyse %s erreur: %s',a,e); return
    st=r['status']; fp=r0['first_pass']; lp=r0['last_pass']
    if st in ('PASS','WATCH_AV'):
        fp=fp or t; lp=t
    c.execute('UPDATE wallets SET status=?,n_fails=?,fails=?,result=?,sim=?,last_analyzed=?,next_due=?,first_pass=?,last_pass=?,n_analyses=n_analyses+1,last_error=NULL WHERE addr=?',
              (st,r['n_fails'],r['fails'],json.dumps(r['result'],default=str),json.dumps(simr) if simr else r0['sim'] if st not in('PASS','WATCH_AV') else None,t,t+schedule(st,r['n_fails'],r['fails']),fp,lp,a))
    HI.append(c,a,t,st,r['n_fails'],r['fails'],r['result'],simr)   # historique daté, en ajout seul
    c.commit()
    if st in ('PASS','WATCH_AV'): log.info('%s %s sim5=%s',st,a,(simr or {}).get('d5',{}).get('pnl'))
# ---------------- book re-check
def book_check(c):
    exits={}
    for a,(b,tag) in BOOK_ADDR.items():
        if STOP: return
        try:
            r,simr,port=analyze(a,keep=True); o=r['result']
            reasons=EX.exit_reasons(o,EX.day_move(port)); al=[x['text'] for x in reasons]; exits[a]=reasons
            info_={'status':r['status'],'fails':r['fails'],'sim':simr}
            c.execute('INSERT OR REPLACE INTO book VALUES(?,?,?,?)',(a,time.time(),json.dumps({'r':o,'sim':simr,'status':r['status'],'fails':r['fails']},default=str),json.dumps(al)))
            c.commit()
            if al: log.warning('ALERTE book %s %s: %s',b,tag,'; '.join(al))
        except Exception as e:
            log.warning('book %s erreur %s',tag,e)
            c.execute('INSERT OR REPLACE INTO book VALUES(?,?,COALESCE((SELECT result FROM book WHERE addr=?),"{}"),?)',(a,time.time(),a,json.dumps([f'erreur de vérification: {e}'])))
            c.commit()
        heartbeat(c,f'book {tag}')
    # sorties recommandées, lues côté PC par tools/apply_exits.py (un wallet en erreur n'y figure pas ce tour-ci)
    try:
        tmp=EXITS_FILE+'.tmp'
        with open(tmp,'w',encoding='utf-8') as fh: json.dump(BK.exits_payload(BOOK_ADDR,exits,time.time()),fh,indent=1)
        os.replace(tmp,EXITS_FILE)
    except Exception as e: log.warning('book_exits.json non écrit: %s',e)
    meta_set(c,'book_last',time.time())
# ---------------- outputs
def f_(x,n=0,pct=False):
    if x is None or (isinstance(x,float) and x!=x): return 'n/d'
    if x==float('inf'): return 'inf'
    return f'{x*100:.{n}f}%' if pct else f'{x:,.{n}f}'.replace(',',' ')
def write_outputs(c):
    t=time.time(); al=aliases()
    rows=c.execute("SELECT * FROM wallets WHERE first_pass IS NOT NULL AND result IS NOT NULL").fetchall()
    out=[]
    opn={}
    for r in rows:
        if r['addr'] in BOOK_ADDR: continue
        o=json.loads(r['result']); s=json.loads(r['sim']) if r['sim'] else {}
        cur=r['status'] in ('PASS','WATCH_AV')
        if not cur and t-(r['last_pass'] or 0)>14*86400: continue
        stat='nouveau' if cur and t-r['first_pass']<48*3600 else ('toujours valide' if cur else 'dégradé')
        fd=load_fills(r['addr'])
        if fd: opn[r['addr']]=opens(fd)
        out.append((r,o,s,stat))
    for a in BOOK_ADDR:
        fd=load_fills(a)
        if fd: opn[a]=opens(fd)
    tags={a:(BOOK_ADDR[a][1] if a in BOOK_ADDR else a[2:6]) for a in opn}
    recs=[]
    for r,o,s,stat in out:
        a=r['addr']; links=[]
        for b in opn:
            if b==a or a not in opn: continue
            x=linked(opn[a],opn[b])
            if x and x[0]>=0.3: links.append(f'{tags[b]}{"(book)" if b in BOOK_ADDR else ""} {x[0]:.0%} lag {f_(x[1],0)}s')
        d5=(s.get('d5') or {}); d30=(s.get('d30') or {}); av=o.get('av')
        d5h=(s.get('d5h') or {}); d30h=(s.get('d30h') or {})
        # HIP-3 counts only if, after HIP-3 fees and slippage, it improves BOTH latency sims and its own contribution is > 0.
        hip3_ok=bool(hip3_enabled() and d5h and d30h and (d5h.get('pnl_hip3') or 0)>0 and d5h.get('hip3_orders',0)>0
                     and d5h['pnl']>(d5.get('pnl') or 0) and d30h['pnl']>(d30.get('pnl') or 0))
        if hip3_ok: d5,d30=d5h,d30h
        block=[]
        fl=o.get('flags') or ''
        if 'MM?' in fl: block.append('MM')
        if 'grid' in fl: block.append('grid')
        if 'PF>10' in fl: block.append('PF>10')
        if (o.get('hip3_share') or 0)>0.3 and not hip3_ok: block.append('HIP-3>30%' if not d5h else 'HIP-3>30% non rentable')
        if o.get('is_vault'): block.append('vault')
        if links: block.append('lié/copy-bot')
        if (o.get('hist_days') or 99)<30: block.append('historique<30j')
        if not d5 or d5.get('pnl') is None: block.append('sim n/d')
        elif d5['pnl']<=0 or (d30.get('pnl') or 0)<=0: block.append('sim<=0')
        elif d5['pnl']<10: block.append('sim<10$')
        elif d5.get('orders',0)<10: block.append('sim <10 ordres')
        if r['status']=='WATCH_AV': block.append('AV hors bande 20k-2M')
        verdict='COPIABLE' if stat!='dégradé' and not block else ('DÉGRADÉ' if stat=='dégradé' else 'À SURVEILLER')
        ratio=S_SIZ/av if av else None
        levs=o.get('lev_by_coin') or {}
        ign=sorted(set(o.get('open_coins') or []))
        recs.append({'verdict':verdict,'status':stat,'addr':a,'first_found':zt(r['first_pass']),'last_check':zt(r['last_analyzed']),'first_seen':zt(r['first_seen']),
            'av_total':round(av) if av else None,'pnl_7d':round(o['total_7d']) if o.get('total_7d') is not None else None,'pnl_30d':round(o['total_30d']) if o.get('total_30d') is not None else None,
            'dd_mtm':round(o['dd_mtm']*100,1) if o.get('dd_mtm') is not None else None,'pf_mtm':round(o['pf_mtm'],2) if isinstance(o.get('pf_mtm'),(int,float)) and o['pf_mtm']!=float('inf') else o.get('pf_mtm'),
            'wr':round(o['wr_mtm'],2) if o.get('wr_mtm') is not None else None,'closed':o.get('closed_trades'),'open':o.get('n_open'),'hold_win_h':round(o['hold_med_win_h'],1) if o.get('hold_med_win_h') else None,
            'avg_lev':round(o['acct_lev_twavg'],2) if o.get('acct_lev_twavg') is not None else None,'maker':round(o.get('maker_share') or 0,2),'hip3':round(o.get('hip3_share') or 0,2),
            'long_share':round(o['long_share'],2) if o.get('long_share') is not None else None,'hist_days':round(o['hist_days']) if o.get('hist_days') else None,
            'sim30_d5':d5.get('pnl'),'sim30_d30':d30.get('pnl'),'sim_mdd_pct':d5.get('mdd_pct_margin'),'sim_orders':d5.get('orders'),'sim_quarters':'/'.join(str(x) for x in d5.get('quarters') or []),
            'sizing_S':S_SIZ,'margin':M_MAR,'ratio':f'{ratio:.4e}' if ratio else None,'lev_cap':'min(3x, levier leader/coin, maxLev coin), isolé',
            'hip3_ok':hip3_ok,'sim30_d5_sans_hip3':(s.get('d5') or {}).get('pnl'),'sim30_d5_avec_hip3':d5h.get('pnl'),'sim_hip3_contrib':d5h.get('pnl_hip3'),
            'ignore_coins':';'.join(([] if hip3_ok else ['HIP-3'])+ign),'leader_lev_now':';'.join(f'{k}:{v}x' for k,v in levs.items()),
            'blocking':'; '.join(block),'flags':fl,'linked':'; '.join(links),'sources':r['sources'],'hypurrscan':al.get(a,''),
            'hs_grade':json.loads(r['ext'] or '{}').get('hs_grade'),'hd_copyscore':json.loads(r['ext'] or '{}').get('hd_copyscore')})
    order={'COPIABLE':0,'À SURVEILLER':1,'DÉGRADÉ':2}
    recs.sort(key=lambda x:(order[x['verdict']],-(x['sim30_d5'] or -1e9)))
    tmp=os.path.join(BASE,'found_wallets.csv.tmp')
    with open(tmp,'w',newline='') as f:
        cols=list(recs[0].keys()) if recs else ['verdict','status','addr']
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); [w.writerow(x) for x in recs]
    os.replace(tmp,os.path.join(BASE,'found_wallets.csv'))
    # md
    B=c.execute('SELECT * FROM book').fetchall(); bk={x['addr']:x for x in B}
    L=[f'# Wallets trouvés par le scanner (mis à jour {zt(t)} heure de Zurich)','',
       f'Règles: MTM complètes (30j>0 ; 7j≤0 = drapeau, DD_MTM≤20%, PF_MTM≥1,5, trades, hold, levier≤8x, best≤50%, actif 96h, AV 20k–2M, pas vault, holds-losers) + robustesse du 10/11 (≥20 jours actifs, borne basse du PnL journalier >0, ≥50% de semaines positives, pas « beta seul » contre BTC ; PF>10 bloquant). Sim copie 30j: sizing {S_SIZ:.0f}$, marge {M_MAR:.0f}$, 3x, min 10$ (deltas cumulés), IOC + 2 bps, délai 5s/30s, positions ouvertes dans la fenêtre seulement. HIP-3 (dex xyz/io, vol 24h≥2M$, frais HIP-3 officiels, glissement ≥2 bps via impactPxs) compté seulement s’il améliore les 2 sims (colonne hip3_ok), sinon exclu ; fichier hip3.disabled pour couper. Bougies 15m (le délai 5s vs 30s est donc peu différencié). mdd sim mesuré aux ordres (sous-estimé).','',
       '## Alertes (books)','']
    na=0
    for b,d in BOOK.items():
        for tag,a in d.items():
            x=bk.get(a)
            if x is None: L.append(f'- {b}/{tag}: pas encore vérifié'); continue
            als=json.loads(x['alerts'] or '[]')
            if als: na+=1; L.append(f'- **{b}/{tag}** `{a}` ({zt(x["t"])}): ' + '; '.join(als))
    if not na and bk: L.append('- Aucune règle de retrait déclenchée sur les wallets vérifiés.')
    L+=['','## État des books','','| book | wallet | vérif. | statut MTM | AV | PnL 7j | PnL 30j | DD30 | latent<0 %AV | lev moy | inactif j | sim30 5s | échecs |','|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for b,d in BOOK.items():
        for tag,a in d.items():
            x=bk.get(a)
            if not x: continue
            R=json.loads(x['result'] or '{}'); o=R.get('r',{}); s=(R.get('sim') or {}).get('d5') or {}
            L.append(f"| {b} | {tag} | {zt(x['t'])} | {R.get('status','n/d')} | {f_(o.get('av'))} | {f_(o.get('total_7d'))} | {f_(o.get('total_30d'))} | {f_(o.get('dd_mtm_30d'),1,True)} | {f_(o.get('unreal_loss_pct_av'),1,True)} | {f_(o.get('acct_lev_twavg'),2)} | {f_(o.get('days_since_last'),1)} | {f_(s.get('pnl'),1)} | {R.get('fails') or '—'} |")
    for v in ('COPIABLE','À SURVEILLER','DÉGRADÉ'):
        X=[x for x in recs if x['verdict']==v]
        L+=['',f'## {v} ({len(X)})','']
        if not X: L.append('- aucun'); continue
        for x in X:
            L.append(f"- `{x['addr']}` — {x['status']} (trouvé {x['first_found']}, revu {x['last_check']})")
            L.append(f"  - AV total {f_(x['av_total'])}$ | PnL 7j {f_(x['pnl_7d'])}$ / 30j {f_(x['pnl_30d'])}$ | DD {f_(x['dd_mtm'],1)}% | PF {x['pf_mtm']} | WR {f_(x['wr'],2)} | {x['closed']} fermés/{x['open']} ouverts | hold gagnants {f_(x['hold_win_h'],1)}h | lev moy {f_(x['avg_lev'],2)}x | maker {f_(x['maker'],2,True) if False else x['maker']} | HIP-3 {x['hip3']} | historique {f_(x['hist_days'])}j")
            L.append(f"  - Sim 30j: {f_(x['sim30_d5'],1)}$ (5s) / {f_(x['sim30_d30'],1)}$ (30s), mdd {f_(x['sim_mdd_pct'],1)}% marge, {f_(x['sim_orders'])} ordres, quarts {x['sim_quarters'] or 'n/d'}")
            L.append(f"  - Réglages suggérés: sizing {x['sizing_S']:.0f}$ / marge {x['margin']:.0f}$, ratio {x['ratio']} (×AV total perp+spot à recalculer à l’activation), levier {x['lev_cap']}, ignorer jusqu’à flat: {x['ignore_coins']}")
            extra=[y for y in (x['blocking'] and 'bloquant: '+x['blocking'], x['flags'] and 'flags: '+x['flags'], x['linked'] and 'lié: '+x['linked'], x['hypurrscan'] and 'label HypurrScan: '+x['hypurrscan']) if y]
            L.append(f"  - Sources: {x['sources']}" + (' | '+' | '.join(extra) if extra else ''))
    ds=meta_get(c,'disc_status',{})
    L+=['','## Sources (dernier passage)','']+[f"- {k}: {zt(v['t'])} — {v['res']}" for k,v in ds.items()]
    tmp=os.path.join(BASE,'found_wallets.md.tmp'); open(tmp,'w').write('\n'.join(L)+'\n'); os.replace(tmp,os.path.join(BASE,'found_wallets.md'))
# ---------------- housekeeping
def du(p):
    n=0
    for r,_,fs in os.walk(p):
        for f in fs:
            try: n+=os.path.getsize(os.path.join(r,f))
            except OSError: pass
    return n
def disk_guard(c):
    for f in os.listdir(CD):
        p=os.path.join(CD,f)
        if time.time()-os.path.getmtime(p)>3*86400: os.remove(p)
    if du(DATA)<=DISK_CAP: return
    keep=set(BOOK_ADDR)|{r[0] for r in c.execute("SELECT addr FROM wallets WHERE status IN ('PASS','WATCH_AV') OR (first_pass IS NOT NULL AND last_pass>?)",(time.time()-CR.KEEP_FILLS_DAYS_AFTER_PASS*86400,))}
    L=sorted((os.path.getmtime(os.path.join(FD,f)),f) for f in os.listdir(FD) if f[:-8] not in keep)
    for _,f in L:
        os.remove(os.path.join(FD,f))
        if du(DATA)<=DISK_CAP*0.85: break
    log.info('disk guard: data=%.2f Go',du(DATA)/1024**3)
def heartbeat(c,step):
    q=dict(c.execute("SELECT COUNT(*) n, SUM(next_due<=?) due, SUM(status='NEW') new, SUM(status IN ('PASS','WATCH_AV')) pas FROM wallets",(time.time(),)).fetchone())
    d={'ts':time.time(),'time':zt(time.time()),'pid':os.getpid(),'step':step,'addresses':q['n'],'due':q['due'],'never_analyzed':q['new'],'passing':q['pas'],
       'cycle':meta_get(c,'cycle_n',0)}
    tmp=HB+'.tmp'; json.dump(d,open(tmp,'w')); os.replace(tmp,HB)
# ---------------- main loop
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--once',action='store_true'); ap.add_argument('--max',type=int,default=0,help='max analyses (test)')
    ap.add_argument('--no-discovery',action='store_true'); ap.add_argument('--no-book',action='store_true'); args=ap.parse_args()
    h=logging.handlers.RotatingFileHandler(os.path.join(BASE,'daemon.log'),maxBytes=5*1024**2,backupCount=3)
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s','%Y-%m-%d %H:%M:%S')); logging.basicConfig(level=logging.INFO,handlers=[h])
    signal.signal(signal.SIGTERM,_stop); signal.signal(signal.SIGINT,_stop)
    open(PID,'w').write(str(os.getpid()))
    global _C
    c=db(); _C=c; log.info('démarrage pid %s',os.getpid()); seed(c); load_vaults(c); aliases()
    done=0; last_out=0
    try:
        while not STOP:
            if not args.no_discovery: discovery(c)
            if not args.no_book and time.time()-meta_get(c,'book_last',0)>=BOOK_EVERY:
                book_check(c); write_outputs(c)
            due=c.execute("SELECT addr FROM wallets WHERE next_due<=? ORDER BY CASE status WHEN 'NEW' THEN 0 WHEN 'SEED_PASS' THEN 0 WHEN 'SEED_WATCH_AV' THEN 0 WHEN 'PASS' THEN 1 WHEN 'WATCH_AV' THEN 1 ELSE 2 END, COALESCE(n_fails,9), next_due LIMIT 20",(time.time(),)).fetchall()
            if not due:
                meta_set(c,'cycle_n',meta_get(c,'cycle_n',0)+1); meta_set(c,'queue_empty_at',time.time())
                write_outputs(c); disk_guard(c); HI.prune(c,time.time()); c.commit(); heartbeat(c,'idle')
                if args.once: break
                for _ in range(60):
                    if STOP: break
                    time.sleep(5)
                continue
            for (a,) in due:
                if STOP: break
                if a in BOOK_ADDR:
                    c.execute('UPDATE wallets SET next_due=? WHERE addr=?',(time.time()+RECHECK['PASS'],a)); c.commit(); continue
                process(c,a); done+=1; heartbeat(c,f'analyse {a}')
                if args.max and done>=args.max: break
            if time.time()-last_out>600: write_outputs(c); disk_guard(c); HI.prune(c,time.time()); c.commit(); last_out=time.time()
            if args.max and done>=args.max: break
    except Exception:
        log.error('crash: %s',traceback.format_exc()); raise
    finally:
        try: write_outputs(c); heartbeat(c,'stopped')
        except Exception: log.error('final write: %s',traceback.format_exc())
        if os.path.exists(PID) and open(PID).read().strip()==str(os.getpid()): os.remove(PID)
        log.info('arrêt (analyses: %d)',done)
if __name__=='__main__': main()
