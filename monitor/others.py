"""The other allocators: what they are likely to do in the next 47 hours, position by position.

The record. Every allocation of every position on every chain (the LeafVoter's GaugesAllocated) is kept for 35 days:
the new ones from each run, the older ones read once from each chain's history, a few windows per run (backfill). A
deallocation is an allocation to a placeholder address (SINK) followed by Deallocated in the same transaction: without
the placeholder, a position's allocation on a chain is what its last event says. Replayed this way the record gives
back the allocations the monitor reads from the chains for 99.9% of the positions (checked on 21 November 2026).
A position allocates through the root voter and each chain hears it minutes apart: the events of one position within
three hours are one decision. An allocation that keeps the pools and their weights within 2% (a deposit or a
withdrawal rescaling it) is not a decision.

Timing. After a decision a position cannot move for the cooldown (47 hours); how soon after that it moves is a habit.
Of the positions that moved within three hours of the end of their cooldown, half did it again the next time; of the
others, a quarter. The chance of the next decision, hour by hour after the end of the cooldown, is estimated for nine
groups (under 10,000 votes, under a million, more; no decision yet, usually within three hours, otherwise), weighted
by votes (at most 5 million per position), on the record before the moment it is used for. The share of its votes a
position moves is its own average, toward its group's while it has moved only a few times.

Forecast. For every pool with at least 30,000 votes, four numbers: the share of its votes expected to leave in the
next 47 hours (averaged over the hours: a vote leaving in hour 40 counts 7/47), its APR (fees and incentives of the
last 48 hours per vote) against the whole market's, above and below it separately, and the votes that entered it in
the last 6 hours. A robust regression turns them into the pool's average votes over the 47 hours against today's,
relative to the typical pool's change (growth of the whole market does not choose between pools). It is fitted on
what happened after every six-hour mark of the record, with five times the weight on the 20 pools with the highest
APR: those are the ones an allocation chooses among. The last 7 days are kept out of the fit and checked: the error on
those pools' votes, and what the 3, 5 and 10 pools with the highest APR, counted with the forecast votes instead of
today's, really paid. The forecast is used only where it beat "votes stay as they are" there.

On the record of 22 October - 21 November 2026, checked on the last week (28 marks): the 5 pools with the highest APR
gained 20% of votes over the 47 hours compared with the typical pool; the forecast cut the error on their votes from
58% to 51%, and the 3 pools chosen with it paid 1.44 times the typical pool against 1.36 with today's votes (better
at 11 marks, worse at 6).
"""
import bisect
import collections
import math
import time

from . import aero as A
from . import chain as C
from .rpc import RpcError, TimeBudget

HOUR, DAY = 3600, 86400
SINK = "0x2e67fe7bd6843e2d0095f5d02c60a7cd29a26902"   # deallocations pass through it (see above)
KEEP = 35 * DAY             # allocations kept
MERGE = 3 * HOUR            # one decision reaches the chains up to this far apart
COOLDOWN = 169200           # 47 hours; the chains' own value replaces it when known
AHEAD = 47                  # hours forecast: the cooldown of an allocation made now
EDGES = [0, 1, 2, 4, 8, 16, 24, 48, 96, 192, 384]       # hours after the cooldown: the hazard is flat within each
SIZE_CUTS = (10_000, 1_000_000)
PROMPT_H = 3
RESCALE = 0.02
W_CAP = 5_000_000           # votes counted per position when the habits are estimated
MIN_V = 30_000              # pools forecast (and fitted on)
TOP_N, TOP_W = 20, 5.0      # the pools with the highest APR weigh this much more in the fit
STEP = 6 * HOUR             # marks of the record the regression learns from
WARM = 6 * DAY              # the first mark: this long after the record starts
TEST = 7 * DAY              # the last days kept out of the fit and checked
ROWS_KEEP = 28 * DAY
ROW_BUDGET = 60             # seconds per run spent on past marks
BF_BUDGET = 100             # seconds per chain and run spent reading the history
HUBER = 0.1
ROWS_V = 1                  # version of the marks' measure: a change measures them again
FEATS = ["voti in uscita previsti", "APR sopra il mercato", "APR sotto il mercato", "voti entrati nelle ultime 6 ore"]
GROUP_NAMES = ["nuove", "puntuali", "le altre"]
SIZE_NAMES = ["sotto 10.000 voti", "da 10.000 a 1 milione", "oltre 1 milione"]


# ---------------------------------------------------------------- the record

