"""Builds the snapshot published for the page and used by the alert rules."""
import statistics

from . import aero as A
from . import chain as C
from . import marketdata as MD

HOUR = 3600


def cooldown_end(t, cooldown):
    if not t.get("last"):
        return 0
    return int(t["last"] + cooldown - min(int(t.get("red", 0) or 0), cooldown))


def is_active(t):
    """Has moved at least once after its first allocation, so it is likely to move again."""
    return t.get("n", 0) >= 2 or (t.get("last", 0) - t.get("first", t.get("last", 0))) > HOUR


def _closest(series, ts, tol=3 * HOUR):
    best = None
    for p in series or []:
        if abs(p[0] - ts) <= tol and (best is None or abs(p[0] - ts) < abs(best[0] - ts)):
            best = p
    return best


def record_routing(state, routing, now):
    hour = now - now % HOUR
    hist = state["routing_hist"]
    for key, aggs in routing.items():
        for agg, sizes in aggs.items():
            for size, (sp, sa) in sizes.items():
                series = hist.setdefault(key, {}).setdefault(f"{agg}:{size}", [])
                if series and series[-1][0] == hour:
                    series[-1] = [hour, sp, sa]
                else:
                    series.append([hour, sp, sa])
                cutoff = now - 8 * 24 * HOUR
                hist[key][f"{agg}:{size}"] = [p for p in series if p[0] >= cutoff]


def _r4(x):
    return round(x, 4) if x is not None else None


def chain_cooldown(state, cid, default):
    return ((state["chains"].get(str(cid)) or {}).get("params") or {}).get("cooldown") or default


def positions(state, default_cooldown):
    """Every (chain, position) with an allocation: (cid, tid, token state, size, cooldown end, active)."""
    out = []
    for cid, cs in state["chains"].items():
        cd = chain_cooldown(state, cid, default_cooldown)
        for tid, t in cs.get("tokens", {}).items():
            size = sum(t["a"].values())
            if size <= 0:
                continue
            out.append((int(cid), tid, t, size, cooldown_end(t, cd), is_active(t)))
    return out


