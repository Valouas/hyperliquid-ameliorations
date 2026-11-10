"""Multi-book PAPER runner: many independent single-wallet books in ONE process.

* One shared websocket: l2Book + trades for every relevant coin, allMids, and
  userFills for at most 10 'realtime' wallets (Hyperliquid: 10 unique users).
* One rate-limited REST worker shared by all books (token bucket, backoff with
  jitter on 429/errors).
* Each book keeps its own folder (checkpoint.json, status.json,
  observations-*.jsonl) in the exact format of manual_copy_runner.Service, so
  existing checkpoints continue. Engine and observation semantics come from the
  deployed manual_copy_engine / manual_copy_runner (imported, not copied).
* 'realtime' book: observations applied when received, as before.
* 'delayed' book: no user subscription. Leader fills are read from the public
  trades feed (field users = [buyer, seller]) and from REST (clearinghouseState
  every 60 s; userFillsByTime when positions do not match, at start and after a
  websocket gap). The book runs on a virtual clock delayed_lag_seconds behind
  real time: each leader fill is injected at leader_time + copy_latency_ms and
  executed against the BUFFERED order book of that instant. Without buffered
  depth: real book received within late_book_seconds, else a synthetic book
  from the 1 s allMids mid (approx_mid) or the 1 m candle (approx_candle), both
  with an adverse extra slippage and flagged.
There is deliberately no signing key, exchange client or live execution path.
"""
import argparse
import asyncio
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, deque
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import heapq
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from manual_copy_engine import Engine, fill_order_key, load_policy, position_map, number, sign
from manual_copy_engine import HIP3_DEFAULTS, dex_of, hip3_options
from manual_copy_runner import apply_observation, fetch_wallet, merge_dex_states

LOG = logging.getLogger("manual_copy_multi")
ROOT = Path(__file__).resolve().parent
VARIANTS = ("post_only", "all_ioc")
PREFIX = re.compile(r"[0-9a-f]{4,8}")
DEFAULTS = {
    "policy_dir": "policies_multi",
    "state_dir": "hypertracker_state/multi",
    "stress_state_dir": "hypertracker_state/multi_stress",
    "shadow_state_dir": "hypertracker_state/multi_shadow",
    "market_store_dir": "hypertracker_state/multi_market",
    "runner_status": "hypertracker_state/multi2_runner_status.json",
    "default_feed": "delayed",
    "realtime": [],
    "max_realtime_users": 10,
    "books": {},
    "shadow_delayed": [],
    "copy_latency_ms": 1500,
    "stress_latency_ms": 3000,
    "delayed_lag_seconds": 75,
    "max_extra_hold_seconds": 120,
    "buffer_seconds": 300,
    "late_book_seconds": 3,
    "approx_extra_slippage_bps": 10,
    "approx_half_spread_major_bps": 1,
    "approx_half_spread_other_bps": 4,
    "rest_budget_weight_per_min": 700,
    "top_volume_coins": 40,
    "max_market_coins": 150,
    "seed_fills_hours": 48,
    "status_seconds": 3,
    "checkpoint_seconds": 15,
    "book_feed": "pending",
    "live_data_feed": True,
    "ws_send_per_second": 10,
    # HIP-3 (builder-deployed perps): runtime options per book, NEVER in policies_multi/*.json.
    # "hip3_defaults" applies to every book, "books": {"<prefix>": {"allow_hip3": true, ...}} overrides it.
    "hip3_defaults": {},
    # Engine runtime options valid for EVERY book (equity guard, stop reaction delay, liquidation model, cf_log).
    # Merge order: engine_defaults < hip3_defaults < books.<prefix>. Same key set as hip3_defaults.
    "engine_defaults": {},
    # 1a: zero-delay "ghost" unit per delayed main book (fills at the leader's exact fill price/time).
    # Pure computation on leader fills already received: no extra REST. books.<prefix>.ghost=false disables one.
    "ghosts": True,
    "ghost_state_dir": "hypertracker_state/multi_ghost",
    # 1c: placebo books = policies in placebo_policy_dir (wallets that pass the scanner filters but were NOT chosen).
    "placebos": True,
    "placebo_policy_dir": "policies_placebo",
    "placebo_state_dir": "hypertracker_state/multi_placebo",
    "max_placebos": 5,
    # 3/1b: counterfactual log, leader entries, consensus, feed-lag log.
    "cf_dir": "hypertracker_state/multi_cf",
    "consensus_window_hours": 6,
    "feed_lag_log": True,
    # 2026-11-10 correlation caps across books of the same role (null = off). A NEW entry is refused while that many
    # OTHER books already hold the same coin in the same direction / while their summed notional reaches the cap.
    "max_books_same_coin_side": None,
    "max_portfolio_coin_side_notional": None,
}
# books.<prefix>.mode: "live" (default) | "probation" (new wallet: only its ghost trades until promoted)
# | "retired" (leader no longer passes the selection: exits only). Not live => the main book opens nothing.
MODES = ("live", "probation", "retired")
HIP3_BOOK_KEYS = set(HIP3_DEFAULTS) - {"hip3_exit_only"}
BOOK_KEYS = {"enabled", "feed", "copy_latency_ms", "stress_latency_ms", "ghost", "mode"} | HIP3_BOOK_KEYS
W2 = {"l2Book", "allMids", "clearinghouseState", "orderStatus", "spotClearinghouseState", "exchangeStatus"}
PER20 = {"recentTrades", "historicalOrders", "userFills", "userFillsByTime", "fundingHistory", "userFunding",
         "nonUserFundingUpdates", "twapHistory", "userTwapSliceFills", "userTwapSliceFillsByTime",
         "delegatorHistory", "delegatorRewards", "validatorStats"}


def perp(coin):
    return bool(coin) and ":" not in coin and "/" not in coin and not coin.startswith(("@", "+"))


