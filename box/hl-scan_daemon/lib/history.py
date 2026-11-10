"""Historique en ajout seul des analyses du scanner (table `analyses` de state.db).

La table `wallets` ne garde que la DERNIÈRE analyse : impossible de savoir ce que le scanner disait d'un wallet au
moment où il a été choisi. Cette table conserve chaque verdict daté, pour valider les filtres sans regarder le futur.
"""
import json
import sqlite3
from typing import Iterator, Optional

from . import criteria as CR

_KEPT_TEXT = ("fails", "flags", "trades_rule", "status")


def ensure(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """CREATE TABLE IF NOT EXISTS analyses(addr TEXT NOT NULL, t REAL NOT NULL, status TEXT, n_fails INTEGER,
               fails TEXT, result TEXT, sim TEXT);
           CREATE INDEX IF NOT EXISTS ix_analyses_addr_t ON analyses(addr, t);
           CREATE INDEX IF NOT EXISTS ix_analyses_t ON analyses(t);""")


def compact(result: Optional[dict]) -> dict:
    """Garde les nombres, booléens et quelques textes courts ; écarte listes, dictionnaires et longs libellés."""
    kept = {}
    for key, value in (result or {}).items():
        if isinstance(value, bool) or value is None:
            kept[key] = value
        elif isinstance(value, (int, float)):
            kept[key] = value if value == value and abs(value) != float("inf") else None
        elif isinstance(value, str) and key in _KEPT_TEXT:
            kept[key] = value
    return kept


def append(connection: sqlite3.Connection, addr: str, t: float, status: str, n_fails: Optional[int],
           fails: Optional[str], result: Optional[dict], sim: Optional[dict]) -> None:
    """Ajoute une analyse. Un échec au préfiltre ne garde que son statut et ses motifs (pas de charge utile)."""
    scored = status != "PREFILTER_FAIL"
    connection.execute("INSERT INTO analyses(addr,t,status,n_fails,fails,result,sim) VALUES(?,?,?,?,?,?,?)",
                       (addr.lower(), t, status, n_fails, fails or "",
                        json.dumps(compact(result)) if scored else None,
                        json.dumps(sim) if scored and sim else None))


def prune(connection: sqlite3.Connection, now: float, keep_days: int = CR.HISTORY_KEEP_DAYS) -> int:
    cursor = connection.execute("DELETE FROM analyses WHERE t < ?", (now - keep_days * 86400,))
    return cursor.rowcount


def rows(connection: sqlite3.Connection, min_t: float = 0, max_t: Optional[float] = None) -> Iterator[dict]:
    """Analyses par ordre chronologique, charges utiles décodées."""
    query = "SELECT addr,t,status,n_fails,fails,result,sim FROM analyses WHERE t >= ?"
    params = [min_t]
    if max_t is not None:
        query += " AND t <= ?"
        params.append(max_t)
    for addr, t, status, n_fails, fails, result, sim in connection.execute(query + " ORDER BY t, addr", params):
        yield {"addr": addr, "t": t, "status": status, "n_fails": n_fails, "fails": fails,
               "result": json.loads(result) if result else None, "sim": json.loads(sim) if sim else None}
