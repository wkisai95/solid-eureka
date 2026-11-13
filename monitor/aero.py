"""Aero allocation state on every chain: replays leaf voter events, keeps positions, gauges, fees and incentives.

Keys: a gauge is "chainId:address", an ERC-20 is "chainId:address", a rewards manager is "chainId:address" and an
incentive program is "chainId:vrm:programId". Positions live per chain in state["chains"][cid]["tokens"],
because every chain has its own LeafVoter with its own allocations and cooldowns.
"""
import bisect
import time

from eth_abi import encode

from . import chain as C
from .rpc import RpcError, TimeBudget

HOUR = 3600
FEE_WINDOW = 100 * HOUR   # fees and incentives kept: the last 48 hours and the 48 before, for the change
FEE_HISTORY = 49 * HOUR
DRIFT_MAX = 60            # seconds: Base's block clock (chain.block_ts) is set aside when the chain is further off
VRM_SEL = {k: C.sel(v) for k, v in {
    "gauge": "gauge()",
    "totalSupply": "totalSupply()",
    "supplyAt": "supplyAt(uint256)",
}.items()}


def gkey(chain_id, gauge):
    return f"{int(chain_id)}:{gauge.lower()}"


def tkey(chain_id, token):
    return f"{int(chain_id)}:{token.lower()}"


def chain_of(key):
    return int(str(key).split(":", 1)[0])


def addr_of(key):
    return str(key).split(":")[1]


def new_state(now):
    return {"v": 3, "created": now, "chains": {}, "gauges": {}, "vrm_map": {}, "erc20": {}, "fees": {},
            "fees_pending": [], "incentives": {}, "incentives_pending": [], "ap": {}, "ap_past": {},
            "ap_hist": {}, "routing_hist": {}, "prices": {}, "alerts": {}, "alert_log": [], "last_digest": "",
            "keepalive": now, "param_events": []}


def new_chain_state():
    return {"last_block": 0, "vrm_block": 0, "anchors": [], "tokens": {}, "dirty": [], "inc_count": {},
            "rejected": [], "eligibility_at": 0, "params": {}, "status": {}, "synced": False}


def chain_state(state, cid):
    cs = state["chains"].setdefault(str(int(cid)), new_chain_state())
    for k, v in new_chain_state().items():
        cs.setdefault(k, v)
    return cs


def migrate(state, now, log):
    """State v2 (Base only, plain addresses) to v3 (every chain, chain-prefixed keys)."""
    if state.get("v", 0) >= 3:
        return state
    base = str(C.BASE_CHAIN)
    new = new_state(state.get("created", now))
    cs = new_chain_state()
    for k in ("last_block", "vrm_block", "fee_back", "fee_back_target", "inc_count", "eligibility_at", "dirty"):
        if k in state:
            cs[k] = state[k]
    cs["tokens"] = state.get("tokens", {})
    cs["rejected"] = state.get("rejected_emitters", [])
    cs["synced"] = bool(state.get("last_block"))
    new["chains"][base] = cs
    new["gauges"] = state.get("gauges", {})
    new["erc20"] = {tkey(base, a): v for a, v in state.get("erc20", {}).items()}
    new["prices"] = {tkey(base, a): v for a, v in state.get("prices", {}).items()}
    new["fees"] = state.get("fees", {})
    new["fees_pending"] = [[p[0], C.BASE_CHAIN, *p[1:]] for p in state.get("fees_pending", [])]
    new["incentives"] = {f"{base}:{k}": {**v, "tok": tkey(base, v["tok"])}
                         for k, v in state.get("incentives", {}).items()}
    new["incentives_pending"] = [{**p, "chain": C.BASE_CHAIN} for p in state.get("incentives_pending", [])]
    for k in ("ap", "ap_past", "ap_hist", "routing_hist", "alerts", "alert_log", "last_digest", "keepalive",
              "welcome_sent", "last_routing", "dex_slugs"):
        if k in state:
            new[k] = state[k]
    new["param_events"] = [[*p[:3], C.BASE_CHAIN] if len(p) == 3 else p for p in state.get("param_events", [])]
    # the simulation restarts: from now on it chooses among the pools of every chain
    new["tracker"] = None
    new["migrated_at"] = now
    log(f"state migrated to v3: {len(cs['tokens'])} Base positions, {len(new['gauges'])} gauges kept; "
        f"simulation restarted for all chains")
    return new


# ---------------------------------------------------------------- block timestamps

def add_anchor(cs, block, ts):
    a = cs.setdefault("anchors", [])
    i = bisect.bisect_left([x[0] for x in a], block)
    if i < len(a) and a[i][0] == block:
        a[i] = [block, ts]
    else:
        a.insert(i, [block, ts])
    if len(a) > 400:
        # keep the oldest anchor (start of the fee history) and the recent ones
        cs["anchors"] = a[:1] + a[-300:]


def ts_fn(cid, cs):
    """Block number -> timestamp. Base follows its block clock (chain.block_ts) while the chain agrees with it;
    other chains, and Base once it no longer agrees, interpolate between anchors read from the chain."""
    base = int(cid) == C.BASE_CHAIN
    if base and not cs.get("ts_drift"):
        return C.block_ts
    anchors = [tuple(x) for x in cs.get("anchors") or []]
    blocks = [a[0] for a in anchors]
    bt = cs.get("bt") or 2.0

    def f(block):
        if not anchors:
            return int(time.time())
        i = bisect.bisect_right(blocks, block)
        if i == 0:
            b, t = anchors[0]
            return int(t - (b - block) * bt)
        if i == len(anchors):
            b, t = anchors[-1]
            return int(t + (block - b) * bt)
        (b0, t0), (b1, t1) = anchors[i - 1], anchors[i]
        return int(t0 + (block - b0) * (t1 - t0) / max(1, b1 - b0))
    if base:
        ok = int(cs.get("ts_ok") or 0)       # the last block where the clock still matched the chain
        return lambda block: C.block_ts(block) if block <= ok else f(block)
    return f


def _base_clock(rpc, cs, head, now, log):
    """(clock ok, the head's timestamp read from the chain or None). Base's block clock is checked on the head
    every run, so a change of block time is caught at once instead of skewing every timestamp after it."""
    calc = C.block_ts(head)
    try:
        real = rpc.block_ts(head)
    except RpcError as e:
        log(f"Base: block clock not checked this run ({str(e)[:120]})")
        return not cs.get("ts_drift"), None
    if abs(real - calc) <= DRIFT_MAX:
        cs["ts_ok"] = head
        if cs.pop("ts_drift", None):
            log("Base: the block clock matches the chain again")
        return True, real
    if not cs.get("ts_drift"):
        ok = int(cs.get("ts_ok") or 0)
        if ok:
            add_anchor(cs, ok, C.block_ts(ok))
    cs["ts_drift"] = {"at": now, "head": head, "by": int(real - calc)}
    log(f"Base: block {head} is at {real}, the block clock says {calc} ({real - calc:+d} s): block times read "
        f"from the chain until the clock is updated")
    return False, real


