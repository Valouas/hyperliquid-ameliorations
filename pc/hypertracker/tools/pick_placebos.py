"""Pick placebo wallets: random wallets that pass the scanner's hard filters but were NOT chosen for a book.

Method (reproducible): candidates = scanner state.db rows with status PASS (all hard filters incl. the new
>=180 d history and <=96 h inactivity once the scanner has re-analysed them), minus every wallet already followed
(policies_multi), minus vaults. The list is SORTED by address, shuffled with random.Random(SEED), then walked in that
order; each candidate is RE-VERIFIED LIVE (portfolio: account age >= --min-age-days (30, as in the scanner since 10-31), 7 d and 30 d pnl > 0, AV 20k..2M; fills: a
perp fill in the last 96 h) and the first N that pass are kept. Output: placebo_wallets.json (+ policies via add_wallet.render).
Read-only on the scanner DB; only public Hyperliquid info requests."""
import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
API = "https://api.hyperliquid.xyz/info"
DAY = 86400000


def post(body, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(API, json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=20))
        except Exception:
            time.sleep(2+3*i)
    return None


def check(addr, min_age_days=30):
    now = int(time.time()*1000)
    port = post({"type": "portfolio", "user": addr})
    if not port:
        return None, "portfolio unavailable"
    port = dict(port)
    ts = [float(s["accountValueHistory"][0][0]) for k in ("allTime", "perpAllTime") if port.get(k) and port[k].get("accountValueHistory") for s in (port[k],)]
    age = (now-min(ts))/DAY if ts else None
    why = []
    if age is None or age < min_age_days:
        why.append("age<%dd" % min_age_days)
    for k, lab in (("week", "7d"), ("month", "30d")):
        ph = (port.get(k) or {}).get("pnlHistory") or []
        if len(ph) < 2 or float(ph[-1][1])-float(ph[0][1]) <= 0:
            why.append("pnl%s<=0" % lab)
    avh = (port.get("day") or {}).get("accountValueHistory") or []
    av = float(avh[-1][1]) if avh else None
    if av is None or not 20e3 <= av <= 2e6:
        why.append("AV")
    fills = post({"type": "userFillsByTime", "user": addr, "startTime": now-96*3600000, "aggregateByTime": False})
    perp = [f for f in (fills or []) if not f["coin"].startswith("@") and "/" not in f["coin"]]
    if not perp:
        why.append("inactive96h")
    return {"acct_age_days": age, "av": av, "perp_fills_96h": len(perp)}, "|".join(why)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/workspace/hl-scan/daemon/state.db")
    ap.add_argument("--policies", default="policies_multi")
    ap.add_argument("--out", default="placebo_wallets.json")
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--min-age-days", type=int, default=30, help="same value as MIN_ACCT_AGE_DAYS of the scanner (score.py)")
    args = ap.parse_args()
    followed = set()
    for f in os.listdir(args.policies):
        if f.startswith("policy_") and f.endswith(".json"):
            followed.add(json.load(open(os.path.join(args.policies, f), encoding="utf-8-sig"))["wallets"][0]["address"].lower())
    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    cands = []
    for addr, res in con.execute("SELECT addr, result FROM wallets WHERE status='PASS'"):
        addr = addr.lower()
        try:
            vault = bool(json.loads(res).get("is_vault"))
        except (TypeError, ValueError):
            vault = False
        if addr not in followed and not vault:
            cands.append(addr)
    cands.sort()
    random.Random(args.seed).shuffle(cands)
    print("candidates (PASS, not followed, not vault): %d, seed %d" % (len(cands), args.seed))
    picked, rejected = [], []
    for addr in cands:
        info, why = check(addr, args.min_age_days)
        if why:
            rejected.append({"addr": addr, "why": why})
            print("reject", addr, why)
            continue
        picked.append(dict(addr=addr, **info))
        print("PICK  ", addr, info)
        if len(picked) >= args.n:
            break
    out = {"seed": args.seed, "method": __doc__.split("\n\n")[0:2], "picked_at_epoch": int(time.time()), "candidates": len(cands), "picked": picked,
           "rejected_before_pick": rejected, "shuffled_order_head": cands[:30]}
    json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
