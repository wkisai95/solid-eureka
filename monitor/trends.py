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
import math
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


HOURLY_DAYS = 28            # hourly volume and price moves kept per pool, for bestchoice's model


def _hourly(candles, days):
    """[first hour, [volume per hour], [|ln(close / open)| x 10000 per hour]] over the last HOURLY_DAYS complete days."""
    if not days:
        return None
    end = max(int(d) for d in days) + DAY
    start = max(min(int(d) for d in days), end - HOURLY_DAYS * DAY)
    n = (end - start) // HOUR
    vol, mv = [0] * n, [0] * n
    for c in candles:
        if not c or len(c) < 6:
            continue
        i = (int(c[0]) - start) // HOUR
        if 0 <= i < n:
            o, cl = MD._num(c[1]), MD._num(c[4])
            vol[i] = round(MD._num(c[5]))
            mv[i] = round(abs(math.log(cl / o)) * 10000) if o > 0 and cl > 0 else 0
    return [start, vol, mv]


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
        return now - p.get("at", 0) > EVERY or "bt" not in p or "hx" not in p

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
        P[k] = {"at": now, "tried": now, "d": {**old, **days}, "b": _blocks(days, hours),
                "bt": backtest(days, hours), "hx": _hourly(candles, days)}
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


# ---------------------------------------------------------------- which forecast of the next 48 hours is right

BT_LOOK, BT_AHEAD, BT_WARM, BT_STEP = 96, 48, 168, 6      # hours


def _w(n):
    return lambda x: sum(x[-n:]) * BT_AHEAD / n


def _med4(x):
    return statistics.median(sum(x[i:i + 24]) for i in range(0, 96, 24)) * 2


def _ewma(half):
    def f(x):
        tot = wsum = 0.0
        for age, v in enumerate(reversed(x)):
            w = 0.5 ** (age / half)
            tot += v * w
            wsum += w
        return tot / wsum * BT_AHEAD
    return f


def _gm(*fs):
    def f(x):
        vals = [g(x) for g in fs]
        if min(vals) <= 0:
            return sum(vals) / len(vals)
        prod = 1.0
        for v in vals:
            prod *= v
        return prod ** (1 / len(vals))
    return f


# name: (hours of history it reads, forecast of the next 48 hours from the last 96 hourly values, oldest first)
PREDICTORS = {
    "48h": (48, _w(48)), "24h": (24, _w(24)), "12h": (12, _w(12)), "6h": (6, _w(6)), "96h": (96, _w(96)),
    "mediana": (96, _med4),                       # twice the median of the last four days: deaf to one spike
    "ewma24": (96, _ewma(24)), "ewma12": (72, _ewma(12)),
    "48h+mediana": (96, _gm(_w(48), _med4)), "24h+96h": (96, _gm(_w(24), _w(96))),
    "24h+48h+96h": (96, _gm(_w(24), _w(48), _w(96))),
}
LABELS = {"48h": "ultime 48 ore", "24h": "ultime 24 ore", "12h": "ultime 12 ore", "6h": "ultime 6 ore",
          "96h": "ultimi 4 giorni", "mediana": "mediana di 4 giorni", "ewma24": "media pesata (metà peso a 24 ore)",
          "ewma12": "media pesata (metà peso a 12 ore)", "48h+mediana": "48 ore e mediana insieme",
          "24h+96h": "24 ore e 4 giorni insieme", "24h+48h+96h": "24, 48 ore e 4 giorni insieme"}


