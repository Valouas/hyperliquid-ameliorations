#!/usr/bin/env python3
"""Validation walk-forward des filtres du scanner : un filtre prédit-il le résultat des 30 jours SUIVANTS ?

Deux modes, lecture seule (aucun appel réseau, aucune écriture hors --out) :

  fills    Pour chaque wallet du cache data/fills : les filtres sont calculés sur les jours -90 à -30, le résultat est
           mesuré sur les jours -30 à 0. BIAIS CONNU : le cache ne contient que des wallets qui passent ou passaient
           presque AUJOURD'HUI, donc dont les 30 derniers jours ont plutôt été bons. Les taux de réussite sont
           surestimés ; seule la COMPARAISON entre « passe le filtre » et « ne le passe pas » est exploitable.

  history  Utilise la table `analyses` (verdict du scanner daté, sans regard vers le futur) : pour chaque analyse
           vieille d'au moins 30 jours, compare le statut d'alors au résultat réalisé ensuite. Mode sans biais de
           sélection, exploitable dès que l'historique couvre plus de 30 jours.

Résultat mesuré : PnL réalisé net des cycles OUVERTS après la date de coupe (les seuls qu'une copie aurait pu suivre).
Un filtre est utile si « diff » (taux de wallets gagnants avec le filtre moins sans) est positif avec une p-value
faible. Avec moins de 30 wallets par groupe, considérer le résultat comme non concluant.
"""
import argparse
import glob
import gzip
import json
import os
import random
import sqlite3
import sys
from typing import Dict, Iterable, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from lib import criteria as CR, daily, history, metrics, robust  # noqa: E402

MIN_GROUP = 30


def perp_fills(fills: Iterable[dict]) -> List[dict]:
    return [f for f in fills if f["dir"] not in ("Buy", "Sell") and not f["coin"].startswith("@") and "/" not in f["coin"]]


def split(fd: dict, horizon_days: int = 30) -> Tuple[List[dict], List[dict], float]:
    """(fills de formation, fills de test, date de coupe en ms) : la coupe est `horizon_days` avant la collecte."""
    cut = fd["fetched_at"] - horizon_days * CR.DAY_MS
    fills = perp_fills(fd["fills"])
    return [f for f in fills if f["time"] < cut], [f for f in fills if f["time"] >= cut], cut


def outcome(test_fills: List[dict]) -> dict:
    """PnL réalisé net des cycles ouverts dans la fenêtre de test (positions antérieures ignorées)."""
    if not test_fills:
        return {"pnl": 0.0, "trades": 0}
    end = max(f["time"] for f in test_fills)
    _, trades = metrics.compute("", {"fills": test_fills}, {}, end)
    opened_inside = [t for t in trades if not t["partial"]]
    still_open = [t for t in metrics.LAST_OPEN.values() if not t["partial"]]
    return {"pnl": sum(t["pnl"] for t in opened_inside) + sum(t["pnl"] for t in still_open), "trades": len(opened_inside)}


def formation_features(addr: str, formation_fills: List[dict], cut: float) -> Dict[str, Optional[bool]]:
    """Filtres candidats, calculés uniquement avec ce qui était connu à la date de coupe."""
    r, trades = metrics.compute(addr, {"fills": formation_fills}, {}, cut)
    closed = [t for t in trades if not t["partial"]]
    m = robust.metrics(formation_fills, closed, cut, None)
    pf, best = r["profit_factor"], r["best_share"]
    hold = r["hold_median_min"]
    return {
        "pnl_30d>0": r["real_pnl_30d"] > 0,
        "pnl_7d>0": r["real_pnl_7d"] > 0,
        "PF>=1.5": pf is not None and pf >= 1.5,
        "PF<=10": pf is not None and pf <= CR.MAX_PLAUSIBLE_PF,
        "trades>=25": r["trades"] >= 25,
        "medHold>=30m": hold is not None and hold >= 30,
        "bestTrade<=50%": best is not None and best <= 0.5,
        "activeDays>=20": m["daily_active_days"] >= CR.MIN_ACTIVE_DAYS,
        "dailyLower>0": m["daily_boot_lower"] is not None and m["daily_boot_lower"] > 0,
        "posWeeks>=50%": m["weeks_positive_share"] is not None and m["weeks_positive_share"] >= CR.MIN_POSITIVE_WEEK_SHARE,
        "pnl_long>0": m["pnl_long"] > 0,
        "pnl_short>0": m["pnl_short"] > 0,
    }


def _hit(rows: List[dict]) -> float:
    return sum(r["pnl"] > 0 for r in rows) / len(rows)


