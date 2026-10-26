"""Paper allocation of a fixed sAERO size.

Every run it records, for each strategy, the allocation it would recommend now and what that allocation is
expected to earn during one cooldown. Each recommendation is then followed for the whole cooldown with the
fees and incentives that really arrive, diluted by the real allocation of everybody else, so over time it
shows which pools and which way of choosing them actually pay the most.
"""
import heapq
import statistics

from . import aero as A
from . import chain as C
from . import marketdata as MD

HOUR = 3600
DAY = 86400
HIST_HOURS = 8 * 24
DAILY_DAYS = 90
CLOSED_DAYS = 30
STRATEGIES = {
    "48h": "Ultime 48 ore",
    "trend": "Tendenza 12 ore",
    "prudente": "Prudente",
    "nuove": "Chain nuove",
}
RULES = {}          # per-strategy settings from config.json ("strategies"), set by main


def new_tracker(size, now):
    return {"v": 1, "size": size, "start": now, "last": 0, "open": {s: [] for s in STRATEGIES},
            "closed": {s: [] for s in STRATEGIES}, "virtual": {}, "pool_hist": {}, "pool_daily": {},
            "pool_reco": {}}


# ---------------------------------------------------------------- recommendation

def candidates(state, prices, now, cooldown, strategy):
    """{gauge: (expected revenue during one cooldown in USD, allocation of everybody else)}."""
    ap = state["ap"]
    keys = set(state["fees"]) | {p["g"] for p in state["incentives"].values()}
    if strategy == "nuove":
        # only big pairs on the chains where Aero is still small, valued like "Ultime 48 ore"
        universe, _ = MD.new_chain_universe(state, RULES.get("nuove"))
        keys &= universe
    out = {}
    for k in keys:
        m = state["gauges"].get(k) or {}
        if m.get("ok") is False:
            continue  # not activated or zero emission cap: an ordinary position cannot vote it
        f48 = A.fees_usd(state, k, now - 48 * HOUR, now + HOUR)
        inc = A.incentive_usd(state, prices, k, now, now + cooldown)
        if strategy == "trend":
            f12 = A.fees_usd(state, k, now - 12 * HOUR, now + HOUR) * 4
            base = 0.5 * f48 + 0.5 * f12
        else:
            base = f48
        rev = base * cooldown / (48 * HOUR) + inc
        if rev > 0.01:
            out[k] = (rev, float(ap.get(k, 0.0)))
    if strategy == "prudente" and out:
        # pools far above the typical yield attract other allocators: assume their allocation grows
        ys = [r / a for r, a in out.values() if a >= 1_000_000]
        med = statistics.median(ys) if ys else None
        if med:
            for k, (r, a) in list(out.items()):
                y = r / max(a, 1.0)
                out[k] = (r, a * (1 + 0.5 * min(2.0, max(0.0, y / med - 1))) if a > 0 else a)
    return out


def recommend(cands, size, max_pools, slices=100):
    """Greedy marginal allocation in 1% slices; returns ({gauge: amount}, expected USD).

    max_pools is the LeafVoter limit, which applies to each chain separately.
    """
    if not cands or size <= 0:
        return {}, 0.0
    step = size / slices
    alloc = {}
    per_chain = {}

    def share(k, x):
        r, a = cands[k]
        return r * x / (a + x) if x > 0 else 0.0

    def gain(k):
        x = alloc.get(k, 0.0)
        return share(k, x + step) - share(k, x)

    heap = [(-gain(k), k) for k in cands]
    heapq.heapify(heap)
    for _ in range(slices):
        while heap:
            neg, k = heapq.heappop(heap)
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
    pred = sum(share(k, x) for k, x in alloc.items())
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

    for s in STRATEGIES:
        still = []
        for snap in tr["open"].get(s, []):
            t, alloc, pred, real = snap
            tot, per = earned(alloc)
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
                tr["closed"].setdefault(s, []).append([t, round(pred, 4), round(real, 4), len(alloc)])
            else:
                still.append([t, alloc, pred, real])
        tr["open"][s] = still
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
    for s in STRATEGIES:
        cands = candidates(state, prices, now, cooldown, s)
        alloc, pred = recommend(cands, tr["size"], max_pools)
        rows = {k: [round(x, 2), round(cands[k][0] * x / (cands[k][1] + x), 6), 0.0] for k, x in alloc.items()}
        if rows:
            tr["open"].setdefault(s, []).append([now, rows, round(pred, 6), 0.0])
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
    return {"generated": now, "size": size, "start": tr["start"], "collected_h": round(collected_h, 1),
            "new_chains": new_chains,
            "cooldown_h": round(cooldown / HOUR, 2), "aero_usd": aero_usd, "strategies": strategies,
            "pools": pools[:150]}
