"""«Best choice (probably)»: everything the monitor knows about the next 48 hours of a pool, in one forecast.

Fees. One model for all pools, fitted on their hourly history (trends.py keeps four weeks of it): the fees of the
next 48 hours against a base forecast (average weighted toward the last 24 hours), explained by
  - the pool's own recent windows (6, 48, 96 hours, the median of four days) against that base;
  - its habits by day of the week and time of day, and the whole market's (steadier than one pool's);
  - where the whole market is going (its last day against its last four);
  - how much prices are moving, the pool's and everyone's: fees rise with volatility, and volatility lasts.
It is fitted on the first two thirds of the history and judged on the last third, which it has never seen; it is
used only if there it beats the best simple forecast, otherwise that one is used. Every six hours its forecasts are
saved and checked 48 hours later against the measured fees, like the others (trends.verify).

Votes. APR is fees over votes, and votes move toward what pays: a second model, on the eight days of hourly votes
the simulation keeps, gives the change of a pool's votes in 48 hours from its APR against the market's and from
how its votes moved in the last day. Same rule: used only if it beats "votes stay as they are" on unseen data.

Calendar. On the days the US stock market is closed (NYSE holidays below) the pools of tokenized stocks count as
on a Saturday.

The page does the rest (the allocation): it values each pool by this forecast, lowers the fee part by the pool's
own typical error (incentives are scheduled: no discount), dilutes it by the votes expected, and spreads the votes.
"""
import calendar
import math
import time

from . import aero as A
from . import trends as TR

HOUR, DAY = 3600, 86400
EVERY = 6 * HOUR            # the models are fitted again this often
WARM, AHEAD, STEP = 168, 48, 6
RIDGE = 5.0
FEE_FEATS = ["ultime 6 ore", "ultime 48 ore", "ultimi 4 giorni", "mediana di 4 giorni", "giorno e ora del pool",
             "giorno e ora del mercato", "direzione del mercato", "volatilità del pool", "volatilità del mercato"]
VOTE_FEATS = ["APR rispetto al mercato", "voti dell'ultimo giorno"]
MIN_GAIN = 0.99             # a model is used when its error on unseen data is below this share of the simple one's
# days the New York Stock Exchange is closed (UTC day starts), 2026-2027
NYSE_CLOSED = ["2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
               "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"]
CLOSED = {calendar.timegm(time.strptime(d, "%Y-%m-%d")) for d in NYSE_CLOSED}

_ewma = TR.PREDICTORS["ewma24"][1]
_med = TR.PREDICTORS["mediana"][1]


def _clip(v, lim=3.0):
    return max(-lim, min(lim, v))


def _lr(a, b, lim=3.0):
    return _clip(math.log(a / b), lim) if a > 0 and b > 0 else 0.0


def _ridge(rows, d, lam=RIDGE):
    """Least squares with a penalty: rows of (features, target) -> coefficients, intercept first."""
    n = d + 1
    M = [[0.0] * (n + 1) for _ in range(n)]
    for f, y in rows:
        x = [1.0] + list(f)
        for i in range(n):
            xi = x[i]
            if xi:
                Mi = M[i]
                for j in range(n):
                    Mi[j] += xi * x[j]
                Mi[n] += xi * y
    for i in range(1, n):
        M[i][i] += lam
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[piv][i]) < 1e-12:
            continue
        M[i], M[piv] = M[piv], M[i]
        pv = M[i][i]
        M[i] = [v / pv for v in M[i]]
        for r in range(n):
            if r != i and M[r][i]:
                f = M[r][i]
                M[r] = [a - f * b for a, b in zip(M[r], M[i])]
    return [M[i][n] for i in range(n)]


def _apply(coef, f, lim=1.0):
    return _clip(coef[0] + sum(c * x for c, x in zip(coef[1:], f)), lim)


def _fee_feats(x, s, ms, mx, v, mv):
    """Features from the last 96 hourly values of the pool (x) and of the market (mx), the season factors (s, ms)
    and the last 96 hourly price moves of the pool (v) and of the market (mv). Returns (base forecast, features)."""
    base = _ewma(x)
    if base <= 0:
        return 0.0, None
    f = [_lr(sum(x[-6:]) * 8, base), _lr(sum(x[-48:]), base), _lr(sum(x) / 2, base), _lr(_med(x), base),
         math.log(s), math.log(ms), _lr(sum(mx[-24:]) * 4, sum(mx)),
         _lr(sum(v[-24:]) * 4, sum(v), 2.0) if v else 0.0, _lr(sum(mv[-24:]) * 4, sum(mv), 2.0) if mv else 0.0]
    return base, f