def chain_head(rpc, cid, cs, lag, now, log):
    """Latest safe block of a chain, its timestamp and the average block time."""
    head = max(0, rpc.block_number() - lag)
    clock_ok, real = _base_clock(rpc, cs, head, now, log) if int(cid) == C.BASE_CHAIN else (False, None)
    if clock_ok:
        ts = C.block_ts(head)
        cs["bt"] = C.block_time(head)
    else:
        ts = real if real is not None else rpc.block_ts(head)
        add_anchor(cs, head, ts)
        if not cs.get("bt") or now - cs.get("bt_at", 0) > 6 * HOUR:
            back = 20000
            b2 = max(0, head - back)
            t2 = rpc.block_ts(b2)
            if ts > t2:
                cs["bt"] = round((ts - t2) / (head - b2), 5)
            cs.setdefault("bt", 2.0)
            cs["bt_at"] = now
            add_anchor(cs, b2, t2)
    cs["head"], cs["head_ts"] = head, ts
    return head, ts


def start_block(rpc, cid, cs, ccfg, head, head_ts, log):
    """First block to scan on a chain: the LeafVoter deployment, from the config or found by timestamp."""
    if ccfg.get("deploy_block"):
        return int(ccfg["deploy_block"])
    ts = int(ccfg.get("deploy_ts") or 1792160000)
    b = rpc.block_at_ts(ts, head, head_ts, cs.get("bt") or 2.0)
    add_anchor(cs, b, rpc.block_ts(b))
    log(f"{C.chain_short(cid)}: scanning from block {b} (deployment time)")
    return b


# ---------------------------------------------------------------- event replay

def apply_events(state, cid, events, cooldown):
    cid = int(cid)
    cs = chain_state(state, cid)
    tokens = cs["tokens"]
    dirty = set(cs.get("dirty", []))
    for ev in sorted(events, key=lambda e: (e["block"], e["idx"])):
        k = ev["kind"]
        if k == "leaf_alloc":
            tid = str(ev["token"])
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": ev["ts"], "last": 0, "red": 0})
            if t.get("last"):
                remaining = max(0, cooldown - (ev["ts"] - t["last"]))
                t["red"] = max(0, t.get("red", 0) - remaining)
            t["a"] = {gkey(cid, g): round(a, 4) for g, a in ev["allocs"].items()}
            t["n"] = t.get("n", 0) + 1
            t["last"] = ev["ts"]
            t.setdefault("first", ev["ts"])
            if ev.get("epv"):
                # AERO emitted per vote per second on this chain, as of the newest allocation (bootstrap.py)
                cs["epv"] = [ev["ts"], ev["epv"] / 1e18]
            for g in ev["allocs"]:
                state["gauges"].setdefault(gkey(cid, g), {})
        elif k == "leaf_reduction":
            t = tokens.setdefault(str(ev["token"]), {"a": {}, "n": 0, "first": ev["ts"], "last": 0, "red": 0})
            t["red"] = ev["accumulated"]
        elif k == "leaf_dirty":
            dirty.add(str(ev["token"]))
        elif k == "param":
            state.setdefault("param_events", []).append([ev["ts"], ev["name"], ev["value"], cid])
            state["param_events"] = state["param_events"][-40:]
        elif k == "gauge_registered":
            state["gauges"].setdefault(gkey(cid, ev["gauge"]), {})
        elif k == "root_cleared":
            # the root voter cleared the position everywhere: rebuild it on every chain it touched
            for c in ev["chains"]:
                if str(c) in state["chains"]:
                    other = chain_state(state, c)
                    other["dirty"] = sorted(set(other.get("dirty", [])) | {str(ev["token"])})
        elif k == "fees":
            state["fees_pending"].append([ev["ts"], cid, ev["gauge"], ev["token"], str(ev["amount"]),
                                          ev["emitter"]])
            state.setdefault("collected", []).append([cid, ev["gauge"], ev["token"], str(ev["amount"]),
                                                      ev["emitter"]])
        elif k == "incentive":
            state["incentives_pending"].append({"chain": cid, "vrm": ev["emitter"], "pid": ev["program"],
                                                "tok": ev["token"], "amt": str(ev["amount"]), "start": ev["start"],
                                                "end": ev["end"], "created": ev["ts"], "creator": ev["creator"]})
    cs["dirty"] = sorted(dirty | set(cs.get("dirty", [])))


def _count(events, kind):
    return sum(1 for e in events if e["kind"] == kind)


def _blocks(seconds, bt, lo, hi):
    return int(max(lo, min(hi, seconds / max(bt, 0.01))))


def fetch_voter_events(rpc, cid, cs, head, ccfg, log, deadline):
    """Allocation, cooldown and gauge events of the LeafVoter (and on Base the root voter) since the last run.

    A chain seen for the first time is read from the LeafVoter deployment, in windows, within the time budget:
    whatever is left is read by the next runs.
    """
    cid = int(cid)
    t0 = time.time()
    if not cs.get("last_block"):
        cs["last_block"] = start_block(rpc, cid, cs, ccfg, head, cs.get("head_ts") or int(time.time()), log) - 1
    start = cs["last_block"] + 1
    if start > head:
        cs["synced"] = True
        return []
    bt = cs.get("bt") or 2.0
    # windows of a few hours: progress is kept window by window, so a slow or rate-limited chain resumes next run
    window = _blocks(6 * HOUR, bt, 2000, 2_000_000)
    chunk = min(cs.get("leaf_chunk") or window, window)
    ts_of = ts_fn(cid, cs)
    events, scanned, raw_n = [], start - 1, 0
    while scanned < head:
        hi = min(head, scanned + window)
        try:
            raw = list(rpc.get_logs(scanned + 1, hi, address=C.LEAF, topics=[C.LEAF_TOPICS], chunk=chunk,
                                    max_chunk=window, deadline=deadline))
            if cid == C.BASE_CHAIN:
                raw += list(rpc.get_logs(scanned + 1, hi, address=C.ROOT, topics=[C.ROOT_TOPICS], chunk=chunk,
                                         max_chunk=window, deadline=deadline))
        except TimeBudget:
            log(f"{C.chain_short(cid)}: voter history paused at block {scanned} (time budget)")
            break
        except RpcError as e:
            if scanned < start:
                raise          # nothing read at all: report the chain as failing
            log(f"{C.chain_short(cid)}: voter history paused at block {scanned}: {str(e)[:160]}")
            break
        chunk = rpc.last_chunk or chunk
        raw_n += len(raw)
        events += [e for e in (C.decode_log(lg, ts_of) for lg in raw) if e]
        scanned = hi
    cs["leaf_chunk"] = chunk
    cs["last_block"] = scanned
    cs["synced"] = scanned >= head
    left = head - scanned
    log(f"{C.chain_short(cid)}: voter events {raw_n} logs, blocks {start}-{scanned}, {time.time() - t0:.1f}s "
        f"({_count(events, 'leaf_alloc')} allocations, {_count(events, 'gauge_registered')} new gauges)"
        + (f", {left} blocks still to read (~{left * bt / 3600:.1f} h)" if left else ""))
    return events