def book(state):
    b = state.get("oth")
    if not isinstance(b, dict) or b.get("v") != 1:
        b = state["oth"] = {"v": 1, "gi": [], "log": {}, "bf": {}, "rows": {}, "tim": None, "fit": None, "fc": None}
    return b


_GIDX = {}


def _gidx(b):
    gi = b["gi"]
    m = _GIDX.get(id(gi))
    if m is None or len(m) != len(gi):
        _GIDX.clear()
        m = _GIDX[id(gi)] = {k: i for i, k in enumerate(gi)}
    return m


def _pay(b, cid, allocs):
    gidx, out = _gidx(b), []
    for g, v in allocs.items():
        g = g.lower()
        if g == SINK or not v or v <= 0:
            continue
        k = A.gkey(cid, g)
        i = gidx.get(k)
        if i is None:
            i = gidx[k] = len(b["gi"])
            b["gi"].append(k)
        out.append([i, round(float(v), 3)])
    return out


def record(state, cid, events, prev_last):
    """This run's allocations on one chain. The first time, the blocks before this run's are left to the backfill."""
    b = book(state)
    c = str(int(cid))
    lg = b["log"].setdefault(c, [])
    if c not in b["bf"]:
        b["bf"][c] = {"next": 0, "to": int(prev_last or 0), "done": not prev_last}
    new = sorted((e for e in events if e.get("kind") == "leaf_alloc"), key=lambda e: (e["block"], e["idx"]))
    for e in new:
        lg.append([int(e["ts"]), int(e["block"]), int(e["idx"]), int(e["token"]), _pay(b, cid, e["allocs"])])


def backfill_job(state, cid):
    """What the chain's worker needs to read its history this run (a copy), or None."""
    bf = (book(state)["bf"].get(str(int(cid))) or {})
    return dict(bf) if bf and not bf.get("done") else None


def backfill(rpc, cid, cs, ccfg, job, deadline, log):
    """Older allocations of one chain, in windows of 12 hours of blocks within the time budget. Runs in the chain's
    worker: it touches only its chain and returns what it found."""
    if not job:
        return None
    cid = int(cid)
    t0 = time.time()
    stop = min(deadline, t0 + BF_BUDGET)
    nxt = int(job.get("next") or 0)
    if not nxt:
        nxt = A.start_block(rpc, cid, cs, ccfg, cs.get("head") or 0, cs.get("head_ts") or int(t0), log)
    to = int(job["to"])
    win = A._blocks(12 * HOUR, cs.get("bt") or 2.0, 2000, 2_000_000)
    # wide ranges first (old blocks are sparse): the reader halves them where an endpoint refuses
    chunk = min(win, max(int(cs.get("leaf_chunk") or 0), 50_000))
    clock = A.ts_fn(cid, cs) if cid == C.BASE_CHAIN and not cs.get("ts_drift") else None
    found, n = [], 0
    while nxt <= to and time.time() < stop:
        hi = min(to, nxt + win - 1)
        try:
            if clock:
                ts_of = clock
            else:
                ta, tb = rpc.block_ts(nxt), rpc.block_ts(hi)
                ts_of = (lambda lo, up, ta, tb: lambda blk: int(ta + (blk - lo) * (tb - ta) / max(1, up - lo)))(nxt, hi, ta, tb)
            raw = list(rpc.get_logs(nxt, hi, address=C.LEAF, topics=[[C.T["leaf_alloc"]]], chunk=chunk,
                                    max_chunk=win, deadline=stop))
        except TimeBudget:
            break
        except RpcError as e:
            log(f"{C.chain_short(cid)}: allocation history paused at block {nxt}: {str(e)[:140]}")
            break
        chunk = rpc.last_chunk or chunk
        for lg in raw:
            ev = C.decode_log(lg, ts_of)
            if ev and ev["kind"] == "leaf_alloc":
                found.append((ev["ts"], ev["block"], ev["idx"], ev["token"], ev["allocs"]))
        n += 1
        nxt = hi + 1
    return {"events": found, "next": nxt, "to": to, "done": nxt > to, "secs": round(time.time() - t0, 1), "windows": n}


def merge_backfill(state, cid, res, log):
    if not res:
        return
    b = book(state)
    c = str(int(cid))
    rows = [[int(ts), int(blk), int(idx), int(tok), _pay(b, cid, al)] for ts, blk, idx, tok, al in res["events"]]
    lg = b["log"].setdefault(c, [])
    if rows:
        lg.extend(rows)
        lg.sort(key=lambda r: (r[1], r[2]))
    bf = b["bf"][c]
    bf["next"] = int(res["next"])
    bf["done"] = bool(res["done"])
    left = max(0, int(res["to"]) - int(res["next"]) + 1)
    log(f"{C.chain_short(int(cid))}: allocation history +{len(rows)} events in {res['windows']} windows, "
        f"{res['secs']}s" + (", complete" if res["done"] else f", {left:,} blocks to go"))


