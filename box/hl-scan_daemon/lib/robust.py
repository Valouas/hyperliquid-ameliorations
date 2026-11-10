"""Filtres de robustesse ajoutés au score : rendements journaliers, régularité hebdomadaire, alpha contre beta.

`metrics` calcule, `judge` décide. score.py appelle les deux ; les seuils sont dans criteria.py.
"""
from typing import List, Optional, Tuple

from . import alpha, daily
from . import criteria as CR


def metrics(fills: List[dict], closed_trades: List[dict], now_ms: float, bench: Optional[alpha.PriceSeries]) -> dict:
    """Nouvelles métriques d'un wallet. `bench` None (référence indisponible) laisse les champs de régime vides."""
    by_day = daily.daily_pnl(fills)
    stats = daily.daily_stats(by_day)
    weeks_active, weeks_share = daily.weekly_positive_share(by_day, now_ms)
    result = {"daily_active_days": stats["active_days"], "daily_mean": stats["mean"], "daily_tstat": stats["tstat"],
              "daily_boot_lower": stats["boot_lower"], "daily_pos_share": stats["pos_day_share"],
              "weeks_active": weeks_active, "weeks_positive_share": weeks_share,
              "bench_excess": None, "bench_reductions": None, "pnl_down_days": None, "pnl_up_days": None,
              "days_down": None, "beta_only": None}
    result.update(alpha.side_stats(closed_trades))
    if bench is None:
        return result
    excess = alpha.benchmark_excess(fills, bench)
    regime = alpha.regime_split(by_day, alpha.bench_daily_returns(bench, by_day))
    if excess:
        result.update(bench_excess=excess["excess"], bench_reductions=excess["reductions"])
    result.update(pnl_down_days=regime["pnl_down"], pnl_up_days=regime["pnl_up"], days_down=regime["days_down"],
                  beta_only=alpha.beta_only(excess, regime))
    return result


def judge(m: dict) -> Tuple[List[str], List[str]]:
    """(échecs bloquants, drapeaux) à partir des métriques de `metrics` et de quelques champs du score."""
    fails: List[str] = []
    flags: List[str] = []
    days = m.get("daily_active_days") or 0
    if days < CR.MIN_ACTIVE_DAYS:
        fails.append("activeDays<%d(%d)" % (CR.MIN_ACTIVE_DAYS, days))
    lower = m.get("daily_boot_lower")
    if days >= CR.MIN_ACTIVE_DAYS and (lower is None or lower <= 0):
        fails.append("dailyLower<=0")
    weeks, share = m.get("weeks_active") or 0, m.get("weeks_positive_share")
    if weeks < CR.MIN_ACTIVE_WEEKS:
        fails.append("weeks<%d(%d)" % (CR.MIN_ACTIVE_WEEKS, weeks))
    elif share is None or share < CR.MIN_POSITIVE_WEEK_SHARE:
        fails.append("posWeeks<%d%%(%d)" % (round(CR.MIN_POSITIVE_WEEK_SHARE * 100), round((share or 0) * 100)))
    if m.get("beta_only") is True:
        fails.append("BETA_ONLY")
    elif m.get("beta_only") is None:
        flags.append("régime baissier non jugé")
    if m.get("total_7d") is not None and m["total_7d"] <= 0:
        flags.append("7j<=0")
    pf = m.get("pf_mtm")
    if pf is not None and pf > CR.MAX_PLAUSIBLE_PF:
        flags.append("PF>10 (échantillon/grille?)")
    long_pnl, short_pnl = m.get("pnl_long") or 0, m.get("pnl_short") or 0
    if long_pnl > 0 >= short_pnl:
        flags.append("gagne seulement en long")
    return fails, flags
