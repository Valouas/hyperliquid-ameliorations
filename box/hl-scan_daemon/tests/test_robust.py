from conftest import DAY, T0, FlatSeries
from lib import robust


def good_metrics(**override):
    base = {"daily_active_days": 40, "daily_boot_lower": 3.0, "weeks_active": 10, "weeks_positive_share": 0.7,
            "beta_only": False, "pf_mtm": 2.5, "wr_closed": 0.6, "total_7d": 100.0, "pnl_long": 50.0, "pnl_short": 20.0,
            "pnl_down_days": 5.0, "days_down": 20}
    return dict(base, **override)


def test_judge_passes_a_robust_profile():
    fails, flags = robust.judge(good_metrics())

    assert fails == [] and flags == []


def test_judge_fails_too_few_active_days():
    fails, _ = robust.judge(good_metrics(daily_active_days=12))

    assert fails == ["activeDays<20(12)"]


def test_judge_fails_when_daily_lower_bound_not_positive():
    fails, _ = robust.judge(good_metrics(daily_boot_lower=-0.5))

    assert fails == ["dailyLower<=0"]


def test_judge_fails_on_inconsistent_weeks():
    fails, _ = robust.judge(good_metrics(weeks_positive_share=0.4))

    assert fails == ["posWeeks<50%(40)"]


def test_judge_fails_with_too_few_active_weeks():
    fails, _ = robust.judge(good_metrics(weeks_active=3))

    assert fails == ["weeks<4(3)"]


def test_judge_fails_beta_only_wallet():
    fails, _ = robust.judge(good_metrics(beta_only=True))

    assert fails == ["BETA_ONLY"]


def test_negative_week_is_a_flag_not_a_failure():
    fails, flags = robust.judge(good_metrics(total_7d=-50.0))

    assert fails == []
    assert flags == ["7j<=0"]


def test_implausible_profit_factor_and_win_rate_are_flagged():
    _, flags = robust.judge(good_metrics(pf_mtm=51.4, wr_closed=0.95))

    assert "PF>10 (échantillon/grille?)" in flags
    assert "WR>90% (grid?)" not in flags          # déjà signalé par score.py, pas en double


def test_one_sided_wallet_is_flagged():
    _, flags = robust.judge(good_metrics(pnl_short=-10.0))

    assert "gagne seulement en long" in flags


def test_unjudged_regime_is_flagged():
    _, flags = robust.judge(good_metrics(beta_only=None))

    assert "régime baissier non jugé" in flags


def test_metrics_wires_daily_weekly_side_and_benchmark(factory):
    now = T0 + 60 * DAY
    for n in range(30):                                   # 30 jours, un aller-retour long gagnant par jour
        opened = T0 + (n * 2) * DAY + 3600000
        factory.round_trip("ALT", True, 1, 100, 105, opened, opened + 3600000)
    trades = [{"side": "L", "pnl": 5.0, "partial": False}] * 30

    m = robust.metrics(factory.fills, trades, now, FlatSeries({}, default=100.0))

    assert m["daily_active_days"] == 30
    assert m["daily_boot_lower"] > 0
    assert m["weeks_active"] >= 8
    assert m["pnl_long"] == 150.0 and m["pnl_short"] == 0.0
    assert abs(m["bench_excess"] - 150.0) < 1e-6
    assert m["beta_only"] is None                          # référence plate : aucun jour baissier


def test_metrics_without_benchmark_keeps_regime_fields_empty(factory):
    factory.round_trip("ALT", True, 1, 100, 105, T0, T0 + 3600000)

    m = robust.metrics(factory.fills, [], T0 + DAY, None)

    assert m["bench_excess"] is None and m["beta_only"] is None and m["pnl_down_days"] is None
