"""Vérifie le câblage dans score.py : nouveaux champs présents, 7 jours négatifs = drapeau, BETA_ONLY = échec."""
from conftest import DAY, T0, FlatSeries
from lib import score


def profile(factory, week_total=-50.0, long=True):
    """Un aller-retour par jour pendant 90 jours (1 perdant sur 6), compte de 100 000 $ ; PnL 7 j réglable."""
    now = T0 + 90 * DAY
    for n in range(90):
        opened = T0 + n * DAY + 3600000
        entry, exit_price = (100, 104) if long else (104, 100)
        if n % 6 == 0:                                         # quelques perdants pour un PF fini
            exit_price = entry - 2 if long else entry + 2
        factory.round_trip("ALT", long, 10, entry, exit_price, opened, opened + 2 * 3600000, fee=0.2)
    fd = {"fetched_at": now, "start": T0, "fills": factory.fills}
    series = lambda first, total: {"pnlHistory": [[first, "0"], [now, str(total)]],
                                   "accountValueHistory": [[first, "100000"], [now, "100000"]]}
    port = {"week": series(now - 7 * DAY, week_total), "month": series(now - 30 * DAY, 800.0),
            "allTime": series(T0 - 300 * DAY, 5000.0)}
    state = {"time": now, "marginSummary": {"accountValue": "100000"}, "assetPositions": []}
    return fd, port, state


def test_negative_week_no_longer_fails_the_wallet(factory):
    fd, port, state = profile(factory, week_total=-50.0)

    o = score.score("0xabc", fd, port, state, set())

    assert "total7d<=0" not in o["fails"]
    assert "7j<=0" in o["flags"]


def test_new_metrics_are_published(factory):
    fd, port, state = profile(factory)

    o = score.score("0xabc", fd, port, state, set())

    for key in ("daily_active_days", "daily_boot_lower", "weeks_positive_share", "pnl_long", "pnl_short", "beta_only"):
        assert key in o
    assert o["daily_active_days"] >= 20
    assert o["bench_excess"] is None                             # pas de référence fournie


def test_wallet_that_only_rides_the_market_fails_beta_only(factory):
    fd, port, state = profile(factory, long=True)
    # La référence fait exactement comme l'alt : +4 % les jours gagnants, -2 % les jours perdants.
    falling = {}
    for n in range(90):
        if n % 6 == 0:
            falling[(T0 + n * DAY) // DAY] = True

    class Bench:
        def at(self, t):
            hours = (t % DAY) / 3600000
            down = falling.get(int(t // DAY), False)
            return 100.0 + (-2.0 if down else 4.0) * min(max(hours - 1, 0), 2) / 2

    o = score.score("0xabc", fd, port, state, set(), bench=Bench())

    assert o["beta_only"] is True
    assert "BETA_ONLY" in o["fails"]


def test_flat_benchmark_does_not_fail_a_stock_picker(factory):
    fd, port, state = profile(factory)

    o = score.score("0xabc", fd, port, state, set(), bench=FlatSeries({}, default=100.0))

    assert o["bench_excess"] > 0
    assert "BETA_ONLY" not in o["fails"]
