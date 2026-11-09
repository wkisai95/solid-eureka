"""Fee tendencies of each pool: by day of the week, by time of day, and the days something moved them.

Source. GeckoTerminal's hourly candles of each Aero pool (up to 1000 hours, about six weeks): volume and price.
A pool's fees follow its volume, so the weekly shape of the volume is the weekly shape of the fees; the monitor's own
measured fees (aero.fee_hours) are kept day by day next to it and say how many dollars a dollar traded is worth.
A few pools per run keep inside the free API limit; each pool is read again twice a day.

What comes out (trends.json):
- weekday index: the pool's typical day of the week against its typical day (1 = average). Days when something
  exceptional happened (below) are capped, so one event does not pass for a habit;
- 28 six-hour blocks of the week (Monday 00:00 UTC first), from the ordinary days only: with them the page compares
  the coming hours with the hours an APR was measured on;
- catalysts: days with at least 2.5 times the pool's median volume, with the pool's price move, whether the whole
  market moved that day (many pools at once, with ETH's and BTC's move) or only this pool, and whether an incentive
  program started on it.
"""
import statistics
import urllib.error

from . import aero as A
from . import marketdata as MD

HOUR, DAY = 3600, 86400
PER_RUN = 10                # candle requests per run
EVERY = 12 * HOUR           # a pool's candles are read again when older
RETRY_MISSING = 2 * DAY     # a pool GeckoTerminal does not know is tried again after this
MIN_AP = 50_000             # pools with at least these votes
MAX_POOLS = 160
KEEP_DAYS = 60
FEE_DAYS = 120
SPIKE = 2.5                 # a catalyst day: this many times the pool's median day
SPIKE_USD = 25_000          # ... and at least this volume
CAP = 3.0                   # weekday averages count a day at most this many times the median
MIN_DAYS = 14               # days of history before a pool's tendencies are shown
MARKET_SHARE = 0.25         # a market day: this share of the pools had a catalyst day ...
MARKET_X = 1.6              # ... or all pools together traded this many times their median day


def book(state):
    t = state.setdefault("trend", {})
    t.setdefault("p", {})
    t.setdefault("f", {})
    return t


def _targets(state):
    rows = [(a, k) for k, a in state["ap"].items()
            if a >= MIN_AP and (state["gauges"].get(k) or {}).get("pool") and A.chain_of(k) in MD.GT_NETWORKS]
    return [k for _, k in sorted(rows, reverse=True)[:MAX_POOLS]]


def _digest(candles, now):
    """Hourly candles [[ts, o, h, l, c, volume], ...] -> (days {day: [volume, change %, range %]},
    hourly {hour: volume}) for the complete days they span."""
    rows = sorted((int(c[0]), [MD._num(x) for x in c[1:6]]) for c in candles if c and len(c) >= 6)
    if not rows:
        return {}, {}
    today = now - now % DAY
    first = rows[0][0] - rows[0][0] % DAY + DAY          # the first day may be cut by the 1000-candle limit
    by = {}
    for ts, v in rows:
        d = ts - ts % DAY
        if first <= d < today:
            by.setdefault(d, []).append((ts, v))
    days, hours = {}, {}
    for d in range(first, today, DAY):
        hs = by.get(d) or []
        if not hs:
            days[str(d)] = [0, 0.0, 0.0]
            continue
        o, c = hs[0][1][0], hs[-1][1][3]
        hi, lo = max(v[1] for _, v in hs), min(v[2] for _, v in hs)
        days[str(d)] = [round(sum(v[4] for _, v in hs)), round((c / o - 1) * 100, 2) if o > 0 else 0.0,
                        round((hi / lo - 1) * 100, 2) if lo > 0 else 0.0]
        for ts, v in hs:
            hours[ts] = v[4]
    return days, hours


def _spikes(days):
    """{day: times the median} for the catalyst days of {day: [volume, ...]}."""
    vols = [v[0] for v in days.values()]
    if len(vols) < 7:
        return {}
    med = statistics.median(vols)
    if med <= 0:
        pos = [v for v in vols if v > 0]
        med = statistics.median(pos) if len(pos) >= 3 else 0
    if med <= 0:
        return {}
    return {d: round(v[0] / med, 2) for d, v in days.items() if v[0] >= SPIKE * med and v[0] >= SPIKE_USD}