def fetch_reward_events(rpc, cid, cs, head, log, deadline, now):
    """Fee transfers to the rewards managers of Aero gauges on one chain.

    New blocks are read every run. The history needed for the 48-hour figures is filled in backwards a slice
    at a time, within a time budget, so the first runs stay short and public endpoints are not overloaded.
    """
    cid = int(cid)
    t0 = time.time()
    bt = cs.get("bt") or 2.0
    hour_blocks = _blocks(HOUR, bt, 50, 400_000)
    chunk = cs.get("fee_chunk") or hour_blocks
    raw = []
    prev = cs.get("vrm_block") or 0
    if not prev:
        head_ts = cs.get("head_ts") or now
        if cid == C.BASE_CHAIN and not cs.get("ts_drift"):
            target = max(C.START_BLOCK, C.ts_block(head_ts - FEE_HISTORY))
        else:
            target = rpc.block_at_ts(now - FEE_HISTORY, head, head_ts, bt)
            add_anchor(cs, target, rpc.block_ts(target))
        cs["fee_back"] = head + 1
        cs["fee_back_target"] = target
    elif prev < head:
        try:
            raw += list(rpc.get_logs(prev + 1, head, topics=[[C.T["fees"]]], chunk=chunk, max_chunk=4 * hour_blocks,
                                     deadline=deadline))
            chunk = rpc.last_chunk or chunk
        except RpcError as e:
            # keep vrm_block: the same blocks are read again next run
            log(f"{C.chain_short(cid)}: new fee events not read: {str(e)[:160]}")
            head = prev
    cs["vrm_block"] = max(prev, head)
    back, target, filled = cs.get("fee_back"), cs.get("fee_back_target"), 0
    while back and target and back > target:
        lo = max(target, back - chunk)
        try:
            raw += list(rpc.get_logs(lo, back - 1, topics=[[C.T["fees"]]], chunk=chunk, max_chunk=chunk,
                                     deadline=deadline))
        except RpcError as e:
            if not isinstance(e, TimeBudget):
                log(f"{C.chain_short(cid)}: fee history paused at block {back}: {str(e)[:160]}")
            chunk = max(50, (rpc.last_chunk or chunk))
            break
        chunk = rpc.last_chunk or chunk
        filled += back - lo
        back = lo
        cs["fee_back"] = back
    cs["fee_chunk"] = chunk
    ts_of = ts_fn(cid, cs)
    seen, events = set(), []
    for lg in raw:
        key = (lg.get("transactionHash"), lg.get("logIndex"), lg["address"].lower())
        if key in seen:
            continue
        seen.add(key)
        e = C.decode_log(lg, ts_of)
        if e:
            events.append(e)
    left = max(0, (back or 0) - (target or 0)) if back and target else 0
    cs["fee_hours"] = round(min(FEE_HISTORY, (cs.get("head_ts") or now) - ts_of(back)) / HOUR, 1) if back else 49.0
    if back:
        cs["fee_from"] = int(ts_of(back))      # fee transfers are known from here on
    log(f"{C.chain_short(cid)}: fee events {len(events)} from {len(raw)} logs, {time.time() - t0:.1f}s, history "
        f"{cs['fee_hours']} h" + (f", {left} blocks still to read (~{left * bt / 3600:.1f} h)" if left else ""))
    return events


def refresh_incentives(rpc, cid, state, now, log):
    """Incentive programs read straight from each rewards manager of one chain."""
    cid = int(cid)
    cs = chain_state(state, cid)
    pref = f"{cid}:"
    vrms = sorted(addr_of(k) for k in state["vrm_map"] if k.startswith(pref))
    counts = cs.setdefault("inc_count", {})
    res = rpc.multicall([(v, C.S["incentiveCount"]) for v in vrms]) if vrms else []
    todo = []
    for v, r in zip(vrms, res):
        n = C._dec(["uint256"], *r)
        if n is None:
            continue
        for pid in range(int(counts.get(v, 0)) + 1, int(n) + 1):
            todo.append((v, pid))
        counts[v] = int(n)
    out = rpc.multicall([(v, C.S["incentive"] + encode(["uint256"], [pid])) for v, pid in todo]) if todo else []
    new = []
    for (v, pid), r in zip(todo, out):
        p = C._dec(["(address,uint256,uint256,uint48,uint48,address)"], *r)
        if not p:
            continue
        tok, amount, _rate, start, end, creator = p
        if int(end) <= now - FEE_WINDOW:
            continue
        key = f"{cid}:{v}:{pid}"
        state["incentives"][key] = {"g": state["vrm_map"][f"{cid}:{v}"], "tok": tkey(cid, tok), "amt": str(amount),
                                    "start": int(start), "end": int(end), "created": now,
                                    "creator": creator.lower()}
        new.append(key)
    if todo or new:
        log(f"{C.chain_short(cid)}: incentives {len(todo)} new programs read, {len(new)} running or recent")
    return new


# ---------------------------------------------------------------- metadata

def rebuild_vrm_map(state):
    state["vrm_map"] = {f"{chain_of(k)}:{m['vrm']}": k for k, m in state["gauges"].items() if m.get("vrm")}


