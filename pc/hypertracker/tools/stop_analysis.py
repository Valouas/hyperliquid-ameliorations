"""Les stops protègent-ils ou coupent-ils avant le rebond ?

Pour chaque copie fermée par un stop (paper_fill, cause "stop"), compare le prix du stop au prix auquel le LEADER a
ensuite réduit ou fermé la même position (`exit_ref` de la ligne de position, postérieur au stop).
  coût > 0 : tenir jusqu'à la sortie du leader aurait rapporté davantage -> le stop a coûté
  coût < 0 : le stop a évité une perte supplémentaire
Limites : `exit_ref` est la DERNIÈRE réduction connue du leader sur ce coin (elle peut appartenir à un cycle
ultérieur) ; funding et frais de la sortie alternative ignorés ; un leader encore en position n'est pas chiffré.
Moins de 30 stops chiffrés = anecdote, pas une preuve. Lecture seule.

Usage : py -3 tools\\stop_analysis.py --state hypertracker_state [--out fichier.json]
"""
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book_data  # noqa: E402
from val_common import fmt, parse_args, table, write_out  # noqa: E402

MIN_SAMPLE = 30


def stop_rows(events: List[dict], positions: dict) -> List[dict]:
    rows = []
    for event in events:
        if event.get("kind") != "paper_fill" or event.get("cause") != "stop":
            continue
        position = positions.get("%s:%s" % (event.get("address"), event["coin"])) or {}
        exit_ref, exit_at = position.get("exit_ref"), position.get("exit_ref_at")
        priced = exit_ref is not None and exit_at is not None and exit_at > event["at"]
        rows.append({"coin": event["coin"], "at": event["at"], "stop_price": event["price"], "quantity": event["quantity"],
                     "leader_exit": exit_ref if priced else None,
                     "status": "leader_exited" if priced else "leader_still_open",
                     "cost": -event["quantity"] * (exit_ref - event["price"]) if priced else None})
    return rows


def summarize(rows: List[dict]) -> dict:
    priced = [r["cost"] for r in rows if r["cost"] is not None]
    return {"stops": len(rows), "priced": len(priced), "net_cost": sum(priced),
            "stops_that_cost": sum(c > 0 for c in priced), "stops_that_saved": sum(c < 0 for c in priced)}


def main() -> None:
    args = parse_args(__doc__)
    per_book, everything = [], []
    for prefix in book_data.prefixes(args.state, "multi"):
        book = book_data.load(args.state, "multi", prefix)
        rows = stop_rows(book["events"], book["positions"]) if book else []
        if rows:
            summary = summarize(rows)
            per_book.append([prefix, summary["stops"], summary["priced"], fmt(summary["net_cost"]),
                             summary["stops_that_cost"], summary["stops_that_saved"]])
            everything += [dict(r, book=prefix) for r in rows]
    total = summarize(everything)
    sys.stdout.write(table(per_book, ["book", "stops", "chiffrés", "coût_net_$", "ont_coûté", "ont_protégé"]) + "\n\n")
    sys.stdout.write("TOTAL : %d stops, %d chiffrés, coût net %s $ (positif = les stops ont coûté ; négatif = ils ont protégé).\n"
                     % (total["stops"], total["priced"], fmt(total["net_cost"])))
    if total["priced"] < MIN_SAMPLE:
        sys.stdout.write("Moins de %d stops chiffrés : résultat non concluant.\n" % MIN_SAMPLE)
    write_out(args, {"total": total, "rows": everything})


if __name__ == "__main__":
    main()
