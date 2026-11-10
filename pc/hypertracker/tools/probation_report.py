"""Essai fantôme : un nouveau wallet (books.<préfixe>.mode = "probation") n'ouvre rien sur son compte principal ;
seul son fantôme (0 s de latence, prix du leader) trade. Ce rapport applique une règle de promotion FIXÉE À L'AVANCE.

Règle (ne pas la modifier après avoir vu les résultats) :
  prêt     = (>= 14 jours OU >= 20 allers-retours fermés) ET >= 7 jours ET >= 5 allers-retours
  PROMOTE  = prêt, PnL fantôme > 0 et PnL/jour > médiane des placebos (si des placebos actifs existent)
  REJECT   = prêt sans remplir ces conditions, ou stop global atteint
  WAIT     = pas encore prêt
Le rapport ne modifie rien. Pour promouvoir : retirer "mode" du book dans multi_runner.json puis redémarrer multi2.

Usage : py -3 tools\\probation_report.py --state hypertracker_state --config multi_runner.json [--out fichier.json]
"""
import json
import os
import statistics
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book_data  # noqa: E402
from val_common import fmt, parse_args, table, write_out  # noqa: E402

MIN_DAYS, MIN_TRIPS = 14.0, 20
FLOOR_DAYS, FLOOR_TRIPS = 7.0, 5


def verdict(days: Optional[float], trips: int, pnl: float, placebo_per_day: Optional[float],
            global_stop: int) -> Tuple[str, str]:
    if global_stop:
        return "REJECT", "stop global atteint"
    days = days or 0.0
    ready = (days >= MIN_DAYS or trips >= MIN_TRIPS) and days >= FLOOR_DAYS and trips >= FLOOR_TRIPS
    if not ready:
        return "WAIT", "%.1f j, %d allers-retours : preuve insuffisante" % (days, trips)
    if pnl <= 0:
        return "REJECT", "PnL fantôme <= 0"
    if placebo_per_day is not None and pnl / days <= placebo_per_day:
        return "REJECT", "ne bat pas la médiane des placebos (%.2f $/j)" % placebo_per_day
    return "PROMOTE", "PnL > 0 et au-dessus des placebos"


def placebo_reference(state_dir: str) -> Optional[float]:
    """Médiane du PnL par jour des placebos qui ont au moins un aller-retour fermé (None s'il n'y en a pas)."""
    rates = []
    for prefix in book_data.prefixes(state_dir, "multi_placebo"):
        book = book_data.load(state_dir, "multi_placebo", prefix)
        if book and book["days"] and book["trips"]:
            rates.append(book["pnl"] / book["days"])
    return statistics.median(rates) if rates else None


def report(state_dir: str, config: dict) -> List[dict]:
    reference = placebo_reference(state_dir)
    rows = []
    for prefix, own in sorted(config.get("books", {}).items()):
        if own.get("mode") != "probation":
            continue
        ghost = book_data.load(state_dir, "multi_ghost", prefix)
        if ghost is None:
            rows.append({"book": prefix, "verdict": "WAIT", "why": "fantôme pas encore démarré", "days": None,
                         "trips": 0, "pnl": None, "placebo_per_day": reference})
            continue
        status, why = verdict(ghost["days"], len(ghost["trips"]), ghost["pnl"], reference, ghost["global_stop"])
        rows.append({"book": prefix, "verdict": status, "why": why, "days": ghost["days"], "trips": len(ghost["trips"]),
                     "pnl": ghost["pnl"], "placebo_per_day": reference})
    return rows


def main() -> None:
    args = parse_args(__doc__, lambda p: p.add_argument("--config", default="multi_runner.json"))
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    rows = report(args.state, config)
    lines = [[r["book"], r["verdict"], fmt(r["days"], 1), r["trips"], fmt(r["pnl"]), r["why"]] for r in rows]
    sys.stdout.write(table(lines, ["book", "verdict", "jours", "trips", "pnl_fantôme", "motif"]) + "\n")
    if not rows:
        sys.stdout.write('Aucun book en mode "probation" dans la configuration.\n')
    write_out(args, {"rule": {"min_days": MIN_DAYS, "min_trips": MIN_TRIPS, "floor_days": FLOOR_DAYS,
                              "floor_trips": FLOOR_TRIPS}, "rows": rows})


if __name__ == "__main__":
    main()