def feature_table(rows: List[dict], permutations: int = 2000, seed: int = 1) -> List[dict]:
    """Par filtre : effectifs, taux de wallets gagnants avec / sans, écart et p-value par permutation (bilatérale)."""
    rng = random.Random(seed)
    names = sorted({name for row in rows for name in row["features"]})
    table = []
    for name in names:
        judged = [r for r in rows if r["features"].get(name) is not None]
        passed = [r for r in judged if r["features"][name]]
        failed = [r for r in judged if not r["features"][name]]
        if not passed or not failed:
            continue
        diff = _hit(passed) - _hit(failed)
        wins = [r["pnl"] > 0 for r in judged]
        hits = 0
        for _ in range(permutations):
            rng.shuffle(wins)
            a, b = wins[:len(passed)], wins[len(passed):]
            if abs(sum(a) / len(a) - sum(b) / len(b)) >= abs(diff) - 1e-12:
                hits += 1
        table.append({"feature": name, "n_pass": len(passed), "n_fail": len(failed), "hit_pass": _hit(passed),
                      "hit_fail": _hit(failed), "diff": diff, "p_value": (hits + 1) / (permutations + 1),
                      "underpowered": min(len(passed), len(failed)) < MIN_GROUP})
    return sorted(table, key=lambda r: -r["diff"])


def fills_rows(fills_by_addr: Dict[str, dict], horizon_days: int = 30, min_formation_trades: int = 5) -> List[dict]:
    rows = []
    for addr, fd in fills_by_addr.items():
        formation, test, cut = split(fd, horizon_days)
        if not formation or not test:
            continue
        features = formation_features(addr, formation, cut)
        r, _ = metrics.compute(addr, {"fills": formation}, {}, cut)
        if r["trades"] < min_formation_trades:
            continue
        rows.append({"addr": addr, "features": features, "pnl": outcome(test)["pnl"]})
    return rows


def history_rows(analyses: Iterable[dict], fills_by_addr: Dict[str, dict], horizon_days: int = 30) -> List[dict]:
    """Une ligne par analyse assez ancienne dont les fills couvrent tout l'horizon : statut d'alors, résultat ensuite."""
    rows = []
    for a in analyses:
        fd = fills_by_addr.get(a["addr"])
        if not fd or a["status"] == "PREFILTER_FAIL":
            continue
        start = a["t"] * 1000
        end = start + horizon_days * CR.DAY_MS
        if end > fd["fetched_at"]:
            continue
        window = [f for f in perp_fills(fd["fills"]) if start <= f["time"] < end]
        fails = set(filter(None, (a.get("fails") or "").split("|")))
        features: Dict[str, Optional[bool]] = {"PASS": a["status"] in ("PASS", "WATCH_AV")}
        for name in ("BETA_ONLY", "dailyLower<=0"):
            features["sans " + name] = name not in fails
        rows.append({"addr": a["addr"], "t": a["t"], "features": features, "pnl": outcome(window)["pnl"]})
    return rows


def load_fills(folder: str) -> Dict[str, dict]:
    loaded = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.json.gz"))):
        try:
            with gzip.open(path, "rt") as handle:
                loaded[os.path.basename(path)[:-8]] = json.load(handle)
        except (OSError, ValueError):
            continue
    return loaded


def render(table: List[dict], n_rows: int) -> str:
    lines = ["%d wallets/analyses mesurés" % n_rows,
             "%-18s %6s %6s %9s %9s %7s %7s" % ("filtre", "n_oui", "n_non", "gagn_oui", "gagn_non", "écart", "p"),
             "-" * 68]
    for r in table:
        lines.append("%-18s %6d %6d %8.0f%% %8.0f%% %+6.0f%% %7.3f%s" % (
            r["feature"], r["n_pass"], r["n_fail"], r["hit_pass"] * 100, r["hit_fail"] * 100, r["diff"] * 100,
            r["p_value"], "  (<%d par groupe : non concluant)" % MIN_GROUP if r["underpowered"] else ""))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("fills", "history"), default="fills")
    parser.add_argument("--fills", default=os.path.join(BASE, "data", "fills"))
    parser.add_argument("--db", default=os.path.join(BASE, "state.db"))
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--out", default=None, help="écrit aussi le résultat en JSON")
    args = parser.parse_args()
    fills_by_addr = load_fills(args.fills)
    if args.mode == "fills":
        rows = fills_rows(fills_by_addr, args.horizon_days)
        sys.stdout.write("MODE fills : univers biaisé (wallets qui passent aujourd'hui), comparer les groupes seulement.\n")
    else:
        connection = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
        try:
            rows = history_rows(history.rows(connection), fills_by_addr, args.horizon_days)
        finally:
            connection.close()
        sys.stdout.write("MODE history : verdicts datés du scanner contre le résultat des %d jours suivants.\n" % args.horizon_days)
    table = feature_table(rows)
    sys.stdout.write(render(table, len(rows)) + "\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"mode": args.mode, "rows": len(rows), "table": table}, handle, indent=1)


if __name__ == "__main__":
    main()
