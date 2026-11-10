"""Shared helpers of the validation reports (read-only, stdlib only, run from the hypertracker folder).

Round trip = one position cycle of one book on one coin, rebuilt from feed_exec.jsonl `paper_fill` records
(variant all_ioc, the variant the previous analyses used). PnL = price PnL - fees (recorded `fee`, else taker fee
x notional); funding is NOT included (it is small and not in feed_exec)."""
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict

TAKER_FEE = 0.00045


def jsonl(pattern):
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue          # a half-written last line


def feed_exec(root, role_dir, prefix):
    return jsonl(os.path.join(root, role_dir, prefix, "feed_exec.jsonl"))


def prefixes(root, role_dir):
    base = os.path.join(root, role_dir)
    return sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "feed_exec.jsonl"))) if os.path.isdir(base) else []


def round_trips(fills):
    """fills: paper_fill records (all_ioc) in time order -> list of closed round trips (dicts)."""
    pos = {}      # coin -> state
    trips = []
    for f in sorted(fills, key=lambda r: r["at"]):
        coin, qty, px = f["coin"], f["qty"], f["price"]
        fee = f.get("fee")
        if fee is None:
            fee = abs(qty)*px*TAKER_FEE
        s = pos.get(coin)
        if s is None or abs(s["qty"]) < 1e-12:
            s = pos[coin] = {"qty": 0.0, "entry": 0.0, "pnl": 0.0, "fees": 0.0, "opened": f["at"], "signal_at": f.get("signal_at"),
                             "consensus": f.get("consensus"), "dir": 1 if qty > 0 else -1, "coin": coin, "entry_notional": 0.0,
                             "exit_causes": set(), "first_px": px}
        if s["qty"] == 0 or (s["qty"] > 0) == (qty > 0):
            s["entry"] = (abs(s["qty"])*s["entry"] + abs(qty)*px)/(abs(s["qty"])+abs(qty))
            s["qty"] += qty
            s["entry_notional"] += abs(qty)*px
            s["fees"] += fee
        else:
            closed = min(abs(qty), abs(s["qty"]))
            s["pnl"] += closed*(1 if s["qty"] > 0 else -1)*(px-s["entry"])
            s["fees"] += fee
            s["qty"] += qty
            s["exit_causes"].add(f.get("cause") or "signal")
            if abs(s["qty"]) < 1e-12:
                s["closed"] = f["at"]
                s["net"] = s["pnl"]-s["fees"]
                s["exit_causes"] = sorted(s["exit_causes"])
                trips.append(s)
                del pos[coin]
    return trips, pos


def summarize(values):
    """n, sum, mean, median, win rate, profit factor of a list of net PnL values."""
    if not values:
        return {"n": 0, "sum": 0.0, "mean": None, "median": None, "win": None, "pf": None}
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    return {"n": len(values), "sum": sum(values), "mean": sum(values)/len(values), "median": statistics.median(values),
            "win": sum(1 for v in values if v > 0)/len(values), "pf": (wins/losses if losses else None)}


def fmt(x, nd=2):
    return "-" if x is None else ("%.*f" % (nd, x))


def table(rows, header):
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)] if rows else [len(h) for h in header]
    line = lambda r: "  ".join(str(x).rjust(w) if i else str(x).ljust(w) for i, (x, w) in enumerate(zip(r, widths)))
    out = [line(header), line(["-"*w for w in widths])] + [line(r) for r in rows]
    return "\n".join(out)


def percentile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values)-1, int(q*len(sorted_values)))]


def spearman(xs, ys):
    """Spearman rank correlation with average ranks for ties. Returns (rho, n); rho None when undefined."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None and not (math.isnan(x) or math.isnan(y))]
    n = len(pairs)
    if n < 3:
        return None, n
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        r = [0.0]*len(values)
        i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and values[order[j+1]] == values[order[i]]:
                j += 1
            for k in range(i, j+1):
                r[order[k]] = (i+j)/2+1
            i = j+1
        return r
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx)/n, sum(ry)/n
    cov = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    vx, vy = sum((a-mx)**2 for a in rx), sum((b-my)**2 for b in ry)
    return (cov/math.sqrt(vx*vy) if vx > 0 and vy > 0 else None), n


def spearman_pvalue(rho, n, permutations=20000, xs=None, ys=None, seed=1):
    """Two-sided permutation p-value (exact enough for n ~ 30; no scipy needed)."""
    import random
    if rho is None or xs is None:
        return None
    rng = random.Random(seed)
    ys = list(ys)
    hits = 0
    for _ in range(permutations):
        rng.shuffle(ys)
        r, _ = spearman(xs, ys)
        if r is not None and abs(r) >= abs(rho)-1e-12:
            hits += 1
    return (hits+1)/(permutations+1)


def parse_args(description, extra=None):
    import argparse
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--state", default="hypertracker_state", help="hypertracker_state folder (default: ./hypertracker_state)")
    parser.add_argument("--since", type=float, default=0, help="epoch seconds lower bound")
    parser.add_argument("--out", default=None, help="also write the result as JSON to this file")
    if extra:
        extra(parser)
    return parser.parse_args()


def write_out(args, result):
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