def _block_of(ts):
    return ((ts // DAY + 3) % 7) * 4 + (ts % DAY) // (6 * HOUR)


def _season(blocks, t, look, closed=()):
    """Typical level of the 48 hours after t against the `look` hours before it, from 28 weekly blocks; 1 if
    unknown; kept between 0.6 and 1.6."""
    def avg(a, b):
        # a day its market is closed (closed: day starts) counts as a Saturday
        vals = [blocks[20 + (h % DAY) // (6 * HOUR) if h - h % DAY in closed else _block_of(h)]
                for h in range(a, b, HOUR)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None
    nxt, past = avg(t, t + BT_AHEAD * HOUR), avg(t - look * HOUR, t)
    if not nxt or not past or nxt <= 0 or past <= 0:
        return 1.0
    return max(0.6, min(1.6, nxt / past))


def backtest(days, hours):
    """Walks through the pool's hourly history: at each step every forecast of the next 48 hours is made with what
    was known then (the weekly blocks too, from the hours before) and compared with what happened. Returns
    {name: [sum of |ln(forecast / real)|, sum of ln(forecast / real), samples]}; "+g" names are corrected for the
    day of the week."""
    if not days:
        return {}
    start = min(int(d) for d in days)
    end = max(int(d) for d in days) + DAY
    x = [float(hours.get(t, 0.0)) for t in range(start, end, HOUR)]
    out = {}
    for i in range(max(BT_LOOK, BT_WARM), len(x) - BT_AHEAD + 1, BT_STEP):
        real = sum(x[i:i + BT_AHEAD])
        past = x[i - BT_LOOK:i]
        if real <= 0 or sum(past) <= 0:
            continue
        t = start + i * HOUR
        tot, n = [0.0] * 28, [0] * 28
        for j in range(i):
            b = _block_of(start + j * HOUR)
            tot[b] += x[j]
            n[b] += 1
        blocks = [tot[b] / n[b] if n[b] else None for b in range(28)]
        for name, (look, f) in PREDICTORS.items():
            pred = f(past)
            for nm, v in ((name, pred), (name + "+g", pred * _season(blocks, t, look))):
                if v <= 0:
                    continue
                e = math.log(v / real)
                o = out.setdefault(nm, [0.0, 0.0, 0])
                o[0] += abs(e)
                o[1] += e
                o[2] += 1
    return {k: [round(v[0], 4), round(v[1], 4), v[2]] for k, v in out.items()}


NAMES = [n + g for n in PREDICTORS for g in ("", "+g")]
MODEL = "bc"                # bestchoice's model: checked live with the others, chosen only by its own option
_simple = lambda sc: {nm: v for nm, v in sc.items() if nm.replace("+g", "") in PREDICTORS}
LIVE_EVERY = 6 * HOUR       # the monitor's own forecasts are saved this often and checked 48 hours later
LIVE_WEIGHT = 3             # a check on the fees really measured counts as this many backtest samples
LIVE_MIN_USD = 5            # pools that earned less in the 48 hours are not scored: the ratio would be noise
SHRINK = 20                 # a pool's own bias counts fully only after many more checks than this


def _all_forecasts(state, key, h0, look):
    """Every forecast of the fees of the 48 hours after h0, in NAMES order (None for those that read more than
    `look` hours), from the fees measured hour by hour."""
    fh = A.fee_hours(state, key)
    x = [fh.get(h, 0.0) for h in range(h0 - 96 * HOUR, h0, HOUR)]
    blocks = (book(state)["p"].get(key) or {}).get("b")
    out = []
    for name, (lk, f) in PREDICTORS.items():
        if lk > look or sum(x[-lk:]) <= 0:
            out += [None, None]
            continue
        v = f(x if look >= 96 else [0.0] * 48 + x[48:])
        out += [round(v, 4), round(v * (_season(blocks, h0, lk) if blocks else 1.0), 4)]
    return out


def _measured_look(state, key, h0):
    """Hours of measured fees behind h0 that the forecasts of this pool may read: 96, 48 or 0."""
    cs = state["chains"].get(str(A.chain_of(key))) or {}
    since = int(cs.get("acc_backfill") and cs.get("acc_from") or 0)
    if not since or since > h0 - 48 * HOUR:
        return 0
    return 96 if since <= h0 - 96 * HOUR else 48


def verify(state, now, log, model=None):
    """The live check that refines the choice: every six hours every forecast of every pool is saved, and 48 hours
    later compared with the fees really measured. The errors add to the backtest's (scores) and give each pool
    its own bias (forecast). Also keeps how much the votes of each pool moved in those 48 hours."""
    t = book(state)
    lv = t.setdefault("lv", {"snaps": [], "s": {}, "n": 0})
    h0 = now - now % HOUR
    done = 0
    for snap in [s for s in lv["snaps"] if s["t"] + BT_AHEAD * HOUR <= h0]:
        lv["snaps"].remove(snap)
        for key, (ap0, vals) in snap["f"].items():
            real = A.fees_usd(state, key, snap["t"], snap["t"] + BT_AHEAD * HOUR)
            if real < LIVE_MIN_USD:
                continue
            book_k = lv["s"].setdefault(key, {})
            for nm, v in zip(NAMES + [MODEL], vals):
                if v is None or v <= 0:
                    continue
                e = math.log(v / real)
                o = book_k.setdefault(nm, [0.0, 0.0, 0])
                o[0], o[1], o[2] = round(o[0] + abs(e), 4), round(o[1] + e, 4), o[2] + 1
            ap1 = float(state["ap"].get(key, 0.0))
            if ap0 > 0 and ap1 > 0:
                d = book_k.setdefault("_ap", [0.0, 0])
                d[0], d[1] = round(d[0] + math.log(ap1 / ap0), 4), d[1] + 1
            done += 1
        lv["n"] = lv.get("n", 0) + 1
    if h0 % LIVE_EVERY == 0 and not any(s["t"] == h0 for s in lv["snaps"]):
        f = {}
        for key, ap in state["ap"].items():
            look = _measured_look(state, key, h0)
            if ap < 1000 or not look:
                continue
            vals = _all_forecasts(state, key, h0, look) + [(model or {}).get(key)]
            if any(v for v in vals):
                f[key] = [round(ap), vals]
        if f:
            lv["snaps"].append({"t": h0, "f": f})
    for key in [k for k in lv["s"] if k not in state["ap"]]:
        del lv["s"][key]
    if done:
        log(f"trends: {done} forecasts of 48 hours ago checked against the measured fees")


def _pool_scores(state, key):
    """{name: [sum |error|, sum error, samples]} of one pool: its backtest plus its live checks, weighted."""
    t = book(state)
    out = {nm: list(v) for nm, v in ((t["p"].get(key) or {}).get("bt") or {}).items()}
    for nm, v in ((t.get("lv") or {}).get("s") or {}).get(key, {}).items():
        if nm.startswith("_"):
            continue
        o = out.setdefault(nm, [0.0, 0.0, 0])
        o[0] += v[0] * LIVE_WEIGHT
        o[1] += v[1] * LIVE_WEIGHT
        o[2] += v[2] * LIVE_WEIGHT
    return out


def scores(state, live_only=False):
    """{name: (typical error as a factor, bias as a factor, samples)}: every pool's backtest plus the live checks
    of the monitor's own forecasts (each worth LIVE_WEIGHT samples); live_only: the live checks alone."""
    t = book(state)
    tot = {}
    if live_only:
        rows = [{nm: v for nm, v in s.items() if not nm.startswith("_")} for s in (t.get("lv") or {}).get("s", {}).values()]
    else:
        rows = [_pool_scores(state, k) for k in set(t["p"]) | set((t.get("lv") or {}).get("s", {}))]
    for r in rows:
        for nm, v in r.items():
            o = tot.setdefault(nm, [0.0, 0.0, 0])
            o[0] += v[0]
            o[1] += v[1]
            o[2] += v[2]
    need = 50 if live_only else 200
    return {nm: (math.exp(v[0] / v[2]), math.exp(v[1] / v[2]), v[2]) for nm, v in tot.items() if v[2] >= need}


def best(state, max_look=96):
    """Name of the forecast with the smallest typical error among those reading at most max_look hours."""
    sc = {nm: v for nm, v in _simple(scores(state)).items() if PREDICTORS[nm.replace("+g", "")][0] <= max_look}
    return min(sc, key=lambda nm: sc[nm][0]) if sc else None


def forecast(state, key, now, sc=None):
    """(fees expected in the next 48 hours in USD, name of the forecast used, the pool's own typical error with it
    as a factor or None, expected change of the pool's votes in 48 hours as a factor or None), or None while there
    is no backtest. Made on the fees measured hour by hour; a chain measured for less than four days uses the best
    forecast that reads only 48 hours. The forecast's average bias is taken out: the pool's own, as far as its
    checks allow (shrunk toward everyone's)."""
    h0 = now - now % HOUR
    look = _measured_look(state, key, h0)
    sc = sc if sc is not None else scores(state)
    cands = {nm: v for nm, v in _simple(sc).items() if PREDICTORS[nm.replace("+g", "")][0] <= look}
    if not cands:
        return None
    name = min(cands, key=lambda nm: cands[nm][0])
    usd = _all_forecasts(state, key, h0, look)[NAMES.index(name)]
    if usd is None:
        return None
    own = _pool_scores(state, key).get(name)
    bias = math.log(sc[name][1])
    if own and own[2] > 0:
        w = own[2] / (own[2] + SHRINK)
        bias = (1 - w) * bias + w * max(-0.35, min(0.35, own[1] / own[2]))
    usd /= math.exp(bias)
    d = (((book(state).get("lv") or {}).get("s") or {}).get(key) or {}).get("_ap")
    drift = math.exp(d[0] / d[1] * d[1] / (d[1] + 4)) if d and d[1] else None
    return usd, name, (math.exp(own[0] / own[2]) if own and own[2] >= 10 else None), drift


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
    sc = scores(state)
    out["forecasts"] = [{"k": nm, "name": LABELS.get(nm.replace("+g", ""), nm) + (", corretta per il giorno della settimana" if nm.endswith("+g") else ""),
                         "err": round(v[0], 3), "bias": round(v[1], 3), "n": v[2]}
                        for nm, v in sorted(_simple(sc).items(), key=lambda kv: kv[1][0])]
    out["best"] = best(state)
    lv = book(state).get("lv") or {}
    live = scores(state, live_only=True)
    out["live"] = {"checks": sum(v.get(out["best"], [0, 0, 0])[2] for v in (lv.get("s") or {}).values()) if out["best"] else 0,
                   "waiting": len(lv.get("snaps") or []),
                   "err": {nm: round(v[0], 3) for nm, v in live.items()}}
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