def refresh_metadata(rpc, cid, state, log):
    cid = int(cid)
    pref = f"{cid}:"
    gauges = state["gauges"]
    todo = [k for k, m in gauges.items() if k.startswith(pref) and "vrm" not in m]
    if todo:
        meta = C.read_gauge_meta(rpc, [addr_of(k) for k in todo])
        for k in todo:
            gauges[k] = meta.get(addr_of(k), {"vrm": None})
        log(f"{C.chain_short(cid)}: metadata for {len(todo)} new gauges")
    rebuild_vrm_map(state)
    toks = set()
    for k, m in gauges.items():
        if k.startswith(pref):
            for t in (m.get("t0"), m.get("t1")):
                if t:
                    toks.add(t)
    toks |= {p[3] for p in state["fees_pending"] if int(p[1]) == cid}
    toks |= {p["tok"] for p in state["incentives_pending"] if int(p.get("chain", C.BASE_CHAIN)) == cid}
    toks |= {addr_of(p["tok"]) for k, p in state["incentives"].items() if k.startswith(pref)}
    new = sorted(t for t in toks if tkey(cid, t) not in state["erc20"])
    if new:
        for t, v in C.read_erc20(rpc, new).items():
            state["erc20"][tkey(cid, t)] = v


def gauge_name(state, key):
    m = state["gauges"].get(key) or {}
    e = state["erc20"]
    cid = chain_of(key)
    if m.get("t0") and m.get("t1"):
        s0 = (e.get(tkey(cid, m["t0"])) or {}).get("sym") or m["t0"][:6]
        s1 = (e.get(tkey(cid, m["t1"])) or {}).get("sym") or m["t1"][:6]
        return f"{s0}/{s1}"
    addr = addr_of(key)
    return addr[:6] + "…" + addr[-4:]


def pool_label(state, key):
    """Pool name with the chain, except on Base where most pools are."""
    name = gauge_name(state, key)
    cid = chain_of(key)
    return name if cid == C.BASE_CHAIN else f"{name} ({C.chain_short(cid)})"


# ---------------------------------------------------------------- authoritative reads

def refresh_allocation_power(rpc, cid, state, now, log):
    """Total allocation (decayed voting power) per gauge of one chain, now and 1/6/24 hours ago."""
    cid = int(cid)
    pref = f"{cid}:"
    cs = chain_state(state, cid)
    keys = [k for k, m in state["gauges"].items() if k.startswith(pref) and m.get("vrm")]
    res = rpc.multicall([(state["gauges"][k]["vrm"], VRM_SEL["totalSupply"]) for k in keys]) if keys else []
    ap = {}
    for k, r in zip(keys, res):
        val = C._dec(["uint256"], *r)
        if val is not None:
            ap[k] = val / 1e18
    past = {}
    big = [k for k in keys if ap.get(k, 0) >= 1000]
    for hours in (1, 6, 24, 48):
        ts = now - hours * HOUR
        res = rpc.multicall([(state["gauges"][k]["vrm"], VRM_SEL["supplyAt"] + encode(["uint256"], [ts]))
                             for k in big]) if big else []
        for k, r in zip(big, res):
            val = C._dec(["uint256"], *r)
            past.setdefault(k, {})[hours] = (val / 1e18) if val is not None else None
    nominal = {}
    for t in cs["tokens"].values():
        for g, a in t["a"].items():
            nominal[g] = nominal.get(g, 0) + a
    missing = [g for g, v in nominal.items() if g not in ap and v > 0]
    for g in missing:
        ap[g] = nominal[g]
    if missing:
        log(f"{C.chain_short(cid)}: {len(missing)} gauges without a readable rewards manager, using position sums")
    state["ap"] = {k: v for k, v in state["ap"].items() if not k.startswith(pref)}
    state["ap"].update({k: round(v, 2) for k, v in ap.items() if v > 0})
    state["ap_past"] = {k: v for k, v in (state.get("ap_past") or {}).items() if not k.startswith(pref)}
    state["ap_past"].update({k: {h: (round(v, 2) if v is not None else None) for h, v in d.items()}
                             for k, d in past.items()})
    log(f"{C.chain_short(cid)}: allocation {len(ap)} gauges, total {sum(ap.values()):,.0f}")


def record_ap_history(state, now):
    hour = str(now - now % HOUR)
    top = sorted(state["ap"].items(), key=lambda kv: -kv[1])[:150]
    state["ap_hist"][hour] = {k: v for k, v in top}
    cutoff = now - 8 * 24 * HOUR
    state["ap_hist"] = {h: v for h, v in state["ap_hist"].items() if int(h) >= cutoff}


def refresh_tokens(rpc, cid, state, cfg, log):
    cid = int(cid)
    cs = chain_state(state, cid)
    tokens = cs["tokens"]
    sizes = sorted(((sum(t["a"].values()), tid) for tid, t in tokens.items()), reverse=True)
    min_size = cfg["thresholds"]["calendar_min_saero"] / 4
    watch = [tid for s, tid in sizes if s >= min_size][:900]
    mine = [str(p) for p in cfg.get("my_positions", [])]
    dirty = cs.get("dirty", [])
    # own positions are checked only where they have something, plus on Base
    mine_here = [m for m in mine if m in tokens or cid == C.BASE_CHAIN]
    ids = sorted(set(watch) | set(mine_here) | set(dirty), key=int)
    if ids:
        st = C.read_token_states(rpc, ids)
        for tid, s in st.items():
            if tid not in tokens and not s["last"] and not s["budget"]:
                continue
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": s["last"], "last": 0, "red": 0})
            if s["last"]:
                t["last"] = s["last"]
            t["red"] = s["red"]
            t["budget"] = round(s["budget"], 2)
    fix = sorted(set(dirty) | {m for m in mine_here if m in tokens and not tokens[m]["a"]}, key=int)
    if fix:
        allocs = C.read_token_allocations(rpc, fix)
        for tid, a in allocs.items():
            if tid not in tokens and not a:
                continue
            t = tokens.setdefault(tid, {"a": {}, "n": 0, "first": 0, "last": 0, "red": 0})
            t["a"] = {gkey(cid, g): round(v, 4) for g, v in a.items()}
            for g in a:
                state["gauges"].setdefault(gkey(cid, g), {})
    cs["dirty"] = []
    log(f"{C.chain_short(cid)}: positions {len(tokens)}, refreshed {len(ids)} cooldowns, rebuilt {len(fix)}")


# ---------------------------------------------------------------- fees and incentives

MAX_TRANSFER_USD = 2_000_000
MAX_HOUR_USD = 5_000_000


