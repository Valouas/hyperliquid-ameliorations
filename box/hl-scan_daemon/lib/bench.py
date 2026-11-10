"""Prix horaires de la référence (BTC) pour mesurer l'alpha : une requête candleSnapshot par heure, mise en cache.

Le module ne dépend pas de net.py : la fonction `info` est injectée (net.info dans le scanner, un faux dans les tests).
`load` ne lève jamais d'exception : réponse absente ou mal formée -> cache périmé s'il existe, sinon série vide.
Après un échec, l'API n'est pas rappelée avant RETRY_AFTER_S secondes (pas de requête répétée pour chaque wallet).
"""
import bisect
import json
import os
import time
from typing import Callable, List, Optional, Sequence, Tuple

from . import criteria as CR

Point = Tuple[float, float]
RETRY_AFTER_S = 300


class Series:
    """Série de prix interpolée linéairement ; constante hors de ses bornes ; None si elle est vide."""

    def __init__(self, points: Sequence[Sequence[float]]):
        self.times: List[float] = [p[0] for p in points]
        self.values: List[float] = [p[1] for p in points]

    def at(self, t: float) -> Optional[float]:
        if not self.times:
            return None
        i = bisect.bisect_right(self.times, t)
        if i == 0:
            return self.values[0]
        if i >= len(self.times):
            return self.values[-1]
        t0, t1, v0, v1 = self.times[i - 1], self.times[i], self.values[i - 1], self.values[i]
        return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass          # cache facultatif : un disque plein ne doit pas arrêter le scanner


def _series(cached: Optional[dict]) -> Optional[Series]:
    """Série du cache si sa forme est valide (liste de paires numériques), sinon None."""
    points = (cached or {}).get("pts")
    if not isinstance(points, list) or not points:
        return None
    try:
        return Series([(float(t), float(v)) for t, v in points])
    except (TypeError, ValueError):
        return None


def _parse(rows: object) -> Optional[List[Point]]:
    """Bougies de l'API -> points triés (ouverture et clôture de chaque bougie). None si la réponse est mal formée."""
    if not isinstance(rows, list) or not rows:
        return None
    points = {}
    try:
        for row in rows:
            points[float(row["t"])] = float(row["o"])
            points[float(row["T"])] = float(row["c"])
    except (TypeError, KeyError, ValueError):
        return None
    return sorted(points.items())


def load(cache_dir: str, info: Callable[[dict], object], now_ms: float, coin: str = CR.BENCH_COIN,
         days: int = CR.BENCH_DAYS, max_age_s: float = 3600, clock: Callable[[], float] = time.time) -> Series:
    """Bougies 1 h de `coin` sur `days` jours. En cas d'échec de l'API : cache périmé, sinon série vide."""
    path = os.path.join(cache_dir, "_bench_%s_1h.json" % coin)
    failed_path = path + ".failed"
    cached = _read_json(path)
    series = _series(cached)
    if series and clock() - float(cached.get("fetched") or 0) < max_age_s:
        return series
    fallback = series or Series([])
    failure = _read_json(failed_path)
    if failure and clock() - float(failure.get("at") or 0) < RETRY_AFTER_S:
        return fallback
    try:
        rows = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                        "startTime": int(now_ms - days * CR.DAY_MS), "endTime": int(now_ms)}})
    except Exception:     # la fonction injectée (réseau) ne doit jamais faire échouer l'analyse d'un wallet ici
        rows = None
    points = _parse(rows)
    if points is None:
        _write_json(failed_path, {"at": clock()})
        return fallback
    _write_json(path, {"fetched": clock(), "pts": points})
    return Series(points)
