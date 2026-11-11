"""Bootstrap finder: pools worth starting yourself, and pools someone has just started.

The play. A gauge with almost no votes and almost no liquidity, on a pair that trades a lot elsewhere: you vote it
(every allocator fee of the pool is yours) and stake some liquidity yourself to get swaps going (the emissions your
votes direct there come back to you as the main liquidity provider). For a day or two the pool's 48-hour APR still
shows the dead hours before, so nobody else sees it.

What the monitor adds for it:
- "to start": votable gauges with few votes and little liquidity whose pair (or whose token, in other pairs) trades
  elsewhere on the chain. The trading elsewhere comes from DexScreener's list of every pool of the token (up to 30),
  read in rotation for the tokens of these gauges and kept for some hours; where a token has not been read yet the
  hourly market snapshot (marketdata.py) stands in. Other Aero gauge pools of the same pair are counted apart.
- "just started": gauges with few votes whose measured fees of the last 24 hours dwarf those of the 24 hours
  before: the 48-hour APR has not caught up yet. With the share of the biggest voter (the one who started it) and
  how liquidity moved.
- emissions: every allocation event carries the chain's emissions per vote per second, and each gauge has an
  emission cap in AERO per second that follows the fees the pool makes (FactoryRegistry.emissionCap; an idle gauge
  sits at the minimum). Both are exported so the page can say what your votes would bring to a pool today, and what
  the cap would allow as fees grow (CAP_K: the median of cap over daily fees among the gauges that earn).

The page turns these into dollars for your votes and the liquidity you would add. Estimates, not promises: they
assume the pair's volume spreads like its liquidity.
"""
import statistics
import time

from . import aero as A
from . import chain as C
from . import earnings as E
from . import marketdata as MD

HOUR, DAY = 3600, 86400
VOTES_MAX = 250_000         # "to start": gauges with fewer votes than this ...
LIQ_MAX = 100_000           # ... and less liquidity than this in the Aero pool (USD)
MIN_PAIR_VOL = 25_000       # ... whose pair trades at least this much a day elsewhere (USD)
MIN_TOKEN_VOL = 150_000     # ... or whose rarer token does, in its other pairs
STARTED_VOTES_MAX = 3_000_000
STARTED_MIN_USD = 10        # "just started": at least this much in fees in the last 24 hours ...
STARTED_RATIO = 0.35        # ... and the 24 hours before at most this share of them
LOOKUPS = 60                # token lookups per run (DexScreener allows 300 a minute)
PACE_S = 0.25
TOKEN_EVERY = 6 * HOUR      # a token's pools are read again when older
TOKEN_KEEP = 3 * DAY
BUDGET_S = 40


def book(state):
    b = state.setdefault("boot", {})
    b.setdefault("t", {})
    return b


def _slug(state, chains_cfg, cid):
    return (chains_cfg.get(str(cid)) or {}).get("dexscreener") or \
        ((state.get("dex_slugs") or {}).get(str(cid)) or {}).get("slug")


def _sym(state, cid, tok):
    return ((state["erc20"].get(A.tkey(cid, tok)) or {}).get("sym") or "?") if tok else "?"


def _major(state, cid, tok):
    return _sym(state, cid, tok).upper() in MD.MAJORS


def _open_gauges(state):
    """Votable gauges with their pool and tokens: {key: (chain, pool, token0, token1)}."""
    out = {}
    for k, m in state["gauges"].items():
        if m.get("ok", True) and m.get("pool") and m.get("t0") and m.get("t1"):
            out[k] = (A.chain_of(k), m["pool"].lower(), m["t0"].lower(), m["t1"].lower())
    return out


def _wanted(state):
    """{(chain, token): set of partner tokens}: the tokens to read for the gauges that could be started. The rarer
    token of each pair says most (a major's 30 biggest pools seldom include a small pair); a pair of two majors is
    read through its second token only when the market snapshot has nothing on it."""
    mk = MD.market_state(state)["pools"]
    want = {}
    for k, (cid, pool, t0, t1) in _open_gauges(state).items():
        if state["ap"].get(k, 0.0) >= VOTES_MAX:
            continue
        m = mk.get(k)
        if m and m[3] is not None and m[3] >= LIQ_MAX:
            continue
        rare = [t for t in (t0, t1) if not _major(state, cid, t)]
        if not rare and not (m and m[4]):
            rare = [t1]
        for t in rare:
            want.setdefault((cid, t), set()).add(t1 if t == t0 else t0)
    return want