def build(state, params, prices, now, cfg, routing_errors=None, status=None):
    cooldown = params["cooldown"]
    aero_usd = prices.get(A.tkey(C.BASE_CHAIN, params["aero"]))
    pos = positions(state, cooldown)

    # per-pool counters
    per_pool = {}
    for cid, tid, t, size, end, active in pos:
        for g, amt in t["a"].items():
            d = per_pool.setdefault(g, {"positions": 0, "nominal": 0.0, "free_now": 0.0, "free_6h": 0.0,
                                        "free_24h": 0.0, "free_now_active": 0.0, "free_6h_active": 0.0,
                                        "free_24h_active": 0.0, "free_3h_active": 0.0})
            d["positions"] += 1
            d["nominal"] += amt
            for label, horizon in (("free_now", 0), ("free_6h", 6), ("free_24h", 24)):
                if end <= now + horizon * HOUR:
                    d[label] += amt
                    if active:
                        d[label + "_active"] += amt
            if active and now < end <= now + cfg["thresholds"]["wave_window_h"] * HOUR:
                d["free_3h_active"] += amt

    # waves: sAERO whose cooldown ends in each of the next 48 hours, on every chain
    waves = [{"h": now - now % HOUR + i * HOUR, "active": 0.0, "all": 0.0} for i in range(49)]
    for cid, tid, t, size, end, active in pos:
        if now < end <= now + 48 * HOUR:
            i = (end - (now - now % HOUR)) // HOUR
            if 0 <= i < len(waves):
                waves[i]["all"] += size
                if active:
                    waves[i]["active"] += size

    # calendar of large positions
    min_size = cfg["thresholds"]["calendar_min_saero"]
    cal = []
    for cid, tid, t, size, end, active in pos:
        if size < min_size or end > now + 48 * HOUR:
            continue
        if end <= now and not active:
            continue
        top = sorted(t["a"].items(), key=lambda kv: -kv[1])[:3]
        cal.append({"id": int(tid), "chain": C.chain_short(cid), "saero": round(size), "ends": end,
                    "active": active, "free": end <= now, "moves": t.get("n", 0),
                    "pools": [[A.gauge_name(state, g), round(a)] for g, a in top]})
    cal.sort(key=lambda r: (r["ends"], -r["saero"]))

    # pools
    rows = []
    mkt = state.get("market") or {}
    mkt_pools = mkt.get("pools") or {}
    universe, new_ok = MD.new_chain_universe(state, (cfg.get("strategies") or {}).get("nuove"))

    def pool_row(key, ap):
        m = state["gauges"].get(key) or {}
        cid = A.chain_of(key)
        fees48 = A.fees_usd(state, key, now - 48 * HOUR, now + HOUR)
        inc48 = A.incentive_usd(state, prices, key, now - 48 * HOUR, now)
        inc_next = A.incentive_usd(state, prices, key, now, now + 48 * HOUR)
        r = {"key": key, "name": A.gauge_name(state, key), "chain": C.chain_short(cid), "cid": cid,
             "kind": m.get("kind", "?"), "pool": m.get("pool"), "ap": round(ap)}
        mv = mkt_pools.get(key)
        if mv:
            # this pool's volume and liquidity, and the same pair on every DEX of the chain
            r["vol24h"], r["liq"] = mv[2], mv[3]
            if mv[4]:
                r["pair_vol24h"] = mv[4]
                r["pair_share"] = round(min(1.0, mv[2] / mv[4]), 4)
        if key in universe:
            r["new_chain_pair"] = True
        return m, r, fees48, inc48, inc_next

    for key, ap in sorted(state["ap"].items(), key=lambda kv: -kv[1])[:500]:
        m, r, fees48, inc48, inc_next = pool_row(key, ap)
        past = (state.get("ap_past") or {}).get(key) or {}
        rev48 = fees48 + inc48
        for h in (1, 6, 24):
            v = past.get(h) if isinstance(past, dict) else None
            if v is None:
                v = past.get(str(h)) if isinstance(past, dict) else None
            r[f"d{h}h"] = round(ap - v) if v is not None else None
        pp = per_pool.get(key, {})
        r.update({k: round(v) for k, v in pp.items() if k != "positions"})
        r["positions"] = pp.get("positions", 0)
        r["fees48h"] = round(fees48, 2)
        r["inc48h"] = round(inc48, 2)
        r["inc_next48h"] = round(inc_next, 2)
        r["rev48h"] = round(rev48, 2)
        if m.get("ok") is False:
            r["blocked"] = True
        if aero_usd and ap > 0:
            r["apr"] = round(rev48 / 2 * 365 / (ap * aero_usd) * 100, 2)
            r["apr_next"] = round((fees48 + inc_next) / 2 * 365 / (ap * aero_usd) * 100, 2)
            r["usd_day_1000"] = round(rev48 / 2 / (ap / 1000), 4)
        hist = state["routing_hist"].get(key) or {}
        if hist:
            r["routing"] = {}
            for series_key, series in hist.items():
                last = series[-1]
                if now - last[0] > 3 * HOUR:
                    continue
                prev = _closest(series, last[0] - 24 * HOUR)
                r["routing"][series_key] = {"pool": last[1], "aero": last[2],
                                            "pool_24h": prev[1] if prev else None,
                                            "aero_24h": prev[2] if prev else None}
        spark = [state["ap_hist"][h].get(key) for h in sorted(state["ap_hist"])[-48:]]
        if any(v for v in spark):
            r["spark"] = [round(v) if v else 0 for v in spark]
        rows.append(r)
    # pools that earned revenue but have no allocation at all (the first allocator takes everything)
    for key in set(state["fees"]) | {p["g"] for p in state["incentives"].values()}:
        if key in state["ap"]:
            continue
        m, r, fees48, inc48, inc_next = pool_row(key, 0)
        if fees48 + inc_next < 1:
            continue
        r.update({"fees48h": round(fees48, 2), "inc48h": round(inc48, 2), "inc_next48h": round(inc_next, 2),
                  "rev48h": round(fees48 + inc48, 2), "positions": 0, "unallocated": True})
        if m.get("ok") is False:
            r["blocked"] = True
        rows.append(r)

    # chains
    chains = []
    for cid_s, cs in sorted(state["chains"].items(), key=lambda kv: -sum(
            v for k, v in state["ap"].items() if k.startswith(kv[0] + ":"))):
        cid = int(cid_s)
        pref = cid_s + ":"
        crow = [r for r in rows if r["cid"] == cid]
        st = cs.get("status") or {}
        aprs_c = [r["apr"] for r in crow if r.get("apr") is not None and r["ap"] >= 100_000]
        chains.append({
            "id": cid, "name": C.chain_name(cid), "short": C.chain_short(cid),
            "ap": round(sum(v for k, v in state["ap"].items() if k.startswith(pref))),
            "pools": len(crow), "positions": sum(1 for p in pos if p[0] == cid),
            "fees48h": round(sum(r.get("fees48h") or 0 for r in crow), 2),
            "inc_next48h": round(sum(r.get("inc_next48h") or 0 for r in crow), 2),
            "median_apr": round(statistics.median(aprs_c), 2) if aprs_c else None,
            "synced": bool(cs.get("synced")), "fee_hours": cs.get("fee_hours"),
            "ok": st.get("ok", False), "error": st.get("error"), "checked": st.get("at"),
            "head": cs.get("head"), "cooldown_h": round(chain_cooldown(state, cid, cooldown) / HOUR, 2),
            "aero_vol24h": ((mkt.get("chains") or {}).get(cid_s) or [None, None])[0],
            "dex_vol24h": ((mkt.get("chains") or {}).get(cid_s) or [None, None])[1],
            "share": _r4(MD.chain_share(mkt, cid)),
            "share_24h": _r4(MD.chain_share(mkt, cid, ago=24 * HOUR, now=now)),
            "share_7d": _r4(MD.chain_share(mkt, cid, ago=7 * 24 * HOUR, now=now)),
            "new": cid in new_ok,
        })
    for cid in params.get("chains", []):
        if str(cid) not in state["chains"]:
            chains.append({"id": cid, "name": C.chain_name(cid), "short": C.chain_short(cid), "ap": 0,
                           "pools": 0, "positions": 0, "ok": False, "error": "chain non configurata"})

    # incentive programs that start soon or just started
    upcoming = []
    for pid, p in state["incentives"].items():
        if p["start"] < now - 2 * HOUR or p["start"] > now + 48 * HOUR:
            continue
        price = prices.get(p["tok"])
        dec = (state["erc20"].get(p["tok"]) or {}).get("dec", 18)
        amount = int(p["amt"]) / 10 ** dec
        upcoming.append({"id": pid, "pool": A.gauge_name(state, p["g"]), "chain": C.chain_short(A.chain_of(p["g"])),
                         "label": A.pool_label(state, p["g"]), "key": p["g"],
                         "token": (state["erc20"].get(p["tok"]) or {}).get("sym", A.addr_of(p["tok"])[:8]),
                         "amount": amount, "usd": round(amount * price, 2) if price else None,
                         "start": p["start"], "end": p["end"], "ap": round(state["ap"].get(p["g"], 0))})
    upcoming.sort(key=lambda r: r["start"])

    # my positions, chain by chain
    mine = []
    for tid in cfg.get("my_positions", []):
        found = False
        for cid_s, cs in state["chains"].items():
            t = cs.get("tokens", {}).get(str(tid))
            if not t or not sum(t["a"].values()):
                continue
            found = True
            cid = int(cid_s)
            end = cooldown_end(t, chain_cooldown(state, cid, cooldown))
            mine.append({"id": int(tid), "cid": cid, "chain": C.chain_short(cid), "saero": round(sum(t["a"].values())),
                         "ends": end, "free": end <= now,
                         "pools": [[A.gauge_name(state, g), round(a)] for g, a in
                                   sorted(t["a"].items(), key=lambda kv: -kv[1])]})
        if not found:
            mine.append({"id": int(tid), "missing": True})

    aprs = [r["apr"] for r in rows if r.get("apr") is not None and r["ap"] >= 1_000_000]
    ids = {p[1] for p in pos}
    active_ids = {p[1] for p in pos if p[5]}
    return {
        "generated": now, "cooldown_h": round(cooldown / HOUR, 2), "aero_usd": aero_usd,
        "status": status or {}, "routing_errors": routing_errors or {},
        "totals": {"ap_total": round(sum(state["ap"].values())),
                   "ap_base": round(sum(v for k, v in state["ap"].items() if A.chain_of(k) == C.BASE_CHAIN)),
                   "positions": len(ids), "active_positions": len(active_ids),
                   "by_chain": {c["name"]: c["ap"] for c in chains if c.get("ap")},
                   "median_apr_1m": round(statistics.median(aprs), 2) if aprs else None},
        "chains": chains,
        "waves": [{"h": w["h"], "active": round(w["active"]), "all": round(w["all"])} for w in waves],
        "calendar": cal[:150],
        "pools": rows,
        "incentives_upcoming": upcoming[:60],
        "mine": mine,
        "alerts": state.get("alert_log", [])[-40:][::-1],
    }