def settle_rewards(state, prices, now, log):
    """Validates fee transfers and incentive programs against the gauge's rewards manager, then prices them."""
    # fees used to be kept for 72 hours: anything older was already dropped when the window grew to 100 hours
    state.setdefault("fee_complete_from", now - 72 * HOUR)
    vrm_map = state["vrm_map"]
    keep, priced, rejected, fresh = [], 0, 0, []
    for entry in state["fees_pending"]:
        ts, cid, gauge, tok, amount, emitter = entry
        if ts < now - FEE_WINDOW:
            continue
        key = gkey(cid, gauge)
        if vrm_map.get(f"{int(cid)}:{emitter}") != key:
            if (state["chains"].get(str(int(cid))) or {}).get("synced"):
                rejected += 1   # same event signature emitted by a contract that is not an Aero rewards manager
            else:
                keep.append(entry)   # the chain's gauges are still being read
            continue
        tk = tkey(cid, tok)
        p = prices.get(tk)
        dec = (state["erc20"].get(tk) or {}).get("dec", 18)
        if p is None:
            keep.append(entry)
            continue
        usd = int(amount) / 10 ** dec * p
        if usd > MAX_TRANSFER_USD:
            # no Aero pool collects this much in one transfer: the price is wrong, try again next run
            log(f"rewards: fee transfer of {usd:,.0f} USD on {key} held back (token {tk} at {p:.6g})")
            keep.append(entry)
            continue
        fresh.append((ts, key, usd))
        if not (state["chains"].get(str(int(cid))) or {}).get("acc_from"):
            # before the gauges' pending fees were measured, the transfers stood for the fees
            hour = str(ts - ts % HOUR)
            bucket = state["fees"].setdefault(key, {})
            bucket[hour] = round(bucket.get(hour, 0) + usd, 4)
        priced += 1
    state["fees_pending"] = keep[-40000:]
    cutoff = now - FEE_WINDOW
    for book in (state["fees"], state.setdefault("acc", {})):
        for key in list(book):
            # hours over the limit were priced with a broken price before the checks existed
            book[key] = {h: v for h, v in book[key].items() if int(h) >= cutoff and v <= MAX_HOUR_USD}
            if not book[key]:
                del book[key]
    new_inc, waiting = [], []
    for p in state["incentives_pending"]:
        cid = int(p.get("chain", C.BASE_CHAIN))
        key = vrm_map.get(f"{cid}:{p['vrm']}")
        if key:
            pid = f"{cid}:{p['vrm']}:{p['pid']}"
            state["incentives"][pid] = {"g": key, "tok": tkey(cid, p["tok"]),
                                        **{k: p[k] for k in ("amt", "start", "end", "created", "creator")}}
            new_inc.append(pid)
        elif p["created"] > now - 7 * 24 * HOUR and not (state["chains"].get(str(cid)) or {}).get("synced"):
            waiting.append(p)
    state["incentives_pending"] = waiting
    state["incentives"] = {k: v for k, v in state["incentives"].items() if v["end"] > now - FEE_WINDOW}
    log(f"rewards: priced {priced} fee transfers, {len(state['fees_pending'])} waiting for a price or a gauge, "
        f"rejected {rejected}; {len(state['incentives'])} incentive programs tracked")
    return new_inc, fresh


MAX_RUN_USD = 1_000_000      # fees one gauge can earn between two runs: more means a broken price, held back
FEE_SLACK = 15               # ... and so does this many times the pool's traded volume times its fee
FEE_SLACK_MIN_USD = 20       # (amounts below this are never held back)
MARKET_MAX_AGE = 3 * HOUR


def _fee_ceiling(state, key, seconds, now):
    """The most a pool can plausibly have earned in `seconds`: FEE_SLACK times its trading pace (the fastest of
    the last hour, 6 and 24 hours, from the hourly market snapshot) times its fee. None when the pool has no
    recent market figures or no known fee: then only MAX_RUN_USD applies. Measured fees sit at about 1 time
    volume times fee (9 in 10 pools under 1.5), so only a broken price or amount gets past 15."""
    mk = state.get("market") or {}
    m = (mk.get("pools") or {}).get(key)
    fee = (state["gauges"].get(key) or {}).get("fee_raw")
    if not m or not fee or now - int(mk.get("at") or 0) > MARKET_MAX_AGE:
        return None
    pace = max(float(m[2] or 0) / 86400, float(m[1] or 0) / 21600, float(m[0] or 0) / 3600)
    return max(FEE_SLACK_MIN_USD, FEE_SLACK * fee / 1e6 * pace * max(1, seconds))


def chain_gauges(state, cid):
    """Addresses of the known gauges of a chain."""
    return sorted(k.split(":", 1)[1] for k, g in state["gauges"].items()
                  if chain_of(k) == int(cid) and g.get("ok", True))


def read_pending(rpc, cid, cs, state, now, log):
    """Reads what every gauge of the chain has pending for its voters (chain.read_pending_fees), for
    settle_accrual. Only this chain's state is written: chains are read in parallel."""
    # (an address the voter lists without a pool behind it is not a gauge: nothing to read)
    gauges = [g for g in chain_gauges(state, cid) if (state["gauges"].get(gkey(cid, g)) or {}).get("pool")]
    if not gauges:
        return
    v = C.read_pending_fees(rpc, gauges)
    cs["pend_now"] = {"t": int(now), "v": {g: [a, b] for g, (a, b) in v.items()}}
    if len(v) < len(gauges):
        log(f"{C.chain_short(cid)}: pending fees read for {len(v)} of {len(gauges)} gauges")


def _spread(bucket, t0, t1, usd):
    """Adds usd to the hour buckets between t0 and t1, in proportion to the time in each hour."""
    t0, t1 = int(t0), int(t1)
    if t1 <= t0:
        h = str(t1 - t1 % HOUR)
        bucket[h] = round(bucket.get(h, 0.0) + usd, 6)
        return
    t = t0
    while t < t1:
        h = t - t % HOUR
        nxt = min(t1, h + HOUR)
        bucket[str(h)] = round(bucket.get(str(h), 0.0) + usd * (nxt - t) / (t1 - t0), 6)
        t = nxt


def _accrued(state, prices, cid, key, prev, now_p, collected):
    """(USD earned, raw amounts carried, USD of it that was waiting) from a gauge's pending fees before and now, the
    raw amounts collected in between and the carry: a negative remainder (a collection whose event comes next run)
    or an amount without a price waits for the next run. The third figure is the part of the USD that comes from
    amounts carried by earlier runs, which accrued before the last reading."""
    g = state["gauges"].get(key) or {}
    usd, old, carry = 0.0, 0.0, []
    for i, tok in enumerate((g.get("t0"), g.get("t1"))):
        before = int(prev["c"][i])
        d = int(now_p[i]) - int(prev["p"][i]) + before + int(collected.get((tok or "").lower(), 0))
        tk = tkey(cid, tok) if tok else None
        price = prices.get(tk) if tk else None
        if d <= 0 or price is None:
            carry.append(d)
            continue
        dec = (state["erc20"].get(tk) or {}).get("dec", 18)
        usd += d / 10 ** dec * price
        old += min(d, max(0, before)) / 10 ** dec * price
        carry.append(0)
    return usd, carry, old