def _blocks(days, hours):
    """28 six-hour blocks of the week: average hourly volume in each, from the ordinary days only."""
    skip = {int(d) for d in _spikes(days)}
    tot, n = [0.0] * 28, [0] * 28
    for d in days:
        d = int(d)
        if d in skip:
            continue
        wd = (d // DAY + 3) % 7                    # 1 Jan 1970 was a Thursday: Monday = 0
        for q in range(4):
            i = wd * 4 + q
            n[i] += 6
            tot[i] += sum(hours.get(d + (q * 6 + j) * HOUR, 0.0) for j in range(6))
    return [round(tot[i] / n[i], 2) if n[i] else None for i in range(28)]


def refresh(state, now, log):
    """Reads the candles of a few pools: those never read first, then the oldest."""
    t = book(state)
    P = t["p"]
    targets = _targets(state)
    for k in [k for k in P if k not in targets and now - P[k].get("at", 0) > 7 * DAY]:
        del P[k]

    def due(k):
        p = P.get(k) or {}
        if p.get("miss"):
            return now - p.get("tried", 0) > RETRY_MISSING
        return now - p.get("at", 0) > EVERY

    queue = sorted((k for k in targets if due(k)), key=lambda k: (P.get(k) or {}).get("at", 0))[:PER_RUN]
    done = missing = 0
    for k in queue:
        net = MD.GT_NETWORKS[A.chain_of(k)]
        pool = state["gauges"][k]["pool"]
        p = P.setdefault(k, {})
        p["tried"] = now
        tok = _priced(state, k)
        try:
            res = MD._get(f"{MD.GT}/networks/{net}/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000&currency=usd"
                          + (f"&token={tok}" if tok else ""), retries=0)
        except MD.Limited:
            log(f"trends: geckoterminal limit reached after {done} pools, the rest next run")
            break
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                p["miss"] = p.get("miss", 0) + 1
                missing += 1
                continue
            log(f"trends: {k}: HTTP {e.code}")
            continue
        except Exception as e:
            log(f"trends: {k}: {str(e)[:100]}")
            continue
        candles = ((((res or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        days, hours = _digest(candles, now)
        if not days:
            p["miss"] = p.get("miss", 0) + 1
            missing += 1
            continue
        old = {d: v for d, v in (p.get("d") or {}).items() if d not in days and int(d) >= now - KEEP_DAYS * DAY}
        P[k] = {"at": now, "tried": now, "d": {**old, **days}, "b": _blocks(days, hours)}
        done += 1
    _fee_days(state, t, now)
    if queue:
        have = sum(1 for k in targets if (P.get(k) or {}).get("d"))
        log(f"trends: candles of {done} pools read" + (f", {missing} unknown to geckoterminal" if missing else "")
            + f"; {have} of {len(targets)} pools have their history")
    return done


def _fee_days(state, t, now):
    """Keeps the measured fees of each pool for every complete day, once a day."""
    day = now - now % DAY - DAY
    if t.get("fd") == day:
        return
    for k in set(state.get("acc") or {}):
        cs = state["chains"].get(str(A.chain_of(k))) or {}
        if not cs.get("acc_from") or int(cs["acc_from"]) > day:
            continue
        usd = A.fees_usd(state, k, day, day + DAY)
        if usd > 0:
            t["f"].setdefault(k, {})[str(day)] = round(usd, 2)
    for k in list(t["f"]):
        t["f"][k] = {d: v for d, v in t["f"][k].items() if int(d) >= now - FEE_DAYS * DAY}
        if not t["f"][k]:
            del t["f"][k]
    t["fd"] = day


def _weekdays(days):
    """([7 indexes, Monday first], [7 sample counts]): the typical day of the week against the typical day, each
    day counted at most CAP times the median."""
    vols = [v[0] for v in days.values()]
    med = statistics.median(vols) if vols else 0
    cap = CAP * med if med > 0 else max(vols or [0])
    tot, n = [0.0] * 7, [0] * 7
    for d, v in days.items():
        wd = (int(d) // DAY + 3) % 7
        tot[wd] += min(v[0], cap)
        n[wd] += 1
    means = [tot[i] / n[i] if n[i] else None for i in range(7)]
    known = [m for m in means if m is not None]
    avg = sum(known) / len(known) if known else 0
    return [round(m / avg, 3) if m is not None and avg > 0 else None for m in means], n


def _sym(state, cid, tok):
    return ((state["erc20"].get(A.tkey(cid, tok)) or {}).get("sym") or "").upper()


STABLES = {"USDC", "USDT", "USD₮0", "USDBC", "DAI", "USDG", "USDS", "GHO", "USDE", "EURC", "USDAI", "SUSDAI", "U"}


def _priced(state, k):
    """The token of the pool whose USD price the candles follow: the one that is not a stablecoin."""
    g = state["gauges"].get(k) or {}
    cid = A.chain_of(k)
    toks = [x for x in (g.get("t0"), g.get("t1")) if x]
    for x in toks:
        if _sym(state, cid, x) not in STABLES:
            return x
    return toks[0] if toks else None


def _reference(state, P, syms):
    """{day: price change %} of one of these tokens, from the biggest tracked pool pairing it with a stablecoin."""
    best = None
    for k, p in P.items():
        g = state["gauges"].get(k) or {}
        if not p.get("d") or not g.get("t0") or not g.get("t1"):
            continue
        cid = A.chain_of(k)
        pair = {_sym(state, cid, g["t0"]), _sym(state, cid, g["t1"])}
        if pair & set(syms) and pair & STABLES:
            vol = sum(v[0] for v in p["d"].values())
            if best is None or vol > best[0]:
                best = (vol, p["d"])
    return {d: v[1] for d, v in best[1].items()} if best else {}


def export(state, names, now):
    """names: {gauge key: (pool name, chain name, fees per dollar traded or None)} from the report."""
    t = book(state)
    P = {k: p for k, p in t["p"].items() if p.get("d") and k in state["ap"]}
    out = {"generated": now, "pools": [], "market": None, "min_days": MIN_DAYS,
           "tracked": len(_targets(state)), "have": len(P)}
    if not P:
        return out
    spikes = {k: _spikes(p["d"]) for k, p in P.items()}
    full = [k for k, p in P.items() if len(p["d"]) >= MIN_DAYS]
    # the market: all the tracked pools together, day by day
    all_days = sorted({d for p in P.values() for d in p["d"]}, key=int)
    tot = {d: sum(p["d"][d][0] for p in P.values() if d in p["d"]) for d in all_days}
    cover = {d: sum(1 for k in full if d in P[k]["d"]) for d in all_days}
    solid = [d for d in all_days if cover[d] >= 0.6 * max(1, len(full))]
    med = statistics.median([tot[d] for d in solid]) if solid else 0
    eth = _reference(state, P, ("WETH", "ETH"))
    btc = _reference(state, P, ("CBBTC", "WBTC", "TBTC"))
    events = {}
    for d in solid:
        share = sum(1 for k in full if d in spikes[k]) / max(1, cover[d])
        x = tot[d] / med if med > 0 else 0
        if share >= MARKET_SHARE or x >= MARKET_X:
            events[d] = {"day": int(d), "x": round(x, 2), "share": round(share, 3), "eth": eth.get(d),
                         "btc": btc.get(d)}
    mwd, mn = _weekdays({d: [tot[d]] for d in solid})
    mb_tot, mb_n = [0.0] * 28, [0] * 28
    for k in full:
        b = P[k].get("b") or []
        for i, v in enumerate(b[:28]):
            if v is not None:
                mb_tot[i] += v
                mb_n[i] += 1
    mb_avg = sum(mb_tot) / 28 if sum(mb_tot) > 0 else 0
    out["market"] = {"wd": mwd, "n": mn, "b": [round(v / mb_avg, 3) if mb_avg > 0 else None for v in mb_tot],
                     "days": [[int(d), round(tot[d]), round(tot[d] / med, 2) if med > 0 else None] for d in solid],
                     "events": sorted(events.values(), key=lambda e: -e["day"]),
                     "from": int(solid[0]) if solid else None, "to": int(solid[-1]) if solid else None}
    started = {}
    for p in state["incentives"].values():
        c = int(p.get("created") or p.get("start") or 0)
        started.setdefault(p["g"], set()).add(c - c % DAY)
    for k, p in sorted(P.items(), key=lambda kv: -state["ap"].get(kv[0], 0)):
        days = p["d"]
        name, chain = (names.get(k) or (None, None, None))[:2]
        if not name:
            continue
        wd, n = _weekdays(days)
        b = p.get("b") or []
        known = [v for v in b if v is not None]
        avg = sum(known) / len(known) if known else 0
        vols = [v[0] for v in days.values()]
        fees = t["f"].get(k) or {}
        # dollars of fees per dollar traded on the days both are known: what a day of volume is worth
        both = [(fees[d], days[d][0]) for d in fees if d in days and days[d][0] > 0]
        # (the report's own rate is on another source's volume: not mixed with these candles)
        rate = sum(f for f, _ in both) / sum(v for _, v in both) if both else None
        ev = []
        for d, x in sorted(spikes[k].items(), key=lambda kv: -int(kv[0]))[:6]:
            kind = "mercato" if d in events else "pool"
            inc = any(abs(int(d) - s) <= DAY for s in started.get(k, ()))
            ev.append([int(d), x, days[d][1], days[d][2], kind, 1 if inc else 0])
        out["pools"].append({
            "k": k, "name": name, "chain": chain, "ap": round(state["ap"].get(k, 0)), "n": len(days),
            "wd": wd, "wn": n, "b": [round(v / avg, 3) if v is not None and avg > 0 else None for v in b],
            "vol": round(statistics.median(vols)) if vols else 0,
            "fee": round(statistics.median(vols) * rate, 2) if vols and rate else None,
            "ev": ev})
    return out
