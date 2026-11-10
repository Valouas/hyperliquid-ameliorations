"""Outils du 2026-11-10 : essai fantôme, sorties, export du book, analyse des stops."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import apply_exits  # noqa: E402
import book_data  # noqa: E402
import export_book  # noqa: E402
import probation_report  # noqa: E402
import stop_analysis  # noqa: E402

A = "0x" + "a" * 40


def fill(coin, quantity, price, at, cause=None, fee=0.0):
    event = {"kind": "paper_fill", "address": A, "coin": coin, "quantity": quantity, "price": price, "at": at, "fee": fee}
    if cause:
        event["cause"] = cause
    return event


def write_book(root, role, prefix, events, pnl=0.0, baseline=0.0, last=86400.0, global_stop=0, positions=None, equity=50000.0):
    folder = Path(root) / role / prefix
    folder.mkdir(parents=True)
    wallet = {"baseline_at": baseline, "equity": equity, "equity_median_7d": equity}
    book = {"events": events, "wallets": {A: wallet}, "positions": positions or {}, "global_stop": global_stop, "last_at": last}
    (folder / "checkpoint.json").write_text(json.dumps({"all_ioc": book, "post_only": book}), encoding="utf-8")
    status = {"books": {"all_ioc": {"pnl_net_usd": pnl, "global_stop": global_stop, "wallets": {A: wallet}}}}
    (folder / "status.json").write_text(json.dumps(status), encoding="utf-8")


class BookDataTests(unittest.TestCase):
    def test_fill_records_keep_only_paper_fills(self):
        events = [{"kind": "baseline", "at": 1}, fill("BTC", 1, 100, 2), fill("BTC", -1, 110, 3, cause="stop", fee=0.1)]
        records = book_data.fill_records(events)
        self.assertEqual([(r["qty"], r["price"], r["cause"]) for r in records], [(1, 100, None), (-1, 110, "stop")])

    def test_load_reads_pnl_days_and_trips(self):
        root = tempfile.mkdtemp()
        write_book(root, "multi_ghost", "abcd", [fill("BTC", 1, 100, 10), fill("BTC", -1, 110, 20)], pnl=9.5, last=2 * 86400.0)
        book = book_data.load(root, "multi_ghost", "abcd")
        self.assertEqual(book["pnl"], 9.5)
        self.assertAlmostEqual(book["days"], 2.0)
        self.assertEqual(len(book["trips"]), 1)
        self.assertIsNone(book_data.load(root, "multi_ghost", "zzzz"))


class ProbationTests(unittest.TestCase):
    def verdict(self, **override):
        base = {"days": 15.0, "trips": 25, "pnl": 12.0, "placebo_per_day": 0.2, "global_stop": 0}
        base.update(override)
        return probation_report.verdict(**base)[0]

    def test_promotes_a_profitable_ghost_that_beats_the_placebos(self):
        self.assertEqual(self.verdict(), "PROMOTE")

    def test_waits_until_enough_evidence(self):
        self.assertEqual(self.verdict(days=5.0, trips=25), "WAIT")       # sous le plancher de 7 jours
        self.assertEqual(self.verdict(days=10.0, trips=8), "WAIT")       # ni 14 jours ni 20 allers-retours
        self.assertEqual(self.verdict(days=15.0, trips=3), "WAIT")       # sous le plancher de 5 allers-retours

    def test_twenty_trips_are_enough_before_fourteen_days(self):
        self.assertEqual(self.verdict(days=8.0, trips=20), "PROMOTE")

    def test_rejects_losers_and_those_not_beating_placebos(self):
        self.assertEqual(self.verdict(pnl=-3.0), "REJECT")
        self.assertEqual(self.verdict(pnl=1.0, placebo_per_day=5.0), "REJECT")

    def test_global_stop_rejects_at_once(self):
        self.assertEqual(self.verdict(days=1.0, trips=1, global_stop=1), "REJECT")

    def test_without_placebo_reference_only_the_sign_counts(self):
        self.assertEqual(self.verdict(placebo_per_day=None), "PROMOTE")

    def test_report_lists_only_probation_books(self):
        root = tempfile.mkdtemp()
        trips = [e for n in range(25) for e in (fill("BTC", 1, 100, n * 10 + 1), fill("BTC", -1, 101, n * 10 + 2))]
        write_book(root, "multi_ghost", "abcd", trips, pnl=20.0, last=15 * 86400.0)
        write_book(root, "multi_ghost", "ef01", trips, pnl=20.0, last=15 * 86400.0)
        write_book(root, "multi_placebo", "9999", trips, pnl=1.5, last=15 * 86400.0)
        config = {"books": {"abcd": {"mode": "probation"}, "ef01": {}}}
        rows = probation_report.report(root, config)
        self.assertEqual([r["book"] for r in rows], ["abcd"])
        self.assertEqual(rows[0]["verdict"], "PROMOTE")


class ApplyExitsTests(unittest.TestCase):
    def test_plan_retires_only_on_retiring_codes_and_never_mutates(self):
        config = {"books": {"abcd": {"allow_hip3": False}, "ef01": {"mode": "retired"}}}
        exits = {"abcd": [{"code": "AV_BELOW_MIN", "text": "AV 914 $"}], "1234": [{"code": "AV_DROP_24H", "text": "retrait"}],
                 "ef01": [{"code": "DD30", "text": "dd"}]}
        new, changes = apply_exits.plan(config, exits, known={"abcd", "ef01", "1234"})
        self.assertEqual(new["books"]["abcd"], {"allow_hip3": False, "mode": "retired"})
        self.assertNotIn("1234", new["books"])                      # alerte transitoire : pas de retrait
        self.assertEqual([c["book"] for c in changes], ["abcd"])   # ef01 déjà retiré
        self.assertNotIn("mode", config["books"]["abcd"])           # entrée non modifiée

    def test_plan_ignores_unknown_books(self):
        new, changes = apply_exits.plan({"books": {}}, {"zzzz": [{"code": "DD30", "text": "dd"}]}, known={"abcd"})
        self.assertEqual(changes, [])
        self.assertEqual(new, {"books": {}})

    def test_local_exits_flag_drained_leaders(self):
        root = tempfile.mkdtemp()
        write_book(root, "multi", "abcd", [], equity=914.0)
        write_book(root, "multi", "ef01", [], equity=80000.0)
        exits = apply_exits.local_exits(root)
        self.assertEqual(list(exits), ["abcd"])
        self.assertEqual(exits["abcd"][0]["code"], "AV_BELOW_MIN")

    def test_scanner_exits_reads_a_fresh_file_and_rejects_a_stale_one(self):
        payload = {"generated_at": 1000.0, "exits": {A: {"group": "main", "tag": "abcd", "reasons": [{"code": "DD30", "text": "dd"}]}}}
        self.assertEqual(list(apply_exits.scanner_exits(payload, now=1000.0 + 3600)), ["abcd"])
        with self.assertRaises(ValueError):
            apply_exits.scanner_exits(payload, now=1000.0 + 13 * 3600)       # scanner arrêté depuis 13 h
        with self.assertRaises(ValueError):
            apply_exits.scanner_exits({"exits": {}}, now=1_790_000_000.0)    # pas de date : refusé

    def test_merge_combines_scanner_and_local_reasons(self):
        merged = apply_exits.merge({"abcd": [{"code": "DD30", "text": "a"}]},
                                   {"abcd": [{"code": "AV_BELOW_MIN", "text": "b"}], "ef01": [{"code": "DD30", "text": "c"}]})
        self.assertEqual([r["code"] for r in merged["abcd"]], ["DD30", "AV_BELOW_MIN"])
        self.assertIn("ef01", merged)


class ExportBookTests(unittest.TestCase):
    def test_exports_every_policy_as_main(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "policy_aaaa.json").write_text(json.dumps({"wallets": [{"address": A}]}), encoding="utf-8")
        self.assertEqual(export_book.build(folder), {"main": {"aaaa": A}})


class StopAnalysisTests(unittest.TestCase):
    def test_stop_that_cut_before_a_rebound_has_a_positive_cost(self):
        events = [fill("BTC", 2, 100, 10), fill("BTC", -2, 90, 20, cause="stop")]
        positions = {A + ":BTC": {"exit_ref": 105.0, "exit_ref_at": 50.0}}
        rows = stop_analysis.stop_rows(events, positions)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "leader_exited")
        self.assertAlmostEqual(rows[0]["cost"], 30.0)                 # tenir jusqu'à 105 : +30 de mieux qu'à 90

    def test_stop_that_avoided_a_further_fall_has_a_negative_cost(self):
        events = [fill("ETH", -1, 100, 10), fill("ETH", 1, 110, 20, cause="stop")]   # short stoppé à 110
        positions = {A + ":ETH": {"exit_ref": 130.0, "exit_ref_at": 60.0}}
        self.assertAlmostEqual(stop_analysis.stop_rows(events, positions)[0]["cost"], -20.0)

    def test_leader_still_in_position_is_not_priced(self):
        events = [fill("BTC", -1, 90, 20, cause="stop")]
        positions = {A + ":BTC": {"exit_ref": 95.0, "exit_ref_at": 5.0}}             # sortie antérieure au stop
        row = stop_analysis.stop_rows(events, positions)[0]
        self.assertEqual(row["status"], "leader_still_open")
        self.assertIsNone(row["cost"])

    def test_summary_totals(self):
        rows = [{"cost": 30.0, "status": "leader_exited"}, {"cost": -20.0, "status": "leader_exited"},
                {"cost": None, "status": "leader_still_open"}]
        summary = stop_analysis.summarize(rows)
        self.assertEqual(summary, {"stops": 3, "priced": 2, "net_cost": 10.0, "stops_that_cost": 1, "stops_that_saved": 1})


if __name__ == "__main__":
    unittest.main()