def refresh(state, chains_cfg, now, log, busy=False):
    """Reads the pools of a few tokens, the oldest first. busy: this run already read the hourly market snapshot
    from the same API, so this waits for the next run."""
    b = book(state)
    T = b["t"]
    for key in [key for key, v in T.items() if now - v.get("at", 0) > TOKEN_KEEP]:
        del T[key]
    if busy:
        return 0
    want = _wanted(state)
    aero_pools = {}
    for k, (cid, pool, _, _) in _open_gauges(state).items():
        aero_pools.setdefault(cid, set()).add(pool)
    wrapped = {}
    queue = sorted((key for key in want if now - (T.get(f"{key[0]}:{key[1]}") or {}).get("at", 0) > TOKEN_EVERY),
                   key=lambda key: (T.get(f"{key[0]}:{key[1]}") or {}).get("at", 0))
    done, last, deadline = 0, 0.0, time.time() + BUDGET_S
    for cid, tok in queue[:LOOKUPS]:
        slug = _slug(state, chains_cfg, cid)
        if not slug:
            T[f"{cid}:{tok}"] = {"at": now, "none": 1}
            continue
        if time.time() > deadline:
            break
        wait = PACE_S - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        try:
            res = MD._get(f"{MD.DS}/token-pairs/v1/{slug}/{tok}", retries=0)
        except MD.Limited:
            log(f"bootstrap: dexscreener limit reached after {done} tokens, the rest next run")
            break
        except Exception as e:
            log(f"bootstrap: {C.chain_short(cid)} {tok[:10]}: {str(e)[:100]}")
            continue
        if cid not in wrapped:
            wrapped[cid] = MD._chain_tokens(state, cid)
        T[f"{cid}:{tok}"] = _digest(tok, res if isinstance(res, list) else [], want[(cid, tok)],
                                    aero_pools.get(cid) or set(), wrapped[cid], now)
        done += 1
    if queue:
        log(f"bootstrap: pools of {done} tokens read, {max(0, len(queue) - done)} waiting; "
            f"{len(want)} tokens followed")
    return done


def _digest(tok, pairs, partners, aero, wrapped, now):
    """One token's pools -> {at, v: its volume in every pool (24 h), l: liquidity, n: pools,
    p: {partner: {o: [volume, liquidity, pools, biggest DEX] on other DEXes, a: {Aero gauge pool: [volume,
    liquidity]}}}} for the partners that matter."""
    tot_v = tot_l = 0.0
    n = 0
    per = {}
    for p in pairs:
        bt, qt = p.get("baseToken") or {}, p.get("quoteToken") or {}
        a = MD._token(bt.get("address"), bt.get("symbol"), wrapped)
        q = MD._token(qt.get("address"), qt.get("symbol"), wrapped)
        if tok not in (a, q) or a == q:
            continue
        other = q if a == tok else a
        vol = MD._num((p.get("volume") or {}).get("h24"))
        liq = MD._num((p.get("liquidity") or {}).get("usd"))
        tot_v += vol
        tot_l += liq
        n += 1
        if other not in partners:
            continue
        rec = per.setdefault(other, {"o": [0.0, 0.0, 0, "", 0.0], "a": {}})
        addr = (p.get("pairAddress") or "").lower()
        if addr in aero:
            rec["a"][addr] = [round(vol), round(liq)]
        else:
            o = rec["o"]
            o[0] += vol
            o[1] += liq
            o[2] += 1
            if vol >= o[4]:
                o[3], o[4] = str(p.get("dexId") or "")[:24], vol
    for rec in per.values():
        o = rec["o"]
        rec["o"] = [round(o[0]), round(o[1]), o[2], o[3]]
    return {"at": now, "v": round(tot_v), "l": round(tot_l), "n": n, "p": per}


