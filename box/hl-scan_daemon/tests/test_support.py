"""Tests de bench (prix de référence), history (historique des analyses), exits et book."""
import json
import sqlite3

import pytest

from conftest import DAY, T0
from lib import bench, book, exits, history


# ---------------- bench
def test_series_interpolates_and_clamps():
    series = bench.Series([(0, 100.0), (10, 200.0)])

    assert series.at(5) == 150.0
    assert series.at(-3) == 100.0 and series.at(99) == 200.0
    assert bench.Series([]).at(5) is None


def test_load_fetches_once_then_reads_the_cache(tmp_path):
    calls = []

    def info(body):
        calls.append(body)
        return [{"t": T0, "T": T0 + 3600000, "o": "100", "c": "110"}]

    first = bench.load(str(tmp_path), info, T0 + DAY, clock=lambda: 1000.0)
    second = bench.load(str(tmp_path), info, T0 + DAY, clock=lambda: 1500.0)

    assert len(calls) == 1 and calls[0]["req"]["interval"] == "1h"
    assert first.at(T0) == 100.0 and second.at(T0 + 3600000) == 110.0


def test_load_keeps_stale_cache_when_the_api_fails(tmp_path):
    bench.load(str(tmp_path), lambda body: [{"t": T0, "T": T0 + 1, "o": "100", "c": "100"}], T0, clock=lambda: 0.0)

    series = bench.load(str(tmp_path), lambda body: None, T0, clock=lambda: 99999.0)

    assert series.at(T0) == 100.0


def test_load_returns_empty_series_without_cache_or_api(tmp_path):
    assert bench.load(str(tmp_path), lambda body: None, T0).at(T0) is None


# ---------------- history
@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    history.ensure(connection)
    return connection


def test_history_is_append_only(db):
    history.append(db, "0xabc", 100.0, "PASS", 0, "", {"pf_mtm": 2.0, "coins_top": "BTC:5", "open_coins": ["BTC"]}, {"d5": {"pnl": 3}})
    history.append(db, "0xabc", 200.0, "FAIL", 1, "PF_MTM<1.5", {"pf_mtm": 1.2}, None)

    rows = list(history.rows(db))

    assert [r["status"] for r in rows] == ["PASS", "FAIL"]
    assert rows[0]["result"] == {"pf_mtm": 2.0}            # champs texte/listes volumineux écartés
    assert rows[0]["sim"] == {"d5": {"pnl": 3}}


def test_history_keeps_prefilter_failures_without_payload(db):
    history.append(db, "0xabc", 100.0, "PREFILTER_FAIL", 1, "total30d<=0", {"av": 5.0}, None)

    row = next(history.rows(db))

    assert row["result"] is None and row["fails"] == "total30d<=0"


def test_history_prune_drops_only_old_rows(db):
    history.append(db, "0xabc", 100.0, "PASS", 0, "", {}, None)
    history.append(db, "0xabc", 100.0 + 200 * 86400, "PASS", 0, "", {}, None)

    removed = history.prune(db, now=100.0 + 200 * 86400, keep_days=180)

    assert removed == 1 and len(list(history.rows(db))) == 1


def test_history_rows_filters_by_time(db):
    for t in (10.0, 20.0, 30.0):
        history.append(db, "0xabc", t, "PASS", 0, "", {}, None)

    assert [r["t"] for r in history.rows(db, min_t=15, max_t=25)] == [20.0]


# ---------------- exits
def healthy(**override):
    base = {"av": 50000.0, "dd_mtm_30d": 0.05, "unreal_loss_pct_av": 0.01, "days_since_last": 0.5,
            "acct_lev_twavg": 2.0, "total_30d": 1200.0, "fails": ""}
    return dict(base, **override)


def codes(reasons):
    return [r["code"] for r in reasons]


def test_no_exit_reason_for_a_healthy_leader():
    assert exits.exit_reasons(healthy()) == []


@pytest.mark.parametrize("override,code", [
    ({"av": 914.0}, "AV_BELOW_MIN"),
    ({"dd_mtm_30d": 0.27}, "DD30"),
    ({"unreal_loss_pct_av": 0.2}, "LATENT_LOSS"),
    ({"days_since_last": 9.0}, "INACTIVE"),
    ({"acct_lev_twavg": 11.0}, "LEVERAGE"),
    ({"total_30d": -5.0}, "PNL30_NEG"),
    ({"fails": "PF_MTM<1.5(1.2)|HOLDS_LOSERS:disp3.4"}, "HOLDS_LOSERS"),
])
def test_each_rule_produces_its_exit_code(override, code):
    assert codes(exits.exit_reasons(healthy(**override))) == [code]


def test_day_move_reports_a_large_drop_as_transient_alert():
    port = {"day": {"accountValueHistory": [[0, "1000"], [1, "600"]], "pnlHistory": [[0, "0"], [1, "-350"]]}}

    reasons = exits.exit_reasons(healthy(), exits.day_move(port))

    assert codes(reasons) == ["AV_DROP_24H"]
    assert "perte" in reasons[0]["text"]


def test_missing_values_do_not_trigger_an_exit():
    assert exits.exit_reasons({"av": None, "fails": None}) == []


# ---------------- book
def test_book_falls_back_to_default_when_file_is_missing(tmp_path):
    default = {"main": {"a2e3": "0xa2e366c4e2d453cab49cc9c64f0670fd2fd060a3"}}

    assert book.load(str(tmp_path / "book.json"), default) == default


def test_book_loads_and_normalises_addresses(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps({"main": {"05c6": "0x05C6959D997CDE99D2F967367E5E9F1959BA9706"}}), encoding="utf-8")

    loaded = book.load(str(path), {})

    assert loaded == {"main": {"05c6": "0x05c6959d997cde99d2f967367e5e9f1959ba9706"}}


def test_book_rejects_an_invalid_address(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps({"main": {"05c6": "0x123"}}), encoding="utf-8")

    with pytest.raises(ValueError):
        book.load(str(path), {})


def test_exits_payload_lists_only_wallets_with_reasons():
    addresses = {"0xaaa": ("main", "aaaa"), "0xbbb": ("main", "bbbb")}
    alerts = {"0xaaa": [{"code": "AV_BELOW_MIN", "text": "AV 914 $ < 20 000 $"}], "0xbbb": []}

    payload = book.exits_payload(addresses, alerts, now=123.0)

    assert payload["generated_at"] == 123.0
    assert list(payload["exits"]) == ["0xaaa"]
    assert payload["exits"]["0xaaa"]["tag"] == "aaaa"