class _Blocks:
    """The 28 weekly blocks of a series, grown hour by hour: at any step they hold only the hours before it."""

    def __init__(self, start):
        self.start, self.i = start, 0
        self.tot, self.n = [0.0] * 28, [0] * 28

    def upto(self, x, i):
        while self.i < i:
            b = TR._block_of(self.start + self.i * HOUR)
            self.tot[b] += x[self.i]
            self.n[b] += 1
            self.i += 1
        return [self.tot[b] / self.n[b] if self.n[b] else None for b in range(28)]


def _series(state, now):
    """(first hour, hours, {pool: (volumes, price moves)}) of the pools with hourly history, on one clock."""
    P = {k: p["hx"] for k, p in TR.book(state)["p"].items() if p.get("hx")}
    if not P:
        return None
    end = min(max(h[0] + len(h[1]) * HOUR for h in P.values()), now - now % DAY)
    start = end - TR.HOURLY_DAYS * DAY
    n = (end - start) // HOUR
    out = {}
    for k, (s0, vol, mv) in P.items():
        off = (s0 - start) // HOUR
        x, v = [0.0] * n, [0.0] * n
        for j in range(len(vol)):
            i = j + off
            if 0 <= i < n:
                x[i], v[i] = float(vol[j]), float(mv[j])
        if sum(x) > 0:
            out[k] = (x, v)
    return start, n, out


