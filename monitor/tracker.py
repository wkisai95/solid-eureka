"""Paper allocation of a fixed sAERO size.

Every run (about every 15 minutes) it records, for each method, the allocation it would recommend now and what that
allocation is expected to earn during one cooldown. Each of these allocations is then followed for the whole cooldown
with the fees and incentives that really arrive, diluted by the real allocation of everybody else, so over time it
shows which pools and which way of choosing them actually pay the most. Every method follows the options loaded from
the page: largest and smallest share per pool, chains, excluded pools and searches, volume spikes.
"""
import heapq
import re
import statistics

from . import aero as A
from . import chain as C
from . import marketdata as MD

HOUR = 3600
DAY = 86400
HIST_HOURS = 8 * 24
DAILY_DAYS = 90
CLOSED_DAYS = 30
PERF_DAYS = 30
STRATEGIES = {
    "48h": "Ultime 48 ore",
    "trend": "Tendenza 12 ore",
    "prudente": "Prudente",
    "nuove": "Chain nuove",
    "radar": "Radar",
}
RULES = {}          # per-strategy settings from config.json ("strategies"), set by main
OPTIONS = {}        # the page's options loaded into the simulations (state["options"]), set by main
DEFAULT_OPTIONS = {"cap": 100, "min": 0, "chains_off": [], "excl": [], "rules": [], "spike": False, "spike_k": 75,
                   "trend": 0, "comp": 3}
KEY_RE = re.compile(r"\d{1,7}:0x[0-9a-f]{40}")


def new_tracker(size, now):
    return {"v": 1, "size": size, "start": now, "last": 0, "open": {s: [] for s in STRATEGIES},
            "closed": {s: [] for s in STRATEGIES}, "virtual": {}, "pool_hist": {}, "pool_daily": {},
            "pool_reco": {}, "perf": {}}


# ---------------------------------------------------------------- options from the page

def parse_options(raw):
    """The page's options, checked: anything out of range is refused rather than guessed."""
    if not isinstance(raw, dict):
        raise ValueError("options must be an object")
    o = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_OPTIONS.items()}

    def num(k, lo, hi, cast):
        if raw.get(k) is None:
            return
        v = cast(raw[k])
        if not lo <= v <= hi:
            raise ValueError(f"{k} out of range")
        o[k] = v

    num("cap", 1, 100, int)
    num("min", 0, 50, int)
    num("spike_k", 0, 1000, float)
    num("trend", 0, 100, float)
    num("comp", 0, 50, float)
    if "spike" in raw:
        o["spike"] = bool(raw["spike"])
    for k, limit in (("chains_off", 20), ("excl", 3000), ("rules", 100)):
        v = raw.get(k)
        if v is not None and (not isinstance(v, list) or len(v) > limit):
            raise ValueError(f"{k} must be a list of at most {limit} items")
    names = set(C.CHAIN_SHORT.values())
    o["chains_off"] = sorted({str(x) for x in raw.get("chains_off") or [] if str(x) in names})
    o["excl"] = sorted({str(x).lower() for x in raw.get("excl") or [] if KEY_RE.fullmatch(str(x).lower())})
    rules = []
    for x in raw.get("rules") or []:
        t = " ".join(str(x).split())[:80]
        if t and t.lower() not in [r.lower() for r in rules]:
            rules.append(t)
    o["rules"] = rules
    return o


def options():
    return {**DEFAULT_OPTIONS, **{k: v for k, v in OPTIONS.items() if k in DEFAULT_OPTIONS}}


def describe_options(o):
    parts = [f"max {o['cap']}%" if o["cap"] < 100 else "no max", f"min {o['min']}%" if o["min"] else "no min"]
    if o["chains_off"]:
        parts.append("without " + ", ".join(o["chains_off"]))
    if o["excl"] or o["rules"]:
        parts.append(f"{len(o['excl'])} pools and {len(o['rules'])} searches excluded")
    if o["spike"]:
        parts.append(f"volume spikes trimmed, incentives kept (tolerance {o['spike_k']:g}%)")
    parts.append(f"radar trend {o['trend']:g}%, competition {o['comp']:g}x")
    return ", ".join(parts)


