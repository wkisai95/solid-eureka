"""Aero allocation state: replays voter events, keeps positions, gauges, fees and incentive programs."""
import time

from eth_abi import decode, encode

from . import chain as C

HOUR = 3600
FEE_WINDOW = 72 * HOUR
VRM_SEL = {k: C.sel(v) for k, v in {
    "gauge": "gauge()",
    "totalSupply": "totalSupply()",
    "supplyAt": "supplyAt(uint256)",
}.items()}


def gkey(chain_id, gauge):
    return f"{int(chain_id)}:{gauge.lower()}"


def new_state(now):
    return {"v": 2, "created": now, "last_block": 0, "tokens": {}, "xchain": {}, "gauges": {},
            "vrm_map": {}, "erc20": {}, "fees": {}, "fees_pending": [], "incentives": {},
            "incentives_pending": [], "ap": {}, "ap_hist": {}, "routing_hist": {}, "prices": {},
            "alerts": {}, "alert_log": [], "dirty": [], "last_digest": "", "keepalive": now,
            "rejected_emitters": []}


# ---------------------------------------------------------------- event replay

def apply_events(state, events, cooldown):
    tokens, xchain = state["tokens"], state["xchain"]
    dirty = set(state.get("dirty", []))
    for ev in sorted(events, key=lambda e: (e["block"], e["idx"])):
        k = ev["kind"]
        if k == "leaf_alloc":
            tid = str(ev["token"])
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": ev["ts"], "last": 0, "red": 0})
            if t.get("last"):
                remaining = max(0, cooldown - (ev["ts"] - t["last"]))
                t["red"] = max(0, t.get("red", 0) - remaining)
            t["a"] = {gkey(C.BASE_CHAIN, g): round(a, 4) for g, a in ev["allocs"].items()}
            t["n"] = t.get("n", 0) + 1
            t["last"] = ev["ts"]
            t.setdefault("first", ev["ts"])
            for g in ev["allocs"]:
                state["gauges"].setdefault(gkey(C.BASE_CHAIN, g), {})
        elif k == "leaf_reduction":
            t = tokens.setdefault(str(ev["token"]), {"a": {}, "n": 0, "first": ev["ts"], "last": 0, "red": 0})
            t["red"] = ev["accumulated"]
        elif k == "leaf_dirty":
            dirty.add(str(ev["token"]))
        elif k == "param":
            state.setdefault("param_events", []).append([ev["ts"], ev["name"], ev["value"]])
            state["param_events"] = state["param_events"][-20:]
        elif k == "gauge_registered":
            state["gauges"].setdefault(gkey(C.BASE_CHAIN, ev["gauge"]), {})
        elif k == "root_alloc":
            if ev["chain"] == C.BASE_CHAIN:
                continue  # the Base leaf event is authoritative
            key = f"{ev['token']}:{ev['chain']}"
            x = xchain.setdefault(key, {"a": {}, "n": 0, "first": ev["ts"], "last": 0})
            x["a"] = {gkey(ev["chain"], g): round(a, 4) for g, a in ev["allocs"].items()}
            x["n"] += 1
            x["last"] = ev["ts"]
        elif k == "root_cleared":
            for c in ev["chains"]:
                if c == C.BASE_CHAIN:
                    dirty.add(str(ev["token"]))
                else:
                    x = xchain.get(f"{ev['token']}:{c}")
                    if x:
                        x["a"] = {}
        elif k == "fees":
            state["fees_pending"].append([ev["ts"], ev["gauge"], ev["token"], str(ev["amount"]), ev["emitter"]])
        elif k == "incentive":
            state["incentives_pending"].append({"vrm": ev["emitter"], "pid": ev["program"], "tok": ev["token"],
                                                "amt": str(ev["amount"]), "start": ev["start"], "end": ev["end"],
                                                "created": ev["ts"], "creator": ev["creator"]})
    state["dirty"] = sorted(dirty)


def _count(events, kind):
    return sum(1 for e in events if e["kind"] == kind)


