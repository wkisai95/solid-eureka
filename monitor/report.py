"""Builds the snapshot published for the page and used by the alert rules."""
import statistics

from . import aero as A
from . import chain as C

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


def build(state, params, prices, now, cfg, routing_errors=None, status=None):
    cooldown = params["cooldown"]
    aero_usd = prices.get(params["aero"])
    tokens = state["tokens"]

    # positions on Base: size, cooldown end, activity
    pos = []
    for tid, t in tokens.items():
        size = sum(t["a"].values())
        if size <= 0:
            continue
        pos.append((tid, t, size, cooldown_end(t, cooldown), is_active(t)))

    # per-pool counters
    per_pool = {}
    for tid, t, size, end, active in pos:
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

    # waves: sAERO whose cooldown ends in each of the next 48 hours
    waves = [{"h": now - now % HOUR + i * HOUR, "active": 0.0, "all": 0.0} for i in range(49)]
    for tid, t, size, end, active in pos:
        if now < end <= now + 48 * HOUR:
            i = (end - (now - now % HOUR)) // HOUR
            if 0 <= i < len(waves):
                waves[i]["all"] += size
                if active:
                    waves[i]["active"] += size

    # calendar of large positions
    min_size = cfg["thresholds"]["calendar_min_saero"]
    cal = []
    for tid, t, size, end, active in pos:
        if size < min_size or end > now + 48 * HOUR:
            continue
        if end <= now and not active:
            continue
        top = sorted(t["a"].items(), key=lambda kv: -kv[1])[:3]
        cal.append({"id": int(tid), "saero": round(size), "ends": end, "active": active,
                    "free": end <= now, "moves": t.get("n", 0),
                    "pools": [[A.gauge_name(state, g), round(a)] for g, a in top]})
    cal.sort(key=lambda r: (r["ends"], -r["saero"]))

    # pools
    rows = []
    for key, ap in sorted(state["ap"].items(), key=lambda kv: -kv[1])[:400]:
        m = state["gauges"].get(key) or {}
        past = (state.get("ap_past") or {}).get(key) or {}
        fees48 = A.fees_usd(state, key, now - 48 * HOUR, now + HOUR)
        inc48 = A.incentive_usd(state, prices, key, now - 48 * HOUR, now)
        inc_next = A.incentive_usd(state, prices, key, now, now + 48 * HOUR)
        rev48 = fees48 + inc48
        r = {"key": key, "name": A.gauge_name(state, key), "chain": "Base", "kind": m.get("kind", "?"),
             "pool": m.get("pool"), "ap": round(ap)}
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
    # Base pools that earned revenue but have no allocation at all (first allocator takes everything)
    for key in set(state["fees"]) | {p["g"] for p in state["incentives"].values()}:
        if key in state["ap"]:
            continue
        fees48 = A.fees_usd(state, key, now - 48 * HOUR, now + HOUR)
        inc_next = A.incentive_usd(state, prices, key, now, now + 48 * HOUR)
        if fees48 + inc_next < 1:
            continue
        m = state["gauges"].get(key) or {}
        rows.append({"key": key, "name": A.gauge_name(state, key), "chain": "Base", "kind": m.get("kind", "?"),
                     "pool": m.get("pool"), "ap": 0, "fees48h": round(fees48, 2),
                     "inc48h": round(A.incentive_usd(state, prices, key, now - 48 * HOUR, now), 2),
                     "inc_next48h": round(inc_next, 2), "positions": 0, "unallocated": True})

    # other chains (allocations dispatched from Base)
    xtot, xpos = {}, {}
    for k, x in state["xchain"].items():
        for g, a in x["a"].items():
            xtot[g] = xtot.get(g, 0) + a
            xpos[g] = xpos.get(g, 0) + 1
    xrows = [{"key": g, "name": g.split(":", 1)[1][:6] + "…" + g[-4:], "chain": C.chain_name(g.split(":")[0]),
              "ap": round(a), "positions": xpos[g]} for g, a in sorted(xtot.items(), key=lambda kv: -kv[1])[:60]]
    by_chain = {}
    for g, a in xtot.items():
        by_chain[C.chain_name(g.split(":")[0])] = by_chain.get(C.chain_name(g.split(":")[0]), 0) + a
    by_chain["Base"] = sum(state["ap"].values())

    # incentive programs that start soon or just started
    upcoming = []
    for pid, p in state["incentives"].items():
        if p["start"] < now - 2 * HOUR or p["start"] > now + 48 * HOUR:
            continue
        price = prices.get(p["tok"])
        dec = (state["erc20"].get(p["tok"]) or {}).get("dec", 18)
        amount = int(p["amt"]) / 10 ** dec
        upcoming.append({"id": pid, "pool": A.gauge_name(state, p["g"]), "key": p["g"],
                         "token": (state["erc20"].get(p["tok"]) or {}).get("sym", p["tok"][:8]),
                         "amount": amount, "usd": round(amount * price, 2) if price else None,
                         "start": p["start"], "end": p["end"], "ap": round(state["ap"].get(p["g"], 0))})
    upcoming.sort(key=lambda r: r["start"])

    # my positions
    mine = []
    for tid in cfg.get("my_positions", []):
        t = tokens.get(str(tid))
        if not t:
            mine.append({"id": int(tid), "missing": True})
            continue
        end = cooldown_end(t, cooldown)
        mine.append({"id": int(tid), "saero": round(sum(t["a"].values())), "ends": end, "free": end <= now,
                     "pools": [[A.gauge_name(state, g), round(a)] for g, a in
                               sorted(t["a"].items(), key=lambda kv: -kv[1])],
                     "other_chains": [[C.chain_name(k.split(":")[1]), round(sum(x["a"].values()))]
                                      for k, x in state["xchain"].items()
                                      if k.split(":")[0] == str(tid) and x["a"]]})

    aprs = [r["apr"] for r in rows if r.get("apr") is not None and r["ap"] >= 1_000_000]
    return {
        "generated": now, "cooldown_h": round(cooldown / HOUR, 2), "aero_usd": aero_usd,
        "status": status or {}, "routing_errors": routing_errors or {},
        "totals": {"ap_base": round(sum(state["ap"].values())), "positions": len(pos),
                   "active_positions": sum(1 for p in pos if p[4]), "by_chain": {k: round(v) for k, v in by_chain.items()},
                   "median_apr_1m": round(statistics.median(aprs), 2) if aprs else None},
        "waves": [{"h": w["h"], "active": round(w["active"]), "all": round(w["all"])} for w in waves],
        "calendar": cal[:150],
        "pools": rows,
        "other_chains": xrows,
        "incentives_upcoming": upcoming[:60],
        "mine": mine,
        "alerts": state.get("alert_log", [])[-40:][::-1],
    }


def positions_export(state, params, now):
    """Every Base position with its cooldown end, so the page can show the viewer's own positions locally."""
    cooldown = params["cooldown"]
    names, idx, out = [], {}, {}
    for tid, t in state["tokens"].items():
        size = sum(t["a"].values())
        if size <= 0:
            continue
        pools = []
        for g, a in sorted(t["a"].items(), key=lambda kv: -kv[1])[:4]:
            n = A.gauge_name(state, g)
            if n not in idx:
                idx[n] = len(names)
                names.append(n)
            pools.append([idx[n], round(a)])
        out[tid] = [cooldown_end(t, cooldown), round(size), 1 if is_active(t) else 0, t.get("n", 0), pools]
    other = {}
    for k, x in state["xchain"].items():
        tid, cid = k.split(":")
        amt = round(sum(x["a"].values()))
        if amt > 0:
            other.setdefault(tid, []).append([C.chain_name(cid), amt])
    return {"generated": now, "cooldown_h": round(cooldown / HOUR, 2), "names": names, "p": out, "x": other}