def _hay(state, k):
    m = state["gauges"].get(k) or {}
    kind = m.get("kind") if m.get("kind") not in (None, "?") else ""
    return f"{A.gauge_name(state, k)} {kind} {C.chain_short(A.chain_of(k))}".lower()


def rule_hit(hay, rule):
    """The page's search: every word of it appears in the pool's name, kind or chain."""
    words = rule.lower().split()
    return bool(words) and all(w in hay for w in words)


def excluded(state, keys, opts):
    """Pools the options leave out: chains switched off, pools unticked, saved searches."""
    off = set(opts.get("chains_off") or [])
    excl = set(opts.get("excl") or [])
    rules = opts.get("rules") or []
    out = set()
    for k in keys:
        if k in excl or C.chain_short(A.chain_of(k)) in off:
            out.add(k)
        elif rules and any(rule_hit(_hay(state, k), r) for r in rules):
            out.add(k)
    return out


def spikes(state, keys, now, tolerance):
    """Pools whose fees of the last 48 hours rose more than the whole market's change plus the tolerance.

    The threshold is the market's change plus tolerance% of it: market +50% and tolerance 75% flag pools above
    +87.5%. Pools without a change (short history, or almost no fees before) count as unchanged.
    Returns ({pool: fees of the 48 hours before}, (market change, threshold)).
    """
    cur_sum = prev_sum = 0.0
    changes, prevs = {}, {}
    for k in keys:
        cur = A.fees_usd(state, k, now - 48 * HOUR, now + HOUR)
        d, prev = A.fee_change(state, k, now)
        cur_sum += cur
        if d is not None and d > -100:
            changes[k] = d
            prevs[k] = prev
            prev_sum += prev
        else:
            prev_sum += cur
    if not changes or prev_sum <= 0:
        return {}, None
    dm = (cur_sum / prev_sum - 1) * 100
    thr = dm + tolerance / 100 * abs(dm)
    return {k: prevs[k] for k, d in changes.items() if d > thr}, (round(dm, 1), round(thr, 1))


# ---------------------------------------------------------------- recommendation

_INPUTS = {}


def _inputs(state, prices, now, cooldown):
    """Per pool, once per run: fees of 48 and 12 hours, incentives over the next cooldown, fee change."""
    tag = (id(state), now, cooldown)
    if _INPUTS.get("tag") != tag:
        keys = set(state["fees"]) | {p["g"] for p in state["incentives"].values()}
        rows = {}
        for k in keys:
            rows[k] = (A.fees_usd(state, k, now - 48 * HOUR, now + HOUR),
                       A.fees_usd(state, k, now - 12 * HOUR, now + HOUR),
                       A.incentive_usd(state, prices, k, now, now + cooldown),
                       A.fee_change(state, k, now)[0])
        _INPUTS.clear()
        _INPUTS.update({"tag": tag, "rows": rows})
    return _INPUTS["rows"]


