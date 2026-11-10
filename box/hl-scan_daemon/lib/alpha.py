"""Alpha contre beta : le wallet gagne-t-il autrement qu'en suivant le marché ?

Trois mesures, toutes sur les fills déjà téléchargés :
- side_stats        : PnL des allers-retours longs et courts séparément ;
- benchmark_excess  : PnL réalisé comparé à un témoin qui prend, aux mêmes instants, le même notionnel dans le même
                      sens sur la référence (BTC). Mesure le choix des actifs, PAS le timing : un trader qui ne traite
                      que la référence a un excédent nul par construction ;
- regime_split      : PnL réalisé les jours où la référence baisse / monte (mesure le timing et le sens).
`beta_only` ne condamne un wallet que si les deux dernières mesures sont défavorables.
"""
from typing import Dict, Iterable, List, Optional, Protocol

from . import criteria as CR

EPS = 1e-12


class PriceSeries(Protocol):
    def at(self, t: float) -> Optional[float]: ...


def _profit_factor(values: List[float]) -> Optional[float]:
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v <= 0)
    return gains / losses if losses > 0 else None


def side_stats(trades: Iterable[dict]) -> dict:
    """PnL, nombre et profit factor des allers-retours complets, par sens. PF None s'il n'y a aucune perte."""
    by_side: Dict[str, List[float]] = {"L": [], "S": []}
    for trade in trades:
        if not trade.get("partial"):
            by_side[trade["side"]].append(trade["pnl"])
    return {"pnl_long": sum(by_side["L"]), "pnl_short": sum(by_side["S"]),
            "n_long": len(by_side["L"]), "n_short": len(by_side["S"]),
            "pf_long": _profit_factor(by_side["L"]), "pf_short": _profit_factor(by_side["S"])}


def benchmark_excess(fills: List[dict], bench: PriceSeries) -> Optional[dict]:
    """PnL brut réalisé du wallet moins celui du témoin de référence. None si la référence n'a pas de prix.

    Comme la simulation de copie, un coin n'est suivi qu'à partir du moment où le wallet y est à plat (ou retourne
    sa position) : les positions ouvertes avant la fenêtre sont ignorées des deux côtés.
    """
    followed: Dict[str, bool] = {}
    qty: Dict[str, float] = {}       # quantité de référence détenue par le témoin, par coin
    avg: Dict[str, float] = {}       # prix moyen d'entrée du témoin
    wallet_gross = bench_gross = 0.0
    reductions = 0
    for fill in sorted(fills, key=lambda f: f["time"]):
        ref = bench.at(fill["time"])
        if not ref or ref <= 0:
            return None
        coin, size, price = fill["coin"], float(fill["sz"]), float(fill["px"])
        direction = 1 if fill["side"] == "B" else -1
        start = float(fill["startPosition"])
        end = start + direction * size
        if coin not in followed:
            followed[coin] = abs(start) < EPS
        if not followed[coin]:
            if abs(end) < EPS:
                followed[coin] = True
            elif start * end < 0:                       # retournement : le reste ouvre un nouveau cycle
                followed[coin] = True
                qty[coin], avg[coin] = direction * abs(end) * price / ref, ref
            continue
        held = qty.get(coin, 0.0)
        if abs(start) < EPS or start * direction > 0:   # ouverture ou renforcement
            added = direction * size * price / ref
            if not added:                               # fill de taille ou de prix nul
                continue
            avg[coin] = (abs(held) * avg.get(coin, ref) + abs(added) * ref) / (abs(held) + abs(added))
            qty[coin] = held + added
            continue
        closed_fraction = min(size, abs(start)) / abs(start)
        closed = held * closed_fraction
        bench_gross += closed * (ref - avg.get(coin, ref))
        wallet_gross += float(fill["closedPnl"])
        reductions += 1
        qty[coin] = held - closed
        remainder = size - abs(start)
        if remainder > EPS:                             # retournement : le reste ouvre dans l'autre sens
            qty[coin], avg[coin] = direction * remainder * price / ref, ref
    return {"wallet_gross": wallet_gross, "bench_gross": bench_gross,
            "excess": wallet_gross - bench_gross, "reductions": reductions}


def bench_daily_returns(bench: PriceSeries, days: Iterable[int]) -> Dict[int, float]:
    """Rendement de la référence entre l'ouverture et la clôture de chaque jour UTC demandé."""
    returns: Dict[int, float] = {}
    for day in days:
        first, last = bench.at(day * CR.DAY_MS), bench.at((day + 1) * CR.DAY_MS - 1)
        if first and last and first > 0:
            returns[day] = last / first - 1
    return returns


def regime_split(by_day: Dict[int, float], bench_returns: Dict[int, float]) -> dict:
    """PnL réalisé et nombre de jours actifs, selon que la référence a baissé ou non ce jour-là."""
    split = {"pnl_down": 0.0, "pnl_up": 0.0, "days_down": 0, "days_up": 0}
    for day, pnl in by_day.items():
        if day not in bench_returns:
            continue
        key = "down" if bench_returns[day] < 0 else "up"
        split["pnl_" + key] += pnl
        split["days_" + key] += 1
    return split


def beta_only(excess: Optional[dict], regime: Optional[dict]) -> Optional[bool]:
    """True = aucun indice de gain hors beta : ne bat pas le témoin ET perd les jours de baisse. None = non jugé."""
    if not excess or not regime:
        return None
    if excess["reductions"] < CR.MIN_BENCH_REDUCTIONS or regime["days_down"] < CR.MIN_DOWN_DAYS:
        return None
    return excess["excess"] <= 0 and regime["pnl_down"] <= 0
