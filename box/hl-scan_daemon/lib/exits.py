"""Règles de sortie d'un wallet suivi, à partir de son dernier score.

Chaque motif a un code stable (lu par tools/apply_exits.py côté PC) et un texte lisible. AV_DROP_24H est une alerte
transitoire (souvent un retrait) : le moteur de copie a déjà sa propre garde, ce code ne retire pas le wallet.
"""
from typing import List, Optional, Tuple

from . import criteria as CR

DayMove = Tuple[float, Optional[float], float]      # (variation relative de l'AV, PnL du jour, AV de départ)


def day_move(port: dict) -> Optional[DayMove]:
    """Variation de la valeur du compte sur la fenêtre 'day' du portfolio, ou None si elle est inconnue."""
    day = port.get("day") or {}
    values, pnl = day.get("accountValueHistory") or [], day.get("pnlHistory") or []
    if len(values) < 2 or float(values[0][1]) <= 0:
        return None
    first = float(values[0][1])
    change = float(values[-1][1]) / first - 1
    day_pnl = float(pnl[-1][1]) - float(pnl[0][1]) if len(pnl) >= 2 else None
    return change, day_pnl, first


def _reason(code: str, text: str) -> dict:
    return {"code": code, "text": text}


def exit_reasons(o: dict, move: Optional[DayMove] = None) -> List[dict]:
    """Motifs de sortie d'un wallet (liste vide = rien à signaler). Une valeur inconnue ne déclenche jamais."""
    reasons: List[dict] = []
    av = o.get("av")
    if av is not None and av < CR.EXIT_MIN_AV:
        reasons.append(_reason("AV_BELOW_MIN", "AV %.0f $ < %.0f $" % (av, CR.EXIT_MIN_AV)))
    dd30 = o.get("dd_mtm_30d")
    if dd30 is not None and dd30 > CR.EXIT_MAX_DD30:
        reasons.append(_reason("DD30", "DD_MTM30j %.1f%% > %.0f%%" % (dd30 * 100, CR.EXIT_MAX_DD30 * 100)))
    latent = o.get("unreal_loss_pct_av")
    if latent is not None and latent > CR.EXIT_MAX_LATENT_LOSS:
        reasons.append(_reason("LATENT_LOSS", "latent négatif %.1f%% AV > %.0f%%" % (latent * 100, CR.EXIT_MAX_LATENT_LOSS * 100)))
    if move is not None and move[0] <= CR.EXIT_DAY_DROP:
        change, day_pnl, first = move
        cause = ""
        if day_pnl is not None:
            cause = " (PnL 24h %+.0f$ → %s)" % (day_pnl, "perte" if day_pnl < -0.2 * first else "probablement retrait")
        reasons.append(_reason("AV_DROP_24H", "AV %.0f%% en 24h%s" % (change * 100, cause)))
    idle = o.get("days_since_last")
    if idle is not None and idle > CR.EXIT_MAX_INACTIVE_DAYS:
        reasons.append(_reason("INACTIVE", "inactif %.1fj > %.0fj" % (idle, CR.EXIT_MAX_INACTIVE_DAYS)))
    leverage = o.get("acct_lev_twavg")
    if leverage is not None and leverage > CR.EXIT_MAX_AVG_LEVERAGE:
        reasons.append(_reason("LEVERAGE", "levier moyen %.1fx > %.0fx" % (leverage, CR.EXIT_MAX_AVG_LEVERAGE)))
    total_30d = o.get("total_30d")
    if total_30d is not None and total_30d <= 0:
        reasons.append(_reason("PNL30_NEG", "PnL 30j %.0f $ <= 0" % total_30d))
    if "HOLDS_LOSERS" in (o.get("fails") or ""):
        reasons.append(_reason("HOLDS_LOSERS", "garde ses positions perdantes"))
    return reasons