def fetch_voter_events(rpc, state, head, log):
    """Allocation, cooldown and gauge events of the root and leaf voters since the last processed block."""
    start = state["last_block"] + 1 if state["last_block"] else C.DEPLOY_BLOCK
    t0 = time.time()
    raw = list(rpc.get_logs(start, head, address=C.LEAF, topics=[C.LEAF_TOPICS]))
    raw += list(rpc.get_logs(start, head, address=C.ROOT, topics=[C.ROOT_TOPICS]))
    events = [e for e in (C.decode_log(lg) for lg in raw) if e]
    log(f"voter events: {len(raw)} logs, blocks {start}-{head}, {time.time() - t0:.1f}s "
        f"({_count(events, 'leaf_alloc')} allocations, {_count(events, 'gauge_registered')} new gauges)")
    return events


def fetch_reward_events(rpc, state, head, log, batch=80):
    """Fee transfers and incentive programs, read only from the rewards managers of known Aero gauges."""
    vrms = sorted(state["vrm_map"])
    if not vrms:
        return []
    prev = state.get("vrm_block") or 0
    first = not prev
    t0 = time.time()
    raw = []
    for i in range(0, len(vrms), batch):
        part = vrms[i:i + batch]
        if first:
            raw += list(rpc.get_logs(C.START_BLOCK, head, address=part, topics=[[C.T["incentive"]]]))
            raw += list(rpc.get_logs(max(C.START_BLOCK, head - FEE_WINDOW // 2), head, address=part,
                                     topics=[[C.T["fees"]]]))
        else:
            raw += list(rpc.get_logs(prev + 1, head, address=part, topics=[C.REWARD_TOPICS]))
    # rewards managers discovered in this run start from their creation, which is after the previous block
    known_before = set(state.get("vrm_scanned", []))
    fresh = [v for v in vrms if v not in known_before]
    if not first and fresh and len(fresh) < len(vrms):
        start = max(C.START_BLOCK, head - FEE_WINDOW // 2)
        for i in range(0, len(fresh), batch):
            raw += list(rpc.get_logs(start, prev, address=fresh[i:i + batch], topics=[C.REWARD_TOPICS]))
    state["vrm_scanned"] = vrms
    state["vrm_block"] = head
    seen, events = set(), []
    for lg in raw:
        key = (lg.get("transactionHash"), lg.get("logIndex"), lg["address"].lower())
        if key in seen:
            continue
        seen.add(key)
        e = C.decode_log(lg)
        if e:
            events.append(e)
    log(f"reward events: {len(raw)} logs from {len(vrms)} rewards managers, {time.time() - t0:.1f}s "
        f"({_count(events, 'fees')} fee transfers, {_count(events, 'incentive')} incentive programs)")
    return events


# ---------------------------------------------------------------- metadata

def _vrm_gauges(rpc, vrms):
    res = rpc.multicall([(v, VRM_SEL["gauge"]) for v in vrms])
    out = {}
    for v, r in zip(vrms, res):
        g = C._dec_addr(*r)
        if g:
            out[v] = g
    return out


def refresh_metadata(rpc, state, log):
    gauges = state["gauges"]
    # emitters of fee / incentive events whose gauge we do not know yet
    known_vrms = set(state["vrm_map"])
    rejected = set(state.get("rejected_emitters", []))
    candidates = sorted({p[4] for p in state["fees_pending"]} | {p["vrm"] for p in state["incentives_pending"]})
    candidates = [v for v in candidates if v not in known_vrms and v not in rejected]
    if candidates:
        for v, g in _vrm_gauges(rpc, candidates).items():
            gauges.setdefault(gkey(C.BASE_CHAIN, g), {})
    todo = [k for k, m in gauges.items() if k.startswith("8453:") and "vrm" not in m]
    if todo:
        meta = C.read_gauge_meta(rpc, [k.split(":", 1)[1] for k in todo])
        for k in todo:
            gauges[k] = meta.get(k.split(":", 1)[1], {"vrm": None})
        log(f"metadata: read {len(todo)} new gauges")
    state["vrm_map"] = {m["vrm"]: k for k, m in gauges.items() if m.get("vrm")}
    for v in candidates:
        if v not in state["vrm_map"]:
            rejected.add(v)
    state["rejected_emitters"] = sorted(rejected)[-5000:]
    toks = set()
    for m in gauges.values():
        for t in (m.get("t0"), m.get("t1")):
            if t:
                toks.add(t)
    toks |= {p[2] for p in state["fees_pending"]} | {p["tok"] for p in state["incentives_pending"]}
    for p in state["incentives"].values():
        toks.add(p["tok"])
    new = sorted(t for t in toks if t not in state["erc20"])
    if new:
        state["erc20"].update(C.read_erc20(rpc, new))


def gauge_name(state, key):
    m = state["gauges"].get(key) or {}
    e = state["erc20"]
    if m.get("t0") and m.get("t1"):
        s0 = (e.get(m["t0"]) or {}).get("sym") or m["t0"][:6]
        s1 = (e.get(m["t1"]) or {}).get("sym") or m["t1"][:6]
        return f"{s0}/{s1}"
    addr = key.split(":", 1)[1]
    return addr[:6] + "…" + addr[-4:]


# ---------------------------------------------------------------- authoritative reads

def refresh_allocation_power(rpc, state, now, log):
    """Total allocation (decayed voting power) per Base gauge, now and 1/6/24 hours ago."""
    keys = [k for k, m in state["gauges"].items() if m.get("vrm")]
    calls = []
    for k in keys:
        v = state["gauges"][k]["vrm"]
        calls.append((v, VRM_SEL["totalSupply"]))
    res = rpc.multicall(calls)
    ap = {}
    for k, r in zip(keys, res):
        val = C._dec(["uint256"], *r)
        if val is not None:
            ap[k] = val / 1e18
    past = {}
    big = [k for k in keys if ap.get(k, 0) >= 1000]
    for hours in (1, 6, 24):
        ts = now - hours * HOUR
        res = rpc.multicall([(state["gauges"][k]["vrm"], VRM_SEL["supplyAt"] + encode(["uint256"], [ts]))
                             for k in big])
        for k, r in zip(big, res):
            val = C._dec(["uint256"], *r)
            past.setdefault(k, {})[hours] = (val / 1e18) if val is not None else None
    nominal = {}
    for t in state["tokens"].values():
        for g, a in t["a"].items():
            nominal[g] = nominal.get(g, 0) + a
    missing = [g for g, v in nominal.items() if g not in ap and v > 0]
    for g in missing:
        ap[g] = nominal[g]
    if missing:
        log(f"allocation power: {len(missing)} gauges without a readable rewards manager, using position sums")
    state["ap"] = {k: round(v, 2) for k, v in ap.items() if v > 0}
    state["ap_past"] = {k: {h: (round(v, 2) if v is not None else None) for h, v in d.items()}
                        for k, d in past.items()}
    hour = str(now - now % HOUR)
    top = sorted(state["ap"].items(), key=lambda kv: -kv[1])[:80]
    state["ap_hist"][hour] = {k: v for k, v in top}
    cutoff = now - 8 * 24 * HOUR
    state["ap_hist"] = {h: v for h, v in state["ap_hist"].items() if int(h) >= cutoff}
    log(f"allocation power: {len(ap)} gauges, total {sum(ap.values()):,.0f}")


def refresh_tokens(rpc, state, cfg, log):
    tokens = state["tokens"]
    sizes = sorted(((sum(t["a"].values()), tid) for tid, t in tokens.items()), reverse=True)
    min_size = cfg["thresholds"]["calendar_min_saero"] / 4
    watch = [tid for s, tid in sizes if s >= min_size][:900]
    mine = [str(p) for p in cfg.get("my_positions", [])]
    dirty = state.get("dirty", [])
    ids = sorted(set(watch) | set(mine) | set(dirty), key=int)
    if ids:
        st = C.read_token_states(rpc, ids)
        for tid, s in st.items():
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": s["last"], "last": 0, "red": 0})
            if s["last"]:
                t["last"] = s["last"]
            t["red"] = s["red"]
            t["budget"] = round(s["budget"], 2)
    fix = sorted(set(dirty) | {m for m in mine if m not in tokens or not tokens[m]["a"]}, key=int)
    if fix:
        allocs = C.read_token_allocations(rpc, fix)
        for tid, a in allocs.items():
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": 0, "last": 0, "red": 0})
            t["a"] = {gkey(C.BASE_CHAIN, g): round(v, 4) for g, v in a.items()}
            for g in a:
                state["gauges"].setdefault(gkey(C.BASE_CHAIN, g), {})
    state["dirty"] = []
    log(f"positions: {len(tokens)} on Base, refreshed {len(ids)} cooldowns, rebuilt {len(fix)}")


# ---------------------------------------------------------------- fees and incentives

def settle_rewards(state, prices, now, log):
    """Validates fee transfers and incentive programs against the gauge's rewards manager, then prices them."""
    vrm_map = state["vrm_map"]
    keep, priced, rejected = [], 0, 0
    for ts, gauge, tok, amount, emitter in state["fees_pending"]:
        if ts < now - FEE_WINDOW:
            continue
        key = gkey(C.BASE_CHAIN, gauge)
        if vrm_map.get(emitter) != key:
            if emitter in state.get("rejected_emitters", []) or key in state["gauges"] and \
                    state["gauges"][key].get("vrm") and state["gauges"][key]["vrm"] != emitter:
                rejected += 1
                continue
            keep.append([ts, gauge, tok, amount, emitter])
            continue
        p = prices.get(tok)
        dec = (state["erc20"].get(tok) or {}).get("dec", 18)
        if p is None:
            keep.append([ts, gauge, tok, amount, emitter])
            continue
        usd = int(amount) / 10 ** dec * p
        hour = str(ts - ts % HOUR)
        bucket = state["fees"].setdefault(key, {})
        bucket[hour] = round(bucket.get(hour, 0) + usd, 4)
        priced += 1
    state["fees_pending"] = keep[-20000:]
    cutoff = now - FEE_WINDOW
    for key in list(state["fees"]):
        state["fees"][key] = {h: v for h, v in state["fees"][key].items() if int(h) >= cutoff}
        if not state["fees"][key]:
            del state["fees"][key]
    new_inc, waiting = [], []
    rejected_set = set(state.get("rejected_emitters", []))
    for p in state["incentives_pending"]:
        key = vrm_map.get(p["vrm"])
        if key:
            pid = f"{p['vrm']}:{p['pid']}"
            state["incentives"][pid] = {"g": key, **{k: p[k] for k in
                                                    ("tok", "amt", "start", "end", "created", "creator")}}
            new_inc.append(pid)
        elif p["vrm"] not in rejected_set and p["created"] > now - 7 * 24 * HOUR:
            waiting.append(p)
    state["incentives_pending"] = waiting
    state["incentives"] = {k: v for k, v in state["incentives"].items() if v["end"] > now - FEE_WINDOW}
    log(f"rewards: priced {priced} fee transfers, {len(state['fees_pending'])} waiting for a price, "
        f"rejected {rejected}; {len(state['incentives'])} incentive programs tracked")
    return new_inc


def incentive_usd(state, prices, key, start, end):
    """USD streamed by the incentive programs of a gauge between start and end."""
    total = 0.0
    for p in state["incentives"].values():
        if p["g"] != key:
            continue
        lo, hi = max(start, p["start"]), min(end, p["end"])
        if hi <= lo or p["end"] <= p["start"]:
            continue
        price = prices.get(p["tok"])
        if price is None:
            continue
        dec = (state["erc20"].get(p["tok"]) or {}).get("dec", 18)
        total += int(p["amt"]) / 10 ** dec * price * (hi - lo) / (p["end"] - p["start"])
    return total


def fees_usd(state, key, start, end):
    return sum(v for h, v in (state["fees"].get(key) or {}).items() if start <= int(h) < end)
