"""Lecture d'un compte paper (checkpoint.json + status.json, variante all_ioc) pour les rapports. Lecture seule."""
import json
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from val_common import round_trips  # noqa: E402

VARIANT = "all_ioc"


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def fill_records(events: List[dict]) -> List[dict]:
    """Événements `paper_fill` du checkpoint, au format attendu par val_common.round_trips."""
    return [{"coin": e["coin"], "qty": e["quantity"], "price": e["price"], "at": e["at"], "fee": e.get("fee"),
             "cause": e.get("cause"), "address": e.get("address")}
            for e in events if e.get("kind") == "paper_fill"]


def prefixes(state_dir: str, role_dir: str) -> List[str]:
    base = os.path.join(state_dir, role_dir)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "checkpoint.json")))


def load(state_dir: str, role_dir: str, prefix: str) -> Optional[dict]:
    """Résumé d'un compte : PnL net, durée depuis la baseline, allers-retours fermés, stops, équité du leader."""
    folder = os.path.join(state_dir, role_dir, prefix)
    checkpoint, status = read_json(os.path.join(folder, "checkpoint.json")), read_json(os.path.join(folder, "status.json"))
    if not checkpoint or not status or VARIANT not in checkpoint:
        return None
    book = checkpoint[VARIANT]
    wallet = next(iter(book["wallets"].values()), {})
    baseline = wallet.get("baseline_at")
    last = book.get("last_at")
    trips, _ = round_trips(fill_records(book["events"]))
    return {"book": prefix, "pnl": status["books"][VARIANT]["pnl_net_usd"],
            "days": (last - baseline) / 86400 if baseline is not None and last else None,
            "trips": trips, "events": book["events"], "positions": book.get("positions", {}),
            "global_stop": book.get("global_stop", 0), "equity": wallet.get("equity"),
            "equity_median_7d": wallet.get("equity_median_7d")}