def prune(state, now):
    """Allocations older than KEEP go, except each position's last one before that on each chain."""
    b = book(state)
    cut = now - KEEP
    for c, lg in b["log"].items():
        if not lg or lg[0][0] >= cut:
            continue
        last = {}
        i = 0
        while i < len(lg) and lg[i][0] < cut:
            last[lg[i][3]] = lg[i]
            i += 1
        lg[:] = sorted((r for r in last.values() if r[4]), key=lambda r: (r[1], r[2])) + lg[i:]


def chains_ready(state):
    b = book(state)
    return sorted(c for c, bf in b["bf"].items() if bf.get("done"))


# ---------------------------------------------------------------- positions

class Pos:
    """One position's decisions, across chains: times, allocations after each, which are decisions, and for each
    decision after the first its delay after the cooldown (hours) and the share of its votes it moved."""
    __slots__ = ("t", "a", "dec", "delay", "moved", "w")

    def __init__(self, acts, cooldown):
        self.t = [x[0] for x in acts]
        self.a = [x[1] for x in acts]
        n = len(acts)
        self.dec = [False] * n
        self.delay = [None] * n
        self.moved = [None] * n
        self.w = [0.0] * n
        prev_dec = None
        for k in range(n):
            if k == 0 or not self.a[k] or _norm_moved(self.a[k - 1], self.a[k]) >= RESCALE:
                self.dec[k] = True
                if prev_dec is not None:
                    self.delay[k] = (self.t[k] - self.t[prev_dec] - cooldown) / HOUR
                    old, new = self.a[k - 1], self.a[k]
                    s0 = sum(old.values())
                    if s0 > 0:
                        self.moved[k] = 1 - sum(min(v, new.get(g, 0.0)) for g, v in old.items()) / s0
                        self.w[k] = s0
                prev_dec = k

    def at(self, t):
        """Index of the last allocation before t, or -1."""
        return bisect.bisect_left(self.t, t) - 1

    def habits(self, idx):
        """Up to allocation idx: (delays of the decisions, [(moved share, votes)], index of the last decision)."""
        ds, mv, last = [], [], -1
        for k in range(idx + 1):
            if self.dec[k]:
                last = k
                if self.delay[k] is not None:
                    ds.append(self.delay[k])
                if self.moved[k] is not None:
                    mv.append((self.moved[k], self.w[k]))
        return ds, mv, last


def _norm_moved(a0, a1):
    s0, s1 = sum(a0.values()), sum(a1.values())
    if s0 <= 0 or s1 <= 0:
        return 1.0
    return 1 - sum(min(v / s0, a1.get(g, 0.0) / s1) for g, v in a0.items())


def positions(state, chains, cooldown):
    """{position: Pos} from the record of the given chains: one decision per position within MERGE."""
    b = book(state)
    gi = b["gi"]
    evs = []
    for c in chains:
        ci = int(c)
        for r in b["log"].get(c) or []:
            evs.append((r[0], ci, r[1], r[2], r[3], r[4]))
    evs.sort()
    per, acts = {}, {}
    for ts, ci, _, _, tid, pay in evs:
        pc = per.setdefault(tid, {})
        pc[ci] = {gi[i]: v for i, v in pay}
        full = {}
        for a in pc.values():
            full.update(a)
        lst = acts.setdefault(tid, [])
        if lst and ts - lst[-1][2] <= MERGE:
            lst[-1][1] = full
        else:
            lst.append([ts, full, ts])
    return {tid: Pos(lst, cooldown) for tid, lst in acts.items()}


def _size_class(s):
    return sum(1 for c in SIZE_CUTS if s >= c)


def _habit(ds):
    if not ds:
        return 0
    p = (sum(1 for d in ds if d <= PROMPT_H) + 0.5) / (len(ds) + 1.5)
    return 1 if p >= 0.5 else 2


def _group(size, ds):
    return _size_class(size) * 3 + _habit(ds)


def _bucket(d):
    for i in range(len(EDGES) - 1, -1, -1):
        if d >= EDGES[i]:
            return i
    return -1


