from conftest import DAY, T0
from lib import daily


def test_daily_pnl_sums_closed_pnl_minus_fees_per_utc_day(factory, day):
    factory.round_trip("ETH", True, 1, 100, 110, day(0, 1), day(0, 5), fee=1)
    factory.round_trip("ETH", True, 1, 100, 95, day(1, 1), day(1, 2), fee=1)

    by_day = daily.daily_pnl(factory.fills)

    assert by_day == {T0 // DAY: 8.0, T0 // DAY + 1: -7.0}


def test_daily_stats_is_none_safe_with_a_single_day():
    stats = daily.daily_stats({1: 5.0})

    assert stats["active_days"] == 1
    assert stats["tstat"] is None and stats["boot_lower"] is None


def test_daily_stats_lower_bound_positive_for_steady_winner():
    by_day = {d: 10.0 + (d % 3) for d in range(40)}

    stats = daily.daily_stats(by_day)

    assert stats["active_days"] == 40
    assert stats["boot_lower"] > 0
    assert stats["tstat"] > 2


def test_daily_stats_lower_bound_negative_when_one_day_makes_the_profit():
    by_day = {d: -1.0 for d in range(39)}
    by_day[39] = 200.0      # un seul gros jour gagnant : moyenne positive, mais pas robuste

    stats = daily.daily_stats(by_day)

    assert stats["mean"] > 0
    assert stats["boot_lower"] <= 0


def test_daily_stats_is_deterministic():
    by_day = {d: (-5.0 if d % 4 == 0 else 7.0) for d in range(30)}

    assert daily.daily_stats(by_day) == daily.daily_stats(by_day)


def test_weekly_positive_share_counts_only_active_weeks():
    now = T0 + 84 * DAY
    by_day = {}
    for week in range(6):                       # 6 semaines actives sur 12
        by_day[(now - (week * 7 + 1) * DAY) // DAY] = 10.0 if week < 4 else -10.0

    active, share = daily.weekly_positive_share(by_day, now)

    assert active == 6
    assert abs(share - 4 / 6) < 1e-9


def test_weekly_positive_share_without_activity():
    assert daily.weekly_positive_share({}, T0) == (0, None)
