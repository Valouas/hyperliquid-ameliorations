from conftest import DAY, T0, FlatSeries
from lib import alpha


def test_side_stats_splits_long_and_short():
    trades = [{"side": "L", "pnl": 10.0, "partial": False}, {"side": "L", "pnl": -4.0, "partial": False},
              {"side": "S", "pnl": 6.0, "partial": False}, {"side": "S", "pnl": 99.0, "partial": True}]

    stats = alpha.side_stats(trades)

    assert stats["pnl_long"] == 6.0 and stats["n_long"] == 2
    assert stats["pnl_short"] == 6.0 and stats["n_short"] == 1     # le trade partiel est ignoré
    assert stats["pf_long"] == 2.5
    assert stats["pf_short"] is None                                # aucune perte : PF non défini


def test_benchmark_excess_is_zero_when_wallet_only_rides_the_benchmark(factory, day):
    # Long 1 000 $ d'un alt qui monte exactement comme la référence (+10 %) : aucun excédent.
    bench = FlatSeries({T0 // DAY: 100.0, T0 // DAY + 1: 110.0})
    factory.round_trip("ALT", True, 10, 100, 110, day(0, 1), day(1, 1))

    result = alpha.benchmark_excess(factory.fills, bench)

    assert result["reductions"] == 1
    assert abs(result["wallet_gross"] - 100.0) < 1e-9
    assert abs(result["bench_gross"] - 100.0) < 1e-9
    assert abs(result["excess"]) < 1e-9


def test_benchmark_excess_positive_when_coin_beats_the_benchmark(factory, day):
    bench = FlatSeries({}, default=100.0)                # référence plate
    factory.round_trip("ALT", True, 10, 100, 120, day(0, 1), day(1, 1))

    result = alpha.benchmark_excess(factory.fills, bench)

    assert abs(result["excess"] - 200.0) < 1e-9


def test_benchmark_excess_short_in_falling_market_earns_no_excess(factory, day):
    bench = FlatSeries({T0 // DAY: 100.0, T0 // DAY + 1: 90.0})
    factory.round_trip("ALT", False, 10, 100, 90, day(0, 1), day(1, 1))

    result = alpha.benchmark_excess(factory.fills, bench)

    assert abs(result["wallet_gross"] - 100.0) < 1e-9
    assert abs(result["excess"]) < 1e-9


def test_benchmark_excess_ignores_positions_opened_before_the_window(factory, day):
    bench = FlatSeries({}, default=100.0)
    factory.position["OLD"] = 5.0                         # position antérieure à la fenêtre
    factory.trade("OLD", "A", 5, 150, day(0, 1), closed_pnl=250.0)

    result = alpha.benchmark_excess(factory.fills, bench)

    assert result["reductions"] == 0 and result["wallet_gross"] == 0.0


def test_benchmark_excess_handles_a_flip(factory, day):
    bench = FlatSeries({}, default=100.0)
    factory.trade("ALT", "B", 10, 100, day(0, 1))
    factory.trade("ALT", "A", 15, 110, day(0, 2), closed_pnl=100.0)    # ferme 10, ouvre 5 short
    factory.trade("ALT", "B", 5, 100, day(0, 3), closed_pnl=50.0)

    result = alpha.benchmark_excess(factory.fills, bench)

    assert result["reductions"] == 2
    assert abs(result["excess"] - 150.0) < 1e-9


def test_benchmark_excess_without_benchmark_data(factory, day):
    factory.round_trip("ALT", True, 1, 100, 110, day(0, 1), day(0, 2))

    assert alpha.benchmark_excess(factory.fills, FlatSeries({}, default=None)) is None


def test_regime_split_separates_down_days():
    by_day = {1: 10.0, 2: -30.0, 3: 5.0}
    returns = {1: 0.02, 2: -0.03, 3: -0.01}

    split = alpha.regime_split(by_day, returns)

    assert split == {"pnl_down": -25.0, "pnl_up": 10.0, "days_down": 2, "days_up": 1}


def test_bench_daily_returns_uses_day_open_and_close():
    class Linear:
        def at(self, t):
            return 100.0 + (t - T0) / DAY * 10.0          # +10 par jour

    returns = alpha.bench_daily_returns(Linear(), [T0 // DAY])

    assert abs(returns[T0 // DAY] - 0.10) < 1e-6          # clôture = dernière milliseconde du jour


def test_beta_only_true_when_no_excess_and_losing_on_down_days():
    excess = {"excess": -5.0, "reductions": 20}
    regime = {"pnl_down": -40.0, "days_down": 15}

    assert alpha.beta_only(excess, regime) is True


def test_beta_only_false_when_profitable_on_down_days():
    excess = {"excess": -5.0, "reductions": 20}
    regime = {"pnl_down": 12.0, "days_down": 15}

    assert alpha.beta_only(excess, regime) is False


def test_beta_only_not_judged_with_too_little_evidence():
    assert alpha.beta_only({"excess": -5.0, "reductions": 3}, {"pnl_down": -1.0, "days_down": 15}) is None
    assert alpha.beta_only(None, {"pnl_down": -1.0, "days_down": 15}) is None