def fit_timing(P, t_cut):
    """Hazard of the next decision per group and bucket of hours after the cooldown (lam[group][bucket], per hour),
    and the share of its votes each group moves (m[group]), from the decisions before t_cut."""
    nb = len(EDGES)
    ev = [[0.0] * nb for _ in range(9)]
    ex = [[0.0] * nb for _ in range(9)]
    mn, md = [0.0] * 9, [0.0] * 9
    for p in P.values():
        n = len(p.t)
        ds_before = []
        for k in range(n):
            if p.t[k] >= t_cut:
                break
            if not p.dec[k]:
                continue
            if p.delay[k] is not None:
                ds_before.append(p.delay[k])
            s0 = sum(p.a[k].values())
            if s0 <= 0:
                continue
            j = next((j for j in range(k + 1, n) if p.dec[j]), None)
            t1 = p.t[j] if j is not None else None
            free = p.t[k] + COOLDOWN
            if t1 is not None and t1 <= t_cut and t1 < free:
                continue                     # moved before the end of its cooldown (a withdrawal): no timing
            end = min(t1, t_cut) if t1 is not None else t_cut
            d_end = (end - free) / HOUR
            if d_end < 0:
                continue
            g = _group(s0, ds_before)
            w = min(s0, W_CAP)
            for bi in range(nb):
                lo = EDGES[bi]
                hi = EDGES[bi + 1] if bi + 1 < nb else 1e9
                if d_end <= lo:
                    break
                ex[g][bi] += w * (min(d_end, hi) - lo)
            if t1 is not None and t1 <= t_cut:
                ev[g][_bucket(d_end)] += w
                if p.moved[j] is not None:
                    mn[g] += p.moved[j] * w
                    md[g] += w
    pool_ev = [sum(ev[g][bi] for g in range(9)) for bi in range(nb)]
    pool_ex = [sum(ex[g][bi] for g in range(9)) for bi in range(nb)]
    lam = []
    for g in range(9):
        row = []
        for bi in range(nb):
            base = pool_ev[bi] / pool_ex[bi] if pool_ex[bi] > 0 else 0.0
            k = 5e6 * (EDGES[bi + 1] - EDGES[bi] if bi + 1 < nb else 400)
            row.append(round((ev[g][bi] + base * k) / (ex[g][bi] + k), 6))
        lam.append(row)
    tot = sum(mn) / sum(md) if sum(md) > 0 else 0.7
    m = [round((mn[g] + tot * 5e6) / (md[g] + 5e6), 4) for g in range(9)]
    return {"lam": lam, "m": m, "at": int(t_cut)}


def act_probs(lam_g, d0, hours=AHEAD):
    """P(the next decision falls in hour h), h = 0..hours-1, for a position d0 hours past the end of its cooldown
    (negative: still in it) that has not decided yet."""
    out, surv = [], 1.0
    for h in range(hours):
        d = d0 + h
        if d + 1 <= 0:
            out.append(0.0)
            continue
        lo = max(0.0, d)
        p = surv * (1 - math.exp(-lam_g[_bucket(lo)] * (d + 1 - lo)))
        out.append(p)
        surv -= p
    return out


# ---------------------------------------------------------------- features of one moment

def revenue_fn(state):
    """rev(pool, t0, t1): fees and incentives its voters received between two hours (the simulation's pool history:
    hourly for 8 days, then daily)."""
    tr = state.get("tracker") or {}
    hourly, daily = tr.get("pool_hist") or {}, tr.get("pool_daily") or {}

    def rev(k, t0, t1):
        h, d = hourly.get(k) or {}, daily.get(k) or {}
        s = 0.0
        for t in range(t0 - t0 % HOUR, t1, HOUR):
            v = h.get(str(t))
            if v is not None:
                s += v[0]
            else:
                dv = d.get(str(t - t % DAY))
                if dv is not None:
                    s += dv[0] / 24
        return s
    return rev


