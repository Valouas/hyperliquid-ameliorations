"""HTTP helpers: Hyperliquid info API with a weight budget + 429 backoff; plain GET/POST for discovery."""
import json,time,urllib.request,urllib.error,logging,threading
log=logging.getLogger('net')
HL='https://api.hyperliquid.xyz/info'
UA='Mozilla/5.0 (X11; Linux x86_64) hl-scan-daemon/1.0'
BUDGET_PER_MIN=600          # HL limit is 1200 weight/min/IP; shared by ALL box processes (scanner + discovery modules) via a locked ledger file
import os,fcntl
_LEDGER=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),'data','ratelimit.json')
_lock=threading.Lock()
def _weight(body,n_items=0):
    t=body.get('type')
    base={'clearinghouseState':2,'spotClearinghouseState':2,'allMids':2,'l2Book':2,'metaAndAssetCtxs':20,'userRole':60}.get(t,20)
    extra={'userFillsByTime':n_items//20,'userFills':n_items//20,'candleSnapshot':n_items//60}.get(t,0)
    return base+extra
def _spend(w):
    """Cross-process sliding 60s window stored in data/ratelimit.json (flock)."""
    while True:
        with _lock:
            os.makedirs(os.path.dirname(_LEDGER),exist_ok=True)
            with open(_LEDGER,'a+') as fh:
                fcntl.flock(fh,fcntl.LOCK_EX)
                try:
                    fh.seek(0); raw=fh.read()
                    try: win=json.loads(raw) if raw.strip() else []
                    except Exception: win=[]
                    now=time.time(); win=[x for x in win if x[0]>=now-60]
                    used=sum(x[1] for x in win)
                    if used+w<=BUDGET_PER_MIN or not win:
                        win.append([now,w]); fh.seek(0); fh.truncate(); fh.write(json.dumps(win)); return
                    wait=win[0][0]+60-now
                finally: fcntl.flock(fh,fcntl.LOCK_UN)
        time.sleep(min(max(wait,0.5),10))
def used_last_min():
    try: win=json.load(open(_LEDGER))
    except Exception: return 0
    now=time.time(); return sum(x[1] for x in win if x[0]>=now-60)
import http.client as _hc,ssl
_tl=threading.local()
def _post(body,timeout):
    """Keep-alive HTTPS connection per thread (avoids a TLS handshake per request)."""
    cn=getattr(_tl,'cn',None)
    if cn is None:
        cn=_tl.cn=_hc.HTTPSConnection('api.hyperliquid.xyz',timeout=timeout,context=ssl.create_default_context())
    cn.timeout=timeout
    if cn.sock is not None: cn.sock.settimeout(timeout)
    try:
        cn.request('POST','/info',body=json.dumps(body).encode(),headers={'Content-Type':'application/json','User-Agent':UA,'Connection':'keep-alive'})
        r=cn.getresponse(); data=r.read(); return r.status,data
    except Exception:
        try: cn.close()
        except Exception: pass
        _tl.cn=None; raise
def info(body,retries=8,timeout=60):
    _spend(_weight(body))
    back=5
    typ=body.get('type')
    if typ=='candleSnapshot': retries=min(retries,3); timeout=min(timeout,30)
    for i in range(retries):
        try:
            st,raw=_post(body,timeout)
            if st==429: log.warning('429 on %s, backoff %ss',body.get('type'),back); time.sleep(back); back=min(back*2,300); continue
            if st!=200:
                log.warning('HTTP %s on %s',st,body.get('type'))
                if st==500 and typ=='candleSnapshot': return None   # deterministic (unsupported coin): fail fast, no backoff
                time.sleep(back); back=min(back*2,120); continue
            d=json.loads(raw)
            if isinstance(d,list) and body.get('type') in ('userFillsByTime','candleSnapshot'):
                ex=_weight(body,len(d))-_weight(body)
                if ex>0: _spend(ex)
            return d
        except Exception as e:
            log.warning('err %s on %s',e,body.get('type')); time.sleep(back); back=min(back*2,120)
    return None
def http(url,data=None,headers=None,timeout=90,retries=3):
    h={'User-Agent':UA,'Accept':'application/json'}; h.update(headers or {})
    back=5
    for i in range(retries):
        try:
            req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers=dict(h,**({'Content-Type':'application/json'} if data is not None else {})))
            with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429,503): time.sleep(back); back*=2; continue
            return e.code,b''
        except Exception as e:
            log.warning('http err %s %s',url,e); time.sleep(back); back*=2
    return None,b''