def fit_fees(state, now, log):
    ser = _series(state, now)
    if not ser or len(ser[2]) < 20:
        return None
    start, n, pools = ser
    mx = [sum(p[0][i] for p in pools.values()) for i in range(n)]
    cnt = [sum(1 for p in pools.values() if p[1][i]) for i in range(n)]
    mv = [sum(p[1][i] for p in pools.values()) / cnt[i] if cnt[i] else 0.0 for i in range(n)]
    first = next((i for i, v in enumerate(mx) if v > 0), n)
    steps = list(range(max(first + WARM, 96), n - AHEAD + 1, STEP))
    if len(steps) < 12:
        return None
    mb = _Blocks(start)
    mseason = {}
    for i in steps:
        mseason[i] = TR._season(mb.upto(mx, i), start + i * HOUR, 48)
    samples = []
    for k, (x, v) in pools.items():
        bl = _Blocks(start)
        own = next((i for i, a in enumerate(x) if a > 0), n)
        for i in steps:
            if i < own + WARM:
                continue
            real = sum(x[i:i + AHEAD])
            past = x[i - 96:i]
            if real <= 0 or sum(past[-24:]) <= 0:
                continue
            s = TR._season(bl.upto(x, i), start + i * HOUR, 48)
            base, f = _fee_feats(past, s, mseason[i], mx[i - 96:i], v[i - 96:i], mv[i - 96:i])
            if f is None:
                continue
            samples.append((i, k, f, _clip(math.log(real / base))))
    if len(samples) < 500:
        return None
    cut = steps[len(steps) * 2 // 3]
    train = [(f, y) for i, _, f, y in samples if i + AHEAD <= cut]
    test = [(f, y) for i, _, f, y in samples if i >= cut]
    if len(train) < 300 or len(test) < 150:
        return None
    d = len(FEE_FEATS)
    c0 = _ridge(train, d)
    err_model = sum(abs(y - _apply(c0, f)) for f, y in test) / len(test)
    err_simple = sum(abs(y - f[4]) for f, y in test) / len(test)       # the base, corrected for the pool's week
    err_base = sum(abs(y) for _, y in test) / len(test)
    coef = _ridge([(f, y) for _, _, f, y in samples], d)
    pe = {}
    for _, k, f, y in samples:
        o = pe.setdefault(k, [0.0, 0])
        o[0] += abs(y - _apply(coef, f))
        o[1] += 1
    used = err_model < err_simple * MIN_GAIN
    log(f"best choice: fees model on {len(samples)} samples of {len(pools)} pools: on the unseen third it misses by "
        f"{(math.exp(err_model) - 1) * 100:.0f}%, the simple forecast by {(math.exp(err_simple) - 1) * 100:.0f}%"
        + ("" if used else ": not used"))
    return {"coef": [round(c, 5) for c in coef], "err": round(math.exp(err_model), 4),
            "simple": round(math.exp(err_simple), 4), "base": round(math.exp(err_base), 4), "n": len(samples),
            "test": len(test), "used": used, "pe": {k: [round(v[0], 4), v[1]] for k, v in pe.items()},
            "mb": mb.upto(mx, n), "mv": [round(sum(mv[-24:]) * 4, 2), round(sum(mv[-96:]), 2)]}


def _vote_series(state, now):
    """(first hour, hours, {pool: (revenue per hour, votes per hour)}, total votes per hour): the votes from the
    hourly record of the most voted pools (state["ap_hist"]), the revenue from the simulation's pool history."""
    ah = state.get("ap_hist") or {}
    hist = (state.get("tracker") or {}).get("pool_hist") or {}
    hours = sorted(int(h) for h in ah)
    if len(hours) < 100:
        return None
    start, end = hours[0], hours[-1] + HOUR
    n = (end - start) // HOUR
    rows, last = [], {}
    for i in range(n):
        last = ah.get(str(start + i * HOUR)) or last          # a missing hour: as the hour before
        rows.append(last)
    out = {}
    for k in {k for r in rows for k in r}:
        h = hist.get(k) or {}
        ap = [float(r.get(k, 0.0)) for r in rows]
        rev = [float((h.get(str(start + i * HOUR)) or [0.0])[0]) for i in range(n)]
        out[k] = (rev, ap)
    return start, n, out, [sum(r.values()) for r in rows]


def _vote_feats(rev48, ap, ap24, mkt, tot=1.0, tot24=1.0):
    """mkt: the market's revenue per vote over the same 48 hours; tot, tot24: all the votes now and a day ago."""
    rel = _lr(rev48 / ap, mkt) if rev48 > 0 and ap > 0 and mkt > 0 else -3.0
    return [rel, _clip(_lr(ap, ap24, 1.0) - _lr(tot, tot24, 1.0), 1.0)]


def fit_votes(state, now, log):
    """The change of a pool's share of the votes in 48 hours (its votes against everyone's)."""
    ser = _vote_series(state, now)
    if not ser:
        return None
    start, n, pools, tot = ser
    steps = list(range(48, n - AHEAD + 1, STEP))
    if len(steps) < 9:
        return None
    samples = []
    for i in steps:
        j = i + AHEAD - 1
        if tot[i] <= 0 or tot[j] <= 0 or tot[i - 24] <= 0:
            continue
        r48 = {k: sum(p[0][i - 48:i]) for k, p in pools.items()}
        mkt = sum(r48.values()) / tot[i]
        g = math.log(tot[j] / tot[i])
        for k, (rev, ap) in pools.items():
            if ap[i] < 10_000 or ap[j] <= 0 or ap[i - 24] <= 0:
                continue
            samples.append((i, _vote_feats(r48[k], ap[i], ap[i - 24], mkt, tot[i], tot[i - 24]),
                            _clip(math.log(ap[j] / ap[i]) - g, 2.0)))
    cut = steps[len(steps) * 2 // 3]
    train = [(f, y) for i, f, y in samples if i + AHEAD <= cut]
    test = [(f, y) for i, f, y in samples if i >= cut]
    if len(train) < 200 or len(test) < 100:
        return None
    c0 = _ridge(train, 2, 50.0)
    err_model = sum(abs(y - _apply(c0, f, 0.7)) for f, y in test) / len(test)
    err_still = sum(abs(y) for _, y in test) / len(test)
    used = err_model < err_still * MIN_GAIN
    log(f"best choice: votes model on {len(samples)} samples: on the unseen third it misses a pool's votes after 48 "
        f"hours by {(math.exp(err_model) - 1) * 100:.0f}%, 'votes stay as they are' by "
        f"{(math.exp(err_still) - 1) * 100:.0f}%" + ("" if used else ": not used"))
    return {"coef": [round(c, 5) for c in _ridge([(f, y) for _, f, y in samples], 2, 50.0)],
            "err": round(math.exp(err_model), 4), "still": round(math.exp(err_still), 4), "n": len(samples),
            "test": len(test), "used": used}


def update(state, now, log):
    """Fits the two models again when due."""
    bc = TR.book(state).setdefault("bc", {})
    if now - bc.get("at", 0) < EVERY:
        return bc
    t0 = time.time()
    try:
        fees, votes = fit_fees(state, now, log), fit_votes(state, now, log)
    except Exception as e:
        log(f"best choice: models not fitted: {type(e).__name__}: {str(e)[:200]}")
        return bc
    if fees:
        bc["fees"] = fees
    if votes:
        bc["votes"] = votes
    bc["at"] = now
    log(f"best choice: models fitted in {time.time() - t0:.1f} s")
    return bc


def _stock_pool(state, key):
    from . import stocks as S
    g = state["gauges"].get(key) or {}
    cid = A.chain_of(key)
    for tok in (g.get("t0"), g.get("t1")):
        e = state["erc20"].get(A.tkey(cid, tok)) if tok else None
        if e and S._is_stock(cid, tok.lower(), e.get("sym"), e.get("dec")):
            return True
    return False


def annotate(state, pools, now, log):
    """Sets on each pool of the report: bc_f (fees expected in the next 48 hours, USD), bc_e (the pool's typical
    error as a factor), bc_a (votes expected on average over the 48 hours, as a factor of today's). Returns
    {gauge: fee forecast} for the live checks."""
    bc = TR.book(state).get("bc") or {}
    fm, vm = bc.get("fees"), bc.get("votes")
    out = {}
    h0 = now - now % HOUR
    sc = TR.scores(state)
    T = TR.book(state)
    if fm:
        fh = {p["key"]: A.fee_hours(state, p["key"]) for p in pools if p.get("key")}
        mx = [sum(h.get(t, 0.0) for h in fh.values()) for t in range(h0 - 96 * HOUR, h0, HOUR)]
        ms = TR._season(fm["mb"], h0, 48) if fm.get("mb") else 1.0
        mv24, mv96 = fm.get("mv") or [0, 0]
        mvr = _lr(mv24, mv96, 2.0)
        lv = (T.get("lv") or {}).get("s") or {}
        for p in pools:
            key = p.get("key")
            look = TR._measured_look(state, key, h0) if key else 0
            if not look:
                continue
            x = [fh[key].get(t, 0.0) for t in range(h0 - 96 * HOUR, h0, HOUR)]
            if look < 96:
                x = x[48:] + x[48:]                    # four days not measured yet: the two measured ones twice
            tp = T["p"].get(key) or {}
            closed = CLOSED if tp.get("b") and _stock_pool(state, key) else ()
            s = TR._season(tp["b"], h0, 48, closed) if tp.get("b") else 1.0
            hx = tp.get("hx")
            v = [float(a) for a in hx[2][-96:]] if hx and len(hx[2]) >= 96 else None
            base, f = _fee_feats(x, s, ms, mx, v, None)
            if f is None:
                continue
            f[8] = mvr
            if fm.get("used"):
                usd, name = base * math.exp(_apply(fm["coef"], f)), TR.MODEL
            else:
                simple = TR.forecast(state, key, now, sc)
                if not simple:
                    continue
                usd, name = simple[0], simple[1]
                if simple[2]:
                    p["bc_f"], p["bc_e"], p["bc_by"] = round(usd, 2), round(simple[2], 3), name
                    continue
            # the pool's typical error: its backtest with the model, and its live checks (three times the weight)
            e = list(fm["pe"].get(key) or [0.0, 0]) if fm.get("used") else [0.0, 0]
            own = (lv.get(key) or {}).get(name)
            if own:
                e = [e[0] + own[0] * TR.LIVE_WEIGHT, e[1] + own[2] * TR.LIVE_WEIGHT]
                w = own[2] / (own[2] + TR.SHRINK)
                usd /= math.exp(w * max(-0.35, min(0.35, own[1] / own[2])))      # its own bias, as far as checked
            glob = fm["err"] if fm.get("used") else fm["simple"]
            err = math.exp((e[0] + math.log(glob) * 10) / (e[1] + 10))          # toward everyone's with few samples
            p["bc_f"], p["bc_e"], p["bc_by"] = round(usd, 2), round(err, 3), name
            if fm.get("used"):
                out[key] = round(usd, 4)
    if vm and vm.get("used"):
        was = (state.get("ap_hist") or {}).get(str(h0 - 24 * HOUR)) or {}
        rows = [(p, (p.get("fees48h") or 0) + (p.get("inc48h") or 0), float(p.get("ap") or 0)) for p in pools]
        tot_ap = sum(a for _, _, a in rows)
        mkt = sum(r for _, r, _ in rows) / tot_ap if tot_ap > 0 else 0
        tot24 = sum(was.values()) or tot_ap
        seen = sum(a for p, _, a in rows if p.get("key") in was) or tot_ap       # the same pools, today
        for p, r48, ap in rows:
            if ap < 10_000 or not p.get("key"):
                continue
            d = _apply(vm["coef"], _vote_feats(r48, ap, float(was.get(p["key"]) or ap), mkt, seen, tot24), 0.7)
            p["bc_a"] = round(math.exp(max(-0.5, d) / 2), 3)       # the average of the 48 hours: half the way
    return out


def export(state):
    bc = TR.book(state).get("bc") or {}
    out = {}
    if bc.get("fees"):
        f = bc["fees"]
        out["fees"] = {k: f[k] for k in ("err", "simple", "base", "n", "test", "used")}
        out["fees"]["coef"] = dict(zip(FEE_FEATS, f["coef"][1:]))
    if bc.get("votes"):
        v = bc["votes"]
        out["votes"] = {k: v[k] for k in ("err", "still", "n", "test", "used")}
        out["votes"]["coef"] = dict(zip(VOTE_FEATS, v["coef"][1:]))
    out["closed"] = sorted(CLOSED)
    return out

