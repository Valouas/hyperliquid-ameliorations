"""Liste des wallets suivis (book.json) et fichier des sorties recommandées (book_exits.json).

book.json est produit côté PC par tools/export_book.py à partir de policies_multi/ : le scanner surveille ainsi TOUS
les wallets copiés, plus seulement les 8 adresses codées en dur.
Format : {"main": {"a2e3": "0xa2e3…"}, "test": {…}}.
"""
import json
import os
import re
from typing import Dict, List, Tuple

Book = Dict[str, Dict[str, str]]
_ADDRESS = re.compile(r"0x[0-9a-f]{40}")


def load(path: str, default: Book) -> Book:
    """Lit book.json ; renvoie `default` s'il est absent ; ValueError s'il est mal formé."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("book.json : objet {groupe: {tag: adresse}} attendu")
    book: Book = {}
    for group, wallets in raw.items():
        if not isinstance(wallets, dict):
            raise ValueError("book.json : le groupe %s doit être un objet {tag: adresse}" % group)
        book[group] = {}
        for tag, address in wallets.items():
            address = str(address).lower()
            if not _ADDRESS.fullmatch(address):
                raise ValueError("book.json : adresse invalide pour %s/%s" % (group, tag))
            book[group][str(tag)] = address
    return book


def exits_payload(book_addr: Dict[str, Tuple[str, str]], alerts: Dict[str, List[dict]], now: float) -> dict:
    """Contenu de book_exits.json : uniquement les wallets suivis qui ont au moins un motif de sortie."""
    exits = {}
    for address, (group, tag) in book_addr.items():
        reasons = alerts.get(address) or []
        if reasons:
            exits[address] = {"group": group, "tag": tag, "reasons": reasons}
    return {"generated_at": now, "exits": exits}