def _waiting_since(acc, key, prev):
    """Since when the amounts a gauge carries have been waiting: the time written when they were first held
    ("h"), or, for a carry older than that field, the end of the last hour the gauge was credited."""
    t = int(prev["t"])
    if not any(int(c) > 0 for c in prev["c"]):
        return t
    if prev.get("h"):
        return min(t, int(prev["h"]))
    hours = [int(h) for h, v in (acc.get(key) or {}).items() if v > 0]
    return min(t, max(hours) + HOUR) if hours else t


def settle_accrual(state, prices, now, log):
    """The fees each gauge earned for its voters since the last run: pending now, minus pending then, plus what
    was collected in between (the FeesCollected events of this run). Writes them to the hour buckets of
    state["acc"] and returns [(time, gauge, USD)] for this run (what the earnings and the simulation credit).

    A gauge read for the first time starts its count; one not read this run keeps its collected amounts in its carry.

    An amount too big for the pool's trading is held back in the carry (a broken price?). It is judged again every
    run against the trading of the whole time it has waited, not of the last run alone: a real burst of fees,
    bigger than the volume of an hour ago allowed, is credited as soon as the time waited explains it, and spread
    over that time; what a broken price inflates stays out until the price is right again."""
    pend = state.setdefault("pend", {})
    acc = state.setdefault("acc", {})
    vrm_map = state.get("vrm_map") or {}
    collected = {}
    for cid, gauge, tok, amount, emitter in state.pop("collected", []):
        key = gkey(cid, gauge)
        if vrm_map.get(f"{int(cid)}:{emitter}") == key:
            c = collected.setdefault(key, {})
            c[tok.lower()] = c.get(tok.lower(), 0) + int(amount)
    fresh, held, started = [], 0, 0
    for cid, cs in state["chains"].items():
        read = cs.pop("pend_now", None)
        if not read:
            continue
        t = int(read["t"])
        cs.setdefault("acc_from", t)
        for addr, now_p in read["v"].items():
            key = gkey(cid, addr)
            prev = pend.get(key)
            col = collected.pop(key, {})
            if not prev:
                pend[key] = {"p": [str(x) for x in now_p], "c": ["0", "0"], "t": t}
                started += 1
                continue
            usd, carry, old = _accrued(state, prices, cid, key, prev, now_p, col)
            since = _waiting_since(acc, key, prev)
            ceiling = _fee_ceiling(state, key, t - since, now) if usd > FEE_SLACK_MIN_USD else None
            if usd > MAX_RUN_USD or (ceiling is not None and usd > ceiling):
                log(f"rewards: {usd:,.2f} USD of fees on {key} held back, waiting {(t - since) / HOUR:.1f} h "
                    f"(a broken price?" + (f" its volume allows {ceiling:,.2f}" if ceiling is not None else "") + ")")
                g = state["gauges"].get(key) or {}
                carry = [int(now_p[i]) - int(prev["p"][i]) + int(prev["c"][i]) +
                         int(col.get((tok or "").lower(), 0)) for i, tok in enumerate((g.get("t0"), g.get("t1")))]
                usd, old, held = 0.0, 0.0, held + 1
            pend[key] = {"p": [str(x) for x in now_p], "c": [str(x) for x in carry], "t": t}
            if any(c > 0 for c in carry):
                pend[key]["h"] = since                      # still waiting: from when
            if usd > 0:
                # what waited accrued before the last reading, the rest since
                if old > 0 and since < int(prev["t"]):
                    _spread(acc.setdefault(key, {}), since, int(prev["t"]), old)
                    usd_new = usd - old
                else:
                    usd_new = usd
                if usd_new > 0:
                    _spread(acc.setdefault(key, {}), int(prev["t"]), t, usd_new)
                fresh.append((t, key, usd))
    # gauges not read this run: what was collected waits in their carry
    for key, col in collected.items():
        prev = pend.get(key)
        g = state["gauges"].get(key) or {}
        if prev:
            prev["c"] = [str(int(prev["c"][i]) + int(col.get((tok or "").lower(), 0)))
                         for i, tok in enumerate((g.get("t0"), g.get("t1")))]
    log(f"rewards: fees earned measured on {len(fresh)} gauges, {sum(u for _, _, u in fresh):,.0f} USD"
        + (f", {started} gauges counted from now" if started else "") + (f", {held} held back" if held else ""))
    return fresh


def repair_mispriced(state, dropped, prices, now, log):
    """dropped: [(token key, when it was last cached, price)] from market.scrub: prices found broken. The measured
    fee hours of the gauges with that token that stand far above the pool's typical hour go back to it: far means
    100 times (less when the price was off by less; a price is dropped for being off by much more), so an honest
    busy hour stays. The typical hour is the median of the kept hours, the broken ones being few among them.
    Returns {gauge: {hour: USD taken off}}."""
    acc = state.get("acc") or {}
    h1 = now - now % HOUR
    out = {}
    for tk, _, bad in dropped:
        cid, tok = tk.split(":", 1)
        right = prices.get(tk)
        off = max(bad / right, right / bad) if right and bad > 0 else 1e9
        far = max(3.0, min(100.0, off / 5))
        for key, g in state["gauges"].items():
            hours = acc.get(key)
            if not hours or chain_of(key) != int(cid) or tok not in ((g.get("t0") or "").lower(),
                                                                       (g.get("t1") or "").lower()):
                continue
            vals = sorted(hours.get(str(h), 0.0) for h in range(h1 - 95 * HOUR, h1 + HOUR, HOUR))
            typical = (vals[47] + vals[48]) / 2
            for h in list(hours):
                if hours[h] > max(5.0, far * typical):
                    d = out.setdefault(key, {})
                    d[int(h)] = round(d.get(int(h), 0.0) + hours[h] - typical, 6)
                    hours[h] = round(typical, 6)
    for key, d in out.items():
        log(f"rewards: {key}: {len(d)} fee hours priced with a broken price put back "
            f"({sum(d.values()):,.2f} USD taken off)")
    return out


def _block_at(cid, cs, t):
    """Block of a chain at time t: Base's clock, the anchors read from the chain elsewhere."""
    if int(cid) == C.BASE_CHAIN and not cs.get("ts_drift"):
        return C.ts_block(t)
    anchors = sorted(tuple(x) for x in cs.get("anchors") or [])
    if len(anchors) < 2:
        return None
    times = [a[1] for a in anchors]
    i = bisect.bisect_left(times, t)
    if i == 0 or i == len(anchors):
        return None
    (b0, t0), (b1, t1) = anchors[i - 1], anchors[i]
    return int(b0 + (t - t0) * (b1 - b0) / max(1, t1 - t0))


