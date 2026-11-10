"""Exporte la liste de TOUS les wallets copiés (policies_multi/) vers book.json pour le scanner de la box.

Le scanner ne surveillait que 8 adresses codées en dur ; avec ce fichier dans son dossier (hl-scan_daemon/book.json),
il vérifie chaque wallet suivi et écrit ses sorties recommandées dans book_exits.json.

Usage : py -3 tools\\export_book.py [--policies policies_multi] [--out book.json]   puis copier book.json sur la box.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent.parent


def build(policy_dir: Path) -> Dict[str, Dict[str, str]]:
    wallets = {}
    for file in sorted(Path(policy_dir).glob("policy_*.json")):
        policy = json.loads(file.read_text(encoding="utf-8-sig"))
        wallets[file.stem[len("policy_"):]] = policy["wallets"][0]["address"].lower()
    return {"main": wallets}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policies", type=Path, default=ROOT / "policies_multi")
    parser.add_argument("--out", type=Path, default=ROOT / "book.json")
    args = parser.parse_args()
    book = build(args.policies)
    if not book["main"]:
        sys.exit("Aucune politique trouvée dans %s" % args.policies)
    args.out.write_text(json.dumps(book, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write("%d wallets exportés vers %s\n" % (len(book["main"]), args.out))


if __name__ == "__main__":
    main()
