"""Fixtures communes : fabrique de fills synthétiques (aucun appel réseau)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DAY = 86400000
T0 = 1_790_000_000_000 - (1_790_000_000_000 % DAY)   # minuit UTC


class FillFactory:
    """Construit des fills au format userFillsByTime, en suivant la position par coin."""

    def __init__(self):
        self.position = {}
        self.fills = []
        self.tid = 0

    def trade(self, coin, side, size, price, at, closed_pnl=0.0, fee=0.0):
        start = self.position.get(coin, 0.0)
        signed = size if side == "B" else -size
        end = start + signed
        opening = abs(end) > abs(start)
        direction = ("Open " if opening else "Close ") + ("Long" if (end if opening else start) > 0 else "Short")
        self.tid += 1
        self.fills.append({"time": at, "coin": coin, "side": side, "sz": str(size), "px": str(price),
                           "startPosition": str(start), "closedPnl": str(closed_pnl), "fee": str(fee),
                           "dir": direction, "crossed": True, "tid": self.tid, "oid": self.tid})
        self.position[coin] = 0.0 if abs(end) < 1e-12 else end
        return self

    def round_trip(self, coin, long, size, entry, exit_price, opened, closed, fee=0.0):
        """Aller-retour complet : ouverture puis fermeture, PnL calculé."""
        pnl = size * (exit_price - entry) * (1 if long else -1)
        self.trade(coin, "B" if long else "A", size, entry, opened, fee=fee)
        self.trade(coin, "A" if long else "B", size, exit_price, closed, closed_pnl=pnl, fee=fee)
        return self


class FlatSeries:
    """Série de prix de référence : une valeur par jour, constante dans la journée."""

    def __init__(self, by_day, default=100.0):
        self.by_day, self.default = by_day, default

    def at(self, t):
        return self.by_day.get(int(t // DAY), self.default)


@pytest.fixture
def factory():
    return FillFactory()


@pytest.fixture
def day():
    return lambda n, hour=0: T0 + n * DAY + hour * 3600000
