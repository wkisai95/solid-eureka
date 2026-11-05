"""What every sAERO position really earns: fees and incentives of its pools, split by its share of each pool.

Every run, the revenue of each gauge since the last run is divided among the positions allocated to it, in
proportion to their allocation over the pool's total allocation. Positions are keyed by number, across every chain
they allocate on. Nothing here knows who owns a position: the page finds the viewer's positions from their address
and reads their numbers from the public export.

Fees. Aero's rewards manager credits a pool's fees to the votes allocated while they accrue: every allocation change
in the pool first credits what accrued until then, at the votes of that moment. The fees themselves reach the
contract (FeesCollected) only when someone collects them, every few hours on some chains. So where the pool's hourly
volume is known, its fees are estimated as they accrue (volume x the pool's fees per dollar traded over the last 48
hours) and the transfers only add what the estimate missed; elsewhere the transfers are credited as they arrive.
Incentives stream by their schedule.
"""
from . import aero as A
from . import marketdata as MD

HOUR = 3600
DAY = 86400
HOURS = 25                  # hourly buckets for the last day: windows of 1, 6, 12 and 24 hours
WINDOWS = (1, 6, 12, 24)
KEEP_D = 35 * DAY
BACKFILL = 7 * DAY
EST_V = 1                   # version of the fee estimate; a new one sets the uncollected estimates up again


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


def _vol_between(vser, start, end):
    """Volume traded between start and end, from the hourly snapshots: the one taken in an hour is the volume of the
    60 minutes before it, so an hour is best covered by the next hour's snapshot, then by its own, then by the last
    one before it."""
    tot, t = 0.0, start
    known = sorted(int(h) for h in vser)
    while t < end:
        h = t - t % HOUR
        nxt = min(end, h + HOUR)
        snap = vser.get(str(h + HOUR)) or vser.get(str(h))
        if snap is None:
            prev = [x for x in known if x <= h]
            snap = vser.get(str(prev[-1])) if prev and h - prev[-1] <= 3 * HOUR else None
        tot += float((snap or [0])[0] or 0) * (nxt - t) / HOUR
        t = nxt
    return tot


def _fee_rate(state, g, now):
    """(fees per dollar traded over the last 48 hours, the pool's volume snapshots, fees of the last 48 hours)."""
    vser = (MD.market_state(state)["hist"]["pools"]).get(g) or {}
    if not vser:
        return 0.0, {}, 0.0
    _, v48 = MD.hourly_volume(state, g, now)
    f48 = A.fees_usd(state, g, now - 48 * HOUR, now + HOUR)
    return (min(0.01, f48 / v48) if v48 and f48 > 0 else 0.0), vser, f48


def interval_revenue(state, prices, fresh_fees, start, end, pe):
    """{gauge: USD credited to its allocators between start and end}; pe["est"] keeps, per gauge, the fees estimated
    and credited that no transfer has brought yet."""
    est_left = pe.setdefault("est", {})
    got = {}
    for ts, k, usd in fresh_fees:
        if start - 2 * HOUR < ts <= end:
            got[k] = got.get(k, 0.0) + usd
    rev = {}
    for g in set(_by_gauge(state)) | set(got):
        fpv, vser, f48 = _fee_rate(state, g, end)
        est = _vol_between(vser, start, end) * fpv if fpv > 0 else 0.0
        left = est_left.get(g, 0.0) + est
        arrived = got.get(g, 0.0)
        extra = max(0.0, arrived - left)          # what the estimate missed: credited now
        left = min(max(0.0, left - arrived), max(f48, 1.0))
        if left > 0.005:
            est_left[g] = round(left, 4)
        else:
            est_left.pop(g, None)
        if est + extra > 0:
            rev[g] = est + extra
    for k in {p["g"] for p in state["incentives"].values()}:
        r = A.incentive_usd(state, prices, k, start, end)
        if r > 0:
            rev[k] = rev.get(k, 0.0) + r
    return rev


def _setup_estimates(state, pe, now, log):
    """Once: the fees that accrued since each pool's last transfer, estimated hour by hour from its volume and
    credited to the positions allocated in it for the hours they were there; they are what the next transfer
    brings, so they are kept as already credited."""
    gauges = _by_gauge(state)
    est_left, credited = pe.setdefault("est", {}), 0.0
    for g, members in gauges.items():
        fpv, vser, f48 = _fee_rate(state, g, now)
        if fpv <= 0:
            continue
        hours = [int(h) for h, v in (state["fees"].get(g) or {}).items() if v > 0 and int(h) >= now - 48 * HOUR]
        if not hours:
            continue
        t0 = max(hours) + HOUR // 2                 # the last transfer, within its hour
        left = 0.0
        t = t0
        while t < now:
            nxt = min(now, t - t % HOUR + HOUR)
            usd = _vol_between(vser, t, nxt) * fpv
            left += usd
            tot = _total(state, g, members, t - t % HOUR)
            if usd > 0 and tot > 0:
                for tid, a, last in members:
                    if last <= t:
                        _credit(pe, tid, g, usd * a / tot, t, t)
                        credited += usd * a / tot
            t = nxt
        if left > 0.005:
            est_left[g] = round(min(left, max(f48, 1.0)), 4)
    pe["est_v"] = EST_V
    log(f"earnings: fees accrued since the last transfer estimated in {len(est_left)} pools, ${credited:,.2f} credited")


def accrue(state, prices, fresh_fees, now, log):
    """fresh_fees: [(time, gauge, USD)] fee transfers priced in this run (aero.settle_rewards)."""
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
    if pe.get("est_v") != EST_V:
        _setup_estimates(state, pe, last, log)
    revenue = interval_revenue(state, prices, fresh_fees, last, now, pe)
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


def since(p, t0, now, h0):
    """USD credited to the position since t0, from its hourly buckets; None when t0 is older than they reach."""
    if not t0 or t0 < h0 - (HOURS - 1) * HOUR or t0 >= now:
        return None
    usd = 0.0
    for i, v in enumerate(p.get("h") or []):
        b = h0 - i * HOUR
        lo, hi = max(b, t0), min(b + HOUR, now)
        if hi > lo and v:
            usd += v * (hi - lo) / max(1, min(b + HOUR, now) - b)
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
