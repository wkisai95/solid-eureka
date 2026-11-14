"""One-off repairs of what a past bug left in the state. Each runs once: state["repairs"] lists the ones done.

denim_1. Since the Denim upgrade (5 Nov 2026 18:00 UTC) Base makes a block every 200 ms instead of every 2 seconds,
and the monitor kept timing Base blocks at 2 seconds: every Base event after the switch was dated ten times too far
from the switch (three days later, a month in the future). This repair puts those times back where they belong:
  - the hourly fee buckets of the Base pools (the 48-hour fees behind every APR) and the fee transfers still waiting
    for a price;
  - the last and first allocation of the Base positions (the last one read again from the LeafVoter);
  - what the positions earned: with the fees in the future, the Base pools had almost no fees in their last 48 hours,
    so the fee estimate credited a fraction of what they made and the transfers themselves were never credited.
    What each pool's allocators earned since the switch is the fee transfers that reached it, less the fees accrued
    before the switch, plus those accrued since its last transfer; the fee model is replayed hour by hour with the
    wrong fee hours to know what it credited, and the difference is credited hour by hour (shaped like the replay
    with the right fee hours) to the positions allocated in each pool then: from their last allocation, or following
    the moves big.py recorded for the big positions. The estimate waiting for a transfer starts again from the fees
    accrued since the last one;
  - the simulation's record of what each Base pool paid, and the allocations it saved in the last cooldown.

held_1. Fees held back as too big for the pool's volume were judged every run against the last run alone while the
amount waiting grew, so three pools never got them back (USDC/SPCXc and WETH/DRV on Base, USDG/NVDA on Robinhood:
8,889 USD waiting for 18 to 35 hours). The run of 13 Nov 2026 08:51 UTC credited them and spread them over the fee
hours; the simulation's record of what those pools paid took them in that one hour, and is written again from the
fee hours (main.py, with tracker.respread).
"""
import bisect

from . import chain as C
from . import earnings as E
from . import marketdata as MD

HOUR = 3600
HELD_1 = ("8453:0x0ccf7cb21169934f4686cb895518aa179bfcea53", "8453:0x512138372510fd89240fa78d3834f01614204b63",
          "4663:0x5874f5b8728776993de4c90c61473d76eeab90db")
HELD_1_FROM = 1794427200                        # 11 Nov 2026 20:00 UTC, before the first of them began to wait
DENIM = "denim_1"
DENIM_H = C.DENIM_TS - C.DENIM_TS % HOUR       # 18:00, the first hour bucket with post-Denim times


def real_ts(t):
    """Time of a Base event that the old 2-second clock dated t."""
    t = int(t)
    return C.DENIM_TS + (t - C.DENIM_TS) // 10 if t >= C.DENIM_TS else t