def _pair_view(state, k, cid, pool, t0, t1, now):
    """What is known of the trading of a gauge's pair: dict with own (volume, liquidity), elsewhere (volume,
    liquidity, pools, biggest DEX), other Aero pools (volume, liquidity), the rarer token's volume in its other
    pairs, and how old the reading is; None when nothing is known."""
    T = book(state)["t"]
    m = MD.market_state(state)["pools"].get(k)
    views = []
    for tok, other in ((t0, t1), (t1, t0)):
        rec = T.get(f"{cid}:{tok}")
        if not rec or rec.get("none") or now - rec.get("at", 0) > TOKEN_KEEP:
            continue
        pr = (rec.get("p") or {}).get(other) or {"o": [0, 0, 0, ""], "a": {}}
        own = pr["a"].get(pool)
        others = [v for a, v in pr["a"].items() if a != pool]
        pair_vol = pr["o"][0] + sum(v[0] for v in pr["a"].values())
        views.append({"vol": own[0] if own else None, "liq": own[1] if own else None,
                      "pv": pr["o"][0], "pl": pr["o"][1], "pn": pr["o"][2], "dex": pr["o"][3],
                      "av": sum(v[0] for v in others), "al": sum(v[1] for v in others),
                      "tv": max(0, rec.get("v", 0) - pair_vol), "at": rec["at"], "src": "token"})
    best = None
    if views:
        # the pair as the reading that saw more of its pools shows it; the other pairs of the rarer token (the
        # smaller of the two figures) say how much trading of the token the pool could tap
        best = dict(max(views, key=lambda v: (v["pn"], v["pv"])))
        best["tv"] = min(v["tv"] for v in views)
        best["at"] = min(v["at"] for v in views)
    if m:
        own_v, own_l = m[2] or 0, m[3] or 0
        if best is None:
            if m[4] is None:
                return {"vol": own_v, "liq": own_l, "pv": 0, "pl": 0, "pn": 0, "dex": "", "av": 0, "al": 0,
                        "tv": None, "at": None, "src": "none"}
            # the hourly snapshot: the pair on every DEX, this pool included, other Aero pools not told apart
            return {"vol": own_v, "liq": own_l, "pv": max(0, m[4] - own_v), "pl": max(0, (m[5] or 0) - own_l),
                    "pn": max(0, (m[6] or 1) - 1), "dex": "", "av": 0, "al": 0, "tv": None, "at": None,
                    "src": "market"}
        if best["vol"] is None:
            best["vol"], best["liq"] = own_v, own_l
    if best and best["vol"] is None:
        best["vol"], best["liq"] = 0, 0          # DexScreener does not list the pool: empty, or never traded
    return best


def _top_share(members):
    tot = sum(a for _, a, _ in members)
    if tot <= 0:
        return None, None
    tid, a, _ = max(members, key=lambda x: x[1])
    return round(a / tot, 3), tid