def path_of(value):
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_config(path):
    config = dict(DEFAULTS)
    if path and Path(path).exists():
        config.update(json.loads(Path(path).read_text(encoding="utf-8")))
    if config["default_feed"] not in ("realtime", "delayed") or config["book_feed"] not in ("pending", "needed"):
        raise ValueError("Invalid feed option")
    if not 0 < config["max_realtime_users"] <= 10:
        raise ValueError("max_realtime_users must be 1..10 (Hyperliquid: 10 unique users per IP)")
    if config["delayed_lag_seconds"] + config["max_extra_hold_seconds"] + 30 > config["buffer_seconds"]:
        raise ValueError("buffer_seconds must exceed delayed_lag_seconds + max_extra_hold_seconds + 30")
    if not 0 < config["rest_budget_weight_per_min"] <= 1000:
        raise ValueError("rest_budget_weight_per_min must stay well below 1200")
    if set(config["engine_defaults"]) - HIP3_BOOK_KEYS:
        raise ValueError("Unknown engine_defaults key(s): %s" % sorted(set(config["engine_defaults"]) - HIP3_BOOK_KEYS))
    if not 0 < config["consensus_window_hours"] <= 72 or not 0 <= config["max_placebos"] <= 20:
        raise ValueError("consensus_window_hours must be 0..72 and max_placebos 0..20")
    if set(config["hip3_defaults"]) - HIP3_BOOK_KEYS:
        raise ValueError("Unknown hip3_defaults key(s): %s" % sorted(set(config["hip3_defaults"]) - HIP3_BOOK_KEYS))
    for key in ("max_books_same_coin_side", "max_portfolio_coin_side_notional"):
        value = config[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
            raise ValueError(key + " must be null or a number > 0")
    for prefix, own in config["books"].items():
        if set(own) - BOOK_KEYS:
            raise ValueError("books.%s: unknown key(s) %s" % (prefix, sorted(set(own) - BOOK_KEYS)))
        if own.get("mode", "live") not in MODES:
            raise ValueError("books.%s: mode must be one of %s" % (prefix, ", ".join(MODES)))
        hip3_book_options(config, prefix)  # validates values
    return config


def hip3_book_options(config, prefix):
    """Engine runtime options of one book (not hashed into the policy identity)."""
    own = config["books"].get(prefix, {})
    options = {k: v for k, v in config["engine_defaults"].items()}
    options.update(config["hip3_defaults"])
    options.update({k: v for k, v in own.items() if k in HIP3_BOOK_KEYS})
    return hip3_options(options)


def check_mode(prefix, own, feed, ghosts):
    """Probation = only the zero-delay ghost trades, so the book needs one (delayed feed, ghosts on)."""
    if own.get("mode", "live") == "probation" and (feed != "delayed" or not ghosts or own.get("ghost", True) is False):
        raise ValueError(prefix + ": mode probation requires a delayed feed with its ghost enabled")


def unit_options(config, prefix, role):
    """Engine runtime options of one unit: book options, then role rules (placebo, cf_log, paused entries)."""
    options = hip3_book_options(config, prefix)
    if role == "placebo":
        options["allow_hip3"] = False       # placebos trade main-dex perps only (cheaper, no HIP-3 baseline)
    if role != "main":
        options["cf_log"] = False           # counterfactual / leader-entry log: one source per address (main book)
    if config["books"].get(prefix, {}).get("mode", "live") != "live" and role in ("main", "stress", "shadow"):
        options["entries_paused"] = True    # probation / retired: the ghost keeps measuring, the book opens nothing
    return options


def crowding_reason(units, unit, variant, row, max_books, max_notional):
    """Correlation cap: reason to refuse a NEW entry of `unit` on row[coin], or None. Counts OTHER units of the same role."""
    if max_books is None and max_notional is None:
        return None
    direction, coin = sign(row["target"]), row["coin"]
    books, notional = 0, 0.0
    for other in units:
        if other is unit or other.role != unit.role:
            continue
        engine = other.engines[variant]
        held = engine.state["positions"].get(other.address + ":" + coin)
        if not held or not held["qty"] or sign(held["qty"]) != direction:
            continue
        books += 1
        notional += abs(held["qty"]) * engine.marks.get(coin, (held["entry"], 0))[0]
    if max_books is not None and books >= max_books:
        return "portfolio_same_side_books_cap"
    if max_notional is not None and notional >= max_notional:
        return "portfolio_coin_side_notional_cap"
    return None


def hip3_rows(state):
    return any(dex_of(r["coin"]) not in ("", None) and (r["qty"] or r["target"]) for r in state["positions"].values())


def book_plan(config):
    """Every unit to host: (prefix, role, policy_path, folder, feed, latency_ms)."""
    plan, addresses = [], {}
    for file in sorted(path_of(config["policy_dir"]).glob("policy_*.json")):
        prefix = file.stem[len("policy_"):]
        if not PREFIX.fullmatch(prefix):
            raise ValueError("Unexpected policy file name " + file.name)
        policy = load_policy(file)
        if len(policy["wallets"]) != 1:
            raise ValueError(file.name + ": exactly one wallet per book")
        address = policy["wallets"][0]["address"]
        if address[2:2+len(prefix)] != prefix:
            raise ValueError(file.name + ": prefix does not match the address")
        if address in addresses:
            raise ValueError(file.name + ": wallet already followed by " + addresses[address])
        addresses[address] = prefix
        own = config["books"].get(prefix, {})
        if own.get("enabled") is False:
            continue
        feed = own.get("feed") or ("realtime" if prefix in config["realtime"] else config["default_feed"])
        if feed not in ("realtime", "delayed"):
            raise ValueError(prefix + ": feed must be realtime or delayed")
        check_mode(prefix, own, feed, config["ghosts"])
        latency = int(own.get("copy_latency_ms", config["copy_latency_ms"]))
        plan.append((prefix, "main", file, path_of(config["state_dir"]) / prefix, feed, latency))
        stress = int(own.get("stress_latency_ms", config["stress_latency_ms"]))
        if feed == "delayed" and stress > latency:
            plan.append((prefix, "stress", file, path_of(config["stress_state_dir"]) / prefix, "delayed", stress))
        if feed == "realtime" and prefix in config["shadow_delayed"]:
            plan.append((prefix, "shadow", file, path_of(config["shadow_state_dir"]) / prefix, "delayed", latency))
        if feed == "delayed" and config["ghosts"] and own.get("ghost", True):
            plan.append((prefix, "ghost", file, path_of(config["ghost_state_dir"]) / prefix, "delayed", 0))
    if config["placebos"] and config["max_placebos"]:
        files = sorted(path_of(config["placebo_policy_dir"]).glob("policy_*.json"))[:config["max_placebos"]]
        prefixes = {x[0] for x in plan}
        for file in files:
            prefix = file.stem[len("policy_"):]
            policy = load_policy(file)
            address = policy["wallets"][0]["address"]
            if (not PREFIX.fullmatch(prefix) or len(policy["wallets"]) != 1 or address[2:2+len(prefix)] != prefix
                    or address in addresses or prefix in prefixes):
                raise ValueError(file.name + ": invalid placebo (prefix, one wallet, or already followed)")
            addresses[address] = "placebo:" + prefix
            prefixes.add(prefix)
            plan.append((prefix, "placebo", file, path_of(config["placebo_state_dir"]) / prefix, "delayed",
                         int(config["copy_latency_ms"])))
    realtime = {p for p, role, _, _, feed, _ in plan if role == "main" and feed == "realtime"}
    if len(realtime) > config["max_realtime_users"]:
        raise ValueError("%d realtime books > max_realtime_users %d" % (len(realtime), config["max_realtime_users"]))
    return plan


class RestClient:
    """Serialized public /info client with a weight token bucket and backoff."""
    def __init__(self, budget_per_min):
        # 5 % margin for the per-item weight charged after the response.
        self.rate = budget_per_min * 0.85 / 60.0
        self.capacity = max(40.0, budget_per_min / 20.0)
        self.tokens = self.capacity / 2
        self.updated = time.monotonic()
        self.pause_until = 0.0
        self.failures = 0
        self.lock = threading.Lock()
        self.log = deque()
        self.calls, self.errors = Counter(), Counter()
        self.total_weight, self.started = 0, time.time()
        self.rate_limited = 0

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now-self.updated)*self.rate)
        self.updated = now

    def request(self, kind, **kwargs):
        base = 2 if kind in W2 else 60 if kind == "userRole" else 20
        with self.lock:
            while True:
                self._refill()
                wait = max(self.pause_until-time.monotonic(), (base-self.tokens)/self.rate if self.tokens < base else 0)
                if wait <= 0:
                    break
                time.sleep(min(wait, 5))
            self.tokens -= base
            body = json.dumps(dict(type=kind, **kwargs)).encode()
            request = Request("https://api.hyperliquid.xyz/info", data=body,
                              headers={"Content-Type": "application/json"}, method="POST")
            self.calls[kind] += 1
            try:
                with urlopen(request, timeout=10) as response:
                    result = json.load(response)
            except HTTPError as exc:
                self.failures += 1
                self.errors["http_%d" % exc.code] += 1
                if exc.code == 429:
                    self.rate_limited += 1
                    self.tokens = min(self.tokens, 0)
                delay = min(120, 2*2**min(self.failures, 6)) if exc.code == 429 else min(30, 2**min(self.failures, 5))
                self.pause_until = time.monotonic() + delay*(0.6+0.8*random.random())
                self._account(base)
                raise
            except Exception as exc:
                self.failures += 1
                self.errors[type(exc).__name__] += 1
                self.pause_until = time.monotonic() + min(30, 2**min(self.failures, 5))*(0.5+random.random())
                self._account(base)
                raise
            self.failures = 0
            extra = 0
            if isinstance(result, list):
                extra = len(result)//20 if kind in PER20 else len(result)//60 if kind == "candleSnapshot" else 0
            self.tokens -= extra
            self._account(base+extra)
            return result

    def _account(self, weight):
        now = time.time()
        self.total_weight += weight
        self.log.append((now, weight))
        while self.log and self.log[0][0] < now-60:
            self.log.popleft()

    def weight_last_minute(self):
        now = time.time()
        return sum(w for t, w in self.log if t >= now-60)