def real_hour(h):
    """The real hour of an hour bucket of the old clock: its hour holds six real minutes, all in the same hour."""
    h = int(h)
    if h < DENIM_H:
        return h
    m = real_ts(h + HOUR // 2)
    return m - m % HOUR


def _vol_between(vser, start, end):
    """Volume traded between start and end, from the hourly snapshots (as the fee estimate of the time read it)."""
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


def _v48(vser, t):
    """Volume of the 48 hours before t, as marketdata.hourly_volume read it at t."""
    keys = [int(h) for h in vser if int(h) <= t]
    if not keys:
        return 0.0
    last = max(keys)
    cur, old = vser[str(last)], vser.get(str(last - 24 * HOUR))
    return float(cur[1] or 0) + float((old or cur)[1] or 0)


class _Sums:
    """Sums of hour buckets over a time range, from prefix sums."""

    def __init__(self, hours):
        self.keys = sorted(hours)
        self.acc = [0.0]
        for h in self.keys:
            self.acc.append(self.acc[-1] + hours[h])

    def between(self, lo, hi):
        return self.acc[bisect.bisect_left(self.keys, hi)] - self.acc[bisect.bisect_left(self.keys, lo)]


def _f48(sums, t, kept_from, rate):
    """Fees of the 48 hours before a run at t, as aero.fees_usd read them; hours already dropped from the state
    (before kept_from) count at the rate the pool had before the switch."""
    lo = t - 48 * HOUR
    return sums.between(lo, t + HOUR) + rate * max(0, kept_from - lo)


def replay(old, new, vser, start, end, kept_from, left0):
    """The pool's fee model hour by hour from start to end: ([(from, to, credit with the right fee hours, credit with
    the wrong ones)], estimate left waiting for a transfer at the end, by the right replay).

    old, new: {hour: USD} with the wrong and the right post-switch hours (the same hours before the switch)."""
    pre = [h for h in new if kept_from <= h < DENIM_H]
    rate = sum(new[h] for h in pre) / max(HOUR, DENIM_H - kept_from)
    s_new, s_old = _Sums(new), _Sums(old)
    out, left, t = [], left0, start
    while t < end:
        b = min(end, t - t % HOUR + HOUR)
        vol = _vol_between(vser, t, b) if vser else 0.0
        v48 = _v48(vser, b) if vser else 0.0
        f_new, f_old = _f48(s_new, b, kept_from, rate), _f48(s_old, b, kept_from, rate)
        est_new = vol * min(0.01, f_new / v48) if v48 > 0 and f_new > 0 else 0.0
        est_old = vol * min(0.01, f_old / v48) if v48 > 0 and f_old > 0 else 0.0
        arrived = new.get(t - t % HOUR, 0.0) if t - t % HOUR >= DENIM_H else 0.0
        due = left + est_new
        extra = max(0.0, arrived - due)
        left = min(max(0.0, due - arrived), max(f_new, 1.0))
        # the transfers of the wrong replay carried future times: none was ever counted as arrived
        out.append((t, b, est_new + extra, est_old))
        t = b
    return out, left


def _left_at_switch(new, kept_from):
    """Fees accrued at the switch since the pool's last transfer before it, at its pre-switch rate (the estimate
    that the first transfers after the switch paid back)."""
    pre = [h for h, v in new.items() if kept_from <= h < DENIM_H and v > 0]
    if not pre:
        return 0.0
    rate = sum(new[h] for h in pre) / max(HOUR, DENIM_H - kept_from)
    return max(0.0, C.DENIM_TS - (max(pre) + HOUR // 2)) * rate


def _left_at_end(new, end):
    """Fees accrued at `end` since the pool's last transfer, at its rate of the last 12 hours (none without a transfer
    in that time: the next one is credited when it comes)."""
    recent = [h for h, v in new.items() if end - 12 * HOUR <= h < end and v > 0]
    if not recent:
        return 0.0
    rate = sum(new[h] for h in recent) / (12 * HOUR)
    return max(0.0, end - (max(recent) + HOUR // 2)) * rate


def _moves_since_switch(state):
    """{position: [(time, {gauge: change of allocation})]} for the big positions whose moves big.py recorded after
    the switch: their allocation at any time since is the current one minus the later changes."""
    moves = (state.get("big") or {}).get("moves") or []
    if not moves or min(int(m.get("t") or 0) for m in moves) >= C.DENIM_TS:
        return {}               # the oldest moves since the switch may be gone: none is followed
    out = {}
    for m in moves:
        if int(m.get("t") or 0) > C.DENIM_TS:
            out.setdefault(str(m["id"]), []).append((int(m["t"]), m.get("d") or {}))
    return out


def _held(token, g, moves, t, b):
    """Allocation of a position in gauge g, averaged over [t, b), from its current one and its recorded moves."""
    cur = float(((token or {}).get("a") or {}).get(g, 0.0))
    cuts = [t] + sorted({m for m, _ in moves if t < m < b}) + [b]
    held = 0.0
    for p0, p1 in zip(cuts, cuts[1:]):
        a = cur - sum(float(d.get(g, 0.0)) for m, d in moves if m > p0)
        held += max(0.0, a) * (p1 - p0)
    return held / (b - t)


def denim(state, rpc, now, log, fee_window):
    """Runs denim_1 (see the module notes). Everything is computed first and written at the end."""
    pref = f"{C.BASE_CHAIN}:"
    pe = E._book(state)
    end = int(pe.get("last") or 0) or now            # earnings were last credited up to here, by the previous run
    kept_from = end - fee_window
    kept_from += -kept_from % HOUR                    # the oldest hour bucket the previous run kept
    # 1. fee hours and pending transfers
    old_fees = {g: {int(h): v for h, v in b.items()} for g, b in state["fees"].items() if g.startswith(pref)}
    new_fees = {}
    for g, b in old_fees.items():
        nb = {}
        for h, v in b.items():
            k = real_hour(h)
            nb[k] = nb.get(k, 0.0) + v
        new_fees[g] = nb
    moved = sum(1 for b in old_fees.values() for h in b if h >= DENIM_H)
    pending = [[real_ts(e[0]) if int(e[1]) == C.BASE_CHAIN else e[0], *e[1:]] for e in state["fees_pending"]]
    redated = sum(1 for e, n in zip(state["fees_pending"], pending) if e[0] != n[0])

    # 2. allocation times of the Base positions
    tokens = state["chains"][str(C.BASE_CHAIN)]["tokens"]
    ids = sorted((tid for tid, t in tokens.items() if int(t.get("last") or 0) >= C.DENIM_TS), key=int)
    try:
        chain = C.read_token_states(rpc, ids) if ids else {}
    except Exception as e:
        log(f"repair {DENIM}: allocation times not read from the LeafVoter ({str(e)[:120]}), moved back by the clock")
        chain = {}
    fix = {}
    for tid, t in tokens.items():
        last, first = int(t.get("last") or 0), int(t.get("first") or 0)
        s = chain.get(tid)
        upd = {}
        if last >= C.DENIM_TS:
            if s and s["last"]:
                upd = {"last": s["last"], "red": s["red"], "budget": round(s["budget"], 2)}
            elif last > now:
                upd = {"last": real_ts(last)}
        if first > now:
            upd["first"] = real_ts(first)
        nl = upd.get("last", last)
        if nl and upd.get("first", first) > nl:
            upd["first"] = nl
        if upd:
            fix[tid] = upd

    # 3. earnings since the switch, up to the last time they were credited
    vols = MD.market_state(state)["hist"]["pools"]
    members_of = {}
    for g, members in E._by_gauge(state).items():
        if g.startswith(pref):
            members_of[g] = [(tid, a, int(fix.get(tid, {}).get("last", last))) for tid, a, last in members]
    recorded = _moves_since_switch(state)
    credits, lefts, sums = [], {}, [0.0, 0.0, 0.0]
    if end > C.DENIM_TS:
        for g in sorted(set(members_of) | set(new_fees)):
            new, vser = new_fees.get(g) or {}, vols.get(g) or {}
            left0 = _left_at_switch(new, kept_from) if vser else 0.0
            hours, _ = replay(old_fees.get(g) or {}, new, vser, C.DENIM_TS, end, kept_from, left0)
            arrived = sum(v for h, v in new.items() if DENIM_H <= h < end)
            # what the pool's allocators earned: the transfers since the switch, less the fees accrued before it, plus
            # those accrued since the last transfer (the replay's own estimate lags when fees per dollar move)
            lefts[g] = _left_at_end(new, end) if vser else 0.0
            should, did = max(0.0, arrived - left0 + lefts[g]), sum(h[3] for h in hours)
            sums[0] += should
            sums[1] += did
            gap = should - did
            members = members_of.get(g) or []
            pos = [(t, b, k - c) for t, b, k, c in hours if k > c]
            weight = sum(d for _, _, d in pos)
            if gap <= 0.005 or weight <= 0:
                continue
            for t, b, d in pos:
                usd = d * gap / weight
                shares = []
                for tid, a, last in members:
                    # from its last allocation on; positions whose moves are recorded follow them instead
                    if tid not in recorded and last < b:
                        shares.append((tid, a * min(1.0, (b - max(t, last)) / (b - t)), max(t, last)))
                for tid, ms in recorded.items():
                    shares.append((tid, _held(tokens.get(tid), g, ms, t, b), t))
                tot = max(E._total(state, g, members, t - t % HOUR), sum(a for _, a, _ in shares))
                for tid, a, since in shares:
                    if a > 0 and tot > 0:
                        credits.append((tid, g, usd * a / tot, t, since))
                        sums[2] += usd * a / tot

    # 4. the simulation: what each Base pool paid, and the allocations saved in the last cooldown
    tr = state.get("tracker") or {}
    tr_end = int(tr.get("last") or 0)
    sim_hours = [(g, h, v) for g, b in new_fees.items() for h, v in b.items() if DENIM_H <= h < tr_end and v > 0]

    # write everything
    state.setdefault("repairs", []).append(DENIM)
    for g, b in new_fees.items():
        state["fees"][g] = {str(h): round(v, 4) for h, v in b.items()}
    state["fees_pending"] = pending
    for tid, upd in fix.items():
        tokens[tid].update(upd)
    for tid, g, usd, ts, since in credits:
        E._credit(pe, tid, g, usd, ts, since)
    pe.pop("_gi", None)
    est = pe.setdefault("est", {})
    for g in [g for g in est if g.startswith(pref) and g not in lefts]:
        del est[g]
    for g, left in lefts.items():
        if left > 0.005:
            est[g] = round(left, 4)
        else:
            est.pop(g, None)
    sim = _simulation(tr, state, sim_hours) if tr else (0, 0.0)
    log(f"repair {DENIM}: Base times after Denim: {moved} fee hours and {redated} pending transfers dated again, "
        f"{len(fix)} positions with new allocation times ({len(chain)} read again from the LeafVoter)")
    log(f"repair {DENIM}: Base earnings since the switch: the fee model credited {sums[1]:,.0f} USD, the pools earned "
        f"{sums[0]:,.0f}; {sums[2]:,.0f} USD added to {len({c[0] for c in credits})} positions (the rest belonged to "
        f"positions that have left those pools since); simulation: {sim[0]} pool hours completed, "
        f"{sim[1]:,.2f} USD to the saved allocations")
    return {"fee_hours": moved, "positions": len(fix), "credited": sums[2], "due": sums[0], "was": sums[1]}


def _simulation(tr, state, sim_hours):
    """Adds the Base fee transfers the simulation missed: to its record of what each pool paid and to the
    allocations it saved that were still in their cooldown."""
    if "pool_hist" not in tr:
        return 0, 0.0
    ap_hist = state.get("ap_hist") or {}
    added = 0.0
    for g, h, usd in sim_hours:
        a = float((ap_hist.get(str(h)) or {}).get(g, state["ap"].get(g, 0.0)))
        row = tr["pool_hist"].setdefault(g, {}).get(str(h))
        if row is None:
            row = tr["pool_hist"][g][str(h)] = [0.0, round(a), 0.0]
        row[0] = round(row[0] + usd, 4)
        row[2] = round(row[2] + usd * 1000 / (a + 1000), 6)
        for snaps in tr.get("open", {}).values():
            for snap in snaps:
                x = snap[1].get(g)
                if not x:
                    continue
                part = max(0.0, min(1.0, (h + HOUR - snap[0]) / HOUR))   # the transfers of the hour after it was saved
                e = usd * part * x[0] / (a + x[0])
                if e > 0:
                    x[2] = round(x[2] + e, 6)
                    snap[3] = round(snap[3] + e, 6)
                    added += e
    return len(sim_hours), added
