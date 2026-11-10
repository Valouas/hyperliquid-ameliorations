"""Statistiques de PnL journalier : remplacent le profit factor ponctuel par une mesure avec incertitude.

Le PnL est le réalisé net de frais (closedPnl - fee) par jour UTC. Les statistiques sont en dollars : le t et le signe
de la borne basse ne dépendent pas de l'échelle, donc aucune valeur de compte historique n'est nécessaire.
"""
import math
import random
import statistics
from typing import Dict, List, Optional, Tuple

from . import criteria as CR


def daily_pnl(fills: List[dict]) -> Dict[int, float]:
    """PnL réalisé net par jour UTC (clé = time // 86 400 000). Un jour avec seulement des frais compte comme actif."""
    by_day: Dict[int, float] = {}
    for fill in fills:
        day = int(fill["time"] // CR.DAY_MS)
        by_day[day] = by_day.get(day, 0.0) + float(fill["closedPnl"]) - float(fill["fee"])
    return by_day


def _bootstrap_lower(values: List[float], samples: int, quantile: float, seed: int) -> float:
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(samples))
    return means[min(samples - 1, int(quantile * samples))]


def daily_stats(by_day: Dict[int, float], samples: int = CR.BOOTSTRAP_SAMPLES,
                quantile: float = CR.BOOTSTRAP_LOWER_QUANTILE, seed: int = CR.BOOTSTRAP_SEED) -> dict:
    """Jours actifs, moyenne, écart-type, t, part de jours positifs et borne basse bootstrap de la moyenne."""
    values = [by_day[d] for d in sorted(by_day)]
    n = len(values)
    result = {"active_days": n, "mean": None, "std": None, "tstat": None, "boot_lower": None, "pos_day_share": None}
    if not n:
        return result
    mean = sum(values) / n
    result.update(mean=mean, pos_day_share=sum(v > 0 for v in values) / n)
    if n < 2:
        return result
    std = statistics.stdev(values)
    result["std"] = std
    result["tstat"] = mean / (std / math.sqrt(n)) if std > 0 else None
    result["boot_lower"] = _bootstrap_lower(values, samples, quantile, seed)
    return result


def weekly_positive_share(by_day: Dict[int, float], now_ms: float,
                          weeks: int = CR.WEEKS_LOOKBACK) -> Tuple[int, Optional[float]]:
    """(semaines actives, part de semaines positives) sur les `weeks` dernières semaines glissantes avant now_ms."""
    today = now_ms / CR.DAY_MS
    totals: Dict[int, float] = {}
    for day, pnl in by_day.items():
        age_days = today - day          # le jour UTC en cours a un âge entre 0 et 1 : toujours dans la semaine 0
        index = int(age_days // 7)
        if 0 <= age_days and index < weeks:
            totals[index] = totals.get(index, 0.0) + pnl
    if not totals:
        return 0, None
    return len(totals), sum(v > 0 for v in totals.values()) / len(totals)
