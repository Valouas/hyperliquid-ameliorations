"""User-defined, isolated PAPER book. This module cannot submit exchange orders.

Inputs are timestamped observations. State, including baseline, stop latches,
deduplication and unfinished exits, survives restarts. Old research is untouched.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from pathlib import Path
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite observation")
    return result


def sign(value):
    return 1 if value > 0 else -1 if value < 0 else 0


def floor_size(value, decimals):
    return float(Decimal(str(max(0, value))).quantize(Decimal(10) ** -int(decimals), rounding=ROUND_DOWN))


def fill_order_key(fill):
    """Chronological order of leader fills.

    Partial fills of ONE order share the same millisecond and their tid is NOT
    monotonic, so (time, tid) scrambles them and every startPosition looks like
    a gap. Within a millisecond, order by position progression instead: a buy
    increases startPosition, a sell decreases it.
    """
    start = number(fill["startPosition"])
    return (number(fill["time"]), fill["coin"], start if fill["side"] == "B" else -start)


def is_main_perp(coin):
    return bool(coin) and ":" not in coin and "/" not in coin and not coin.startswith(("@", "+"))


def dex_of(coin):
    """'xyz:NVDA' -> 'xyz'; validator-operated perps -> ''; spot/outcomes -> None."""
    if "/" in coin or coin.startswith(("@", "+")):
        return None
    return coin.split(":", 1)[0] if ":" in coin else ""


HIP3_DEFAULTS = {
    # Runtime options, deliberately OUTSIDE the policy JSON (its sha256 is the checkpoint identity).
    "allow_hip3": False,            # new HIP-3 entries allowed
    "hip3_exit_only": False,        # set by the runner when a disabled book still holds HIP-3 copies
    "allowed_dexes": ["xyz"],       # HIP-3 dex names whose coins may be copied
    "hip3_min_volume_24h": None,    # number or {dex: number}; None -> policy min_volume_24h
    "hip3_max_total_notional": None,  # optional extra cap on the sum of HIP-3 notional of the book
    "hip3_fee_multiplier_unknown": 2.0,  # if meta lacks deployerFeeScale: 2x, no growth mode (conservative)
    "hip3_pause_weekend": True,     # no NEW HIP-3 entry Fri 20:00 -> Sun 20:05 America/New_York (xyz internal pricing)
    # Generic per-book runtime overrides (same channel, also outside the policy identity).
    "max_drift_major_bps": None,    # None -> policy max_drift_major_bps
    "max_drift_other_bps": None,    # None -> policy max_drift_other_bps
    "max_sizing_ratio": 1.0,        # sizing_capital/leader_equity above this = leader (almost) emptied: no new sizing
    # --- validation round 2026-10-31 (all runtime-only, outside the policy identity) ---
    "equity_floor_median_mult": 0.5,   # size from max(current equity, mult x 7-day median equity): only ever SMALLER sizing. 0/None = off
    "leader_equity_move_guard_pct": 0.30,  # no NEW entry while |leader account value change over the portfolio 'day' window| > this. 0/None = off
    "stop_exit_delay_s": 1.5,          # a latched stop closes only this long after it was detected (real reaction time). 0 = instant (old)
    "liquidation_model": True,         # isolated-margin liquidation at the Hyperliquid liq price (loss = whole position margin)
    "cf_log": True,                    # emit counterfactual / consensus records (cf_out, drained by the runner; no effect on the books)
    # --- 2026-11-10: selection improvements (runtime-only) ---
    "entries_paused": False,           # no NEW entry (ghost probation / retired leader); open copies still follow every exit
}
NEW_YORK = ZoneInfo("America/New_York")


def hip3_options(options):
    result = deepcopy(HIP3_DEFAULTS)
    unknown = set(options or {}) - set(HIP3_DEFAULTS)
    if unknown:
        raise ValueError("Unknown HIP-3 runtime option(s): " + ", ".join(sorted(unknown)))
    result.update(deepcopy(options or {}))
    for key in ("max_drift_major_bps", "max_drift_other_bps"):
        if result[key] is not None and not (isinstance(result[key], (int, float)) and 0 < result[key] <= 2000):
            raise ValueError(key + " override must be a number in (0, 2000] bps or null")
    if not (isinstance(result["max_sizing_ratio"], (int, float)) and result["max_sizing_ratio"] > 0):
        raise ValueError("max_sizing_ratio must be > 0")
    for key, low, high in (("equity_floor_median_mult", 0, 1), ("leader_equity_move_guard_pct", 0, 10), ("stop_exit_delay_s", 0, 60)):
        value = result[key]
        if value is not None and not (isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high):
            raise ValueError("%s must be null or a number in [%s, %s]" % (key, low, high))
    for key in ("liquidation_model", "cf_log", "entries_paused"):
        if not isinstance(result[key], bool):
            raise ValueError(key + " must be true or false")
    if not all(isinstance(d, str) and re.fullmatch(r"[a-z0-9]{1,8}", d) for d in result["allowed_dexes"]):
        raise ValueError("allowed_dexes must be short lowercase dex names (e.g. ['xyz'])")
    return result


def in_hip3_weekend(now):
    """xyz equities internal-pricing session: Friday 20:00 -> Sunday 20:05 ET (docs.trade.xyz changelog 2026-08-13)."""
    t = datetime.fromtimestamp(now, NEW_YORK)
    minute = t.hour*60 + t.minute
    return ((t.weekday() == 4 and minute >= 20*60) or t.weekday() == 5
            or (t.weekday() == 6 and minute < 20*60+5))


def load_policy(path="manual_copy_policy.json"):
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if policy["mode"] != "paper" or policy.get("automatic_replacements"):
        raise ValueError("Only explicitly configured paper wallets are supported")
    addresses = [w["address"] for w in policy["wallets"]]
    if len(addresses) != len(set(addresses)) or not all(re.fullmatch(r"0x[0-9a-f]{40}", a) for a in addresses):
        raise ValueError("Invalid or duplicate wallet address")
    if sum(number(w["margin"]) for w in policy["wallets"]) + policy["reserve_usd"] != policy["capital_usd"]:
        raise ValueError("Margins plus reserve must equal capital")
    for wallet in policy["wallets"]:
        if any(number(wallet[k]) <= 0 for k in ("margin", "sizing_capital", "max_leverage")):
            raise ValueError("Invalid wallet budget")
    return policy


def total_account_value(portfolio, now):
    """Official consolidated series, NOT perpDay or marginSummary.accountValue.

    Includes spot and perps under classic, unified and portfolio margin modes.
    Avoids adding a unified spot balance to collateral already counted elsewhere.
    No partial-equity fallback: unknown/stale totals must block new sizing.
    """
    points = dict(portfolio)["day"]["accountValueHistory"]
    timestamp, raw = max(points, key=lambda row: number(row[0]))
    at, equity = number(timestamp) / 1000, number(raw)
    if not -5 <= now - at <= 300 or equity <= 0:
        raise ValueError("Consolidated equity is stale, nonpositive or future-dated")
    return equity, at


def account_guards(portfolio, now):
    """Extra leader-equity facts from the SAME portfolio call (no extra request).

    median_7d: median of the 'week' accountValueHistory. move_24h: (last-first)/first of the 'day' series
    (deposits/withdrawals included on purpose: any >30 % jump means the account is not what we sized against).
    """
    data = dict(portfolio)
    result = {"median_7d": None, "move_24h": None, "av_first_24h": None}
    week = [number(v) for _, v in (data.get("week") or {}).get("accountValueHistory", []) if number(v) > 0]
    if len(week) >= 12:
        ordered = sorted(week)
        mid = len(ordered)//2
        result["median_7d"] = ordered[mid] if len(ordered) % 2 else (ordered[mid-1]+ordered[mid])/2
    day = sorted(((number(t), number(v)) for t, v in data["day"]["accountValueHistory"]), key=lambda x: x[0])
    if len(day) >= 2 and day[0][1] > 0 and day[-1][0]-day[0][0] >= 6*3600*1000:
        result["move_24h"] = (day[-1][1]-day[0][1])/day[0][1]
        result["av_first_24h"] = day[0][1]
    return result


def position_map(raw):
    result = {}
    for row in raw["assetPositions"]:
        p = row["position"]
        result[p["coin"]] = {"size": number(p["szi"]), "leverage": number(p["leverage"]["value"])}
    return result


class Engine:
    def __init__(self, policy, state=None, variant="post_only", options=None):
        self.policy = deepcopy(policy)
        # Runtime HIP-3 options: never part of the policy identity below.
        self.options = hip3_options(options)
        self.wallets = {w["address"]: w for w in policy["wallets"]}
        identity = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        self.state = deepcopy(state) if state is not None else {
            "policy_id": identity, "variant": variant, "wallets": {}, "positions": {},
            "seen": {}, "global_stop": 0, "events": [], "funding_pending": {},
            "funding_checked": None, "funding_settled": [], "peak_equity": policy["capital_usd"],
            "max_drawdown_usd": 0, "last_at": None,
        }
        if self.state["policy_id"] != identity or self.state["variant"] != variant:
            raise ValueError("Policy or comparison variant changed; an explicit migration is required")
        self.markets = {}
        self.books = {}
        self.marks = {}
        self.cf_out = []        # counterfactual / consensus records, drained by the runner (never part of the state)
        # Optional cross-book entry guard set by the multi runner: callable(row, now) -> blocking reason or None.
        self.entry_guard = None
        self._cause = "signal"

    def cf_emit(self, kind, **data):
        if self.options["cf_log"] and self.state["variant"] == "all_ioc":
            self.cf_out.append(dict(kind=kind, **data))
            if len(self.cf_out) > 20000:
                del self.cf_out[:5000]

    def event(self, kind, now, **data):
        self.state["events"].append(dict(kind=kind, at=now, **data))
        self.state["events"] = self.state["events"][-2000:]

    def wallet(self, address):
        if address not in self.wallets:
            raise ValueError("Wallet is not in the exclusive allowlist")
        return self.state["wallets"].setdefault(address, {
            "baseline_at": None, "reconciled_at": 0, "ratio": None, "ratio_at": 0,
            "equity_at": 0, "realized": 0, "funding": 0, "stop": 0,
        })

    def row(self, address, coin):
        self.wallet(address)
        return self.state["positions"].setdefault(address + ":" + coin, {
            "address": address, "coin": coin, "source": 0, "blocked_cycle": False,
            "target": 0, "qty": 0, "entry": 0, "leverage": 0, "source_leverage": 0,
            "reference": 0, "signal_at": 0, "exit_attempts": 0, "maker": None,
            "reason": "waiting_signal", "last_source_at": 0,
        })

    def update_ratio(self, address, portfolio, now):
        equity, at = total_account_value(portfolio, now)
        wallet = self.wallet(address)
        capital = self.wallets[address]["sizing_capital"]
        guards = {"median_7d": None, "move_24h": None, "av_first_24h": None}
        try:
            guards = account_guards(portfolio, now)
        except (ValueError, KeyError, TypeError):
            pass  # extra series missing/odd: old behaviour (current equity, no move guard)
        sizing_equity = equity
        mult = self.options["equity_floor_median_mult"]
        if mult and guards["median_7d"]:
            sizing_equity = max(equity, mult*guards["median_7d"])   # never larger sizing than with the current equity
        wallet.update(ratio=capital / sizing_equity, ratio_raw=capital / equity, sizing_equity=sizing_equity,
                      equity=equity, equity_at=at, ratio_at=now, equity_median_7d=guards["median_7d"],
                      av_move_24h=guards["move_24h"], av_first_24h=guards["av_first_24h"])

    @staticmethod
    def raw_ratio(wallet):
        """max_sizing_ratio is judged on the CURRENT equity (leader almost emptied), not on the floored sizing equity."""
        return wallet.get("ratio_raw", wallet["ratio"])

    def ratio_fresh(self, wallet, now):
        return (wallet["ratio"] is not None and 0 <= now-wallet["ratio_at"] <= 310
                and -5 <= now-wallet["equity_at"] <= 610
                and self.raw_ratio(wallet) <= self.options["max_sizing_ratio"])

    def ratio_too_large(self, wallet):
        return wallet["ratio"] is not None and self.raw_ratio(wallet) > self.options["max_sizing_ratio"]

    def equity_jump(self, wallet):
        """Signed 24 h account-value move when it exceeds the guard, else None."""
        limit, move = self.options["leader_equity_move_guard_pct"], wallet.get("av_move_24h")
        return move if (limit and move is not None and abs(move) > limit) else None

    def drift_bps(self, major):
        key = "max_drift_major_bps" if major else "max_drift_other_bps"
        override = self.options[key]
        return self.policy[key] if override is None else override

    def baseline(self, address, positions, at):
        wallet = self.wallet(address)
        if wallet["baseline_at"] is not None:
            return
        wallet.update(baseline_at=at, reconciled_at=at)
        for coin, p in positions.items():
            row = self.row(address, coin)
            row.update(source=p["size"], source_leverage=p["leverage"],
                       blocked_cycle=bool(p["size"]), last_source_at=at)
        self.event("baseline", at, address=address, ignored_open_positions=sum(bool(p["size"]) for p in positions.values()))

    def fill(self, address, fill, now):
        wallet = self.wallet(address)
        at = number(fill["time"]) / 1000
        if wallet["baseline_at"] is None or at <= wallet["baseline_at"] or at > now + 5:
            return
        identity = address + ":" + json.dumps([fill.get("tid"), fill["time"], fill["coin"], fill["side"], fill["sz"], fill["px"]])
        if identity in self.state["seen"]:
            return
        coin = fill["coin"]
        if not self.followed(address, coin, at):
            self.state["seen"][identity] = at
            self._cf_unfollowed(address, fill, at, now)
            return
        row = self.row(address, coin)
        if at < row["last_source_at"]:
            self.state["seen"][identity] = at
            return
        before = number(fill["startPosition"])
        delta = number(fill["sz"]) * (1 if fill["side"] == "B" else -1)
        if fill["side"] not in ("B", "A") or not delta:
            raise ValueError("Invalid fill side/size")
        after = before + delta
        if abs(after) < 1e-12:
            after = 0.0
        if not math.isclose(before, row["source"], abs_tol=1e-8, rel_tol=1e-7):
            self.event("source_gap", now, address=address, coin=coin)
            # A missed fill INSIDE a cycle we were already allowed to follow
            # (flat after baseline, or same-direction unblocked cycle) is not a
            # pre-existing position: resynchronise the target to the leader's
            # current size instead of discarding the whole cycle. Requires a
            # fresh ratio and a fresh fill, otherwise fall back to blocking.
            tracked = (not row["blocked_cycle"] and bool(before)
                       and (not row["source"] or sign(row["source"]) == sign(before)))
            if tracked and self.ratio_fresh(wallet, now) and now-at <= self.policy["max_signal_age_seconds"]:
                self.event("source_gap_resync", now, address=address, coin=coin)
                if abs(before) < abs(row["source"]):
                    self._reduce_source(row, before)
                else:
                    row["target"] = sign(before) * max(abs(row["target"]), abs(before) * wallet["ratio"])
                    row.update(reference=number(fill["px"]), signal_at=at, maker=None)
                row["source"] = before
            else:
                row["blocked_cycle"] = bool(before)
                # A gap can never manufacture an entry. Existing copies still close.
                self._reduce_source(row, before)
        reducing = before and (not after or sign(after) != sign(before) or abs(after) < abs(before))
        if reducing:
            target0, qty0 = row["target"], row["qty"]
            remainder = abs(after / before) if sign(after) == sign(before) else 0
            row["target"] *= remainder
            # A partly executed entry must reduce the actual filled quantity pro rata.
            if sign(row["qty"]) == sign(before):
                row["target"] = sign(before) * min(abs(row["target"]), abs(row["qty"]) * remainder)
            row["maker"] = None
            # Not-yet-executed part of an entry that disappears with the leader's exit = a skipped signal.
            lost = self._pending(target0, qty0, before) - self._pending(row["target"], qty0, before)
            if lost > 0:
                self._cf_unfilled(row, lost, now, row.get("reason"), "leader_reduced")
            row.update(exit_ref=number(fill["px"]), exit_ref_at=at)
            self.cf_reduce(address, coin, number(fill["px"]), at, before, after)
        new_cycle = (not before or sign(before) != sign(after)) and bool(after)
        if new_cycle:
            row["blocked_cycle"] = False
            row["maker"] = None
        added = abs(after) if new_cycle else max(0, abs(after) - abs(before))
        if added:
            self.cf_emit("leader_entry", address=address, coin=coin, dir=sign(after), px=number(fill["px"]), t=at,
                         added=added, new_cycle=bool(new_cycle))
        if added and not row["blocked_cycle"] and self.ratio_fresh(wallet, now) and now-at <= self.policy["max_signal_age_seconds"]:
            row["target"] += sign(after) * added * wallet["ratio"]
            row.update(reference=number(fill["px"]), signal_at=at, maker=None)
        elif added:
            row["reason"] = "entry_not_sized_stale_or_preexisting"
            if row["blocked_cycle"]:
                why = "blocked_cycle_preexisting_or_gap"
            elif self.ratio_too_large(wallet):
                why = "leader_equity_too_small"
            elif not self.ratio_fresh(wallet, now):
                why = "stale_ratio"
            else:
                why = "stale_signal"
            self._cf_signal_skip(address, coin, sign(after), at, number(fill["px"]), added, why, "fill", now, row["source_leverage"], wallet)
        row.update(source=after, last_source_at=at)
        if not after:
            row.update(target=0, blocked_cycle=False, maker=None)
        self.state["seen"][identity] = at

    # ----- counterfactual log (records only; never changes a book) -----
    def _cf_on(self):
        return self.options["cf_log"] and self.state["variant"] == "all_ioc"

    @staticmethod
    def _pending(target, qty, before):
        """Unexecuted size (absolute) of an entry in the leader's current direction."""
        if not target or sign(target) != sign(before):
            return 0
        done = abs(qty) if sign(qty) == sign(target) else 0
        return max(0, abs(target) - done)

    def _cf_effective_ratio(self, wallet):
        ratio = wallet.get("ratio")
        if ratio is None:
            return None
        return min(ratio, self.options["max_sizing_ratio"])   # a drained leader would be copied at the cap at most

    def _cf_signal_skip(self, address, coin, direction, t, px, added, reason, stage, now, leverage, wallet):
        ratio = self._cf_effective_ratio(wallet)
        qty = added*ratio if ratio is not None else 0
        self.cf_skip(address, coin, direction, t, px, qty, added, reason, stage, now, leverage, ratio)

    def _cf_unfilled(self, row, lost_qty, now, reason, stage):
        if not self._cf_on() or lost_qty*max(row["reference"], 0) < 1:
            return
        reason = reason if reason not in (None, "waiting_signal", "ioc_attempted", "reduced", "exit_retry", "post_only_resting",
                                         "post_only_would_cross", "entry_not_sized_stale_or_preexisting") else "unfilled_before_expiry_or_exit"
        ratio = self.wallet(row["address"]).get("ratio")
        self.cf_skip(row["address"], row["coin"], sign(row["target"]) or sign(row["source"]), row["signal_at"], row["reference"], lost_qty,
                     lost_qty/ratio if ratio else None, reason, stage, now, row["source_leverage"], ratio)

    def cf_skip(self, address, coin, direction, t, px, qty, src_size, reason, stage, now, leverage=0, ratio=None):
        if not self._cf_on() or not direction or px <= 0:
            return
        cf = self.state.setdefault("cf", {"lots": {}, "n": 0})
        cf["n"] += 1
        lot = "%s:%s:%d:%d" % (address[2:8], coin, int(t*1000), cf["n"])
        if qty > 0:
            lots = cf["lots"].setdefault(address + ":" + coin, [])
            lots.append({"id": lot, "dir": direction, "qty": qty, "px": px, "t": t})
            del lots[:-100]
        self.cf_emit("skip", id=lot, address=address, coin=coin, dir=direction, t=t, px=px, qty=qty, src_size=src_size,
                     reason=reason, stage=stage, now=now, leverage=leverage, ratio=ratio)

    def cf_reduce(self, address, coin, px, t, before, after):
        """Leader reduced/closed: virtual (skipped) lots of this coin are closed pro rata at the leader's price."""
        lots = self.state.get("cf", {}).get("lots", {}).get(address + ":" + coin) if self._cf_on() else None
        if not lots:
            return
        closing = 1.0 if (not after or sign(after) != sign(before)) else max(0.0, 1-abs(after)/abs(before))
        if closing <= 0:
            return
        for lot in list(lots):
            part = lot["qty"]*closing
            stale = lot["dir"] != sign(before)
            self.cf_emit("cf_exit", id=lot["id"], address=address, coin=coin, dir=lot["dir"], t=t, px=None if stale else px,
                         qty=part, remaining=lot["qty"]-part, stale=stale)
            lot["qty"] -= part
            if lot["qty"] < 1e-12 or stale:
                lots.remove(lot)
        if not lots:
            del self.state["cf"]["lots"][address + ":" + coin]

    def _cf_unfollowed(self, address, fill, at, now):
        """Leader fills on markets this book does not copy (HIP-3 off ...): logged as skipped signals."""
        coin = fill["coin"]
        dex = dex_of(coin)
        if not self._cf_on() or dex is None or "startPosition" not in fill:
            return
        before = number(fill["startPosition"])
        delta = number(fill["sz"]) * (1 if fill["side"] == "B" else -1)
        after = 0.0 if abs(before+delta) < 1e-12 else before+delta
        if before and (not after or sign(after) != sign(before) or abs(after) < abs(before)):
            self.cf_reduce(address, coin, number(fill["px"]), at, before, after)
        new_cycle = (not before or sign(before) != sign(after)) and bool(after)
        added = abs(after) if new_cycle else max(0, abs(after) - abs(before))
        if added:
            wallet = self.wallet(address)
            why = "hip3_not_enabled" if (not self.hip3_active() or dex not in self.options["allowed_dexes"]) else "hip3_baseline_pending"
            self.cf_emit("leader_entry", address=address, coin=coin, dir=sign(after), px=number(fill["px"]), t=at,
                         added=added, new_cycle=bool(new_cycle))
            self._cf_signal_skip(address, coin, sign(after), at, number(fill["px"]), added, why, "fill", now, 0, wallet)

    def _reduce_source(self, row, size):
        old = row["source"]
        if not size or (old and sign(old) != sign(size)):
            row.update(target=0, maker=None)
        elif old and abs(size) < abs(old):
            fraction = abs(size / old)
            row["target"] = sign(old) * min(abs(row["target"])*fraction, abs(row["qty"])*fraction)
            row["maker"] = None
        row["source"] = size

    def hip3_active(self):
        return self.options["allow_hip3"] or self.options["hip3_exit_only"]

    def followed(self, address, coin, at):
        """Main perps always; a HIP-3 coin only if its dex is enabled and was baselined before this fill."""
        if is_main_perp(coin):
            return True
        dex = dex_of(coin)
        if not dex or not self.hip3_active() or dex not in self.options["allowed_dexes"]:
            return False
        seen = self.wallet(address).get("hip3_baseline", {}).get(dex)
        return seen is not None and at > seen

    def hip3_baseline(self, address, positions, at, dexes):
        """First time a HIP-3 dex is visible for this wallet: its open positions are pre-existing (blocked)."""
        wallet = self.wallet(address)
        if wallet["baseline_at"] is None or not self.hip3_active():
            return
        marks = wallet.setdefault("hip3_baseline", {})
        for dex in dexes:
            if dex in marks or dex not in self.options["allowed_dexes"]:
                continue
            marks[dex] = at
            ignored = 0
            for coin, p in positions.items():
                if dex_of(coin) == dex:
                    row = self.row(address, coin)
                    row.update(source=p["size"], source_leverage=p["leverage"], blocked_cycle=bool(p["size"]), last_source_at=at)
                    ignored += bool(p["size"])
            self.event("hip3_baseline", at, address=address, dex=dex, ignored_open_positions=ignored)

    def reconcile(self, address, positions, at, dexes=None):
        wallet = self.wallet(address)
        if wallet["baseline_at"] is None:
            return self.baseline(address, positions, at)
        coins = set(positions) | {r["coin"] for r in self.state["positions"].values() if r["address"] == address}
        if dexes is not None:
            # Only coins of dexes actually fetched in this snapshot can be compared (missing dex != flat).
            visible = {""} | {d for d in dexes if d in wallet.get("hip3_baseline", {})}
            coins = {c for c in coins if dex_of(c) in visible}
        for coin in coins:
            row = self.row(address, coin)
            p = positions.get(coin, {"size": 0, "leverage": 0})
            if row["last_source_at"] > at:
                continue
            if not math.isclose(row["source"], p["size"], abs_tol=1e-8, rel_tol=1e-7):
                self.event("reconciliation_gap", at, address=address, coin=coin)
                self._reduce_source(row, p["size"])
                row["blocked_cycle"] = bool(p["size"])
            row.update(source_leverage=p["leverage"], last_source_at=at)
        wallet["reconciled_at"] = at

    def market(self, metadata, contexts, at):
        if len(metadata["universe"]) != len(contexts):
            raise ValueError("Market metadata/context mismatch")
        for meta, ctx in zip(metadata["universe"], contexts):
            coin = meta["name"]
            self.markets[coin] = dict(meta, volume=number(ctx["dayNtlVlm"]), at=at)
            px = number(ctx["markPx"])
            if px > 0:
                self.marks[coin] = (px, at)

    def book(self, data, at):
        # Exchange timestamps are necessary: a delayed snapshot is not fresh data.
        observed = number(data["time"]) / 1000
        if not -5 <= at-observed <= self.policy["max_book_age_seconds"]:
            return
        coin = data["coin"]
        previous = self.books.get(coin)
        if previous and previous["exchange_at"] >= observed:
            return
        levels = [[{"px": number(x["px"]), "sz": number(x["sz"])} for x in side] for side in data["levels"]]
        if len(levels) != 2 or not all(levels) or not 0 < levels[0][0]["px"] <= levels[1][0]["px"]:
            return
        self.books[coin] = {"levels": levels, "at": at, "exchange_at": observed}
        self.marks[coin] = ((levels[0][0]["px"]+levels[1][0]["px"])/2, observed)

    def pnls(self, now, include_funding=True):
        values = {a: self.wallet(a)["realized"] + self.wallet(a)["funding"] for a in self.wallets}
        complete = True
        for row in self.state["positions"].values():
            if not row["qty"]:
                continue
            price, at = self.marks.get(row["coin"], (row["entry"], 0))
            complete &= 0 <= now-at <= 70
            # Reserve the taker fee required to liquidate each open copy.
            values[row["address"]] += row["qty"] * (price-row["entry"]) - abs(row["qty"])*price*self.fee_rate(row["coin"])
        if include_funding:
            complete &= not bool(self.state["funding_pending"])
        return values, bool(complete)

    def stops(self, now):
        # Hourly funding is settled ~2 min after each hour; an unsettled debt is
        # tiny versus the stops and must not block every entry at HH:00-HH:03.
        # report() still flags valuation_complete=False until it is settled.
        values, complete = self.pnls(now, include_funding=False)
        total = sum(values.values())
        global_stop = 2 if total <= -self.policy["global_close_loss"] else 1 if total <= -self.policy["global_pause_loss"] else 0
        if global_stop > self.state["global_stop"]:
            self.event("global_stop", now, level=global_stop, pnl=total)
            if global_stop >= 2:
                self.state["global_stop_hit_at"] = now
        self.state["global_stop"] = max(self.state["global_stop"], global_stop)
        for address, pnl in values.items():
            wallet, config = self.wallet(address), self.wallets[address]
            level = 0
            for stage, key in enumerate(("wallet_alert_fraction", "wallet_pause_fraction", "wallet_close_fraction"), 1):
                if pnl <= -config["margin"] * self.policy[key]:
                    level = stage
            if level > wallet["stop"]:
                self.event("wallet_stop", now, address=address, level=level, pnl=pnl)
                if level >= 3:
                    wallet["stop_hit_at"] = now
            wallet["stop"] = max(wallet["stop"], level)
        return complete

    def stop_close_due(self, row, now):
        """Latched close-stop: execute only stop_exit_delay_s after detection (legacy latches without a timestamp: at once)."""
        wallet = self.wallet(row["address"])
        hits = []
        if self.state["global_stop"] >= 2:
            hits.append(self.state.get("global_stop_hit_at"))
        if wallet["stop"] >= 3:
            hits.append(wallet.get("stop_hit_at"))
        hits = [h for h in hits if h is not None]
        if not hits or len(hits) < (int(self.state["global_stop"] >= 2) + int(wallet["stop"] >= 3)):
            return True   # legacy latch: old behaviour
        return now >= min(hits) + (self.options["stop_exit_delay_s"] or 0)

    def accrue_funding(self, now):
        previous = self.state["funding_checked"]
        if previous is not None:
            for hour in range((int(previous)//3600+1)*3600, int(now)+1, 3600):
                for key, row in self.state["positions"].items():
                    if row["qty"]:
                        self.state["funding_pending"].setdefault(key+":"+str(hour), {
                            "address": row["address"], "coin": row["coin"], "hour": hour, "qty": row["qty"],
                        })
        self.state["funding_checked"] = now

    def settle_funding(self, coin, hour, rate, price):
        rate, price = number(rate), number(price)
        if price <= 0:
            raise ValueError("Invalid historical funding price")
        for key, debt in list(self.state["funding_pending"].items()):
            if debt["coin"] == coin and debt["hour"] == hour:
                amount = -debt["qty"] * price * rate
                self.wallet(debt["address"])["funding"] += amount
                self.state["funding_settled"].append(dict(debt, amount=amount, rate=rate, price=price))
                del self.state["funding_pending"][key]

    def eligible(self, row, now):
        coin, address = row["coin"], row["address"]
        meta = self.markets.get(coin)
        hip3 = not is_main_perp(coin)
        if not meta or (hip3 and not self.followed(address, coin, math.inf)):
            return "excluded_market"
        if meta.get("isDelisted") or coin in self.wallets[address]["exclude_coins"]:
            return "excluded_coin"
        if now-meta["at"] > 310 or meta["volume"] < self.min_volume(coin):
            return "volume_missing_or_below_2m"
        if hip3:
            reason = self.hip3_entry_block(row, now)
            if reason:
                return reason
        if self.state["global_stop"] or self.wallet(address)["stop"] >= 2:
            return "stop_entries"
        if self.options["entries_paused"]:
            return "entries_paused"
        if self.ratio_too_large(self.wallet(address)):
            return "leader_equity_too_small"
        if not self.ratio_fresh(self.wallet(address), now):
            return "stale_ratio"
        if self.equity_jump(self.wallet(address)) is not None:
            return "leader_equity_move_24h"
        if now-self.wallet(address)["reconciled_at"] > 70:
            return "stale_leader_positions"
        if not row["source_leverage"]:
            return "waiting_leader_leverage"
        if self.entry_guard is not None:
            return self.entry_guard(row, now)
        return None

    def min_volume(self, coin):
        threshold = self.options["hip3_min_volume_24h"]
        if is_main_perp(coin) or threshold is None:
            return self.policy["min_volume_24h"]
        if isinstance(threshold, dict):
            return number(threshold.get(dex_of(coin), self.policy["min_volume_24h"]))
        return number(threshold)

    def hip3_entry_block(self, row, now):
        if not self.options["allow_hip3"]:
            return "hip3_exit_only"
        if self.options["hip3_pause_weekend"] and in_hip3_weekend(now):
            return "hip3_weekend_pause"
        return None

    def fee_rate(self, coin, maker=False):
        """Validator perps: policy rates. HIP-3: official formula (docs trading/fees, 'Fee formula for developers'):
        base x (scale+1 if scale<1 else 2*scale) x (0.1 if growth mode). Unknown meta -> conservative multiplier."""
        base = self.policy["maker_fee" if maker else "taker_fee"]
        if is_main_perp(coin):
            return base
        meta = self.markets.get(coin) or {}
        if meta.get("deployerFeeScale") is None:
            return base * self.options["hip3_fee_multiplier_unknown"]
        scale = number(meta["deployerFeeScale"])
        multiplier = scale + 1 if scale < 1 else 2 * scale
        return base * multiplier * (0.1 if meta.get("growthMode") == "enabled" else 1)

    def capacity(self, row, price, leverage, now):
        """Gross caps include BOTH directions, at conservative executable prices."""
        total, by_coin, margin = 0, {}, 0
        for other in self.state["positions"].values():
            mark = self.marks.get(other["coin"], (other["entry"], 0))[0]
            value = abs(other["qty"]) * max(mark, price if other["coin"] == row["coin"] else mark)
            total += value
            by_coin[other["coin"]] = by_coin.get(other["coin"], 0) + value
            if other["address"] == row["address"]:
                margin += value / max(other["leverage"], 1)
        p = self.policy
        coin_cap = p["major_coin_cap"] if row["coin"] in p["major_coins"] else p["other_coin_cap"]
        pnl = self.pnls(now)[0][row["address"]]
        margin_left = self.wallets[row["address"]]["margin"] + min(0, pnl) - margin
        available = min(p["max_total_notional"]-total, coin_cap-by_coin.get(row["coin"], 0),
                        margin_left / (1/leverage + self.fee_rate(row["coin"])))
        if not is_main_perp(row["coin"]) and self.options["hip3_max_total_notional"] is not None:
            hip3_total = sum(v for c, v in by_coin.items() if not is_main_perp(c))
            available = min(available, number(self.options["hip3_max_total_notional"]) - hip3_total)
        for group in p["group_caps"]:
            if row["coin"] in group["coins"]:
                available = min(available, group["cap"] - sum(by_coin.get(c, 0) for c in group["coins"]))
        config = self.wallets[row["address"]]
        own_coin = own_total = 0
        for other in self.state["positions"].values():
            if other["address"] != row["address"] or not other["qty"]:
                continue
            mark = self.marks.get(other["coin"], (other["entry"], 0))[0]
            value = abs(other["qty"]) * max(mark, price if other["coin"] == row["coin"] else mark)
            own_total += value
            if other["coin"] == row["coin"]:
                own_coin += value
        if "max_coin_notional" in config:
            available = min(available, number(config["max_coin_notional"]) - own_coin)
        if "max_total_notional" in config:
            available = min(available, number(config["max_total_notional"]) - own_total)
        return max(0, available)

    def execute(self, row, delta, price, leverage, now, maker=False):
        old = row["qty"]
        wallet = self.wallet(row["address"])
        fee = abs(delta)*price*self.fee_rate(row["coin"], maker)
        if old and sign(old) != sign(delta):
            if abs(delta) > abs(old) + 1e-10:
                raise ValueError("reduceOnly execution cannot reverse a position")
            wallet["realized"] += abs(delta)*sign(old)*(price-row["entry"]) - fee
        else:
            row["entry"] = (abs(old)*row["entry"]+abs(delta)*price)/(abs(old)+abs(delta))
            wallet["realized"] -= fee
        row["qty"] += delta
        if abs(row["qty"]) < 1e-12:
            row["qty"], row["entry"] = 0, 0
        row["leverage"] = leverage
        self.state["fill_count"] = self.state.get("fill_count", 0) + 1
        self.event("paper_fill", now, address=row["address"], coin=row["coin"], quantity=delta,
                   price=price, fee=fee, reduce_only=bool(old and sign(old) != sign(delta)),
                   tif="Alo" if maker else "Ioc", margin_mode="isolated", leverage=leverage, cause=self._cause)

    def liquidation_price(self, row):
        """Hyperliquid isolated liq price: p - side*margin/size/(1 - l*side), l = 1/(2*maxLeverage). None if unknown."""
        meta = self.markets.get(row["coin"])
        if not row["qty"] or not row["entry"] or not meta or not meta.get("maxLeverage") or row["leverage"] <= 0:
            return None
        side, size = sign(row["qty"]), abs(row["qty"])
        l = 1/(2*meta["maxLeverage"])
        margin = size*row["entry"]/row["leverage"]
        return row["entry"] - side*margin/size/(1 - l*side)

    def _liquidate_if_due(self, row, now):
        mark = self.marks.get(row["coin"])
        liq = self.liquidation_price(row)
        if mark is None or liq is None or not 0 <= now-mark[1] <= 70:
            return
        side = sign(row["qty"])
        if (mark[0]-liq)*side > 0:
            return
        # Conservative: closed at the WORSE of liq price / current mark; the whole isolated margin is lost.
        price = min(liq, mark[0]) if side > 0 else max(liq, mark[0])
        margin = abs(row["qty"])*row["entry"]/row["leverage"]
        wallet = self.wallet(row["address"])
        before = wallet["realized"]
        self._cause = "liquidation"
        self.execute(row, -row["qty"], price, row["leverage"], now)
        self._cause = "signal"
        wallet["realized"] = before - margin          # loss capped at (and equal to) the isolated margin
        row.update(target=0, maker=None, blocked_cycle=True, exit_attempts=0, reason="liquidated")
        self.event("paper_liquidation", now, address=row["address"], coin=row["coin"], liq_price=liq, mark=mark[0],
                   exec_price=price, margin_lost=margin)

    def _ioc(self, row, delta, limit, leverage, now):
        side = 1 if delta > 0 else 0
        remaining = abs(delta)
        for level in self.books[row["coin"]]["levels"][side]:
            if (delta > 0 and level["px"] > limit) or (delta < 0 and level["px"] < limit):
                break
            quantity = min(remaining, level["sz"])
            if quantity <= 0:
                continue
            self.execute(row, sign(delta)*quantity, level["px"], leverage, now)
            level["sz"] -= quantity
            remaining -= quantity
            if remaining < 1e-12:
                break
        return remaining

    def tick(self, now):
        self.accrue_funding(now)
        complete = self.stops(now)
        p = self.policy
        # All reductions are processed before any increase; close-reverse is two legs.
        rows = list(self.state["positions"].values())
        # Refresh the leverage ceiling on existing positions as well. A lower
        # ceiling must not create unaffordable isolated margin: trim that book.
        for row in rows:
            meta = self.markets.get(row["coin"])
            if row["qty"] and meta and row["source_leverage"] > 0:
                ceiling = min(self.wallets[row["address"]]["max_leverage"], row["source_leverage"], meta["maxLeverage"])
                if ceiling < row["leverage"]:
                    row["leverage"] = ceiling
                    self.event("paper_leverage_reduced", now, address=row["address"], coin=row["coin"], leverage=ceiling)
        pnls = self.pnls(now)[0]
        for address, config in self.wallets.items():
            used = sum(abs(r["qty"])*self.marks.get(r["coin"], (r["entry"], 0))[0]/max(r["leverage"], 1)
                       for r in rows if r["address"] == address)
            available = max(0, config["margin"]+min(0, pnls[address]))
            if used > available and used > 0:
                for row in rows:
                    if row["address"] == address and row["qty"] and sign(row["target"]) == sign(row["qty"]):
                        row["target"] = sign(row["qty"])*min(abs(row["target"]), abs(row["qty"])*available/used)
        if self.options["liquidation_model"]:
            for row in rows:
                if row["qty"]:
                    self._liquidate_if_due(row, now)
        for row in rows:
            forced = self.state["global_stop"] >= 2 or self.wallet(row["address"])["stop"] >= 3
            if forced and row["qty"] and not self.stop_close_due(row, now):
                continue   # stop detected, reaction delay not elapsed yet
            if forced:
                row.update(target=0, maker=None)
            self._cause = "stop" if forced and row["qty"] else "signal"
            qty, target = row["qty"], row["target"]
            reduce = qty and (sign(qty) != sign(target) or abs(target) < abs(qty)-1e-12)
            if not reduce:
                continue
            row["maker"] = None
            book = self.books.get(row["coin"])
            if not book or now-book["exchange_at"] > p["max_book_age_seconds"]:
                row["reason"] = "exit_waiting_fresh_book"
                continue
            mid = self.marks[row["coin"]][0]
            full = sign(qty) != sign(target) or target == 0
            delta = -qty if full else target-qty
            meta = self.markets.get(row["coin"])
            if not full:
                if not meta:
                    continue
                threshold = p["accumulation_threshold_usd"]
                if abs(target)*mid < threshold:
                    # Target below the minimum position size: close fully, never keep dust.
                    full, delta = True, -qty
                else:
                    delta = sign(delta)*floor_size(abs(delta), meta["szDecimals"])
                    if abs(qty+delta)*mid < threshold:
                        # Partial reduce would leave a position under the minimum: close fully.
                        full, delta = True, -qty
                    elif abs(delta)*mid < threshold:
                        row["reason"] = "accumulating_reduction"
                        continue
            bps = p["exit_slippage_bps"][min(row["exit_attempts"], len(p["exit_slippage_bps"])-1)]
            left = self._ioc(row, delta, mid*(1+sign(delta)*bps/10000), row["leverage"], now)
            row["exit_attempts"] = row["exit_attempts"]+1 if left > 1e-12 else 0
            row["reason"] = "exit_retry" if left > 1e-12 else "reduced"
        self._cause = "signal"
        complete &= self.stops(now)
        for row in rows:
            qty, target = row["qty"], row["target"]
            if not target or (qty and sign(qty) != sign(target)) or abs(target) <= abs(qty)+1e-12:
                continue
            # Expiry first (was after the eligibility check): an entry blocked for longer than its TTL must not
            # wake up hours later when the block clears (leader_equity_move_24h, stale ratio, stop...).
            config0 = self.wallets[row["address"]]
            post0 = config0["entry"] == "post_only_then_ioc" and self.state["variant"] == "post_only"
            lifetime0 = config0.get("post_only_seconds", 120) if post0 else p["max_signal_age_seconds"]
            if now-row["signal_at"] > lifetime0 + p.get("entry_signal_ttl_seconds", p["max_signal_age_seconds"]):
                self._cf_unfilled(row, abs(target)-abs(qty), now, row.get("reason"), "expired")
                row.update(reason="expired_entry_signal", maker=None, target=qty)
                continue
            reason = self.eligible(row, now)
            if reason or not complete:
                row["reason"] = reason or "incomplete_valuation_or_funding"
                row["maker"] = None
                continue
            book = self.books.get(row["coin"])
            if not book or now-book["exchange_at"] > p["max_book_age_seconds"]:
                row["reason"] = "entry_waiting_fresh_book"
                continue
            mid = self.marks[row["coin"]][0]
            config, meta = self.wallets[row["address"]], self.markets[row["coin"]]
            leverage = min(config["max_leverage"], row["source_leverage"], meta["maxLeverage"])
            # Do not silently increase previously committed leverage to free margin.
            if qty:
                leverage = min(leverage, row["leverage"])
            major = row["coin"] in p["major_coins"]
            drift = self.drift_bps(major)
            slip = p["entry_slippage_major_bps" if major else "entry_slippage_other_bps"]
            direction = sign(target)
            if abs(mid/row["reference"]-1)*10000 > drift:
                row["reason"] = "leader_price_drift"
                row["maker"] = None
                continue
            limit = mid*(1+direction*slip/10000)
            # Price drift bound also applies to actual execution, not just mid.
            limit = min(limit, row["reference"]*(1+drift/10000)) if direction > 0 else max(limit, row["reference"]*(1-drift/10000))
            risk_price = max(mid, limit, row["reference"])
            quantity = floor_size(min(abs(target)-abs(qty), self.capacity(row, risk_price, leverage, now)/risk_price), meta["szDecimals"])
            if quantity*min(mid, limit) < p["accumulation_threshold_usd"]:
                row["reason"] = "accumulating_or_risk_cap"
                continue
            post = config["entry"] == "post_only_then_ioc" and self.state["variant"] == "post_only"
            lifetime = config.get("post_only_seconds", 120) if post else p["max_signal_age_seconds"]
            # Accumulated / transiently blocked entries survive up to the TTL; the
            # leader_price_drift check above still refuses to chase the price.
            ttl = p.get("entry_signal_ttl_seconds", p["max_signal_age_seconds"])
            if post and now-row["signal_at"] < lifetime:
                if row["maker"] is None:
                    # Crossing post-only is rejected, and retried until the deadline.
                    crosses = row["reference"] >= book["levels"][1][0]["px"] if direction > 0 else row["reference"] <= book["levels"][0][0]["px"]
                    if crosses:
                        row["reason"] = "post_only_would_cross"
                        continue
                    row["maker"] = {"at": now, "price": row["reference"], "qty": quantity}
                row["maker"]["qty"] = min(row["maker"]["qty"], quantity)
                row["reason"] = "post_only_resting"
                continue
            row["maker"] = None
            self._ioc(row, direction*quantity, limit, leverage, now)
            row["reason"] = "ioc_attempted"
        values, complete = self.pnls(now)
        equity = p["capital_usd"] + sum(values.values())
        if complete:
            self.state["peak_equity"] = max(self.state["peak_equity"], equity)
            self.state["max_drawdown_usd"] = max(self.state["max_drawdown_usd"], self.state["peak_equity"]-equity)
        self.state["last_at"] = now
        # Retain identities across reconnections/restarts; stale fills are also
        # rejected against each position's last reconciled exchange timestamp.
        self.state["seen"] = {k: v for k, v in self.state["seen"].items() if now-v < 172800}

    def public_trade(self, trade, now):
        """Conservative maker model: trade strictly through our price, never touch.

        Queue position is unknowable in paper; this remains an approximation.
        Quantity is bounded by observed public volume and every current risk cap.
        """
        at = number(trade["time"])/1000
        if not -5 <= now-at <= 10:
            return
        identity = "public:" + json.dumps([trade["coin"], trade.get("tid"), trade["time"], trade["px"], trade["sz"], trade["side"]])
        if identity in self.state["seen"]:
            return
        self.state["seen"][identity] = at
        remaining = number(trade["sz"])
        px = number(trade["px"])
        complete = self.stops(now)
        for row in self.state["positions"].values():
            order = row["maker"]
            if not order or row["coin"] != trade["coin"] or at <= order["at"]:
                continue
            if self.eligible(row, now) or not complete or now-row["signal_at"] >= self.wallets[row["address"]].get("post_only_seconds", 120):
                row["maker"] = None
                continue
            direction = sign(row["target"])
            through = trade["side"] == "A" and px < order["price"] if direction > 0 else trade["side"] == "B" and px > order["price"]
            if not through:
                continue
            meta = self.markets[row["coin"]]
            leverage = min(self.wallets[row["address"]]["max_leverage"], row["source_leverage"], meta["maxLeverage"])
            if row["qty"]:
                leverage = min(leverage, row["leverage"])
            quantity = floor_size(min(remaining, order["qty"], max(0, abs(row["target"])-abs(row["qty"])),
                                      self.capacity(row, order["price"], leverage, now)/order["price"]), meta["szDecimals"])
            if quantity > 0:
                self.execute(row, direction*quantity, order["price"], leverage, now, maker=True)
                remaining -= quantity
                order["qty"] -= quantity
                if order["qty"] <= 1e-12:
                    row["maker"] = None

    def exit_spread_reserve(self, now):
        """INFORMATION ONLY: extra cost of closing every open copy against the current book instead of the mid
        (open P&L is marked at mid with only the taker fee reserved). None where no fresh book."""
        total, covered = 0.0, True
        for row in self.state["positions"].values():
            if not row["qty"]:
                continue
            book = self.books.get(row["coin"])
            if not book or now-book["exchange_at"] > 60:
                covered = False
                continue
            mid = (book["levels"][0][0]["px"]+book["levels"][1][0]["px"])/2
            remaining, proceeds = abs(row["qty"]), 0.0
            for level in book["levels"][0 if row["qty"] > 0 else 1]:
                take = min(remaining, level["sz"])
                proceeds += take*level["px"]
                remaining -= take
                if remaining < 1e-12:
                    break
            proceeds += remaining*(level["px"]*(1-sign(row["qty"])*0.01))   # beyond visible depth: 1 % worse than the last level
            total += abs(row["qty"])*mid-proceeds if row["qty"] > 0 else proceeds-abs(row["qty"])*mid
        return {"usd": round(total, 4), "all_positions_covered": covered}

    def report(self, now):
        values, complete = self.pnls(now)
        return {"mode": "paper", "variant": self.state["variant"], "at": now,
                "exit_spread_reserve": self.exit_spread_reserve(now),
                "pnl_net_usd": sum(values.values()), "valuation_complete": complete,
                "capital_usd": self.policy["capital_usd"], "reserve_usd": self.policy["reserve_usd"],
                "max_drawdown_usd": self.state["max_drawdown_usd"], "global_stop": self.state["global_stop"],
                "wallets": {a: dict(self.wallet(a), pnl_net_usd=values[a]) for a in self.wallets},
                "positions": list(self.state["positions"].values()),
                "funding_pending": len(self.state["funding_pending"]),
                "fills": self.state.get("fill_count", 0),
                "missing": [self.policy["leader_exclusions_status"], "REPLACEMENTS_NOT_CONFIGURED"]}
