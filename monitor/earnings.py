"""What every sAERO position really earns: fees and incentives of its pools, split by its share of each pool.

Every run, the revenue that reached each gauge (new fee transfers and streamed incentives) is divided among the
positions allocated to it, in proportion to their allocation over the pool's total allocation. Positions are keyed
by number, across every chain they allocate on. Nothing here knows who owns a position: the page finds the
viewer's positions from their address and reads their numbers from the public export.
"""
from . import aero as A

HOUR = 3600
DAY = 86400
QUARTER = 6 * HOUR          # 6-hour buckets for the last 48 hours: enough for a 24-hour figure, small for 14k positions
KEEP_Q = 2 * DAY
KEEP_D = 35 * DAY
BACKFILL = 7 * DAY


def _book(state):
    pe = state.get("earnings")
    if not isinstance(pe, dict) or "p" not in pe:
        pe = state["earnings"] = {"last": 0, "p": {}}
    pe.setdefault("gi", [])          # gauge keys; positions refer to them by index to keep the state small
    return pe


def gauge_keys(pe):
    return pe.get("gi") or []


def _by_gauge(state):
    """{gauge: [(position number, allocation, last move)]} on every chain."""
    out = {}
    for cs in state["chains"].values():
        for tid, t in (cs.get("tokens") or {}).items():
            for g, a in (t.get("a") or {}).items():
                if a > 0:
                    out.setdefault(g, []).append((tid, float(a), int(t.get("last") or 0)))
    return out


def _credit(pe, tid, g, usd, ts, since):
    if usd <= 0:
        return
    p = pe["p"].get(tid)
    if p is None:
        p = pe["p"][tid] = {"s": int(since), "t": 0.0, "q": {}, "d": {}, "g": {}}
    gi = pe.setdefault("_gi", {k: i for i, k in enumerate(pe["gi"])})
    if g not in gi:
        gi[g] = len(pe["gi"])
        pe["gi"].append(g)
    p["t"] = round(p["t"] + usd, 5)
    q, d, i = str(ts - ts % QUARTER), str(ts - ts % DAY), str(gi[g])
    p["q"][q] = round(p["q"].get(q, 0.0) + usd, 5)
    p["d"][d] = round(p["d"].get(d, 0.0) + usd, 5)
    p["g"][i] = round(p["g"].get(i, 0.0) + usd, 5)


def _total(state, g, members, hour=None):
    """The pool's whole allocation: the monitor's figure, never below what the positions it knows add up to."""
    ap = state["ap"].get(g, 0.0)
    if hour is not None:
        ap = (state.get("ap_hist") or {}).get(str(hour), {}).get(g, ap)
    return max(float(ap), sum(a for _, a, _ in members))


def _backfill(state, prices, pe, now, log):
    """First run: the fee hours already collected, for the hours each position has kept its current allocation."""
    start = max(int(state.get("fee_complete_from") or 0), now - BACKFILL)
    gauges = _by_gauge(state)
    credited = 0
    for g, members in gauges.items():
        cs = (state["chains"].get(str(A.chain_of(g))) or {})
        lo = max(start, int(cs.get("fee_from") or now))
        hours = {int(h): v for h, v in (state["fees"].get(g) or {}).items() if lo <= int(h) < now}
        inc_hours = range(lo - lo % HOUR, now, HOUR) if any(p["g"] == g for p in state["incentives"].values()) else ()
        for h in sorted(set(hours) | set(inc_hours)):
            usd = hours.get(h, 0.0) + (A.incentive_usd(state, prices, g, h, min(now, h + HOUR)) if inc_hours else 0.0)
            if usd <= 0:
                continue
            tot = _total(state, g, members, h)
            for tid, a, last in members:
                since = max(lo, last - last % HOUR + HOUR)
                if h >= since and tot > 0:
                    _credit(pe, tid, g, usd * a / tot, h, since)
                    credited += 1
    log(f"earnings: first run, {len(pe['p'])} positions credited from the last {(now - start) / HOUR:.0f} hours "
        f"({credited} pool-hours)")


def accrue(state, prices, revenue, now, log):
    """revenue: {gauge: USD that reached its allocators since the last run} (tracker.interval_revenue)."""
    pe = _book(state)
    last = int(pe.get("last") or 0)
    pe["last"] = now
    if not last:
        pe["from"] = max(int(state.get("fee_complete_from") or 0), now - BACKFILL)
        _backfill(state, prices, pe, now, log)
        pe.pop("_gi", None)
        return pe
    if now <= last:
        return pe
    gauges = _by_gauge(state)
    for g, usd in revenue.items():
        members = gauges.get(g)
        if not members or usd <= 0:
            continue
        tot = _total(state, g, members)
        for tid, a, _ in members:
            _credit(pe, tid, g, usd * a / tot, now, last)
    pe.pop("_gi", None)
    qcut, dcut = now - KEEP_Q, now - KEEP_D
    for tid, p in list(pe["p"].items()):
        p["q"] = {k: v for k, v in p["q"].items() if int(k) >= qcut - QUARTER}
        p["d"] = {k: v for k, v in p["d"].items() if int(k) >= dcut}
        if not p["d"]:
            del pe["p"][tid]
    return pe


def windows(p, now):
    """(USD, seconds) over about the last day (the last four 6-hour buckets) and the last week (seven days),
    each measured from the start of its oldest bucket or from when the position started earning."""
    s = int(p["s"])
    q0 = now - now % QUARTER - 3 * QUARTER
    d0 = now - now % DAY - 6 * DAY
    u24 = sum(v for k, v in p["q"].items() if int(k) >= q0)
    u7 = sum(v for k, v in p["d"].items() if int(k) >= d0)
    return (u24, max(0, now - max(q0, s))), (u7, max(0, now - max(d0, s)))


def summary(p, now, keys):
    """[since, total, USD ~24 h, its seconds, USD 7 days, its seconds, [[day, USD], ...] 14 days, {gauge: USD}].

    keys: the book's gauge keys (gauge_keys), which the position's pools refer to by index."""
    (u24, s24), (u7, s7) = windows(p, now)
    days = sorted((int(k), round(v, 2)) for k, v in p["d"].items() if int(k) + DAY > now - 14 * DAY)
    return [p["s"], round(p["t"], 2), round(u24, 2), s24, round(u7, 2), s7, [list(x) for x in days],
            {keys[int(i)]: round(v, 2) for i, v in p["g"].items() if int(i) < len(keys)}]
