"""Corrections issues de la relecture : le démon ne doit jamais tomber sur une réponse d'API mal formée."""
import pytest

from conftest import DAY, T0, FlatSeries
from lib import alpha, bench, daily


@pytest.mark.parametrize("reply", [{"error": "rate limited"}, "oops", [{"t": T0}], [{"t": T0, "T": T0 + 1, "o": "x", "c": "1"}], []])
def test_bench_load_survives_a_malformed_reply(tmp_path, reply):
    series = bench.load(str(tmp_path), lambda body: reply, T0)

    assert series.times == []


def test_bench_load_survives_an_exception_of_the_network_function(tmp_path):
    def broken(body):
        raise OSError("réseau coupé")

    assert bench.load(str(tmp_path), broken, T0).times == []


def test_bench_load_does_not_hammer_the_api_after_a_failure(tmp_path):
    calls = []

    def failing(body):
        calls.append(body)
        return None

    bench.load(str(tmp_path), failing, T0, clock=lambda: 1000.0)
    bench.load(str(tmp_path), failing, T0, clock=lambda: 1000.0 + bench.RETRY_AFTER_S - 1)
    bench.load(str(tmp_path), failing, T0, clock=lambda: 1000.0 + bench.RETRY_AFTER_S + 1)

    assert len(calls) == 2


def test_bench_load_ignores_a_corrupted_cache_file(tmp_path):
    (tmp_path / "_bench_BTC_1h.json").write_text('{"fetched": 1, "pts": "pas une liste"}', encoding="utf-8")

    series = bench.load(str(tmp_path), lambda body: [{"t": T0, "T": T0 + 1, "o": "100", "c": "101"}], T0, clock=lambda: 2.0)

    assert series.at(T0) == 100.0


def test_current_utc_day_counts_in_week_zero_before_and_after_noon():
    today = T0 // DAY
    by_day = {today: 5.0}

    morning = daily.weekly_positive_share(by_day, T0 + 3 * 3600000)
    evening = daily.weekly_positive_share(by_day, T0 + 20 * 3600000)

    assert morning == evening == (1, 1.0)


def test_future_dated_day_is_ignored():
    assert daily.weekly_positive_share({T0 // DAY + 2: 5.0}, T0) == (0, None)


def test_zero_size_fill_does_not_divide_by_zero(factory, day):
    factory.trade("ALT", "B", 0, 100, day(0, 1))
    factory.round_trip("ALT", True, 1, 100, 110, day(0, 2), day(0, 3))

    result = alpha.benchmark_excess(factory.fills, FlatSeries({}, default=100.0))

    assert abs(result["excess"] - 10.0) < 1e-9
