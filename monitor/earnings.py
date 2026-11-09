"""What every sAERO position really earns: fees and incentives of its pools, split by its share of each pool.

Every run, the revenue of each gauge since the last run is divided among the positions allocated to it, in
proportion to their allocation over the pool's total allocation. Positions are keyed by number, across every chain
they allocate on. Nothing here knows who owns a position: the page finds the viewer's positions from their address
and reads their numbers from the public export.

Fees. Aero's rewards manager credits a pool's fees to the votes allocated while they accrue: every allocation change
in the pool first credits what the gauge has pending until then, at the votes of that moment. The fees reach the
rewards manager (FeesCollected) only when someone claims, which on a quiet pool can be days later and all at once.
So the fees are measured on the gauge every run (aero.settle_accrual: pending now, minus pending at the last run,
plus what was collected in between) and credited for the run they accrued in. Incentives stream by their schedule.
"""
from . import aero as A

HOUR = 3600
DAY = 86400
HOURS = 25                  # hourly buckets for the last day: windows of 1, 6, 12 and 24 hours
WINDOWS = (1, 6, 12, 24)
KEEP_D = 35 * DAY
BACKFILL = 7 * DAY
SINCE_MAX = 4 * DAY         # earnings since the last allocation are exported when it is at most this old


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
        hours = {h: v for h, v in A.fee_hours(state, g).items() if lo <= h < now}
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


def interval_revenue(state, prices, fresh_fees, start, end):
    """{gauge: USD earned by its allocators between start and end}: the fees measured on the gauge in this run
    (aero.settle_accrual) and the incentives streamed."""
    rev = {}
    for ts, k, usd in fresh_fees:
        if start - 2 * HOUR < ts <= end:
            rev[k] = rev.get(k, 0.0) + usd
    for k in {p["g"] for p in state["incentives"].values()}:
        r = A.incentive_usd(state, prices, k, start, end)
        if r > 0:
            rev[k] = rev.get(k, 0.0) + r
    return rev


def accrue(state, prices, fresh_fees, now, log):
    """fresh_fees: [(time, gauge, USD)] fees measured on the gauges in this run (aero.settle_accrual)."""
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
    pe.pop("est", None)             # estimates of the fees between two claims: the gauges are measured now
    pe.pop("est_v", None)
    revenue = interval_revenue(state, prices, fresh_fees, last, now)
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


def rebuild_hours(state, prices, now, log):
    """Once, when the fees of the last two days have been measured on the gauges (aero.backfill_accrual): the
    hourly buckets of the last day are credited again from them, replacing what the old estimate gave, for every
    position and hour whose allocation is known (from the first full hour after its latest allocation change, on
    chains measured that far back). Total, days and pools move by the same difference."""
    pe = _book(state)
    h0 = int(pe.get("h0") or 0)
    if not h0:
        return
    gauges = _by_gauge(state)
    alloc, last_of = {}, {}
    for cid, cs in state["chains"].items():
        for tid, t in (cs.get("tokens") or {}).items():
            last_of[tid] = max(last_of.get(tid, 0), int(t.get("last") or 0))
            for g, a in (t.get("a") or {}).items():
                if a > 0:
                    alloc.setdefault(tid, {})[g] = float(a)
    measured_from = {cid: int(cs.get("acc_from") or now) for cid, cs in state["chains"].items()}
    fees, inc = {}, {}

    def earned(g, h):
        if g not in fees:
            fees[g] = A.fee_hours(state, g)
        if (g, h) not in inc:
            inc[(g, h)] = A.incentive_usd(state, prices, g, h, min(now, h + HOUR))
        return fees[g].get(h, 0.0) + inc[(g, h)]

    gi = pe.setdefault("_gi", {k: i for i, k in enumerate(pe["gi"])})
    changed, moved = 0, 0.0
    for tid, al in alloc.items():
        last = last_of.get(tid, 0)
        measured = max(measured_from[str(A.chain_of(g))] for g in al)
        p = pe["p"].get(tid)
        for i in range(HOURS):
            h = h0 - i * HOUR
            if h < measured or h + HOUR <= last:
                break
            end = min(now, h + HOUR)
            # in the hour of its latest allocation the position held the new one only from then on: before it, as
            # much as it earned in the hour before
            part = 1.0
            before = 0.0
            if last > h:
                part = max(0.0, (end - last) / max(1, end - h))
                hh = (p or {}).get("h") or []
                before = (hh[i + 1] if len(hh) > i + 1 else 0.0) * (last - h) / HOUR
            per = {}
            for g, a in al.items():
                tot = _total(state, g, gauges.get(g) or [], h)
                if tot > 0:
                    per[g] = earned(g, h) * a / tot * part
            new = sum(per.values()) + before
            if p is None:
                for g, usd in per.items():
                    _credit(pe, tid, g, usd, h, h)
                p = pe["p"].get(tid)
                changed += 1 if p else 0
                moved += new
                continue
            hh = p.setdefault("h", [])
            if len(hh) <= i:
                hh.extend([0.0] * (i + 1 - len(hh)))
            diff = new - hh[i]
            if abs(diff) < 1e-6:
                continue
            hh[i] = round(new, 5)
            p["t"] = round(max(0.0, p["t"] + diff), 5)
            d = str(h - h % DAY)
            p["d"][d] = round(max(0.0, p["d"].get(d, 0.0) + diff), 5)
            weights = per if new > 0 else {g: a for g, a in al.items()}
            wsum = sum(weights.values()) or 1.0
            for g, w in weights.items():
                if g not in gi:
                    gi[g] = len(pe["gi"])
                    pe["gi"].append(g)
                k = str(gi[g])
                p["g"][k] = round(max(0.0, p["g"].get(k, 0.0) + diff * w / wsum), 5)
            changed += 1
            moved += diff
    pe.pop("_gi", None)
    log(f"earnings: last day credited again from the measured fees, {changed} position-hours changed, "
        f"{moved:+,.0f} USD in all")


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


def since(p, t0, now, h0):
    """USD credited to the position since t0: the hourly buckets for the last day, before that the daily ones spread
    evenly over their day. None when t0 is more than SINCE_MAX ago."""
    if not t0 or t0 >= now or t0 < now - SINCE_MAX:
        return None
    usd = 0.0
    for i, v in enumerate(p.get("h") or []):
        b = h0 - i * HOUR
        lo, hi = max(b, t0), min(b + HOUR, now)
        if hi > lo and v:
            usd += v * (hi - lo) / max(1, min(b + HOUR, now) - b)
    hs = h0 - (HOURS - 1) * HOUR                      # where the hourly buckets start
    if t0 < hs:
        for d, v in (p.get("d") or {}).items():
            lo, hi = max(int(d), t0), min(int(d) + DAY, hs)
            if hi > lo and v:
                usd += v * (hi - lo) / DAY
    return usd


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
