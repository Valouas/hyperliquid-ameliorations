"""Améliorations du 2026-11-10 : pause des entrées (essai fantôme / wallet retiré) et plafond de corrélation."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import manual_copy_multi_runner as multi
from manual_copy_engine import Engine, hip3_options
from test_manual_copy import ManualCopyTests

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


class EntriesPausedTests(ManualCopyTests):
    def test_paused_book_opens_nothing_and_reports_why(self):
        self.e = Engine(self.p, self.e.state, options={"entries_paused": True})
        self.meta()
        self.fill(size=20)
        self.tick()
        row = self.e.row(self.a, "BTC")
        self.assertEqual(row["qty"], 0)
        self.assertEqual(row["reason"], "entries_paused")

    def test_pausing_never_blocks_the_exit_of_an_open_copy(self):
        self.fill(size=20)
        self.tick()
        row = self.e.row(self.a, "BTC")
        self.assertGreater(row["qty"], 0)
        self.e = Engine(self.p, self.e.state, options={"entries_paused": True})
        self.meta()
        row = self.e.row(self.a, "BTC")
        self.fill(before=20, size=20, side="A")
        self.tick()
        self.assertEqual(row["qty"], 0)

    def test_option_must_be_a_boolean(self):
        with self.assertRaises(ValueError):
            hip3_options({"entries_paused": "yes"})
        self.assertFalse(hip3_options({})["entries_paused"])


class EntryGuardTests(ManualCopyTests):
    def test_guard_reason_blocks_the_entry(self):
        seen = []
        self.e.entry_guard = lambda row, now: seen.append(row["coin"]) or "portfolio_same_side_books_cap"
        self.fill(size=20)
        self.tick()
        row = self.e.row(self.a, "BTC")
        self.assertEqual(row["qty"], 0)
        self.assertEqual(row["reason"], "portfolio_same_side_books_cap")
        self.assertEqual(seen[0], "BTC")

    def test_guard_returning_none_lets_the_entry_through(self):
        self.e.entry_guard = lambda row, now: None
        self.fill(size=20)
        self.tick()
        self.assertGreater(self.e.row(self.a, "BTC")["qty"], 0)

    def test_guard_is_not_asked_for_exits(self):
        self.fill(size=20)
        self.tick()
        row = self.e.row(self.a, "BTC")
        self.e.entry_guard = lambda row, now: "portfolio_same_side_books_cap"
        self.fill(before=20, size=20, side="A")
        self.tick()
        self.assertEqual(row["qty"], 0)


def unit(address, role="main", qty=0.0, coin="BTC", entry=100.0, mark=None):
    positions = {address + ":" + coin: {"qty": qty, "entry": entry, "target": qty}} if qty else {}
    engine = SimpleNamespace(state={"positions": positions}, marks={coin: (mark, 0)} if mark else {})
    return SimpleNamespace(address=address, role=role, engines={"all_ioc": engine, "post_only": engine})


class CrowdingTests(unittest.TestCase):
    def row(self, address=C, target=1.0):
        return {"address": address, "coin": "BTC", "target": target, "qty": 0}

    def test_no_cap_means_no_block(self):
        units = [unit(A, qty=1), unit(B, qty=1), unit(C)]
        self.assertIsNone(multi.crowding_reason(units, units[2], "all_ioc", self.row(), None, None))

    def test_blocks_when_enough_other_books_hold_the_same_side(self):
        units = [unit(A, qty=1), unit(B, qty=2), unit(C)]
        self.assertEqual(multi.crowding_reason(units, units[2], "all_ioc", self.row(), 2, None),
                         "portfolio_same_side_books_cap")

    def test_opposite_side_and_other_roles_do_not_count(self):
        units = [unit(A, qty=-1), unit(B, role="ghost", qty=5), unit(C)]
        self.assertIsNone(multi.crowding_reason(units, units[2], "all_ioc", self.row(), 1, None))

    def test_own_position_does_not_count(self):
        units = [unit(A, qty=1), unit(C, qty=1)]
        self.assertIsNone(multi.crowding_reason(units, units[1], "all_ioc", self.row(), 2, None))

    def test_notional_cap_uses_marks_then_entry_price(self):
        units = [unit(A, qty=10, mark=120.0), unit(B, qty=5, entry=100.0), unit(C)]
        self.assertEqual(multi.crowding_reason(units, units[2], "all_ioc", self.row(), None, 1700),
                         "portfolio_coin_side_notional_cap")      # 10 x 120 + 5 x 100 = 1700
        self.assertIsNone(multi.crowding_reason(units, units[2], "all_ioc", self.row(), None, 1701))

    def test_short_entry_counts_short_books(self):
        units = [unit(A, qty=-1), unit(B, qty=-1), unit(C)]
        self.assertEqual(multi.crowding_reason(units, units[2], "all_ioc", self.row(target=-1.0), 2, None),
                         "portfolio_same_side_books_cap")


class ConfigTests(unittest.TestCase):
    def config(self, **extra):
        folder = Path(tempfile.mkdtemp())
        path = folder / "multi_runner.json"
        path.write_text(json.dumps(extra), encoding="utf-8")
        return multi.load_config(path)

    def test_mode_values_are_validated(self):
        self.assertEqual(self.config(books={"abcd": {"mode": "probation"}})["books"]["abcd"]["mode"], "probation")
        with self.assertRaises(ValueError):
            self.config(books={"abcd": {"mode": "paused"}})

    def test_caps_must_be_positive_or_null(self):
        self.assertEqual(self.config(max_books_same_coin_side=5)["max_books_same_coin_side"], 5)
        self.assertIsNone(self.config()["max_books_same_coin_side"])
        for bad in ({"max_books_same_coin_side": 0}, {"max_portfolio_coin_side_notional": -1},
                    {"max_books_same_coin_side": True}):
            with self.assertRaises(ValueError):
                self.config(**bad)

    def test_unit_options_pause_main_but_not_ghost(self):
        config = self.config(books={"abcd": {"mode": "probation"}, "ef01": {"mode": "retired"}, "2345": {}})
        self.assertTrue(multi.unit_options(config, "abcd", "main")["entries_paused"])
        self.assertTrue(multi.unit_options(config, "ef01", "main")["entries_paused"])
        self.assertFalse(multi.unit_options(config, "abcd", "ghost")["entries_paused"])
        self.assertFalse(multi.unit_options(config, "2345", "main")["entries_paused"])
        self.assertFalse(multi.unit_options(config, "abcd", "placebo")["allow_hip3"])

    def test_probation_requires_a_ghost(self):
        with self.assertRaises(ValueError):
            multi.check_mode("abcd", {"mode": "probation"}, feed="realtime", ghosts=True)
        with self.assertRaises(ValueError):
            multi.check_mode("abcd", {"mode": "probation", "ghost": False}, feed="delayed", ghosts=True)
        multi.check_mode("abcd", {"mode": "probation"}, feed="delayed", ghosts=True)
        multi.check_mode("abcd", {"mode": "retired", "ghost": False}, feed="realtime", ghosts=False)


class UnitWiringTests(unittest.TestCase):
    """Vrais Runner / Unit / Engine sur un dossier temporaire (aucun réseau)."""

    def setUp(self):
        import add_wallet
        tmp = Path(tempfile.mkdtemp())
        self.addresses = [A, B, C]
        (tmp / "policies").mkdir()
        for address in self.addresses:
            policy = dict(add_wallet.TEMPLATE)
            policy["wallets"] = [{"address": address, "margin": 450, "sizing_capital": 562.5, "max_leverage": 3,
                                  "max_coin_notional": 843.75, "max_total_notional": 1350, "entry": "ioc", "exclude_coins": []}]
            (tmp / "policies" / ("policy_%s.json" % address[2:6])).write_text(add_wallet.render(policy), encoding="utf-8")
        config = {"policy_dir": str(tmp / "policies"), "state_dir": str(tmp / "multi"), "stress_state_dir": str(tmp / "stress"),
                  "shadow_state_dir": str(tmp / "shadow"), "ghost_state_dir": str(tmp / "ghost"),
                  "placebo_policy_dir": str(tmp / "placebo"), "placebo_state_dir": str(tmp / "placebo_state"),
                  "market_store_dir": str(tmp / "market"), "runner_status": str(tmp / "status.json"), "cf_dir": str(tmp / "cf"),
                  "live_data_feed": False, "max_books_same_coin_side": 1, "stress_latency_ms": 1500,
                  "books": {"bbbb": {"mode": "probation"}}}
        (tmp / "multi_runner.json").write_text(json.dumps(config), encoding="utf-8")
        loaded = multi.load_config(tmp / "multi_runner.json")
        self.runner = multi.Runner(loaded, multi.book_plan(loaded))
        self.units = {(u.prefix, u.role): u for u in self.runner.units}

    def tearDown(self):
        for handle in self.runner.cf_handles.values():
            handle.close()

    def test_probation_pauses_the_main_book_only(self):
        self.assertTrue(self.units[("bbbb", "main")].engines["all_ioc"].options["entries_paused"])
        self.assertFalse(self.units[("bbbb", "ghost")].engines["all_ioc"].options["entries_paused"])
        self.assertFalse(self.units[("aaaa", "main")].engines["all_ioc"].options["entries_paused"])

    def test_guard_is_wired_on_main_books_and_not_on_ghosts(self):
        self.assertIsNone(self.units[("aaaa", "ghost")].engines["all_ioc"].entry_guard)
        guard = self.units[("cccc", "main")].engines["all_ioc"].entry_guard
        row = {"address": C, "coin": "BTC", "target": 0.5, "qty": 0}
        self.assertIsNone(guard(row, 0))
        self.units[("aaaa", "main")].engines["all_ioc"].row(A, "BTC").update(qty=0.01, entry=80000.0, target=0.01)
        self.assertEqual(guard(row, 0), "portfolio_same_side_books_cap")
        self.assertIsNone(guard(dict(row, target=-0.5), 0))
        # chaque variante regarde sa propre variante chez les autres books
        self.assertIsNone(self.units[("cccc", "main")].engines["post_only"].entry_guard(row, 0))


if __name__ == "__main__":
    unittest.main()