def moment(P, t, tim, rev, detail=False):
    """At time t (an hour mark): every pool's votes, the share expected to leave in the next AHEAD hours (averaged
    over them), the votes that entered in the last 6 hours, its 48-hour revenue; with detail, each position's
    forecast too."""
    V0 = collections.defaultdict(float)
    out = collections.defaultdict(float)
    in6 = collections.defaultdict(float)
    move_h = [0.0] * AHEAD
    pos = []
    for tid, p in P.items():
        idx = p.at(t)
        if idx < 0:
            continue
        a0 = p.a[idx]
        j6 = p.at(t - 6 * HOUR)
        if j6 < idx:
            a6 = p.a[j6] if j6 >= 0 else {}
            for g, v in a0.items():
                if v > a6.get(g, 0.0):
                    in6[g] += v - a6.get(g, 0.0)
        if not a0:
            continue
        size = sum(a0.values())
        ds, mv, last = p.habits(idx)
        g = _group(size, ds)
        d0 = (t - p.t[last] - COOLDOWN) / HOUR
        pr = act_probs(tim["lam"][g], d0)
        mi = (sum(f for f, _ in mv) + 2 * tim["m"][g]) / (len(mv) + 2)
        cum = gone = 0.0
        for h, x in enumerate(pr):
            cum += x
            gone += cum / AHEAD
            move_h[h] += x * mi * size
        for k, v in a0.items():
            V0[k] += v
            out[k] += gone * mi * v
        if detail:
            pos.append((tid, size, p.t[last] + COOLDOWN, cum, gone * mi, mi, g, ds, a0))
    rv = {k: rev(k, t - 48 * HOUR, t) for k in V0}
    return {"V0": V0, "out": out, "in6": in6, "rev": rv, "move_h": move_h, "pos": pos}


def _pool_rows(m):
    """[(pool, votes, outflow share, log APR against the market, inflow share, revenue 48h)] for the pools forecast."""
    V0, rv = m["V0"], m["rev"]
    tv = sum(V0.values())
    mk = (sum(rv.values()) + 1e-6) / tv if tv > 0 else 1.0
    rows = []
    for k, v in V0.items():
        if v < MIN_V:
            continue
        la = max(-3.0, min(3.0, math.log((rv.get(k, 0.0) + 1e-6) / v / mk)))
        rows.append((k, v, m["out"][k] / v, la, m["in6"].get(k, 0.0) / v, rv.get(k, 0.0)))
    return rows


def _feats(out, la, inn):
    return [out, max(0.0, la), min(0.0, la), math.log1p(inn)]


def _centered(rows):
    """Features of one moment minus their median across its pools."""
    fs = [_feats(r[2], r[3], r[4]) for r in rows]
    if not fs:
        return []
    med = [_median([f[i] for f in fs]) for i in range(len(FEATS))]
    return [[a - b for a, b in zip(f, med)] for f in fs]


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return 0.0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


# ---------------------------------------------------------------- what happened after each mark

