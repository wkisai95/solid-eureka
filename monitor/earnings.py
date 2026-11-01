"""What every sAERO position really earns: fees and incentives of its pools, split by its share of each pool.

Every run, the revenue that reached each gauge (new fee transfers and streamed incentives) is divided among the
positions allocated to it, in proportion to their allocation over the pool's total allocation. Positions are keyed
by number, across every chain they allocate on. Nothing here knows who owns a position: the page finds the
viewer's positions from their address and reads their numbers from the public export.
"""
from . import aero as A

HOUR = 3600
DAY = 86400
HOURS = 25                  # hourly buckets for the last day: windows of 1, 6, 12 and 24 hours
WINDOWS = (1, 6, 12, 24)
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
        p = pe["p"][tid] = {"s": int(since), "t": 0.0, "h": [], "d": {}, "g": {}}
    gi = pe.setdefault("_gi", {k: i for i, k in enumerate(pe["gi"])})
    if g not in gi:
        gi[g] = len(pe["gi"])
        pe["gi"].append(g)
    p["t"] = round(p["t"] + usd, 5)
    _hour_add(pe, p, ts, usd)
    d, i = str(ts - ts % DAY), str(gi[g])
    p["d"][d] = round(p["d"].get(d, 0.0) + usd, 5)
    p["g"][i] = round(p["g"].get(i, 0.0) + usd, 5)


def _hour_add(pe, p, ts, usd):
    """p["h"][i] is the hour that starts i hours before pe["h0"], the current hour."""
    i = (int(pe.get("h0") or 0) - (ts - ts % HOUR)) // HOUR
    if 0 <= i < HOURS:
        h = p.setdefault("h", [])
        if len(h) <= i:
            h.extend([0.0] * (i + 1 - len(h)))
        h[i] = round(h[i] + usd, 5)


def _advance(pe, now):
    """Moves the hourly buckets so that index 0 is the current hour."""
    h0 = now - now % HOUR
    k = (h0 - int(pe.get("h0") or h0)) // HOUR
    pe["h0"] = h0
    if k <= 0:
        return
    for p in pe["p"].values():
        keep = (p.get("h") or [])[:max(0, HOURS - k)]
        p["h"] = [0.0] * k + keep if any(keep) else []


def _total(state, g, members, hour=None):
    """The pool's whole allocation: the monitor's figure, never below what the positions it knows add up to."""
    ap = state["ap"].get(g, 0.0)
    if hour is not None:
        ap = (state.get("ap_hist") or {}).get(str(hour), {}).get(g, ap)
    return max(float(ap), sum(a for _, a, _ in members))


def _backfill(state, prices, pe, now, log, hours_only=False):
    """First run: the fee hours already collected, for the hours each position has kept its current allocation.

    hours_only: rebuilds just the hourly buckets of the last day (totals, days and pools stay as they are)."""
    start = max(int(state.get("fee_complete_from") or 0), now - (HOURS * HOUR if hours_only else BACKFILL))
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
                    if hours_only:
                        if tid in pe["p"]:
                            _hour_add(pe, pe["p"][tid], h, usd * a / tot)
                    else:
                        _credit(pe, tid, g, usd * a / tot, h, since)
                    credited += 1
    log(f"earnings: {'hourly buckets rebuilt' if hours_only else 'first run'}, {len(pe['p'])} positions credited from the last {(now - start) / HOUR:.0f} hours "
        f"({credited} pool-hours)")


def accrue(state, prices, revenue, now, log):
    """revenue: {gauge: USD that reached its allocators since the last run} (tracker.interval_revenue)."""
    pe = _book(state)
    last = int(pe.get("last") or 0)
    pe["last"] = now
    _advance(pe, now)
    if not last:
        pe["from"] = max(int(state.get("fee_complete_from") or 0), now - BACKFILL)
        _backfill(state, prices, pe, now, log)
        pe.pop("_gi", None)
        return pe
    if not pe.get("hourly"):
        # books started with 6-hour buckets: rebuild the last day hour by hour from the fees already collected
        for p in pe["p"].values():
            p.pop("q", None)
            p["h"] = []
        _backfill(state, prices, pe, last, log, hours_only=True)
        pe["hourly"] = True
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
    dcut = now - KEEP_D
    for tid, p in list(pe["p"].items()):
        p["d"] = {k: v for k, v in p["d"].items() if int(k) >= dcut}
        if not p["d"]:
            del pe["p"][tid]
    return pe


def windows(p, now, h0):
    """{hours: (USD, seconds)} for the last 1, 6, 12 and 24 hours, plus 7 days under 168.

    The oldest hourly bucket of a window counts for the part of it inside the window; the seconds start from when
    the position started earning if that is later."""
    s = int(p["s"])
    h = p.get("h") or []
    out = {}
    for n in WINDOWS:
        start = now - n * HOUR
        usd = 0.0
        for i, v in enumerate(h[:n + 2]):
            b = h0 - i * HOUR
            lo, hi = max(b, start), min(b + HOUR, now)
            if hi > lo and v:
                usd += v * (hi - lo) / max(1, min(b + HOUR, now) - b)
        out[n] = (usd, max(0, now - max(start, s)))
    d0 = now - now % DAY - 6 * DAY
    out[168] = (sum(v for k, v in p["d"].items() if int(k) >= d0), max(0, now - max(d0, s)))
    return out


def summary(p, now, keys, h0):
    """[since, total, USD 24 h, its seconds, USD 7 days, its seconds, [[day, USD], ...] 14 days, {gauge: USD},
    [[hours, USD, seconds], ...] for 1, 6, 12 and 24 hours].

    keys: the book's gauge keys (gauge_keys), which the position's pools refer to by index; h0: the book's
    current hour."""
    w = windows(p, now, h0)
    days = sorted((int(k), round(v, 2)) for k, v in p["d"].items() if int(k) + DAY > now - 14 * DAY)
    return [p["s"], round(p["t"], 2), round(w[24][0], 2), w[24][1], round(w[168][0], 2), w[168][1],
            [list(x) for x in days], {keys[int(i)]: round(v, 2) for i, v in p["g"].items() if int(i) < len(keys)},
            [[n, round(w[n][0], 4), w[n][1]] for n in WINDOWS]]