BACKFILL_H = 48


def backfill_accrual(state, make_rpc, prices, now, log, budget=180):
    """Once, when the gauges are first measured: their pending fees at every hour of the last 48, read from
    endpoints that keep old state, give the fees each pool earned hour by hour (pending at the end of the hour,
    minus at its start, plus the transfers of the hour), so the 48-hour figures are measured at once instead of in
    two days. A chain whose endpoints keep no old state keeps its transfers for those hours.

    make_rpc(chain): a fresh client, so the old-state errors do not count against the run's own endpoints."""
    from concurrent.futures import ThreadPoolExecutor
    deadline = time.time() + budget
    h_now = now - now % HOUR
    marks = [h_now - i * HOUR for i in range(BACKFILL_H, 0, -1)] + [h_now]

    def one(cid):
        cs = state["chains"][str(cid)]
        if cs.get("acc_backfill") or int(cs.get("acc_from") or 0) != now:
            return cid, None, "not needed"
        gauges = [k.split(":", 1)[1] for k, v in state.get("pend", {}).items()
                  if chain_of(k) == int(cid) and int(v.get("t") or 0) == now]
        if not gauges:
            return cid, None, "no gauges"
        rpc = make_rpc(cid)
        reads = {}
        # newest first: an endpoint that keeps only some old state still gives the hours it has
        for t in reversed(marks):
            if t >= now or time.time() > deadline:
                continue
            b = _block_at(cid, cs, t)
            if b is None:
                break
            try:
                rpc.eth_call(gauges[0], C.S["pendingFees"], hex(b))    # one call first: fails fast without old state
                v = C.read_pending_fees(rpc, gauges, block=b)
            except Exception as e:
                if not reads:
                    return cid, None, f"no old state ({str(e)[:100]})"
                break
            if len(v) < len(gauges) // 2:
                break
            reads[t] = v
        return cid, reads, f"{len(reads)} of {len(marks)} hours read"

    ids = [int(c) for c in state["chains"]]
    with ThreadPoolExecutor(max_workers=max(1, len(ids))) as ex:
        results = list(ex.map(one, ids))
    acc = state.setdefault("acc", {})
    for cid, reads, why in results:
        cs = state["chains"][str(cid)]
        if not reads:
            if why != "not needed":
                log(f"{C.chain_short(cid)}: fees of the last {BACKFILL_H} hours not measured ({why}): transfers kept")
            continue
        filled, total = 0, 0.0
        for key, end in state["pend"].items():
            if chain_of(key) != cid or int(end.get("t") or 0) != now:
                continue
            addr = key.split(":", 1)[1]
            g = state["gauges"].get(key) or {}
            toks = [tkey(cid, t) if t else None for t in (g.get("t0"), g.get("t1"))]
            px = [prices.get(t) if t else None for t in toks]
            dec = [(state["erc20"].get(t) or {}).get("dec", 18) if t else 18 for t in toks]
            transfers = {int(h): v for h, v in (state["fees"].get(key) or {}).items()}
            series = [(t, reads[t].get(addr)) for t in sorted(reads)]
            series.append((now, [int(x) for x in end["p"]]))
            bucket, carry = {}, 0.0
            for (t0, p0), (t1, p1) in zip(series, series[1:]):
                # the transfers of the segment's hours: a collection empties the gauge's pending fees
                moved = sum(transfers.get(h, 0.0) for h in range(t0, t1, HOUR))
                if p0 is None or p1 is None:
                    usd = moved
                else:
                    usd = moved + carry + sum((int(p1[i]) - int(p0[i])) / 10 ** dec[i] * px[i]
                                              for i in range(2) if px[i] is not None)
                    carry = min(0.0, usd)        # pending fell more than the hour's transfers show: next hour
                    usd = max(0.0, usd)
                if usd > 0:
                    _spread(bucket, t0, t1, usd)
            # the hours the series covers are measured, the transfers of the older ones stay in the old book
            if bucket:
                acc[key] = {h: round(v, 6) for h, v in bucket.items() if v > 0}
                filled += 1
                total += sum(bucket.values())
        cs["acc_from"] = min(reads)
        cs["acc_backfill"] = now
        log(f"{C.chain_short(cid)}: fees of the last {BACKFILL_H} hours measured on {filled} gauges from their "
            f"pending fees ({why}), {total:,.0f} USD")
    return [cid for cid, reads, _ in results if reads]


def accrual_tokens(state):
    """Tokens to price for settle_accrual: those of the gauges whose pending fees moved or that collected fees."""
    want = set()
    pend = state.get("pend") or {}
    for cid, cs in state["chains"].items():
        for addr, now_p in ((cs.get("pend_now") or {}).get("v") or {}).items():
            key = gkey(cid, addr)
            prev = pend.get(key)
            if prev and [str(x) for x in now_p] == prev["p"] and prev["c"] == ["0", "0"]:
                continue
            g = state["gauges"].get(key) or {}
            want |= {tkey(cid, t) for t in (g.get("t0"), g.get("t1")) if t}
    for cid, gauge, tok, _, _ in state.get("collected") or []:
        want.add(tkey(cid, tok))
    return want


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


def fee_hours(state, key):
    """{hour: USD} the pool's voters earned in fees: measured on the gauge from the chain's acc_from on. Before it,
    within the last 48 hours, the estimate of estimate_unmeasured once the chain has been measured long enough; the
    fee transfers otherwise (they stood for the fees until the gauges were measured)."""
    cid = str(chain_of(key))
    t = int(((state.get("chains") or {}).get(cid) or {}).get("acc_from") or 0)
    acc = (state.get("acc") or {}).get(key) or {}
    if not t:
        return {int(h): v for h, v in (state["fees"].get(key) or {}).items()}
    rng = (state.get("acc_est_range") or {}).get(cid)
    out = {}
    for h, v in (state["fees"].get(key) or {}).items():
        h = int(h)
        if h < t and not (rng and rng[0] <= h < rng[1]):
            out[h] = v
    if rng:
        for h, v in ((state.get("acc_est") or {}).get(key) or {}).items():
            out[int(h)] = out.get(int(h), 0.0) + v
    for h, v in acc.items():
        out[int(h)] = out.get(int(h), 0.0) + v
    return out


EST_MIN_H = 6       # hours a chain must have been measured before they stand for its hours before


def _hour_volume(vser, h):
    """Volume traded in the hour starting at h: the snapshot taken in the next hour covers it best."""
    snap = vser.get(str(h + HOUR)) or vser.get(str(h))
    return float((snap or [0])[0] or 0) if snap else None