def candidates(state, prices, now, cooldown, strategy, opts=None):
    """{gauge: (expected revenue during one cooldown in USD, allocation of everybody else)}, within the options."""
    opts = opts if opts is not None else options()
    ap = state["ap"]
    rows = _inputs(state, prices, now, cooldown)
    allowed = set(rows) - excluded(state, rows, opts)
    spiked, dm = {}, 0.0
    if opts.get("spike"):
        spiked, mk = spikes(state, rows, now, float(opts.get("spike_k", 75)))
        dm = mk[0] if mk else 0.0
    if strategy == "nuove":
        # only big pairs on the chains where Aero is still small, valued like "Ultime 48 ore"
        universe, _ = MD.new_chain_universe(state, RULES.get("nuove"))
        allowed &= universe
    out, mkt_rev, mkt_ap = {}, 0.0, 0.0
    w = float(opts.get("trend", 0)) / 100
    for k, (f48, f12, inc, d) in rows.items():
        if strategy == "trend":
            base = 0.5 * f48 + 0.5 * f12 * 4
        elif strategy == "radar":
            # the fee part follows its 48-hour change as much as the trend weight says; incentives are scheduled
            base = f48 * (1 + w * max(-100.0, min(200.0, d or 0.0)) / 100)
        else:
            base = f48
        if k in spiked:
            # a spike rarely repeats: fees go back to the 48 hours before, moved like the market; incentives stay
            base = min(base, max(0.0, spiked[k] * (1 + dm / 100)))
        rev = max(0.0, base) * cooldown / (48 * HOUR) + inc
        a = float(ap.get(k, 0.0))
        mkt_rev += rev
        mkt_ap += a
        if k not in allowed or (state["gauges"].get(k) or {}).get("ok") is False:
            continue  # left out by the options, or not activated / zero emission cap: cannot be voted
        if rev > 0.01:
            out[k] = (rev, a)
    if strategy == "prudente" and out:
        # pools far above the typical yield attract other allocators: assume their allocation grows
        ys = [r / a for r, a in out.values() if a >= 1_000_000]
        med = statistics.median(ys) if ys else None
        if med:
            for k, (r, a) in list(out.items()):
                y = r / max(a, 1.0)
                out[k] = (r, a * (1 + 0.5 * min(2.0, max(0.0, y / med - 1))) if a > 0 else a)
    if strategy == "radar" and out:
        n = float(opts.get("comp", 0))
        avg = mkt_rev / mkt_ap if mkt_ap > 0 else 0.0
        if n > 0 and avg > 0:
            # other allocators come where it pays: no pool keeps more than n times the market's yield per vote
            out = {k: (r, max(a, r / (n * avg))) for k, (r, a) in out.items()}
    return out


def _share(cands, k, x):
    r, a = cands[k]
    return r * x / (a + x) if x > 0 else 0.0


def _greedy(cands, keys, size, max_pools, cap_slices, slices):
    step = size / slices
    alloc, per_chain = {}, {}

    def gain(k):
        x = alloc.get(k, 0.0)
        return _share(cands, k, x + step) - _share(cands, k, x)

    heap = [(-gain(k), k) for k in keys]
    heapq.heapify(heap)
    for _ in range(slices):
        while heap:
            neg, k = heapq.heappop(heap)
            if alloc.get(k, 0.0) >= cap_slices * step - 1e-9:
                continue                         # already at the largest share allowed per pool
            c = A.chain_of(k)
            if k not in alloc and per_chain.get(c, 0) >= max_pools:
                continue
            cur = gain(k)
            if abs(-neg - cur) > 1e-9:
                heapq.heappush(heap, (-cur, k))
                continue
            if k not in alloc:
                per_chain[c] = per_chain.get(c, 0) + 1
            alloc[k] = alloc.get(k, 0.0) + step
            heapq.heappush(heap, (-gain(k), k))
            break
    return alloc