def export(state, report, prices, now):
    """bootstrap.json: {generated, aero_usd, epv: {chain name: AERO per vote per day}, cap_k, min_cap,
    start: [...], started: [...]}."""
    names = {p["key"]: p for p in report.get("pools") or []}
    px = float(report.get("aero_usd") or 0)
    gauges = _open_gauges(state)
    members = E._by_gauge(state)
    mk = MD.market_state(state)
    h0 = now - now % HOUR
    out = {"generated": now, "aero_usd": px, "epv": {}, "start": [], "started": [],
           "limits": {"votes": VOTES_MAX, "liq": LIQ_MAX, "pair_vol": MIN_PAIR_VOL, "token_vol": MIN_TOKEN_VOL}}
    epv = {}
    for cid, cs in state["chains"].items():
        e = cs.get("epv")
        if e and e[1] > 0:
            epv[int(cid)] = e[1] * DAY
            out["epv"][C.chain_short(int(cid))] = float(f"{e[1] * DAY:.6g}")
    # how far above its daily fees a gauge's emission cap sits, among the gauges that earn
    ratios, caps = [], []
    for k in gauges:
        cap = state["gauges"][k].get("cap")
        if cap:
            caps.append(cap)
            f24 = A.fees_usd(state, k, h0 - 24 * HOUR, h0)
            if f24 >= 50 and px > 0:
                ratios.append(cap * DAY * px / f24)
    out["cap_k"] = round(statistics.median(ratios), 2) if len(ratios) >= 10 else None
    out["min_cap"] = float(f"{min(caps) * DAY:.6g}") if caps else None

    def base(k, cid, t0, t1):
        g = state["gauges"][k]
        p = names.get(k) or {}
        cap = g.get("cap")
        ap = float(state["ap"].get(k, 0.0))
        share, tid = _top_share(members.get(k) or [])
        return {"k": k, "name": p.get("name") or f"{_sym(state, cid, t0)}/{_sym(state, cid, t1)}",
                "chain": C.chain_short(cid), "kind": g.get("kind") or "?",
                "fee": g["fee_raw"] / 1e6 if g.get("fee_raw") else None, "ap": round(ap),
                "cap": float(f"{cap * DAY:.6g}") if cap else None,              # AERO a day; None: not read or none
                "epv": float(f"{epv[cid]:.6g}") if cid in epv else None, "top": share, "voters": len(members.get(k) or [])}

    for k, (cid, pool, t0, t1) in gauges.items():
        ap = float(state["ap"].get(k, 0.0))
        v = None
        # ---- to start
        if ap < VOTES_MAX:
            v = _pair_view(state, k, cid, pool, t0, t1, now)
            if v and (v["liq"] or 0) < LIQ_MAX and v["src"] != "none":
                pair_else = v["pv"] + v["av"]
                if pair_else >= MIN_PAIR_VOL or (v["tv"] or 0) >= MIN_TOKEN_VOL:
                    row = base(k, cid, t0, t1)
                    row.update({"vol": round(v["vol"] or 0), "liq": round(v["liq"] or 0), "pv": round(v["pv"]),
                                "pl": round(v["pl"]), "pn": v["pn"], "dex": v["dex"], "av": round(v["av"]),
                                "al": round(v["al"]), "tv": round(v["tv"]) if v["tv"] is not None else None,
                                "age": round((now - v["at"]) / HOUR, 1) if v["at"] else None, "src": v["src"]})
                    out["start"].append(row)
        # ---- just started
        if ap < STARTED_VOTES_MAX and _measured(state, k, h0):
            f24 = A.fees_usd(state, k, h0 - 24 * HOUR, h0)
            if f24 >= STARTED_MIN_USD:
                f_before = A.fees_usd(state, k, h0 - 48 * HOUR, h0 - 24 * HOUR)
                if f_before <= STARTED_RATIO * f24:
                    f6 = A.fees_usd(state, k, h0 - 6 * HOUR, h0)
                    inc = A.incentive_usd(state, prices, k, h0 - 24 * HOUR, h0)
                    m = mk["pools"].get(k)
                    hist = mk["hist"]["pools"].get(k) or {}
                    old = hist.get(str(h0 - 24 * HOUR)) or hist.get(str(h0 - 25 * HOUR)) or hist.get(str(h0 - 23 * HOUR))
                    was = ((state.get("ap_hist") or {}).get(str(h0 - 24 * HOUR)) or {}).get(k)
                    row = base(k, cid, t0, t1)
                    row.update({"f6": round(f6, 2), "f24": round(f24, 2), "fb": round(f_before, 2),
                                "inc24": round(inc, 2), "liq": m[3] if m else None, "liq24": old[2] if old else None,
                                "vol": m[2] if m else None, "pair": m[4] if m else None,
                                "ap24": round(was) if was is not None else None})
                    out["started"].append(row)
    out["start"].sort(key=lambda r: -(r["pv"] + r["av"] + 0.2 * (r["tv"] or 0)))
    out["started"].sort(key=lambda r: -r["f24"])
    T = book(state)["t"]
    want = _wanted(state)
    out["read"] = {"tokens": len(want),
                   "fresh": sum(1 for key in want if now - (T.get(f"{key[0]}:{key[1]}") or {}).get("at", 0) <= TOKEN_KEEP)}
    return out


def _measured(state, key, h0):
    """True when the pool's fees have been measured on its gauge for the 48 hours before h0."""
    cs = state["chains"].get(str(A.chain_of(key))) or {}
    since = int(cs.get("acc_from") or 0)
    return bool(since) and since <= h0 - 48 * HOUR
