"""Règles de sortie : passe en mode "retired" (plus aucune nouvelle entrée, sorties suivies) les books dont le leader
ne remplit plus les critères. Deux sources, cumulables :

  --exits book_exits.json     fichier écrit par le scanner de la box (codes AV_BELOW_MIN, DD30, LATENT_LOSS, INACTIVE,
                              LEVERAGE, PNL30_NEG, HOLDS_LOSERS). AV_DROP_24H est transitoire : jamais de retrait.
  --state hypertracker_state  règle locale, sans le scanner : équité du leader < 20 000 $ (lue dans multi/*/status.json).

Par défaut le script AFFICHE seulement ce qu'il ferait. Avec --write, il sauvegarde multi_runner.json
(multi_runner.json.bak-AAAAMMJJ-HHMMSS) puis écrit la nouvelle configuration ; redémarrer multi2 ensuite.
Un book retiré n'est jamais réactivé automatiquement : pour le réadmettre, retirer "mode" à la main.

Usage : py -3 tools\\apply_exits.py --state hypertracker_state [--exits book_exits.json] [--write]
"""
import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book_data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RETIRE_CODES = frozenset({"AV_BELOW_MIN", "DD30", "LATENT_LOSS", "INACTIVE", "LEVERAGE", "PNL30_NEG", "HOLDS_LOSERS"})
MIN_LEADER_EQUITY = 20_000.0
MAX_EXITS_AGE_HOURS = 12.0      # le scanner réécrit book_exits.json toutes les 1 à 3 h
Exits = Dict[str, List[dict]]


def local_exits(state_dir: str, min_equity: float = MIN_LEADER_EQUITY) -> Exits:
    """Leaders dont l'équité connue est sous le minimum (une équité inconnue ne déclenche rien)."""
    exits: Exits = {}
    for prefix in book_data.prefixes(state_dir, "multi"):
        book = book_data.load(state_dir, "multi", prefix)
        equity = book["equity"] if book else None
        if equity is not None and equity < min_equity:
            exits[prefix] = [{"code": "AV_BELOW_MIN", "text": "équité du leader %.0f $ < %.0f $" % (equity, min_equity)}]
    return exits


def scanner_exits(payload: dict, now: float, max_age_hours: float = MAX_EXITS_AGE_HOURS) -> Exits:
    """book_exits.json du scanner -> {préfixe: motifs} (le tag du scanner est le préfixe du book).

    Un fichier trop ancien (scanner arrêté) est refusé : un retrait est durable, il ne doit pas venir d'un vieux scan.
    """
    age_hours = (now - float(payload.get("generated_at") or 0)) / 3600
    if not 0 <= age_hours <= max_age_hours:
        raise ValueError("book_exits.json date de %.1f h (maximum %.0f h) : relancer le scanner ou recopier le fichier"
                         % (age_hours, max_age_hours))
    return {entry["tag"]: entry["reasons"] for entry in payload.get("exits", {}).values()}


def merge(first: Exits, second: Exits) -> Exits:
    merged = {prefix: list(reasons) for prefix, reasons in first.items()}
    for prefix, reasons in second.items():
        known = {r["code"] for r in merged.get(prefix, [])}
        merged[prefix] = merged.get(prefix, []) + [r for r in reasons if r["code"] not in known]
    return merged


def plan(config: dict, exits: Exits, known: Iterable[str], retire_codes: Set[str] = RETIRE_CODES) -> Tuple[dict, List[dict]]:
    """(nouvelle configuration, changements). `config` n'est pas modifié ; `known` = préfixes ayant une politique."""
    new = copy.deepcopy(config)
    books = new.setdefault("books", {})
    known = set(known)
    changes = []
    for prefix in sorted(exits):
        reasons = [r for r in exits[prefix] if r["code"] in retire_codes]
        if not reasons or prefix not in known or books.get(prefix, {}).get("mode") == "retired":
            continue
        books[prefix] = dict(books.get(prefix, {}), mode="retired")
        changes.append({"book": prefix, "reasons": reasons})
    return new, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "multi_runner.json")
    parser.add_argument("--policies", type=Path, default=ROOT / "policies_multi")
    parser.add_argument("--exits", type=Path, default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.exits and not args.state:
        sys.exit("Indiquer --exits et/ou --state")
    exits: Exits = {}
    if args.exits:
        try:
            exits = merge(exits, scanner_exits(json.loads(args.exits.read_text(encoding="utf-8")), time.time()))
        except ValueError as error:
            sys.exit(str(error))
    if args.state:
        exits = merge(exits, local_exits(args.state))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    known = {f.stem[len("policy_"):] for f in args.policies.glob("policy_*.json")}
    new, changes = plan(config, exits, known)
    for change in changes:
        sys.stdout.write("%s -> retired : %s\n" % (change["book"], " ; ".join(r["text"] for r in change["reasons"])))
    if not changes:
        sys.stdout.write("Aucun book à retirer.\n")
        return
    if not args.write:
        sys.stdout.write("Essai à blanc : rien n'est écrit. Relancer avec --write pour appliquer.\n")
        return
    backup = args.config.with_name(args.config.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.write_text(args.config.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = args.config.with_name(args.config.name + ".tmp")
    temporary.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, args.config)          # remplacement atomique : jamais de fichier tronqué
    sys.stdout.write("%d book(s) retiré(s). Sauvegarde : %s. Redémarrer multi2.\n" % (len(changes), backup.name))


if __name__ == "__main__":
    main()