def recommend(cands, size, max_pools, cap=100, min_pct=0, slices=100):
    """Greedy marginal allocation in 1% slices; returns ({gauge: amount}, expected USD).

    max_pools is the LeafVoter limit, which applies to each chain separately. cap is the largest share of the
    position in one pool, in percent; with too few pools to place everything it grows just enough. min_pct is the smallest
    share: the pool with the smallest share under it is dropped and the slices placed again, until every pool
    reaches it (as in the Radar app).
    """
    if not cands or size <= 0:
        return {}, 0.0
    keys = [k for k, (r, a) in cands.items() if r > 0]
    alloc = {}
    for _ in range(400):
        # with too few pools for the largest share, it grows just enough to place everything
        cap_slices = max(1, int(round(cap * slices / 100)), -(-slices // max(1, len(keys))))
        alloc = _greedy(cands, keys, size, max_pools, cap_slices, slices)
        if not min_pct or len(alloc) <= 1:
            break
        small = [k for k, x in alloc.items() if x / size * 100 < min_pct - 1e-6]
        if not small:
            break
        drop = min(small, key=lambda k: (alloc[k], _share(cands, k, alloc[k])))
        keys = [k for k in keys if k != drop]
    pred = sum(_share(cands, k, x) for k, x in alloc.items())
    return alloc, pred


# ---------------------------------------------------------------- accrual

def interval_revenue(state, prices, fresh_fees, start, end):
    """USD that reached each gauge's allocators between start and end: new fee transfers + streamed incentives."""
    rev = {}
    for ts, k, usd in fresh_fees:
        if start - 2 * HOUR < ts <= end:
            rev[k] = rev.get(k, 0.0) + usd
    for k in {p["g"] for p in state["incentives"].values()}:
        r = A.incentive_usd(state, prices, k, start, end)
        if r > 0:
            rev[k] = rev.get(k, 0.0) + r
    return rev


def _record_market(tr, state, rev, now):
    hour = str(now - now % HOUR)
    ap = state["ap"]
    for k, r in rev.items():
        a = float(ap.get(k, 0.0))
        h = tr["pool_hist"].setdefault(k, {})
        cur = h.get(hour, [0.0, 0.0, 0.0])
        # revenue, latest allocation, revenue that a marginal 1000 sAERO would have earned
        h[hour] = [round(cur[0] + r, 4), round(a), round(cur[2] + r * 1000 / (a + 1000), 6)]
    # roll hours older than the window into days
    cutoff = now - HIST_HOURS * HOUR
    for k, h in list(tr["pool_hist"].items()):
        for hr in [x for x in h if int(x) < cutoff]:
            v = h.pop(hr)
            day = str(int(hr) - int(hr) % DAY)
            d = tr["pool_daily"].setdefault(k, {})
            cur = d.get(day, [0.0, 0.0, 0.0])
            d[day] = [round(cur[0] + v[0], 4), v[1], round(cur[2] + v[2], 6)]
        if not h:
            del tr["pool_hist"][k]
    dcut = now - DAILY_DAYS * DAY
    for k, d in list(tr["pool_daily"].items()):
        for day in [x for x in d if int(x) < dcut]:
            d.pop(day)
        if not d:
            del tr["pool_daily"][k]


def repair_broken_prices(tr, now, log, limit=1e7):
    """Removes what fees priced with a broken price left in the simulation (30 Oct 2026: WBTC/USDT on Ethereum).

    No saved allocation of a few hundred thousand sAERO earns or expects 10 million USD: those snapshots, hours and
    pool buckets go, and the virtual positions that moved on such a forecast may move again on the next run.
    """
    big = lambda *xs: any(isinstance(x, (int, float)) and x > limit for x in xs)
    n = {"open": 0, "closed": 0, "perf": 0, "pool": 0, "virtual": 0}
    for st, snaps in tr["open"].items():
        keep = [x for x in snaps if not big(x[2], x[3]) and not any(big(r[1], r[2]) for r in x[1].values())]
        n["open"] += len(snaps) - len(keep)
        tr["open"][st] = keep
    for st, rows in tr["closed"].items():
        keep = [c for c in rows if not big(c[1], c[2])]
        n["closed"] += len(rows) - len(keep)
        tr["closed"][st] = keep
    for st, hours in tr.get("perf", {}).items():
        for h in [h for h, v in hours.items() if big(v[0])]:
            del hours[h]
            n["perf"] += 1
    for name, col in (("pool_hist", (0, 2)), ("pool_daily", (0, 2)), ("pool_reco", (1, 2))):
        for k, d in list(tr.get(name, {}).items()):
            for t in [t for t, v in d.items() if big(*(v[i] for i in col))]:
                del d[t]
                n["pool"] += 1
            if not d:
                del tr[name][k]
    for v in tr.get("virtual", {}).values():
        if big(v.get("pred")):
            v["until"] = now
            v["moves"] = max(0, v.get("moves", 1) - 1)
            n["virtual"] += 1
    log("tracker: removed what broken prices left: " + ", ".join(f"{k} {x}" for k, x in n.items()))
    return n


def _reco_bucket(tr, k, day):
    return tr["pool_reco"].setdefault(k, {}).setdefault(day, [0.0, 0.0, 0.0, 0])


def accrue(tr, state, prices, fresh_fees, now, cooldown):
    last = tr.get("last") or 0
    tr["last"] = now
    if not last or now <= last:
        return {}
    rev = interval_revenue(state, prices, fresh_fees, last, now)
    ap = state["ap"]
    _record_market(tr, state, rev, now)
    dt = now - last

    def earned(alloc):
        tot, per = 0.0, {}
        for k, v in alloc.items():
            r = rev.get(k)
            if not r:
                continue
            x = v[0] if isinstance(v, list) else v
            e = r * x / (float(ap.get(k, 0.0)) + x)
            per[k] = e
            tot += e
        return tot, per

    perf = tr.setdefault("perf", {})
    hour = str(now - now % HOUR)
    for s in STRATEGIES:
        still = []
        opened = tr["open"].get(s, [])
        got = 0.0
        for snap in opened:
            t, alloc, pred, real = snap[:4]
            opt_at = snap[4] if len(snap) > 4 else 0
            tot, per = earned(alloc)
            got += tot
            for k, e in per.items():
                alloc[k][2] = round(alloc[k][2] + e, 6)
            real = round(real + tot, 6)
            if now >= t + cooldown:
                day = str(t - t % DAY)
                for k, (x, pg, rg) in alloc.items():
                    b = _reco_bucket(tr, k, day)
                    b[0] += x
                    b[1] = round(b[1] + rg, 6)
                    b[2] = round(b[2] + pg, 6)
                    b[3] += 1
                tr["closed"].setdefault(s, []).append([t, round(pred, 4), round(real, 4), len(alloc), opt_at])
            else:
                still.append([t, alloc, pred, real, opt_at])
        tr["open"][s] = still
        # what all the allocations saved every run earned in this interval, and for how many position-seconds
        if opened:
            b = perf.setdefault(s, {}).setdefault(hour, [0.0, 0.0])
            b[0] = round(b[0] + got, 6)
            b[1] = round(b[1] + len(opened) * dt, 1)
        pcut = now - PERF_DAYS * DAY
        perf[s] = {h: v for h, v in (perf.get(s) or {}).items() if int(h) >= pcut}
        cut = now - CLOSED_DAYS * DAY
        tr["closed"][s] = [c for c in tr["closed"].get(s, []) if c[0] >= cut]
        v = tr["virtual"].get(s)
        if v:
            tot, _ = earned(v["alloc"])
            v["usd"] = round(v["usd"] + tot, 6)
            hour = str(now - now % HOUR)
            v["curve"][hour] = round(v["usd"], 4)
            v["hours"] = round(v.get("hours", 0) + dt / HOUR, 4)
    dcut = now - DAILY_DAYS * DAY
    for k, d in list(tr["pool_reco"].items()):
        for day in [x for x in d if int(x) < dcut]:
            d.pop(day)
        if not d:
            del tr["pool_reco"][k]
    return rev


def snapshot(tr, state, prices, now, cooldown, max_pools):
    """Records the recommendation of every strategy; the virtual positions rebalance when their cooldown ends."""
    latest = {}
    opts = options()
    opt_at = int(OPTIONS.get("at") or 0)
    for s in STRATEGIES:
        cands = candidates(state, prices, now, cooldown, s, opts)
        alloc, pred = recommend(cands, tr["size"], max_pools, cap=opts["cap"], min_pct=opts["min"])
        rows = {k: [round(x, 2), round(cands[k][0] * x / (cands[k][1] + x), 6), 0.0] for k, x in alloc.items()}
        if rows:
            tr["open"].setdefault(s, []).append([now, rows, round(pred, 6), 0.0, opt_at])
        latest[s] = {"pred": pred, "alloc": alloc, "cands": cands}
        v = tr["virtual"].get(s)
        if alloc and (not v or now >= v["until"]):
            if not v:
                v = tr["virtual"][s] = {"since": now, "usd": 0.0, "curve": {}, "hours": 0.0, "moves": 0}
            v["alloc"] = {k: round(x, 2) for k, x in alloc.items()}
            v["until"] = now + cooldown
            v["pred"] = round(pred, 4)
            v["moves"] = v.get("moves", 0) + 1
        if v:
            cut = str(now - DAILY_DAYS * DAY)
            v["curve"] = {h: c for h, c in v["curve"].items() if h >= cut}
    return latest


# ---------------------------------------------------------------- public summary

def _pct(x, size):
    """Share of the position in whole percent: allocations are built in 1% slices, so these add up to 100."""
    return int(round(x / size * 100)) if size else 0


def _chain_split(alloc, size):
    """sAERO and percent per chain of an allocation, largest first."""
    out = {}
    for k, x in alloc.items():
        name = C.chain_short(A.chain_of(k))
        out[name] = out.get(name, 0) + (x[0] if isinstance(x, list) else x)
    return [[n, round(v), _pct(v, size)] for n, v in sorted(out.items(), key=lambda kv: -kv[1])]


def _pool_list(state, alloc, size, cands=None):
    """Every pool of an allocation with its share in percent, largest first: what to type in the allocation form."""
    rows = []
    for k, x in alloc.items():
        x = x[0] if isinstance(x, list) else x
        row = {"n": A.gauge_name(state, k), "c": C.chain_short(A.chain_of(k)), "p": _pct(x, size), "x": round(x)}
        m = state["gauges"].get(k) or {}
        if m.get("kind") and m["kind"] != "?":
            row["k"] = m["kind"]
        if cands and k in cands:
            r, a = cands[k]
            row["usd"] = round(r * x / (a + x), 2)
        rows.append(row)
    rows.sort(key=lambda r: (-r["p"], -r["x"], r["n"]))
    return rows


def _sum_hist(tr, k, start, now):
    rev = m = 0.0
    for hr, v in (tr["pool_hist"].get(k) or {}).items():
        if int(hr) >= start:
            rev += v[0]
            m += v[2]
    for day, v in (tr["pool_daily"].get(k) or {}).items():
        if int(day) >= start - start % DAY and int(day) + DAY <= now:
            if int(day) >= start:
                rev += v[0]
                m += v[2]
    return rev, m


def _perf(tr, s, now, cooldown, size, aero_usd):
    """How the allocations saved every run (about every 15 minutes) really paid, in USD a day for one position.

    24 hours and 7 days: revenue that reached every allocation still in its cooldown during that time, divided by
    the time each was held. Since the start: every saved allocation's revenue over its age.
    """
    hours = sorted((int(h), v) for h, v in ((tr.get("perf") or {}).get(s) or {}).items())

    def window(span, need_h):
        inc = pos = 0.0
        covered = 0
        for h, (i, ps) in hours:
            if h >= now - span:
                inc += i
                pos += ps
                covered += 1 if ps > 0 else 0
        return (round(inc / pos * DAY, 2) if pos > 0 and covered >= need_h else None), covered

    def apr(rate):
        return round(rate * 365 / (size * aero_usd) * 100, 2) if rate is not None and aero_usd and size else None

    r24, h24 = window(DAY, 3)
    r7, h7 = window(7 * DAY, 24)
    real = age = pred = 0.0
    saved = 0
    for snap in tr["open"].get(s, []):
        a = max(0, now - snap[0])
        real += snap[3]
        age += a
        pred += snap[2] * a / cooldown
        saved += 1
    for c in tr["closed"].get(s, []):
        real += c[2]
        age += cooldown
        pred += c[1]
        saved += 1
    r_all = round(real / age * DAY, 2) if age >= 3 * HOUR else None
    series = []
    for i, (h, _) in enumerate(hours):
        if h < now - 7 * DAY:
            continue
        inc = pos = 0.0
        for h2, (a, b) in hours[max(0, i - 23):i + 1]:
            if h2 > h - DAY:
                inc += a
                pos += b
        if pos > 0:
            series.append([h, round(inc / pos * DAY, 2)])
    return {"rate_24h": r24, "hours_24h": h24, "rate_7d": r7, "hours_7d": h7, "rate_all": r_all,
            "apr_24h": apr(r24), "apr_7d": apr(r7), "apr_all": apr(r_all),
            "real_vs_pred_all": round(real / pred, 3) if pred > 0 and age >= 3 * HOUR else None,
            "saved": saved, "series": series}


def summary(tr, state, now, aero_usd, cooldown, latest):
    size = tr["size"]
    collected_h = (now - tr["start"]) / HOUR

    def apr(usd_per_day, amount):
        if not aero_usd or not amount:
            return None
        return round(usd_per_day * 365 / (amount * aero_usd) * 100, 2)

    strategies = []
    for s, label in STRATEGIES.items():
        v = tr["virtual"].get(s) or {}
        curve = sorted((int(h), c) for h, c in (v.get("curve") or {}).items())

        def gained_since(t):
            past = [c for h, c in curve if h <= t]
            base = past[-1] if past else 0.0
            return (v.get("usd", 0.0) - base) if curve else 0.0

        hrs = v.get("hours", 0.0)
        # fees reach the pools in lumps (many pools get them in a few hours a day): a daily rate needs a full day
        day_rate = v.get("usd", 0.0) / (hrs / 24) if hrs >= 24 else None
        closed = [c for c in tr["closed"].get(s, []) if c[0] >= now - 7 * DAY]
        ratio = [c[2] / c[1] for c in closed if c[1] > 0]
        reco = latest.get(s) or {}
        alloc = sorted((reco.get("alloc") or {}).items(), key=lambda kv: -kv[1])
        cands = reco.get("cands") or {}
        strategies.append({
            "id": s, "label": label,
            "usd_total": round(v.get("usd", 0.0), 2), "hours": round(hrs, 1),
            "usd_24h": round(gained_since(now - DAY), 2) if curve else None,
            "usd_day": round(day_rate, 2) if day_rate is not None else None,
            "apr": apr(day_rate, size) if day_rate is not None else None,
            "moves": v.get("moves", 0), "until": v.get("until"),
            "holding": [[A.pool_label(state, k), round(x), _pct(x, size)] for k, x in
                        sorted((v.get("alloc") or {}).items(), key=lambda kv: -kv[1])[:20]],
            "hold": _pool_list(state, v.get("alloc") or {}, size),
            "chains": _chain_split(v.get("alloc") or {}, size),
            "curve": [[h, round(c, 2)] for h, c in curve][-24 * 30:],
            "perf": _perf(tr, s, now, cooldown, size, aero_usd),
            "closed_7d": len(closed),
            "real_vs_pred": round(statistics.median(ratio), 3) if ratio else None,
            "avg_real_47h": round(statistics.mean(c[2] for c in closed), 2) if closed else None,
            "now": {"pred_47h": round(reco.get("pred", 0.0), 2),
                    "apr": apr(reco.get("pred", 0.0) / (cooldown / DAY), size),
                    "chains": _chain_split(dict(alloc), size),
                    "reco": _pool_list(state, dict(alloc), size, cands),
                    "pools": [[A.pool_label(state, k), round(x),
                               round(cands[k][0] * x / (cands[k][1] + x), 2), round(cands[k][1]), _pct(x, size)]
                              for k, x in alloc if k in cands]},
        })

    # pools over time: what a marginal 1000 sAERO earned, and how recommendations there really paid
    pools = []
    keys = set(tr["pool_hist"]) | set(tr["pool_daily"]) | set(tr["pool_reco"])
    for k in keys:
        row = {"name": A.gauge_name(state, k), "key": k, "chain": C.chain_short(A.chain_of(k)),
               "ap": round(state["ap"].get(k, 0.0))}
        any_data = False
        for label, span in (("24h", DAY), ("7d", 7 * DAY), ("30d", 30 * DAY)):
            start = max(now - span, tr["start"])
            days = (now - start) / DAY
            if days <= 0:
                continue
            rev, m = _sum_hist(tr, k, start, now)
            row[f"m1000_{label}"] = round(m / days, 4) if days >= 0.25 else None
            row[f"rev_{label}"] = round(rev, 2)
            any_data = any_data or rev > 0
        reco = tr["pool_reco"].get(k) or {}
        week = [b for d, b in reco.items() if int(d) >= now - 7 * DAY]
        if week:
            amt = sum(b[0] for b in week)
            real = sum(b[1] for b in week)
            pred = sum(b[2] for b in week)
            row["reco_n_7d"] = sum(b[3] for b in week)
            row["reco_real_1000_day"] = round(real / amt * 1000 / (cooldown / DAY), 4) if amt else None
            row["reco_real_vs_pred"] = round(real / pred, 3) if pred else None
            any_data = True
        open_n = sum(1 for s in STRATEGIES for snap in tr["open"].get(s, []) if k in snap[1])
        row["reco_open"] = open_n
        if any_data or open_n:
            for label in ("24h", "7d", "30d"):
                v = row.get(f"m1000_{label}")
                row[f"apr_{label}"] = apr(v, 1000) if v is not None else None
            pools.append(row)
    pools.sort(key=lambda r: -(r.get("m1000_7d") or r.get("m1000_24h") or 0))
    universe, ok = MD.new_chain_universe(state, RULES.get("nuove"))
    rules = {**MD.NEW_CHAIN_RULES, **(RULES.get("nuove") or {})}
    mk = MD.market_state(state)
    new_chains = {"chains": [C.chain_short(c) for c in sorted(ok)], "pools": len(universe),
                  "shares": {C.chain_short(c): (round(MD.chain_share(mk, c), 4)
                                                if MD.chain_share(mk, c) is not None else None) for c in sorted(ok)},
                  "max_share": rules["max_chain_share"], "pairs_per_chain": rules["pairs_per_chain"],
                  "min_pair_volume_usd": rules["min_pair_volume_usd"], "max_pair_share": rules["max_pair_share"]}
    # the ranking: the longest window every method has data for
    def best(st):
        p = st["perf"]
        return p["rate_7d"] if rank_by == "7d" else p["rate_24h"] if rank_by == "24h" else p["rate_all"]
    perfs = [st["perf"] for st in strategies]
    rank_by = ("7d" if all(p["rate_7d"] is not None for p in perfs) else
               "24h" if all(p["rate_24h"] is not None for p in perfs) else "all")
    ranking = [st["id"] for st in sorted(strategies, key=lambda st: -(best(st) or -1e9))]
    opts = options()
    return {"generated": now, "size": size, "start": tr["start"], "collected_h": round(collected_h, 1),
            "options": {**opts, "at": OPTIONS.get("at")} if OPTIONS else None,
            "ranking": ranking, "rank_by": rank_by,
            "new_chains": new_chains,
            "cooldown_h": round(cooldown / HOUR, 2), "aero_usd": aero_usd, "strategies": strategies,
            "pools": pools[:150]}
