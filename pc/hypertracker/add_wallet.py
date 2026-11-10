"""Create policies_multi/policy_<prefix>.json for a new 500 $ PAPER book (one wallet).

Usage: py -3 add_wallet.py 0x<40 hex> [--sizing-capital 562.5] [--feed delayed|realtime]
The per-book policy is never edited afterwards (its sha256 is the checkpoint identity).
Feed/latency live in multi_runner.json; --feed realtime adds the prefix to "realtime" there
(refused above 10 realtime wallets).
2026-11-10: a new wallet starts in PROBATION (books.<prefix>.mode = "probation" in multi_runner.json): only its
zero-delay ghost trades until tools/probation_report.py says PROMOTE. --live skips the probation.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
TEMPLATE = {
    "version": 2, "mode": "paper", "capital_usd": 500, "reserve_usd": 50,
    "ratio_refresh_seconds": 300, "reconcile_seconds": 60, "max_total_notional": 1350,
    "major_coins": ["BTC", "ETH", "SOL"], "major_coin_cap": 1350, "other_coin_cap": 900,
    "group_caps": [{"coins": ["PUMP", "CASHCAT"], "cap": 900}], "min_volume_24h": 2000000,
    "accumulation_threshold_usd": 11, "entry_slippage_major_bps": 10, "entry_slippage_other_bps": 25,
    "max_drift_major_bps": 30, "max_drift_other_bps": 50, "entry_signal_ttl_seconds": 600,
    "exit_slippage_bps": [50, 100], "wallet_alert_fraction": 0.1, "wallet_pause_fraction": 0.15,
    "wallet_close_fraction": 0.2, "global_pause_loss": 75, "global_close_loss": 100,
    "taker_fee": 0.00045, "maker_fee": 0.00015, "max_book_age_seconds": 10, "max_signal_age_seconds": 60,
    "leader_exclusions_status": "COMMON_EXCLUSIONS_HIP3_SPOT_VOL2M_PREEXISTING_VIA_BASELINE",
    "automatic_replacements": False, "replacement_prefixes_pending": [],
}


def clean(x):
    x = round(float(x), 6)
    return int(x) if x == int(x) else x


def render(policy):
    lines = ["{"] + ["  %s: %s," % (json.dumps(k), json.dumps(v)) for k, v in policy.items() if k != "wallets"]
    lines += ['  "wallets": [', "    " + json.dumps(policy["wallets"][0]), "  ]", "}"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("--sizing-capital", type=float, default=562.5)
    parser.add_argument("--max-coin-notional", type=float, default=843.75)
    parser.add_argument("--max-total-notional", type=float, default=1350)
    parser.add_argument("--exclude-coins", default="", help="comma separated, e.g. PUMP,FARTCOIN")
    parser.add_argument("--feed", choices=("delayed", "realtime"), default="delayed")
    parser.add_argument("--live", action="store_true", help="no ghost probation: the book trades at once")
    parser.add_argument("--policy-dir", type=Path, default=ROOT / "policies_multi")
    parser.add_argument("--config", type=Path, default=ROOT / "multi_runner.json")
    args = parser.parse_args()
    address = args.address.strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", address):
        sys.exit("Adresse invalide (0x + 40 caractères hexadécimaux, adresse COMPLÈTE)")
    args.policy_dir.mkdir(parents=True, exist_ok=True)
    for file in args.policy_dir.glob("policy_*.json"):
        if json.loads(file.read_text(encoding="utf-8"))["wallets"][0]["address"] == address:
            sys.exit("Wallet déjà suivi : " + file.name)
    prefix = next(address[2:2+n] for n in (4, 6, 8) if not (args.policy_dir / ("policy_%s.json" % address[2:2+n])).exists())
    if args.sizing_capital <= 0 or args.max_coin_notional <= 0 or args.max_total_notional <= 0:
        sys.exit("Montants invalides")
    if not args.live and args.feed == "realtime":
        sys.exit("L'essai fantôme exige le flux différé : ajouter en différé, ou --live pour un book temps réel immédiat")
    policy = dict(TEMPLATE)
    policy["wallets"] = [{"address": address, "margin": 450, "sizing_capital": clean(args.sizing_capital), "max_leverage": 3,
                          "max_coin_notional": clean(args.max_coin_notional), "max_total_notional": clean(args.max_total_notional),
                          "entry": "ioc", "exclude_coins": [c.strip().upper() for c in args.exclude_coins.split(",") if c.strip()]}]
    if args.feed == "realtime":
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
        realtime = config.setdefault("realtime", [])
        if len(realtime) >= min(10, config.get("max_realtime_users", 10)):
            sys.exit("Déjà %d wallets temps réel (max 10 par IP) : ajouter en différé" % len(realtime))
        realtime.append(prefix)
        args.config.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    if not args.live:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
        books = dict(config.get("books", {}))
        books[prefix] = dict(books.get(prefix, {}), mode="probation")
        args.config.write_text(json.dumps(dict(config, books=books), indent=1, ensure_ascii=False) +"\n", encoding="utf-8")
    target = args.policy_dir / ("policy_%s.json" % prefix)
    target.write_text(render(policy), encoding="utf-8")
    print("Créé %s (%s, %s, sizing %s $) -> dossier hypertracker_state\\multi\\%s ; redémarrer multi2 pour l'ajouter"
          % (target.name, args.feed, "LIVE" if args.live else "ESSAI FANTÔME", policy["wallets"][0]["sizing_capital"], prefix))


if __name__ == "__main__":
    main()