class MarketStore:
    """Content-addressed compact metaAndAssetCtxs shared by all journals (journal keeps a ref)."""
    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}

    @staticmethod
    def compact(data):
        meta, contexts = data
        keys = ("name", "szDecimals", "maxLeverage", "isDelisted", "onlyIsolated", "marginTableId")
        # HIP-3 assets also keep the inputs of their fee rate and margin mode (main-dex refs unchanged).
        extra = ("marginMode", "growthMode", "deployerFeeScale")
        universe = [{k: m[k] for k in keys + (extra if ":" in m["name"] else ()) if k in m} for m in meta["universe"]]
        return [{"universe": universe}, [{"dayNtlVlm": c["dayNtlVlm"], "markPx": c["markPx"]} for c in contexts]]

    def put(self, data, at):
        compact = self.compact(data)
        text = json.dumps(compact, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(text.encode()).hexdigest()[:24]
        ref = datetime.fromtimestamp(at, timezone.utc).strftime("%Y%m%d") + "/" + digest
        path = self.root / (ref + ".json")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp%d" % os.getpid())
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
        self.remember(ref, compact)
        return {"ref": ref}

    def remember(self, ref, compact):
        # Main dex + a few HIP-3 dex snapshots alternate: keep the last 16 refs.
        self.cache.pop(ref, None)
        self.cache[ref] = compact
        while len(self.cache) > 16:
            self.cache.pop(next(iter(self.cache)))

    def get(self, ref):
        if ref not in self.cache:
            self.remember(ref, json.loads((self.root / (ref + ".json")).read_text(encoding="utf-8")))
        return self.cache[ref]


class MarketBuffer:
    """Rolling timestamped market data, indexed by RECEPTION time (what a copier knew)."""
    def __init__(self, seconds):
        self.seconds = seconds
        self.books, self.trades = {}, {}
        self.mid_times, self.mid_values = [], []

    def add_book(self, data, recv):
        coin = data["coin"]
        try:
            bids = array("d", [v for x in data["levels"][0][:20] for v in (float(x["px"]), float(x["sz"]))])
            asks = array("d", [v for x in data["levels"][1][:20] for v in (float(x["px"]), float(x["sz"]))])
            exchange = int(data["time"])
        except (KeyError, IndexError, TypeError, ValueError):
            return
        rows = self.books.setdefault(coin, ([], [], []))
        if rows[0] and recv < rows[0][-1]:
            recv = rows[0][-1]
        rows[0].append(recv)
        rows[1].append(exchange)
        rows[2].append((bids, asks))

    def add_trade(self, trade, recv):
        rows = self.trades.setdefault(trade["coin"], ([], []))
        if rows[0] and recv < rows[0][-1]:
            recv = rows[0][-1]
        rows[0].append(recv)
        rows[1].append({k: trade[k] for k in ("coin", "side", "px", "sz", "time", "tid") if k in trade})

    def add_mids(self, mids, recv):
        if self.mid_times and recv-self.mid_times[-1] < 1:
            self.mid_values[-1].update(mids)
            return
        self.mid_times.append(recv)
        self.mid_values.append(dict(self.mid_values[-1]) if self.mid_values else {})
        self.mid_values[-1].update(mids)

    def book_index(self, coin, at):
        rows = self.books.get(coin)
        if not rows:
            return None, -1
        return rows, bisect_right(rows[0], at)-1

    def book_at(self, coin, at):
        rows, i = self.book_index(coin, at)
        return None if i < 0 else (rows[0][i], rows[1][i], rows[2][i])

    def book_after(self, coin, at, until):
        rows, i = self.book_index(coin, at)
        if rows is None or i+1 >= len(rows[0]) or rows[0][i+1] > until:
            return None
        return rows[0][i+1], rows[1][i+1], rows[2][i+1]

    @staticmethod
    def engine_book(coin, entry):
        _, exchange, (bids, asks) = entry
        side = lambda a: [{"px": a[i], "sz": a[i+1]} for i in range(0, len(a), 2)]
        return {"coin": coin, "time": exchange, "levels": [side(bids), side(asks)]}

    def trades_between(self, coin, start, end):
        rows = self.trades.get(coin)
        if not rows:
            return []
        return rows[1][bisect_right(rows[0], start):bisect_right(rows[0], end)]

    def mids_at(self, at):
        i = bisect_right(self.mid_times, at)-1
        return (self.mid_times[i], self.mid_values[i]) if i >= 0 else (None, {})

    def prune(self, now):
        limit = now-self.seconds
        for rows in list(self.books.values()) + list(self.trades.values()):
            k = bisect_left(rows[0], limit)
            if k:
                for column in rows:
                    del column[:k]
        k = bisect_left(self.mid_times, limit)
        if k:
            del self.mid_times[:k]
            del self.mid_values[:k]


class Unit:
    """One independent paper book = one folder, same format as manual_copy_runner.Service."""
    def __init__(self, runner, prefix, role, policy, folder, feed, latency_ms):
        self.runner, self.prefix, self.role, self.policy = runner, prefix, role, policy
        self.folder, self.feed, self.latency = Path(folder), feed, latency_ms/1000.0
        self.address = policy["wallets"][0]["address"]
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "checkpoint.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        options = unit_options(runner.config, prefix, role)
        if not options["allow_hip3"] and any(hip3_rows(saved[n]) for n in VARIANTS if saved.get(n)):
            # HIP-3 switched off while copies are open: keep following exits only, no new HIP-3 entry.
            options["hip3_exit_only"] = True
        self.hip3_dexes = tuple(options["allowed_dexes"]) if options["allow_hip3"] or options["hip3_exit_only"] else ()
        self.hip3 = options
        self.engines = {name: Engine(policy, saved.get(name), variant=name, options=options) for name in VARIANTS}
        self.journal_sequence = int(saved.get("sequence", 0))
        self.clock = max([e.state["last_at"] or 0 for e in self.engines.values()] + [0]) or None
        # Replay observations written after the last checkpoint (same rule as Service).
        recovered = self.journal_sequence
        for journal in sorted(self.folder.glob("observations-*.jsonl")):
            with journal.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    # An unclean shutdown (power loss / reboot) leaves a NUL-filled or torn last line (seen 01.11 14:09).
                    line = line.replace("\x00", "").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        import logging; logging.warning("%s: skipping unreadable journal line in %s (torn write); a real gap is still detected below", self.folder, journal.name)
                        continue
                    if event["sequence"] <= self.journal_sequence:
                        continue
                    if event["sequence"] != recovered+1:
                        raise ValueError(str(self.folder) + ": observation journal gap; recovery requires inspection")
                    self.apply(event["kind"], event["data"], event["at"])
                    recovered = event["sequence"]
                    self.clock = event["at"]
        self.journal_sequence = recovered
        # Correlation cap, installed only AFTER the journal replay: during the replay the other books are not all
        # loaded yet, so a guard would answer differently from one start to the next. Replay = no cap (few seconds).
        if role in ("main", "stress"):
            for name, engine in self.engines.items():
                engine.entry_guard = lambda row, now, name=name: runner.crowding(self, name, row)
        for engine in self.engines.values():
            engine.cf_out = []                  # replayed history was already logged before the restart
        self.ghost_px, self.liq_logged = {}, set()
        self.queue, self.queue_seq = [], 0
        self.hold = None
        self.last_mids, self.last_book, self.book_src, self.prev_pending = -1e18, {}, {}, set()
        self.trade_cursor, self.signal_meta, self.funding_sent = {}, {}, set()
        self.fill_seen = {n: e.state.get("fill_count", 0) for n, e in self.engines.items()}
        self.counts = Counter()
        self.recent = deque(maxlen=20)
        self.checkpoint_at = self.status_at = 0
        self.errors = {}

    @property
    def primary(self):
        return self.engines["post_only"]

    # ----- journal (identical format to Service) -----
    def record(self, kind, data, now):
        self.journal_sequence += 1
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
        line = json.dumps({"sequence": self.journal_sequence, "kind": kind, "at": now, "data": data}, allow_nan=False)
        with (self.folder / ("observations-"+stamp+".jsonl")).open("a", encoding="utf-8") as handle:
            handle.write(line+"\n")

    def apply(self, kind, data, now):
        if kind == "market" and isinstance(data, dict):
            data = self.runner.store.get(data["ref"])
        for engine in self.engines.values():
            apply_observation(engine, kind, data, now)

    def observe(self, kind, data, now):
        self.record(kind, data, now)
        self.apply(kind, data, now)

    def save(self, real_now):
        from reliable_io import atomic_json
        now = self.clock or real_now
        if real_now-self.checkpoint_at >= self.runner.config["checkpoint_seconds"] or self.runner.stopping:
            atomic_json(self.folder / "checkpoint.json", dict(
                ((name, engine.state) for name, engine in self.engines.items()), sequence=self.journal_sequence), backup=True)
            self.checkpoint_at = real_now
        reports = {name: engine.report(now) for name, engine in self.engines.items()}
        connected = self.runner.connected
        status = ("OBSERVING_PAPER" if connected else "WAITING_WEBSOCKET") + ("_DELAYED" if self.feed == "delayed" else "")
        errors = dict(self.errors)
        errors.update({k: v for k, v in self.runner.errors.items() if k in ("websocket", "markets") or k.startswith("funding:")})
        if self.address in self.runner.errors:
            errors[self.address] = self.runner.errors[self.address]
        approx = self.counts["paper_approx_mid"] + self.counts["paper_approx_candle"]
        atomic_json(self.folder / "status.json", {
            "status": status, "at": datetime.fromtimestamp(real_now, timezone.utc).isoformat(), "pid": os.getpid(),
            "live_orders_enabled": False, "websocket_connected": connected,
            "last_message_at": self.runner.last_message_at, "errors": errors, "books": reports,
            "comparison": {"post_only_minus_ioc_usd": reports["post_only"]["pnl_net_usd"]-reports["all_ioc"]["pnl_net_usd"],
                           "maker_model": "observed_trade_strictly_through_price_no_queue_position_guarantee",
                           "historical_30d_claim_verified": False},
            "feed": {"type": self.feed, "role": self.role, "runner": "manual_copy_multi_runner",
                     "copy_latency_ms": round(self.latency*1000), "virtual_at": now,
                     "lag_seconds": real_now-now if self.feed == "delayed" else 0,
                     "counts": dict(self.counts), "approx_fills": approx,
                     "leader_fills_late": self.counts["signal_late"], "recent": list(self.recent)},
        })
        self.status_at = real_now

    # ----- market views fed before each tick -----
    def needed_coins(self):
        return {r["coin"] for e in self.engines.values() for r in e.state["positions"].values()
                if (r["qty"] or r["target"]) and r["coin"] in e.markets}

    def pending_coins(self):
        if self.runner.config["book_feed"] == "needed":
            return self.needed_coins()
        return {r["coin"] for e in self.engines.values() for r in e.state["positions"].values()
                if abs(r["target"]-r["qty"]) > 1e-12 and r["coin"] in e.markets}

    def maker_coins(self):
        return {r["coin"] for e in self.engines.values() for r in e.state["positions"].values() if r["maker"]}

    def direction(self, coin):
        for e in (self.engines["all_ioc"], self.primary):
            r = e.state["positions"].get(self.address+":"+coin)
            if r and abs(r["target"]-r["qty"]) > 1e-12:
                return sign(r["target"]-r["qty"])
        return 0

    def feed_views(self, v, real):
        """Feed mids (1 s), depth for coins with a pending order, public trades for resting maker orders."""
        buffer = self.runner.buffer
        needed = self.needed_coins()
        if needed and v-self.last_mids >= 1:
            _, mids = buffer.mids_at(v)
            selected = {c: mids[c] for c in sorted(needed) if c in mids}
            if self.hip3_dexes and any(":" in c for c in needed):
                _, extra = self.runner.hip3_mids.mids_at(v)
                selected.update({c: extra[c] for c in sorted(needed) if ":" in c and c in extra})
                selected = dict(sorted(selected.items()))
            if selected:
                self.observe("mids", {"mids": selected}, v)
            self.last_mids = v
        pending = self.pending_coins()
        max_age = self.policy["max_book_age_seconds"]
        for coin in sorted(pending):
            if self.role == "ghost":
                ghost = self.ghost_px.get(coin)
                if ghost and 0 <= v-ghost[1] <= 5:
                    if not ghost[2]:
                        self.ghost_book(coin, ghost, v)
                        ghost[2] = True
                    continue
            entry = buffer.book_at(coin, v)
            fresh = entry is not None and -5 <= v-entry[1]/1000 <= max_age
            last = self.last_book.get(coin, (0, -1e18))
            if fresh and entry[1] > last[0] and (coin not in self.prev_pending or v-last[1] >= 1):
                self.observe("book", MarketBuffer.engine_book(coin, entry), v)
                self.last_book[coin] = (entry[1], v)
                late = self.signal_meta.get(coin, {}).get("late_book")
                self.book_src[coin] = "live" if self.feed == "realtime" else "buffer_late" if late else "buffer"
            elif not fresh and self.feed == "delayed" and v-last[1] > max_age:
                if self.approximate(coin, v, real) == "hold":
                    return False
        self.prev_pending = pending
        for coin in sorted(self.maker_coins()):
            if self.feed == "delayed":
                for trade in buffer.trades_between(coin, self.trade_cursor.get(coin, v-1), v):
                    self.observe("trade", trade, v)
            self.trade_cursor[coin] = v
        return True

    def ghost_book(self, coin, ghost, v):
        """Zero-delay ghost: a book whose both sides are the leader's own fill price (unlimited size)."""
        px = ghost[0]
        stamp = int(v*1000)
        for engine in self.engines.values():
            previous = engine.books.get(coin)
            if previous:
                stamp = max(stamp, int(previous["exchange_at"]*1000)+1)
        self.observe("book", {"coin": coin, "time": stamp, "approx": "ghost",
                              "levels": [[{"px": px, "sz": 1e12/px}], [{"px": px, "sz": 1e12/px}]]}, v)
        self.last_book[coin] = (stamp, v)
        self.book_src[coin] = "ghost"
        self.counts["ghost_leader_px"] += 1

    def approximate(self, coin, v, real):
        """No buffered depth for a pending delayed order at v: choose the price source."""
        cfg, buffer = self.runner.config, self.runner.buffer
        meta = self.signal_meta.setdefault(coin, {})
        if buffer.book_after(coin, v, v+cfg["late_book_seconds"]):
            if not meta.get("late_book"):
                self.counts["late_book_wait"] += 1
            meta["late_book"] = True
            return "wait"
        direction = self.direction(coin)
        if not direction:
            return "none"
        at, mids = (self.runner.hip3_mids if ":" in coin else buffer).mids_at(v)
        if coin in mids and at is not None and v-at <= 3:
            price, source = number(mids[coin]), "approx_mid"
        else:
            minute = int(v//60)*60
            candle = self.runner.candles.get((coin, minute))
            if candle is None:
                self.runner.want_candle(coin, minute)
                if self.hold is None:
                    self.hold = {"v": v, "coin": coin, "deadline": real+90}
                    self.counts["holds"] += 1
                if real < self.hold["deadline"]:
                    return "hold"
                self.hold = None
                self.counts["approx_failed"] += 1
                return "none"
            self.hold = None
            if candle is False:
                self.counts["approx_failed"] += 1
                return "none"
            o, c = candle
            price, source = o+(c-o)*min(1, max(0, (v-minute)/60)), "approx_candle"
        major = coin in self.policy["major_coins"]
        half = cfg["approx_half_spread_major_bps" if major else "approx_half_spread_other_bps"]/10000
        mid = price*(1+direction*cfg["approx_extra_slippage_bps"]/10000)
        size = 1e6/price
        self.observe("book", {"coin": coin, "time": int(v*1000), "approx": source,
                              "levels": [[{"px": mid*(1-half), "sz": size}], [{"px": mid*(1+half), "sz": size}]]}, v)
        self.last_book[coin] = (int(v*1000), v)
        self.book_src[coin] = source
        self.counts["synthetic_" + source] += 1
        return "ok"

    def after_tick(self, v):
        for name, engine in self.engines.items():
            count = engine.state.get("fill_count", 0)
            new = count-self.fill_seen[name]
            self.fill_seen[name] = count
            if new <= 0:
                continue
            fills = []
            for event in reversed(engine.state["events"]):
                if len(fills) >= new:
                    break
                if event["kind"] == "paper_fill":
                    fills.append(event)
            for event in reversed(fills):
                coin = event["coin"]
                row = engine.state["positions"].get(event["address"]+":"+coin, {})
                if event["tif"] != "Ioc":
                    src = "maker_trade"
                else:
                    src = self.book_src.get(coin, "live" if self.feed == "realtime" else "buffer")
                meta = self.signal_meta.get(coin, {})
                entry = {"type": "paper_fill", "variant": name, "coin": coin, "at": v, "qty": event["quantity"],
                         "price": event["price"], "reduce_only": event["reduce_only"], "src": src,
                         "signal_at": row.get("signal_at"), "reference": row.get("reference"),
                         "signal_source": meta.get("source"), "detected_at": meta.get("detected_at"),
                         "latency_ms": round(self.latency*1000)}
                _, mids = (self.runner.hip3_mids if ":" in coin else self.runner.buffer).mids_at(v)
                entry["mid"] = number(mids[coin]) if coin in mids else None
                if not event["reduce_only"] and row.get("reference"):
                    entry["bps_vs_leader"] = sign(event["quantity"])*(event["price"]/row["reference"]-1)*10000
                    if meta.get("detected_at"):
                        _, rt_mids = (self.runner.hip3_mids if ":" in coin else self.runner.buffer).mids_at(meta["detected_at"]+0.5)
                        if coin in rt_mids:
                            entry["mid_rt_path"] = number(rt_mids[coin])
                entry["fee"] = event.get("fee")
                entry["cause"] = event.get("cause")
                if event["reduce_only"]:
                    ref = row.get("exit_ref")
                    if ref:
                        # >0 = worse than the leader's own exit price (we sell lower / buy higher)
                        entry["exit_bps_vs_leader"] = sign(event["quantity"])*(event["price"]/ref-1)*10000
                        entry["exit_ref_age_s"] = v-(row.get("exit_ref_at") or v)
                elif row.get("signal_at"):
                    entry["consensus"] = self.runner.consensus(self.address, coin, sign(event["quantity"]), row["signal_at"])
                if name == "all_ioc":
                    self.counts["paper_" + src] += 1
                    self.recent.append({k: entry[k] for k in ("coin", "at", "qty", "price", "src", "reduce_only")})
                self.runner.exec_log(self, entry)
        for name, engine in self.engines.items():
            for event in reversed(engine.state["events"][-10:]):
                key = (name, event["at"], event.get("coin"))
                if event["kind"] == "paper_liquidation" and key not in self.liq_logged:
                    self.liq_logged.add(key)
                    self.runner.exec_log(self, dict(event, type="paper_liquidation", variant=name))

    # ----- realtime -----
    def step_realtime(self, now):
        self.clock = now
        self.feed_views(now, now)
        self.observe("tick", {}, now)
        self.after_tick(now)

    # ----- delayed -----
    def enqueue(self, at, kind, data, meta=None):
        meta = dict(meta or {})
        meta["late"] = self.clock is not None and at <= self.clock
        if meta["late"]:
            at = self.clock
        self.queue_seq += 1
        heapq.heappush(self.queue, (at, self.queue_seq, kind, data, meta))
        if kind == "fill":
            self.counts["signal_" + meta.get("source", "?")] += 1
            if meta["late"]:
                self.counts["signal_late"] += 1

    def apply_event(self, v, kind, data, meta):
        if kind != "fill":
            self.observe(kind, data, v)
            return
        fill = data["fill"]
        if "startPosition" not in fill:
            # Trades-feed fill: the leader position before it is our tracked source.
            row = self.primary.state["positions"].get(self.address+":"+fill["coin"])
            fill = dict(fill, startPosition=repr(float(row["source"] if row else 0.0)))
            data = {"address": data["address"], "fill": fill}
        if self.role == "ghost":
            self.ghost_px[fill["coin"]] = [number(fill["px"]), v, False]
        self.observe("fill", data, v)
        self.signal_meta[fill["coin"]] = {"source": meta.get("source"), "detected_at": meta.get("detected_at"),
                                         "late": meta.get("late")}
        if self.role in ("main", "shadow"):
            self.log_signal(v, fill, meta)

    def log_signal(self, v, fill, meta):
        """Accuracy record: executable top of book at t+latency vs the realtime path and the leader price."""
        buffer, coin = self.runner.buffer, fill["coin"]
        side = 1 if fill["side"] == "B" else -1
        def top(at):
            entry = buffer.book_at(coin, at)
            if entry is None or not -5 <= at-entry[1]/1000 <= self.policy["max_book_age_seconds"]:
                return None
            levels = entry[2][0 if side < 0 else 1]
            return levels[0] if levels else None
        leader = number(fill["px"])
        record = {"type": "signal", "coin": coin, "side": fill["side"], "leader_time": number(fill["time"])/1000,
                  "leader_px": leader, "at": v, "source": meta.get("source"), "late": meta.get("late"),
                  "detected_at": meta.get("detected_at"), "book_at_t_plus_latency": top(v)}
        if meta.get("detected_at"):
            record["book_at_detection_plus_0_5s"] = top(meta["detected_at"]+0.5)
        self.runner.exec_log(self, record)

    def advance(self, real):
        cfg = self.runner.config
        lag = cfg["delayed_lag_seconds"]
        feed = self.runner.feeds[self.address]
        limit = real-lag
        if feed.watermark is None:
            limit = min(limit, self.clock)
        else:
            limit = min(limit, feed.watermark + self.latency + self.policy["reconcile_seconds"] + 15)
        floor = real-lag-cfg["max_extra_hold_seconds"]
        if feed.watermark is None and real-self.runner.started < 300:
            floor = -math.inf  # start-up: wait for the first reconciliation (REST queue is busiest then)
        if limit < floor:
            if not self.errors.get("feed"):
                LOG.warning("%s %s: leader feed stalled, virtual clock released", self.prefix, self.role)
            self.errors["feed"] = "Flux leader en retard: horloge virtuelle libérée (signaux tardifs possibles)"
            limit = floor
        elif feed.watermark is not None:
            self.errors.pop("feed", None)
        for _ in range(2000):
            if self.hold:
                v = self.hold["v"]
            else:
                grid = (math.floor(self.clock*2)+1)/2
                v = min(grid, self.queue[0][0]) if self.queue else grid
                if v > limit:
                    break
                while self.queue and self.queue[0][0] <= v:
                    _, _, kind, data, meta = heapq.heappop(self.queue)
                    self.apply_event(v, kind, data, meta)
                self.clock = v
            if not self.feed_views(v, real):
                break
            self.observe("tick", {}, v)
            self.after_tick(v)

    def deliver_wallet(self, data, unknown):
        """REST snapshot (+ fills the trades feed did not show) for a delayed book."""
        position_at = data["position_at"]
        if "portfolio" in data:
            try:
                points = dict(data["portfolio"])["day"]["accountValueHistory"]
                position_at = max(position_at, max(number(p[0]) for p in points)/1000)
            except (KeyError, TypeError, ValueError):
                pass
        at = position_at + self.latency
        late = []
        for fill in unknown:
            fat = number(fill["time"])/1000 + self.latency
            if self.clock is not None and fat > self.clock:
                self.enqueue(fat, "fill", {"address": self.address, "fill": fill}, {"source": "rest", "detected_at": time.time()})
            else:
                late.append(fill)
                self.counts["signal_rest"] += 1
                self.counts["signal_late"] += 1
        payload = {k: v for k, v in data.items() if k != "fills"}
        if late:
            payload["fills"] = late
        self.enqueue(at, "wallet", payload)


class AddressFeed:
    """Per leader address: REST cadence, fill deduplication and the position tracker of delayed books."""
    def __init__(self, address):
        self.address = address
        self.rt_units, self.dl_units = [], []
        self.seen = {}
        self.snapshot, self.snapshot_at, self.watermark = None, None, None
        self.recent = []
        self.coins = set()
        self.urgent_at = None
        self.pending = None
        self.need_full_fetch = True
        self.last_portfolio = self.last_abstraction = 0
        self.account_mode = None
        self.hip3_dexes = ()

    def tracked(self, coin):
        """Coins whose fills/positions this leader feed follows: main perps, plus enabled HIP-3 dexes."""
        return perp(coin) or (bool(self.hip3_dexes) and dex_of(coin) in self.hip3_dexes)

    def remember(self, fill):
        key = (str(fill.get("tid")), fill["side"])
        if key in self.seen:
            return False
        self.seen[key] = number(fill["time"])/1000
        return True

    def prune(self, now):
        if len(self.seen) > 5000:
            self.seen = {k: t for k, t in self.seen.items() if now-t < 172800}

    def expected(self, until_ms):
        result = dict(self.snapshot or {})
        for t, coin, delta in self.recent:
            if t <= until_ms:
                result[coin] = result.get(coin, 0) + delta
        return result


class Runner:
    def __init__(self, config, plan):
        self.config = config
        self.rest = RestClient(config["rest_budget_weight_per_min"])
        self.store = MarketStore(path_of(config["market_store_dir"]))
        self.buffer = MarketBuffer(config["buffer_seconds"])
        self.stopping = False
        self.connected, self.last_message_at = False, 0
        self.errors = {}
        self.units, self.feeds = [], {}
        self.cf_handles, self.entries = {}, {}
        self.lag_log = deque()
        self.lag_counts = Counter()
        for prefix, role, file, folder, feed, latency in plan:
            unit = Unit(self, prefix, role, load_policy(file), folder, feed, latency)
            self.units.append(unit)
            f = self.feeds.setdefault(unit.address, AddressFeed(unit.address))
            (f.rt_units if feed == "realtime" else f.dl_units).append(unit)
            f.hip3_dexes = tuple(sorted(set(f.hip3_dexes) | set(unit.hip3_dexes)))
            for engine in unit.engines.values():
                for identity in engine.state["seen"]:
                    if identity.startswith("0x"):
                        try:
                            tid, ftime, coin, side = json.loads(identity.split(":", 1)[1])[:4]
                            f.seen[(str(tid), side)] = number(ftime)/1000
                        except (ValueError, TypeError):
                            pass
        self.realtime_users = sorted(a for a, f in self.feeds.items() if f.rt_units)
        if len(self.realtime_users) > min(10, config["max_realtime_users"]):
            raise ValueError("Too many realtime wallets")
        self.jobs = {}
        self.candles, self.candle_wanted = {}, set()
        self.funding, self.funding_wanted = {}, set()
        self.top_coins, self.universe = [], set()
        self.ws_stats = Counter()
        self.subscriptions = set()
        self.ws_opened = 0
        self.exec_handles = {}
        self.rt_trade_units, self.live_coins = {}, set()
        self.started = time.time()
        self.cpu_log = deque(maxlen=120)
        self.stop_file = None
        self.shared = None
        if config["live_data_feed"]:
            try:
                from live_data import LiveData
                self.shared = LiveData(ROOT / "hypertracker_state")
            except Exception as exc:
                self.errors["live_data"] = "LiveData indisponible: " + str(exc)
        try:
            from hyperliquid_client import PERP_SUB_DEXES
            self.dexes = ("",) + tuple(PERP_SUB_DEXES)
        except Exception:
            self.dexes = ("",)
        # HIP-3: dexes enabled by at least one book (empty when every book has HIP-3 off).
        self.hip3_dexes = tuple(sorted({d for u in self.units for d in u.hip3_dexes}))
        self.dexes = tuple(dict.fromkeys(self.dexes + self.hip3_dexes))
        self.hip3_mids = MarketBuffer(config["buffer_seconds"])
        self.hip3_universe = set()
        self.cf_dir = path_of(config["cf_dir"])
        self.cf_dir.mkdir(parents=True, exist_ok=True)
        self.load_entries()

    def crowding(self, unit, variant, row):
        """Entry guard of the engines: correlation caps across the books of the same role."""
        return crowding_reason(self.units, unit, variant, row, self.config["max_books_same_coin_side"],
                               self.config["max_portfolio_coin_side_notional"])

    # ----- counterfactual log, leader entries, consensus, feed lag -----
    def cf_write(self, name, record):
        stamp = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y%m%d")
        key = "%s-%s.jsonl" % (name, stamp)
        handle = self.cf_handles.get(key)
        if handle is None:
            for old in [k for k in self.cf_handles if k.startswith(name+"-")]:
                self.cf_handles.pop(old).close()
            handle = self.cf_handles[key] = (self.cf_dir / key).open("a", encoding="utf-8")
        handle.write(json.dumps(record, allow_nan=False)+"\n")
        handle.flush()

    def load_entries(self):
        """Rebuild the consensus tracker from the leader_entries files of the last window (+1 h margin)."""
        horizon = time.time()-(self.config["consensus_window_hours"]+1)*3600
        for file in sorted(self.cf_dir.glob("leader_entries-*.jsonl"))[-3:]:
            for line in file.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= horizon:
                    self.entries.setdefault((r["coin"], r["dir"]), deque()).append((r["t"], r["address"]))

    def consensus(self, address, coin, direction, t):
        """Distinct OTHER followed leaders with an entry on coin+direction in [t-window, t)."""
        queue = self.entries.get((coin, direction))
        if not queue:
            return 0
        start = t-self.config["consensus_window_hours"]*3600
        return len({a for et, a in queue if start <= et < t and a != address})

    def drain_cf(self, unit):
        engine = unit.engines["all_ioc"]
        if not engine.cf_out:
            return
        records, engine.cf_out = engine.cf_out, []
        for r in records:
            r = dict(r, prefix=unit.prefix)
            if r["kind"] == "leader_entry":
                r["consensus"] = self.consensus(r["address"], r["coin"], r["dir"], r["t"])
                self.cf_write("leader_entries", r)
                queue = self.entries.setdefault((r["coin"], r["dir"]), deque())
                queue.append((r["t"], r["address"]))
                horizon = r["t"]-(self.config["consensus_window_hours"]+2)*3600
                while queue and queue[0][0] < horizon:
                    queue.popleft()
            elif r["kind"] == "skip":
                r["consensus"] = self.consensus(r["address"], r["coin"], r["dir"], r["t"])
            self.cf_write("cf", r)

    def note_lag(self, feed, source, fill, now):
        """Feed lag: when we received a leader fill minus its exchange timestamp (clock skew included)."""
        if not self.config["feed_lag_log"]:
            return
        fill_at = number(fill["time"])/1000
        lag_ms = round((now-fill_at)*1000)
        self.lag_counts[source] += 1
        self.lag_log.append((now, source, lag_ms))
        self.cf_write("feed_lag", {"t": now, "src": source, "address": feed.address, "coin": fill["coin"],
                                   "fill_at": fill_at, "lag_ms": lag_ms, "tid": fill.get("tid")})

    def lag_stats(self, now):
        while self.lag_log and now-self.lag_log[0][0] > 3600:
            self.lag_log.popleft()
        result = {}
        for source in sorted({x[1] for x in self.lag_log}):
            values = sorted(x[2] for x in self.lag_log if x[1] == source)
            pick = lambda q: values[min(len(values)-1, int(q*len(values)))]
            result[source] = {"n_last_hour": len(values), "p50_ms": pick(0.5), "p90_ms": pick(0.9),
                              "p99_ms": pick(0.99), "max_ms": values[-1], "n_since_start": self.lag_counts[source]}
        return result

    def exec_log(self, unit, entry):
        handle = self.exec_handles.get(unit.folder)
        if handle is None:
            handle = self.exec_handles[unit.folder] = (unit.folder / "feed_exec.jsonl").open("a", encoding="utf-8")
        handle.write(json.dumps(dict(entry, prefix=unit.prefix, role=unit.role, feed=unit.feed), allow_nan=False)+"\n")
        handle.flush()

    # ----- scheduling -----
    def schedule(self, key, priority, due, function):
        current = self.jobs.get(key)
        if current is None or due < current[1]:
            self.jobs[key] = (priority, due, function)

    def want_candle(self, coin, minute):
        if (coin, minute) not in self.candles and (coin, minute) not in self.candle_wanted:
            self.candle_wanted.add((coin, minute))
            self.schedule("candle:%s:%d" % (coin, minute), 1, minute+62, lambda: self.job_candle(coin, minute))

    async def call(self, kind, **kwargs):
        return await asyncio.to_thread(self.rest.request, kind, **kwargs)

    async def job_market(self):
        try:
            data = await self.call("metaAndAssetCtxs")
            now = time.time()
            ref = self.store.put(data, now)
            meta, contexts = data
            ranked = sorted(((number(c["dayNtlVlm"]), m["name"]) for m, c in zip(meta["universe"], contexts)
                             if not m.get("isDelisted") and perp(m["name"])), reverse=True)
            self.universe = {name for _, name in ranked}
            self.top_coins = [name for _, name in ranked[:self.config["top_volume_coins"]]]
            for unit in self.units:
                if unit.feed == "realtime":
                    unit.observe("market", ref, now)
                else:
                    unit.enqueue(now, "market", ref)
            self.errors.pop("markets", None)
            due = now+60
            if self.hip3_dexes:
                await self.job_hip3_markets()
        except Exception as exc:
            self.errors["markets"] = str(exc)
            due = time.time()+15
        self.schedule("market", 1, due, self.job_market)

    async def job_hip3_markets(self):
        """metaAndAssetCtxs of each enabled HIP-3 dex -> 'market' observation of the books enabling it."""
        universe = set()
        for dex in self.hip3_dexes:
            try:
                data = await self.call("metaAndAssetCtxs", dex=dex)
                now = time.time()
                if any(not str(m["name"]).startswith(dex + ":") for m in data[0]["universe"]):
                    raise ValueError("unexpected coin names for dex " + dex)
                ref = self.store.put(data, now)
                universe |= {m["name"] for m in data[0]["universe"] if not m.get("isDelisted")}
                for unit in self.units:
                    if dex in unit.hip3_dexes:
                        if unit.feed == "realtime":
                            unit.observe("market", ref, now)
                        else:
                            unit.enqueue(now, "market", ref)
                self.errors.pop("markets:" + dex, None)
            except Exception as exc:
                self.errors["markets:" + dex] = str(exc)[:200]
        if universe:
            self.hip3_universe = universe

    async def fills_between(self, address, start_ms, end_ms):
        result, seen, cursor = [], set(), int(start_ms)
        for _ in range(20):
            batch = await self.call("userFillsByTime", user=address, startTime=cursor, endTime=int(end_ms), aggregateByTime=False)
            if not isinstance(batch, list):
                raise ValueError("Invalid userFillsByTime response")
            for fill in batch:
                key = (fill.get("tid"), fill["side"], fill["time"], fill["coin"])
                if key not in seen:
                    seen.add(key)
                    result.append(fill)
            if len(batch) < 2000:
                return result
            cursor = max(int(f["time"]) for f in batch)
            if cursor >= end_ms:
                return result
        raise ValueError("More than 20 pages of fills; no guessed entries")

    async def job_reconcile(self, feed):
        address, now = feed.address, time.time()
        feed.urgent_at = None
        try:
            if feed.rt_units:
                # Realtime wallet: exactly the original Service.poll cycle.
                unit = feed.rt_units[0]
                wallet = unit.primary.wallet(address)
                start_ms = int(max(wallet["baseline_at"] or now, wallet["reconciled_at"]-1)*1000) if wallet["baseline_at"] else None
                data = await asyncio.to_thread(fetch_wallet, self.rest, address, start_ms,
                                               now-wallet["ratio_at"] >= unit.policy["ratio_refresh_seconds"],
                                               feed.hip3_dexes)
                for u in feed.rt_units:
                    u.observe("wallet", data, time.time())
                self.errors.pop(address, None)
                if data.get("ratio_error"):
                    self.errors[address] = "Equity refresh: " + data["ratio_error"]
                refreshed = unit.primary.wallet(address)
                due = max(time.time()+1, min(now+unit.policy["reconcile_seconds"],
                                             refreshed["ratio_at"]+unit.policy["ratio_refresh_seconds"]))
                if feed.dl_units:
                    feed.pending = (data, data.get("fills"))
                    self.schedule("resolve:"+address, 0, time.time()+2, lambda: self.job_resolve(feed))
            else:
                policy = feed.dl_units[0].policy
                raw = await self.call("clearinghouseState", user=address)
                position_map(raw)
                at = float(raw["time"])/1000
                hip3 = {}
                for dex in feed.hip3_dexes:
                    hip3[dex] = await self.call("clearinghouseState", user=address, dex=dex)
                if hip3:
                    raw = merge_dex_states(raw, hip3)
                data = {"address": address, "requested_at": now, "positions": raw, "position_at": at}
                if hip3:
                    data["hip3_dexes"] = list(feed.hip3_dexes)
                if now-feed.last_portfolio >= policy["ratio_refresh_seconds"]-70:  # every 4th reconcile (240 s)
                    try:
                        data["portfolio"] = await self.call("portfolio", user=address)
                        if now-feed.last_abstraction >= 6*3600:  # informational only (account_mode)
                            feed.account_mode = await self.call("userAbstraction", user=address)
                            feed.last_abstraction = now
                        data["account_mode"] = feed.account_mode
                        feed.last_portfolio = now
                        self.errors.pop(address, None)
                    except Exception as exc:
                        data["ratio_error"] = type(exc).__name__
                        self.errors[address] = "Equity refresh: " + type(exc).__name__
                feed.pending = (data, None)
                self.schedule("resolve:"+address, 0, time.time()+2, lambda: self.job_resolve(feed))
                due = now+policy["reconcile_seconds"]
            for coin in position_map(data["positions"]):
                if feed.tracked(coin):
                    feed.coins.add(coin)
        except Exception as exc:
            self.errors[address] = str(exc)[:200]
            LOG.warning("Wallet %s: %s", address, exc)
            due = time.time()+10
        self.schedule("reconcile:"+address, 1, due, lambda: self.job_reconcile(feed))

    async def job_resolve(self, feed):
        """Compare the REST snapshot with trades-feed fills; fetch fills only when needed."""
        if not feed.pending:
            return
        data, fills = feed.pending
        address, position_at = feed.address, data["position_at"]
        positions = {c: p["size"] for c, p in position_map(data["positions"]).items() if feed.tracked(c)}
        expected = feed.expected(position_at*1000)
        mismatch = feed.snapshot is None or any(
            not math.isclose(expected.get(c, 0), positions.get(c, 0), abs_tol=1e-8, rel_tol=1e-7)
            for c in set(expected) | set(positions))
        try:
            if fills is None and (mismatch or feed.need_full_fetch):
                starts = [u.primary.wallet(address)["reconciled_at"]-1 for u in feed.dl_units
                          if u.primary.wallet(address)["baseline_at"] is not None]
                start = feed.snapshot_at-2 if feed.snapshot_at else (min(starts) if starts else None)
                if start is not None:
                    fills = await self.fills_between(address, start*1000, position_at*1000)
                    for u in feed.dl_units:
                        u.counts["rest_fill_fetches"] += 1
                feed.need_full_fetch = False
        except Exception as exc:
            self.errors[address] = ("Fills: " + str(exc))[:200]
            self.schedule("resolve:"+address, 0, time.time()+10, lambda: self.job_resolve(feed))
            return
        # HIP-3 / spot fills are excluded by the engine anyway and never on the perp trades feed.
        unknown = [f for f in sorted(fills or [], key=fill_order_key) if feed.tracked(f["coin"]) and feed.remember(f)]
        for fill in unknown:
            if feed.tracked(fill["coin"]):
                feed.coins.add(fill["coin"])
        for fill in unknown:
            if feed.snapshot is not None:
                self.note_lag(feed, "rest_missed_by_trades", fill, time.time())
        if feed.snapshot is not None and mismatch:
            for u in feed.dl_units:
                u.counts["reconcile_mismatch"] += 1
        for unit in feed.dl_units:
            unit.deliver_wallet(data, unknown)
        feed.snapshot, feed.snapshot_at = positions, position_at
        feed.recent = [x for x in feed.recent if x[0] > position_at*1000]
        feed.watermark = position_at
        feed.pending = None

    async def job_seed(self, feed):
        try:
            fills = await self.call("userFillsByTime", user=feed.address, aggregateByTime=True,
                                    startTime=int((time.time()-self.config["seed_fills_hours"]*3600)*1000))
            feed.coins |= {f["coin"] for f in fills if feed.tracked(f["coin"])}
            self.errors.pop("seed:"+feed.address[:6], None)
        except Exception as exc:
            self.schedule("seed:"+feed.address, 3, time.time()+120, lambda: self.job_seed(feed))
            self.errors["seed:"+feed.address[:6]] = str(exc)[:200]

    async def job_candle(self, coin, minute):
        try:
            rows = await self.call("candleSnapshot", req={"coin": coin, "interval": "1m",
                                                          "startTime": minute*1000, "endTime": (minute+60)*1000})
            row = next((r for r in rows if int(r["t"]) == minute*1000), None)
            self.candles[(coin, minute)] = (number(row["o"]), number(row["c"])) if row else False
        except Exception as exc:
            self.errors["candle:"+coin] = str(exc)[:200]
            self.candles[(coin, minute)] = False
        self.candle_wanted.discard((coin, minute))

    async def job_funding(self, coin, hour):
        try:
            rates = await self.call("fundingHistory", coin=coin, startTime=hour*1000, endTime=hour*1000+60000)
            candles = await self.call("candleSnapshot", req={"coin": coin, "interval": "1m",
                                                             "startTime": hour*1000, "endTime": (hour+60)*1000})
            # Funding is stamped a few ms after the hour (e.g. HH:00:00.005).
            rate = next(r["fundingRate"] for r in rates if int(r["time"])//3600000*3600 == hour)
            price = next(r["o"] for r in candles if int(r["t"]) == hour*1000)
            self.funding[(coin, hour)] = {"coin": coin, "hour": hour, "rate": rate, "price": price}
            self.errors.pop("funding:"+coin, None)
        except Exception as exc:
            self.errors["funding:"+coin] = str(exc)[:200]
        self.funding_wanted.discard((coin, hour))

    def plan_funding(self, now):
        debts = set()
        for unit in self.units:
            for engine in unit.engines.values():
                for debt in list(engine.state["funding_pending"].values()):
                    key = (debt["coin"], debt["hour"])
                    if key in self.funding:
                        if key not in unit.funding_sent:
                            unit.funding_sent.add(key)
                            if unit.feed == "realtime":
                                unit.observe("funding", self.funding[key], now)
                            else:
                                unit.enqueue(unit.clock, "funding", self.funding[key])
                    elif now >= debt["hour"]+120:
                        debts.add(key)
        for coin, hour in sorted(debts - self.funding_wanted)[:10]:
            self.funding_wanted.add((coin, hour))
            self.schedule("funding:%s:%d" % (coin, hour), 2, now, lambda c=coin, h=hour: self.job_funding(c, h))
        for unit in self.units:
            if len(unit.funding_sent) > 500:
                unit.funding_sent = {k for k in unit.funding_sent if now-k[1] < 86400}

    async def scheduler(self):
        self.schedule("market", 1, 0, self.job_market)
        for i, feed in enumerate(self.feeds.values()):
            if not feed.rt_units:
                # Cheap position snapshots first; equity (weight 20) staggered by 4 s per wallet.
                policy = feed.dl_units[0].policy
                feed.last_portfolio = time.time()-(policy["ratio_refresh_seconds"]-70)+30+i*4
            self.schedule("reconcile:"+feed.address, 1, time.time()+1+i*0.7, lambda f=feed: self.job_reconcile(f))
            self.schedule("seed:"+feed.address, 3, time.time()+20+i*2, lambda f=feed: self.job_seed(f))
        funding_due = 0
        while True:
            now = time.time()
            if now >= funding_due:
                funding_due = now+60
                self.plan_funding(now)
            for feed in self.feeds.values():
                if feed.urgent_at and now >= feed.urgent_at and "resolve:"+feed.address not in self.jobs:
                    self.schedule("reconcile:"+feed.address, 0, feed.urgent_at, lambda f=feed: self.job_reconcile(f))
            ready = [(p, due, key) for key, (p, due, _) in self.jobs.items() if due <= now]
            if not ready:
                await asyncio.sleep(0.2)
                continue
            _, _, key = min(ready)
            _, _, function = self.jobs.pop(key)
            try:
                await function()
            except Exception as exc:
                self.errors["job"] = (key + ": " + str(exc))[:200]
                LOG.exception("Job %s", key)

    # ----- websocket -----
    def wanted_coins(self):
        needed = set()
        for unit in self.units:
            needed |= unit.needed_coins() | unit.pending_coins()
        wallet_coins = sorted(c for f in self.feeds.values() for c in f.coins if not self.universe or c in self.universe)
        ordered = list(dict.fromkeys(sorted(needed) + wallet_coins + self.top_coins))
        wanted = set(ordered[:max(self.config["max_market_coins"], len(needed))]) | needed
        if self.hip3_dexes:
            # HIP-3 coins of HIP-3 books (outside the main-coin budget so other books are unaffected).
            wanted |= {c for f in self.feeds.values() if f.hip3_dexes for c in f.coins
                       if ":" in c and dex_of(c) in f.hip3_dexes and (not self.hip3_universe or c in self.hip3_universe)}
        return wanted

    def wanted_subscriptions(self):
        subs = {json.dumps({"type": "userFills", "user": a}, sort_keys=True) for a in self.realtime_users}
        subs |= {json.dumps({"type": "allMids", "dex": d} if d else {"type": "allMids"}, sort_keys=True) for d in self.dexes}
        for coin in self.wanted_coins():
            for kind in ("l2Book", "trades"):
                subs.add(json.dumps({"type": kind, "coin": coin}, sort_keys=True))
        return subs

    def handle(self, message, now):
        channel, data = message.get("channel"), message.get("data")
        self.ws_stats[channel or "?"] += 1
        if channel == "l2Book":
            self.buffer.add_book(data, now)
            if self.shared is not None and data.get("coin") in self.live_coins:
                self.ingest(message)
        elif channel == "trades":
            for trade in data:
                self.buffer.add_trade(trade, now)
                users = trade.get("users") or ()
                for index, side in ((0, "B"), (1, "A")):
                    if index < len(users):
                        feed = self.feeds.get(str(users[index]).lower())
                        if feed and feed.dl_units and feed.tracked(trade["coin"]):
                            self.leader_trade(feed, trade, side, now)
                for unit in self.rt_trade_units.get(trade["coin"], ()):
                    unit.observe("trade", {k: trade[k] for k in ("coin", "side", "px", "sz", "time", "tid") if k in trade}, now)
        elif channel == "allMids":
            mids = data.get("mids", {})
            # Main-dex message (HIP-3 dex messages only carry "dex:COIN" keys); keep perps only.
            if mids and not any(":" in c for c in mids):
                self.buffer.add_mids({c: p for c, p in mids.items() if perp(c)}, now)
            elif mids and self.hip3_dexes:
                selected = {c: p for c, p in mids.items() if dex_of(c) in self.hip3_dexes}
                if selected:
                    self.hip3_mids.add_mids(selected, now)
            if self.shared is not None:
                self.ingest(message)
        elif channel == "userFills":
            address = data["user"].lower()
            feed = self.feeds.get(address)
            if feed and feed.rt_units:
                for fill in sorted(data.get("fills", []), key=fill_order_key):
                    if not data.get("isSnapshot"):
                        self.note_lag(feed, "userFills", fill, now)
                    for unit in feed.rt_units:
                        unit.observe("fill", {"address": address, "fill": fill}, now)
                        unit.signal_meta[fill["coin"]] = {"source": "userFills", "detected_at": now}
                    row = feed.rt_units[0].primary.state["positions"].get(address+":"+fill["coin"])
                    if row and row["target"] and not row["source_leverage"]:
                        feed.urgent_at = feed.urgent_at or now
        elif channel == "error":
            self.errors["websocket_message"] = str(data)[:200]

    def ingest(self, message):
        try:
            from live_stream import ingest
            ingest(self.shared, message)
        except Exception as exc:
            self.errors["live_data"] = str(exc)[:200]

    def leader_trade(self, feed, trade, side, now):
        fill = {"coin": trade["coin"], "px": trade["px"], "sz": trade["sz"], "side": side, "time": trade["time"],
                "tid": trade.get("tid"), "hash": trade.get("hash"), "derived": "trades"}
        if not feed.remember(fill):
            return
        coin = fill["coin"]
        feed.coins.add(coin)
        # Flat at the last snapshot: leverage unknown -> immediate cheap clearinghouseState.
        if feed.snapshot is not None and not feed.snapshot.get(coin) and not any(c == coin for _, c, _ in feed.recent):
            feed.urgent_at = feed.urgent_at or now+0.5
        feed.recent.append((int(fill["time"]), coin, number(fill["sz"])*(1 if side == "B" else -1)))
        self.note_lag(feed, "trades", fill, now)
        for unit in feed.dl_units:
            unit.enqueue(number(fill["time"])/1000 + unit.latency, "fill", {"address": feed.address, "fill": fill},
                         {"source": "trades", "detected_at": now})

    async def websocket(self):
        from websockets.asyncio.client import connect
        backoff = 5
        while True:
            opened = time.time()
            try:
                async with connect("wss://api.hyperliquid.xyz/ws", ping_interval=20, ping_timeout=20,
                                   close_timeout=5, max_size=2**23) as socket:
                    self.ws_opened += 1
                    self.subscriptions = set()
                    self.connected = True
                    self.errors.pop("websocket", None)
                    if self.ws_opened > 1:
                        for feed in self.feeds.values():
                            feed.need_full_fetch = True  # trades missed while disconnected
                    rate = self.config["ws_send_per_second"]
                    budget, budget_at, synced = 0.0, time.time(), 0
                    while True:
                        now = time.time()
                        budget, budget_at = min(rate, budget + (now-budget_at)*rate), now
                        if now-synced >= 1:
                            synced = now
                            wanted = self.wanted_subscriptions()
                            self.live_coins = set()
                            for unit in self.units:
                                self.live_coins |= unit.needed_coins()
                            first = lambda s: (not ('"userFills"' in s or '"allMids"' in s),
                                               not any('"%s"' % c in s for c in self.live_coins), s)
                            changes = [("subscribe", s) for s in sorted(wanted-self.subscriptions, key=first)]
                            changes += [("unsubscribe", s) for s in sorted(self.subscriptions-wanted)]
                            for method, sub in changes:
                                if budget < 1:
                                    break
                                await socket.send(json.dumps({"method": method, "subscription": json.loads(sub)}))
                                budget -= 1
                                self.ws_stats["sent_" + method] += 1
                                (self.subscriptions.add if method == "subscribe" else self.subscriptions.discard)(sub)
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=0.25)
                        except asyncio.TimeoutError:
                            continue
                        now = time.time()
                        self.last_message_at = now
                        self.handle(json.loads(raw), now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors["websocket"] = str(exc)[:200]
                LOG.warning("Websocket disconnected: %s", exc)
            finally:
                self.connected = False
                self.subscriptions = set()
            if time.time()-opened > 120:
                backoff = 5
            await asyncio.sleep(backoff*(0.7+0.6*random.random()))
            backoff = min(backoff*2, 120)

    # ----- driver -----
    def runner_status(self, now):
        from reliable_io import atomic_json
        subs = Counter(json.loads(s)["type"] for s in self.subscriptions)
        cpu = time.process_time()
        self.cpu_log.append((now, cpu))
        old = self.cpu_log[0]
        atomic_json(path_of(self.config["runner_status"]), {
            "at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "pid": os.getpid(), "live_orders_enabled": False,
            "started_at": self.started, "websocket_connected": self.connected, "websocket_connections_opened": self.ws_opened,
            "subscriptions": dict(subs), "subscriptions_total": len(self.subscriptions),
            "unique_users_subscribed": len({json.loads(s).get("user") for s in self.subscriptions if '"user"' in s}),
            "realtime_wallets": len(self.realtime_users),
            "delayed_wallets": sum(1 for f in self.feeds.values() if not f.rt_units),
            "units": len(self.units), "ws_messages": dict(self.ws_stats),
            "rest_weight_last_minute": self.rest.weight_last_minute(),
            "rest_weight_avg_per_minute": self.rest.total_weight/max(1/60, (now-self.rest.started)/60),
            "rest_budget_per_minute": self.config["rest_budget_weight_per_min"], "rest_calls": dict(self.rest.calls),
            "rest_errors": dict(self.rest.errors), "rest_429": self.rest.rate_limited, "errors": self.errors,
            "jobs_waiting": len(self.jobs), "cpu_fraction": (cpu-old[1])/max(1e-9, now-old[0]),
            "coins_buffered": len(self.buffer.books), "feed_lag": self.lag_stats(now),
            "roles": dict(Counter(u.role for u in self.units)),
            "books": [{"prefix": u.prefix, "role": u.role, "feed": u.feed, "folder": str(u.folder),
                       "copy_latency_ms": round(u.latency*1000), "virtual_lag_seconds": now-(u.clock or now),
                       "counts": dict(u.counts)} for u in self.units],
        })

    async def driver(self, seconds):
        started = time.time()
        for unit in self.units:
            if unit.feed == "realtime":
                unit.clock = time.time()
            else:
                unit.clock = max(unit.clock or 0, time.time()-self.config["delayed_lag_seconds"])
            unit.observe("restart", {}, unit.clock)
        status_at = prune_at = 0
        tasks = [asyncio.create_task(self.scheduler()), asyncio.create_task(self.websocket())]
        try:
            while not seconds or time.time()-started < seconds:
                if self.stop_file is not None and self.stop_file.exists():
                    LOG.info("Stop file found: clean shutdown")
                    break
                for task in tasks:
                    if task.done():
                        task.result()  # an acquisition crash must never look healthy
                loop_at = time.time()
                self.rt_trade_units = {}
                for unit in self.units:
                    if unit.feed == "realtime":
                        for coin in unit.maker_coins():
                            self.rt_trade_units.setdefault(coin, []).append(unit)
                for index, unit in enumerate(self.units):
                    if unit.feed == "realtime":
                        unit.step_realtime(time.time())
                    else:
                        unit.advance(time.time())
                    if unit.role == "main":
                        self.drain_cf(unit)
                    if time.time()-unit.status_at >= self.config["status_seconds"] + (index % 7)*0.1:
                        unit.save(time.time())
                if loop_at-status_at >= 5:
                    status_at = loop_at
                    self.runner_status(loop_at)
                if loop_at-prune_at >= 10:
                    prune_at = loop_at
                    self.buffer.prune(loop_at)
                    self.hip3_mids.prune(loop_at)
                    for feed in self.feeds.values():
                        feed.prune(loop_at)
                    self.candles = {k: v for k, v in self.candles.items() if loop_at-k[1] < 900}
                await asyncio.sleep(max(0.05, 0.5-(time.time()-loop_at)))
        finally:
            self.stopping = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for unit in self.units:
                unit.save(time.time())
            self.runner_status(time.time())
            for handle in list(self.exec_handles.values()) + list(self.cf_handles.values()):
                handle.close()


def check(config, plan):
    """Read-only: validates the config, every policy and that existing checkpoints still load."""
    summary = {"mode": "paper", "live_orders_enabled": False, "books": []}
    addresses = {}
    for prefix, role, file, folder, feed, latency in plan:
        policy = load_policy(file)
        state = "new"
        path = Path(folder) / "checkpoint.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for name in VARIANTS:
                Engine(policy, saved.get(name), variant=name)  # raises if the policy identity changed
            state = "checkpoint_ok"
        options = hip3_book_options(config, prefix)
        summary["books"].append({"prefix": prefix, "role": role, "feed": feed, "copy_latency_ms": latency,
                                 "folder": str(folder), "state": state,
                                 "mode": config["books"].get(prefix, {}).get("mode", "live") if role != "placebo" else "live",
                                 "hip3": ({k: options[k] for k in sorted(HIP3_BOOK_KEYS)} if options["allow_hip3"] else False)})
        if role in ("main", "placebo"):
            addresses[policy["wallets"][0]["address"]] = feed
    summary["realtime_wallets"] = sum(1 for f in addresses.values() if f == "realtime")
    summary["delayed_wallets"] = sum(1 for f in addresses.values() if f == "delayed")
    summary["websocket_connections"] = 1
    summary["user_subscriptions"] = summary["realtime_wallets"]
    summary["rest_budget_weight_per_min"] = config["rest_budget_weight_per_min"]
    summary["estimated_rest_weight_per_min"] = round(
        summary["realtime_wallets"]*36 + summary["delayed_wallets"]*7.5 + 40, 1)  # rough, measured 559 for 10+20
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "multi_runner.json")
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    plan = book_plan(config)
    if args.check:
        print(json.dumps(check(config, plan), indent=1))
        return
    if not plan:
        raise SystemExit("No book to run")
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(ROOT / "logs" / "manual-copy-multi2.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    from selection_benchmark import exclusive_runner
    with ExitStack() as stack:
        lock = path_of(config["runner_status"]).parent / "multi2_runner"
        lock.mkdir(parents=True, exist_ok=True)
        stack.enter_context(exclusive_runner(lock))
        for _, _, _, folder, _, _ in plan:
            Path(folder).mkdir(parents=True, exist_ok=True)
            stack.enter_context(exclusive_runner(Path(folder)))
        runner = Runner(config, plan)
        runner.stop_file = lock / "STOP"  # written by stop_multi2.ps1 for a clean shutdown
        if runner.stop_file.exists():
            runner.stop_file.unlink()
        LOG.info("Started %d books (%d realtime wallets)", len(runner.units), len(runner.realtime_users))
        asyncio.run(runner.driver(args.seconds))


if __name__ == "__main__":
    main()
