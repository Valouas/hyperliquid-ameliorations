from conftest import DAY, T0

import walkforward as wf


def test_split_separates_formation_and_test_windows(factory):
    cut = T0 + 60 * DAY
    factory.round_trip("ALT", True, 1, 100, 105, T0 + DAY, T0 + 2 * DAY)
    factory.round_trip("ALT", True, 1, 100, 105, cut + DAY, cut + 2 * DAY)
    fd = {"fetched_at": T0 + 90 * DAY, "fills": factory.fills}

    formation, test, at = wf.split(fd, horizon_days=30)

    assert at == cut
    assert len(formation) == 2 and len(test) == 2


def test_outcome_counts_only_cycles_opened_after_the_cut(factory):
    cut = T0 + 60 * DAY
    factory.trade("OLD", "B", 1, 100, cut - DAY)                              # ouverte avant la coupe
    factory.trade("OLD", "A", 1, 300, cut + DAY, closed_pnl=200.0)            # non copiable : ignorée
    factory.round_trip("NEW", True, 2, 100, 110, cut + DAY, cut + 2 * DAY, fee=1)
    test = [f for f in factory.fills if f["time"] >= cut]

    result = wf.outcome(test)

    assert result["trades"] == 1
    assert abs(result["pnl"] - 18.0) < 1e-9


def test_formation_features_flag_expected_properties(factory):
    cut = T0 + 60 * DAY
    for n in range(30):
        opened = T0 + n * 2 * DAY + 3600000
        factory.round_trip("ALT", True, 1, 100, 104, opened, opened + 2 * 3600000)

    features = wf.formation_features("0xabc", factory.fills, cut)

    assert features["trades>=25"] is True
    assert features["pnl_long>0"] is True
    assert features["pnl_short>0"] is False
    assert features["dailyLower>0"] is True
    assert features["pnl_7d>0"] is True


def test_feature_table_compares_hit_rates():
    rows = ([{"features": {"f": True}, "pnl": 10.0}] * 8 + [{"features": {"f": True}, "pnl": -10.0}] * 2
            + [{"features": {"f": False}, "pnl": 10.0}] * 3 + [{"features": {"f": False}, "pnl": -10.0}] * 7)

    table = wf.feature_table(rows, permutations=500)

    row = table[0]
    assert row["feature"] == "f"
    assert row["n_pass"] == 10 and row["n_fail"] == 10
    assert abs(row["hit_pass"] - 0.8) < 1e-9 and abs(row["hit_fail"] - 0.3) < 1e-9
    assert abs(row["diff"] - 0.5) < 1e-9
    assert 0 < row["p_value"] < 0.2


def test_feature_table_skips_features_without_both_groups():
    rows = [{"features": {"f": True}, "pnl": 1.0}] * 5

    assert wf.feature_table(rows, permutations=10) == []


def test_history_rows_join_selection_time_status_with_later_outcome(factory):
    selected_at = T0 + 30 * DAY
    factory.round_trip("ALT", True, 1, 100, 90, selected_at + DAY, selected_at + 2 * DAY)
    fills_by_addr = {"0xabc": {"fetched_at": T0 + 70 * DAY, "fills": factory.fills}}
    analyses = [{"addr": "0xabc", "t": selected_at / 1000, "status": "PASS", "fails": "", "result": {}},
                {"addr": "0xabc", "t": (T0 + 65 * DAY) / 1000, "status": "PASS", "fails": "", "result": {}},   # trop récent
                {"addr": "0xzzz", "t": selected_at / 1000, "status": "FAIL", "fails": "PF_MTM<1.5", "result": {}}]  # pas de fills

    rows = wf.history_rows(analyses, fills_by_addr, horizon_days=30)

    assert len(rows) == 1
    assert rows[0]["features"]["PASS"] is True
    assert abs(rows[0]["pnl"] + 10.0) < 1e-9
