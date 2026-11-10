"""Branches complémentaires : retournement d'une position antérieure, lignes walk-forward, rendu du tableau."""
from conftest import DAY, T0, FlatSeries
from lib import alpha

import walkforward as wf


def test_flip_of_a_pre_window_position_starts_a_followed_cycle(factory, day):
    bench = FlatSeries({}, default=100.0)
    factory.position["OLD"] = 5.0                                   # long antérieur à la fenêtre
    factory.trade("OLD", "A", 8, 100, day(0, 1), closed_pnl=999.0)  # ferme 5 (ignoré), ouvre 3 short (suivi)
    factory.trade("OLD", "B", 3, 90, day(0, 2), closed_pnl=30.0)

    result = alpha.benchmark_excess(factory.fills, bench)

    assert result["reductions"] == 1
    assert abs(result["wallet_gross"] - 30.0) < 1e-9                # le PnL de la position antérieure n'est pas compté
    assert abs(result["excess"] - 30.0) < 1e-9


def test_regime_split_ignores_days_without_benchmark_return():
    split = alpha.regime_split({1: 10.0, 2: 5.0}, {1: -0.01})

    assert split == {"pnl_down": 10.0, "pnl_up": 0.0, "days_down": 1, "days_up": 0}


def test_fills_rows_build_one_row_per_wallet_with_both_windows(factory):
    cut = T0 + 60 * DAY
    for n in range(10):
        opened = T0 + n * 5 * DAY + 3600000
        factory.round_trip("ALT", True, 1, 100, 104, opened, opened + 3600000)
    factory.round_trip("ALT", True, 1, 100, 90, cut + DAY, cut + 2 * DAY)
    fills_by_addr = {"0xabc": {"fetched_at": T0 + 90 * DAY, "fills": factory.fills},
                     "0xempty": {"fetched_at": T0 + 90 * DAY, "fills": []}}

    rows = wf.fills_rows(fills_by_addr, horizon_days=30)

    assert [r["addr"] for r in rows] == ["0xabc"]
    assert abs(rows[0]["pnl"] + 10.0) < 1e-9
    assert rows[0]["features"]["pnl_long>0"] is True


def test_fills_rows_skip_wallets_with_too_few_formation_trades(factory):
    cut = T0 + 60 * DAY
    factory.round_trip("ALT", True, 1, 100, 104, T0 + DAY, T0 + 2 * DAY)
    factory.round_trip("ALT", True, 1, 100, 104, cut + DAY, cut + 2 * DAY)

    assert wf.fills_rows({"0xabc": {"fetched_at": T0 + 90 * DAY, "fills": factory.fills}}) == []


def test_render_marks_underpowered_groups():
    table = [{"feature": "f", "n_pass": 10, "n_fail": 12, "hit_pass": 0.8, "hit_fail": 0.3, "diff": 0.5,
              "p_value": 0.04, "underpowered": True}]

    text = wf.render(table, 22)

    assert "22 wallets/analyses" in text and "non concluant" in text and "+50%" in text


def test_outcome_of_an_empty_window():
    assert wf.outcome([]) == {"pnl": 0.0, "trades": 0}