def estimate_unmeasured(state, now):
    """Fees of the hours of the last 48 before a chain's gauges were first measured, from what was measured since:
    each hour's volume times the fees per dollar traded measured, or the measured hourly average for a pool without
    hourly volume. They stand for the fee transfers of those hours, which on a quiet pool carry days of fees in one
    hour. Done each run while the last 48 hours are not all measured and the chain has EST_MIN_H measured."""
    pools = ((state.get("market") or {}).get("hist") or {}).get("pools") or {}
    est, ranges = {}, {}
    lo = now - 49 * HOUR
    lo -= lo % HOUR
    for cid, cs in state["chains"].items():
        t = int(cs.get("acc_from") or 0)
        th = t - t % HOUR
        if not t or th <= lo or now - t < EST_MIN_H * HOUR:
            continue
        ranges[cid] = [lo, th]
        keys = {k for k in state.get("acc") or {} if chain_of(k) == int(cid)} | \
               {k for k in state["fees"] if chain_of(k) == int(cid)}
        full = range(th, now - now % HOUR, HOUR)          # the measured hours already complete
        for key in keys:
            acc = (state.get("acc") or {}).get(key) or {}
            mf = sum(acc.get(str(h), 0.0) for h in full)
            if mf <= 0:
                continue
            vser = pools.get(key) or {}
            mv = sum(_hour_volume(vser, h) or 0.0 for h in full) if vser else 0.0
            n = max(1, len(full))
            out = {}
            for h in range(lo, th, HOUR):
                v = _hour_volume(vser, h) if vser and mv > 0 else None
                usd = v * mf / mv if v is not None else mf / n
                if usd > 0:
                    out[str(h)] = round(usd, 6)
            if out:
                est[key] = out
    state["acc_est"], state["acc_est_range"] = est, ranges
    return ranges


def fees_usd(state, key, start, end):
    return sum(v for h, v in fee_hours(state, key).items() if start <= h < end)


def fee_change(state, key, now):
    """Fees of the last 48 hours against the 48 before: (change in percent, fees of the 48 before).

    None while the chain's fee history is shorter than 96 hours, and the change is None when the earlier window
    had less than $5 (a change from almost nothing says nothing).
    """
    cs = (state.get("chains") or {}).get(str(chain_of(key))) or {}
    if not cs.get("fee_from"):
        return None, None
    # measured fees are compared with measured fees only: transfers came in lumps on quiet pools
    start = max(int(cs["fee_from"]), int(state.get("fee_complete_from") or 0), int(cs.get("acc_from") or 0))
    if now - start < 96 * HOUR - 1800:
        return None, None
    prev = fees_usd(state, key, now - 96 * HOUR, now - 48 * HOUR)
    cur = fees_usd(state, key, now - 48 * HOUR, now + HOUR)
    return (round((cur / prev - 1) * 100, 1) if prev >= 5 else None), prev


def refresh_eligibility(rpc, cid, state, now, log, every=6 * HOUR):
    """Every run: the emission cap of each gauge (an operator rewrites the caps of the earning gauges every few
    minutes, following their fees). Every `every`: which gauges an ordinary position can vote (activated on the
    leaf, with a non-zero cap) and what drifts on their pools (refresh_pool_facts)."""
    cid = int(cid)
    cs = chain_state(state, cid)
    pref = f"{cid}:"
    registry = (cs.get("params") or {}).get("factory_registry") or (C.FACTORY_REGISTRY if cid == C.BASE_CHAIN
                                                                       else None)
    if not registry:
        return
    keys = [k for k, m in state["gauges"].items() if k.startswith(pref) and m.get("vrm")]
    # (the first run that knows about pool facts reads them at once)
    full = now - cs.get("eligibility_at", 0) >= every or not cs.get("facts_at")
    calls = []
    for k in keys:
        g = addr_of(k)
        calls.append((registry, C.S["emissionCap"] + encode(["address"], [g])))
        if full:
            calls.append((C.LEAF, C.S["isActivated"] + encode(["address"], [g])))
    try:
        res = rpc.multicall(calls) if calls else []
    except Exception as e:
        if full:
            raise
        # the caps alone are not worth a failed run: the ones read before stay
        log(f"{C.chain_short(cid)}: emission caps not read this run: {type(e).__name__}: {str(e)[:160]}")
        return
    step = 2 if full else 1
    blocked = 0
    for i, k in enumerate(keys):
        cap = C._dec(["uint128"], *res[step * i])
        if cap is None:
            continue
        m = state["gauges"][k]
        # AERO per second; None: no cap
        m["cap"] = None if cap >= 2 ** 127 else float(f"{cap / 1e18:.6g}")
        if full:
            act = C._dec(["bool"], *res[step * i + 1])
            if act is not None:
                m["ok"] = bool(act) and cap > 0
                blocked += 0 if m["ok"] else 1
    if not full:
        return
    cs["eligibility_at"] = now
    log(f"{C.chain_short(cid)}: eligibility {len(keys)} gauges checked, {blocked} not votable by ordinary positions")
    try:
        refresh_pool_facts(rpc, cid, state, keys, log)
        cs["facts_at"] = now
    except Exception as e:
        log(f"{C.chain_short(cid)}: pool fees not refreshed: {type(e).__name__}: {str(e)[:200]}")


def refresh_pool_facts(rpc, cid, state, keys, log):
    """The swap fee of every pool as it is now (it is dynamic on many: the one read when the gauge was first seen
    goes stale) and the type of the basic pools that were read before they declared one."""
    by_pool = {}
    for k in keys:
        pool = state["gauges"][k].get("pool")
        if pool:
            by_pool.setdefault(pool, []).append(k)
    kind_of = {p: str(state["gauges"][ks[0]].get("kind") or "?") for p, ks in by_pool.items()}
    concentrated = sorted(p for p, kd in kind_of.items() if kd.startswith("CL"))
    basic = sorted(p for p, kd in kind_of.items() if not kd.startswith("CL"))
    facts = C.read_pool_facts(rpc, concentrated, basic)
    fees = named = 0
    for pool, f in facts.items():
        for k in by_pool[pool]:
            m = state["gauges"][k]
            if f.get("fee_raw") and f["fee_raw"] != m.get("fee_raw"):
                m["fee_raw"] = f["fee_raw"]
                fees += 1
            if f.get("kind") and m.get("kind") in (None, "?"):
                m["kind"] = f["kind"]
                named += 1
    if fees or named:
        log(f"{C.chain_short(cid)}: pool fees changed on {fees} gauges" + (f", {named} basic pools named" if named else ""))