def _averages(P, t):
    """Every pool's average votes over the AHEAD hour marks after t."""
    avg = collections.defaultdict(float)
    end = t + AHEAD * HOUR
    for p in P.values():
        idx = p.at(t)
        cur = p.a[idx] if idx >= 0 else {}
        for k, v in cur.items():
            avg[k] += v
        j = idx + 1
        while j < len(p.t) and p.t[j] < end:
            # an allocation at time tau counts from the first hour mark after it
            first = int((p.t[j] - t) // HOUR) + 1
            share = (AHEAD - first + 1) / AHEAD
            new = p.a[j]
            for k in set(cur) | set(new):
                d = new.get(k, 0.0) - cur.get(k, 0.0)
                if d:
                    avg[k] += d * share
            cur = new
            j += 1
    return avg


def build_rows(state, P, now, rev, log, budget=ROW_BUDGET):
    """Rows of the regression for the marks whose 47 hours are over: the moment's pools with what happened after.
    Each mark uses the habits estimated on the record before it. Within the time budget, oldest first."""
    b = book(state)
    rows = b.setdefault("rows", {})
    first = min((p.t[0] for p in P.values() if p.t), default=now) + WARM
    last = now - AHEAD * HOUR
    for k in [k for k in rows if int(k) < now - ROWS_KEEP]:
        del rows[k]
    lo = max(first, now - ROWS_KEEP)
    lo += -lo % STEP
    todo = [t for t in range(lo, last + 1, STEP) if str(t) not in rows]
    t0, done = time.time(), 0
    gidx = _gidx(b)
    for t in todo:
        if time.time() - t0 > budget:
            break
        tim = fit_timing(P, t)
        m = moment(P, t, tim, rev)
        pr = _pool_rows(m)
        avg = _averages(P, t)
        out = []
        for k, v, o, la, inn, r48 in pr:
            a = avg.get(k, 0.0)
            if a <= 0:
                continue
            i = gidx.get(k)
            if i is None:
                i = gidx[k] = len(b["gi"])
                b["gi"].append(k)
            out.append([i, round(v), round(math.log(a / v), 5), round(o, 5), round(la, 4), round(inn, 5),
                        round(r48, 2), round(rev(k, t, t + AHEAD * HOUR), 2)])
        rows[str(t)] = out
        done += 1
    if done:
        log(f"others: {done} marks of the record measured ({len(rows)} kept, {len(todo) - done} to go)")
    return len(todo) - done


# ---------------------------------------------------------------- the regression

def _solve(M, y):
    n = len(y)
    A_ = [row[:] + [y[i]] for i, row in enumerate(M)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(A_[r][i]))
        A_[i], A_[piv] = A_[piv], A_[i]
        if abs(A_[i][i]) < 1e-15:
            continue
        for r in range(n):
            if r != i and A_[r][i]:
                f = A_[r][i] / A_[i][i]
                A_[r] = [x - f * z for x, z in zip(A_[r], A_[i])]
    return [A_[i][n] / A_[i][i] if abs(A_[i][i]) > 1e-15 else 0.0 for i in range(n)]


def _huber(data, d, lam=1e-3, iters=30):
    """Weighted robust least squares without intercept: data [(features, target, weight)]."""
    beta = [0.0] * d
    if not data:
        return beta
    for _ in range(iters):
        M = [[0.0] * d for _ in range(d)]
        y = [0.0] * d
        for x, t, wt in data:
            res = t - sum(b * v for b, v in zip(beta, x))
            w = wt * (1.0 if abs(res) <= HUBER else HUBER / abs(res))
            for i in range(d):
                wx = w * x[i]
                y[i] += wx * t
                Mi = M[i]
                for j in range(d):
                    Mi[j] += wx * x[j]
        for i in range(d):
            M[i][i] += lam * len(data)
        nb = _solve(M, y)
        done = max(abs(a - b) for a, b in zip(nb, beta)) < 1e-7
        beta = nb
        if done:
            break
    return beta


def _mark(rows):
    """One mark's rows -> (features centered, targets centered, APR ranks, rows)."""
    if len(rows) < 10:
        return None
    pr = [(None, r[1], r[3], r[4], r[5]) for r in rows]
    fs = _centered(pr)
    ys = [r[2] for r in rows]
    my = _median(ys)
    order = sorted(range(len(rows)), key=lambda i: -(rows[i][6] / rows[i][1]))
    rank = [0] * len(rows)
    for pos, i in enumerate(order):
        rank[i] = pos
    return fs, [y - my for y in ys], rank, rows


def fit(state, now, log):
    """Fits the regression on the marks before the last TEST days, checks it on those, then fits it on everything."""
    b = book(state)
    marks = {int(t): _mark(r) for t, r in (b.get("rows") or {}).items()}
    marks = {t: m for t, m in marks.items() if m}
    if len(marks) < 4 * 7 + 4 * 3:            # a week to check and at least three days to learn from
        return None
    last = max(marks)
    cut = last - TEST + STEP
    d = len(FEATS)

    def data(ts):
        out = []
        for t in ts:
            fs, ys, rank, _ = marks[t]
            out += [(f, y, TOP_W if r < TOP_N else 1.0) for f, y, r in zip(fs, ys, rank)]
        return out
    train = [t for t in marks if t + AHEAD * HOUR <= cut]
    test = [t for t in marks if t >= cut]
    if len(train) < 12 or len(test) < 8:
        return None
    coef = _huber(data(train), d)
    err = {"all": [0.0, 0.0, 0], "top5": [0.0, 0.0, 0], "top10": [0.0, 0.0, 0]}
    real = {n: [0.0, 0.0, 0, 0, 0] for n in (3, 5, 10)}
    for t in test:
        fs, ys, rank, rows = marks[t]
        pred = [sum(c * x for c, x in zip(coef, f)) for f in fs]
        for y, p, r in zip(ys, pred, rank):
            for k in ("all",) + (("top5",) if r < 5 else ("top10",) if r < 10 else ()):
                err[k][0] += abs(y)
                err[k][1] += abs(y - p)
                err[k][2] += 1
        # what the pools chosen by APR really paid per vote over the 47 hours, against the typical pool
        paid = [r[7] / (r[1] * math.exp(r[2])) for r in rows]
        mk = _median(paid)
        if mk <= 0:
            continue
        still = sorted(range(len(rows)), key=lambda i: -(rows[i][6] / rows[i][1]))
        model = sorted(range(len(rows)), key=lambda i: -(rows[i][6] / (rows[i][1] * math.exp(pred[i]))))
        for n in real:
            a = sum(paid[i] for i in still[:n]) / n / mk
            c = sum(paid[i] for i in model[:n]) / n / mk
            o = real[n]
            o[0] += a
            o[1] += c
            o[2] += 1
            o[3] += c > a + 1e-9
            o[4] += c < a - 1e-9
    e = {k: [round(math.exp(v[0] / v[2]) - 1, 4), round(math.exp(v[1] / v[2]) - 1, 4), v[2]] for k, v in err.items() if v[2]}
    r = {str(n): [round(v[0] / v[2], 4), round(v[1] / v[2], 4), v[2], v[3], v[4]] for n, v in real.items() if v[2]}
    used = bool(e.get("top5") and e["top5"][1] < e["top5"][0] and r.get("3") and r["3"][1] >= r["3"][0]
                and r.get("5") and r["5"][1] >= r["5"][0])
    full = _huber(data(sorted(marks)), d)
    res = {"at": now, "coef": [round(c, 5) for c in full], "test_coef": [round(c, 5) for c in coef],
           "marks": len(marks), "train": len(train), "test": len(test), "from": min(marks), "to": last,
           "err": e, "real": r, "used": used}
    log("others: forecast of the votes fitted on %d marks; on the last %d days (%d marks) the 5 pools with the highest "
        "APR: votes missed by %.0f%% (votes stay: %.0f%%); the 3 chosen with it paid %.2fx the typical pool "
        "(with today's votes %.2fx)%s" % (len(marks), TEST // DAY, len(test), e["top5"][1] * 100, e["top5"][0] * 100,
                                         r["3"][1], r["3"][0], "" if used else ": not used"))
    return res


# ---------------------------------------------------------------- every run

def update(state, now, log, cooldown=None):
    """Keeps the record, fits when due, and forecasts the next AHEAD hours. Returns the forecast or None."""
    global COOLDOWN
    if cooldown:
        COOLDOWN = int(cooldown)
    b = book(state)
    prune(state, now)
    chains = chains_ready(state)
    if not chains or str(C.BASE_CHAIN) not in chains:
        b["fc"] = None
        return None
    if b.get("rows_of") != [ROWS_V, chains]:
        # the marks are measured again when a chain's history completes or the way they are measured changes
        b["rows"], b["rows_of"] = {}, [ROWS_V, chains]
    t0 = time.time()
    P = positions(state, chains, COOLDOWN)
    rev = revenue_fn(state)
    left = build_rows(state, P, now, rev, log)
    h0 = now - now % HOUR
    if not b.get("tim") or now - int(b["tim"].get("at") or 0) >= STEP:
        b["tim"] = fit_timing(P, h0)
    if left == 0 and (not b.get("fit") or now - int(b["fit"].get("at") or 0) >= STEP
                      or b["fit"].get("chains") != chains):
        f = fit(state, now, log)
        if f:
            f["chains"] = chains
            b["fit"] = f
    b["fc"] = forecast(state, P, h0, rev, chains)
    log(f"others: forecast in {time.time() - t0:.1f}s on {len(P)} positions of {len(chains)} chains" +
        (f", {left} marks still to measure" if left else ""))
    return b["fc"]


def forecast(state, P, h0, rev, chains):
    b = book(state)
    tim, ft = b.get("tim"), b.get("fit")
    m = moment(P, h0, tim, rev, detail=True)
    rows = _pool_rows(m)
    fs = _centered(rows)
    coef = ft["coef"] if ft else None
    pools = {}
    for r, f in zip(rows, fs):
        y = sum(c * x for c, x in zip(coef, f)) if coef else 0.0
        pools[r[0]] = [round(math.exp(max(-1.5, min(1.5, y))), 4), round(r[2], 4), round(r[4], 4), round(r[3], 3)]
    # positions: the expected share of their votes gone, averaged over the hours (the page puts back the viewer's)
    _EX.clear()
    for tid, size, free, p47, gone, mi, g, ds, a0 in m["pos"]:
        if gone >= 0.001:
            _EX[int(tid)] = round(gone, 3)
    typ = [round(_median([r[2] for r in rows]), 4), round(_median([r[4] for r in rows]), 4)] if rows else [0.0, 0.0]
    return {"at": h0, "pools": pools, "typ": typ, "move_h": [round(x) for x in m["move_h"]],
            "used": bool(ft and ft.get("used")), "chains": chains, "pos": _movers(state, m["pos"], h0),
            "free": _free_summary(m["pos"], h0)}


def _movers(state, pos, h0, n=25):
    """The positions expected to move the most votes in the next AHEAD hours."""
    out = []
    for tid, size, free, p47, gone, mi, g, ds, a0 in sorted(pos, key=lambda x: -x[1] * x[3] * x[5])[:n]:
        if size * p47 * mi < 100_000:
            continue
        top = sorted(a0.items(), key=lambda kv: -kv[1])[:3]
        out.append({"id": int(tid), "votes": round(size), "free": int(free), "p": round(p47, 3), "moved": round(mi, 3),
                    "habit": g % 3, "n": len(ds), "delay": round(_median(ds), 1) if ds else None,
                    "pools": [[A.gauge_name(state, k), C.chain_short(A.chain_of(k)), round(v)] for k, v in top],
                    "n_pools": len(a0)})
    return out


def _free_summary(pos, h0):
    """Votes free to move now, and becoming free in the next AHEAD hours."""
    now_free = soon = 0.0
    for tid, size, free, p47, gone, mi, g, ds, a0 in pos:
        if free <= h0:
            now_free += size
        elif free <= h0 + AHEAD * HOUR:
            soon += size
    return [round(now_free), round(soon)]


_EX = {}      # this run's forecast per position: share of its votes expected gone, averaged over the hours


def expected_gone(state):
    """{position: share of its votes expected to leave its pools, averaged over the next 47 hours}, this run's."""
    return dict(_EX) if book(state).get("fc") else {}


def summary(state):
    """For latest.json: whether the forecast is there and in use, and how it did on the last week."""
    b = book(state)
    fc, ft = b.get("fc"), b.get("fit")
    out = {"ready": bool(fc), "used": bool(fc and fc.get("used")), "min_votes": MIN_V, "ahead_h": AHEAD,
           "history": {C.chain_short(int(c)): bool(x.get("done")) for c, x in b["bf"].items()}}
    if fc:
        out["at"] = fc["at"]
        out["typ"] = fc.get("typ")         # the typical pool: share of its votes expected to leave, entered in 6 hours
    if ft:
        out["fit"] = {"at": ft["at"], "marks": ft["marks"], "test": ft["test"], "err": ft["err"], "real": ft["real"],
                      "coef": ft["coef"]}
    return out


def factors(state):
    """{pool: average votes over the next 47 hours against today's} when the forecast is in use, else {}."""
    fc = book(state).get("fc")
    if not fc or not fc.get("used"):
        return {}
    return {k: v[0] for k, v in fc["pools"].items()}


def annotate(state, pools):
    """On each pool of the report: oth_f (average votes over the next 47 hours against today's), oth_o (share of its
    votes expected to leave, averaged over the hours), oth_i (votes entered in the last 6 hours, against today's),
    oth_r (its 48-hour APR against the market's)."""
    fc = book(state).get("fc")
    if not fc:
        return
    for p in pools:
        v = fc["pools"].get(p.get("key"))
        if v:
            p["oth_f"], p["oth_o"], p["oth_i"] = v[:3]
            p["oth_r"] = round(math.exp(v[3]), 2) if len(v) > 3 else None


def export(state, now):
    """others.json: the forecast, how it was checked, and the habits behind it."""
    b = book(state)
    fc, ft, tim = b.get("fc"), b.get("fit"), b.get("tim")
    bf = {}
    for c, x in b["bf"].items():
        bf[C.chain_short(int(c))] = bool(x.get("done"))
    out = {"generated": now, "ready": bool(fc), "history": bf, "keep_days": KEEP // DAY, "ahead_h": AHEAD,
           "min_votes": MIN_V, "feats": FEATS}
    if ft:
        out["fit"] = {k: ft[k] for k in ("at", "coef", "marks", "train", "test", "from", "to", "err", "real", "used")}
    if tim:
        groups = []
        for g in range(9):
            lam = tim["lam"][g]
            groups.append({"size": SIZE_NAMES[g // 3], "habit": GROUP_NAMES[g % 3], "moved": tim["m"][g],
                           "p3": round(1 - math.exp(-sum(lam[_bucket(h)] for h in range(3))), 3),
                           "p24": round(1 - math.exp(-sum(lam[_bucket(h)] for h in range(24))), 3),
                           "p47": round(1 - math.exp(-sum(lam[_bucket(h)] for h in range(AHEAD))), 3)})
        out["groups"] = groups
    if fc:
        out["forecast"] = {"at": fc["at"], "used": fc["used"], "move_h": fc["move_h"], "free": fc["free"],
                           "movers": fc["pos"], "chains": [C.chain_short(int(c)) for c in fc["chains"]]}
        names = {}
        for k, v in fc["pools"].items():
            names[k] = [A.gauge_name(state, k), C.chain_short(A.chain_of(k))] + v
        out["pools"] = names
    return out