SHARD = 1000


def positions_export(state, params, now):
    """Every position on every chain with its cooldown end, so the page can show the viewer's own positions.

    Split by position number (SHARD numbers per file) so the page downloads only the files it needs.
    Returns (index, {shard: {"names": [...], "p": {id: [[chainId, cooldown end, sAERO, active, moves,
    [[name index, sAERO], ...]], ...]}}}).
    """
    cooldown = params["cooldown"]
    shards = {}
    for cid, tid, t, size, end, active in positions(state, cooldown):
        sh = shards.setdefault(int(tid) // SHARD, {"names": [], "idx": {}, "p": {}})
        pools = []
        for g, a in sorted(t["a"].items(), key=lambda kv: -kv[1])[:4]:
            n = A.gauge_name(state, g)
            if n not in sh["idx"]:
                sh["idx"][n] = len(sh["names"])
                sh["names"].append(n)
            pools.append([sh["idx"][n], round(a)])
        sh["p"].setdefault(tid, []).append([cid, end, round(size), 1 if active else 0, t.get("n", 0), pools])
    index = {"v": 3, "generated": now, "cooldown_h": round(cooldown / HOUR, 2), "shard": SHARD,
             "shards": sorted(shards),
             "chains": {cid: C.chain_short(cid) for cid in sorted({int(c) for c in state["chains"]})}}
    return index, {k: {"generated": now, "names": v["names"], "p": v["p"]} for k, v in shards.items()}
