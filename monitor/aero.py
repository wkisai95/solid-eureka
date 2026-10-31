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
    """Block number -> timestamp. Base has fixed 2-second blocks; other chains interpolate between anchors."""
    if int(cid) == C.BASE_CHAIN:
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
    return f


def chain_head(rpc, cid, cs, lag, now, log):
    """Latest safe block of a chain, its timestamp and the average block time."""
    head = max(0, rpc.block_number() - lag)
    if int(cid) == C.BASE_CHAIN:
        cs["bt"] = 2.0
        ts = C.block_ts(head)
    else:
        ts = rpc.block_ts(head)
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
        if cid == C.BASE_CHAIN:
            target = max(C.START_BLOCK, head - FEE_HISTORY // 2)
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
        hour = str(ts - ts % HOUR)
        bucket = state["fees"].setdefault(key, {})
        bucket[hour] = round(bucket.get(hour, 0) + usd, 4)
        priced += 1
    state["fees_pending"] = keep[-40000:]
    cutoff = now - FEE_WINDOW
    for key in list(state["fees"]):
        # hours over the limit were priced with a broken price before the checks existed
        state["fees"][key] = {h: v for h, v in state["fees"][key].items() if int(h) >= cutoff and v <= MAX_HOUR_USD}
        if not state["fees"][key]:
            del state["fees"][key]
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


def fee_change(state, key, now):
    """Fees of the last 48 hours against the 48 before: (change in percent, fees of the 48 before).

    None while the chain's fee history is shorter than 96 hours, and the change is None when the earlier window
    had less than $5 (a change from almost nothing says nothing).
    """
    cs = (state.get("chains") or {}).get(str(chain_of(key))) or {}
    if not cs.get("fee_from"):
        return None, None
    start = max(int(cs["fee_from"]), int(state.get("fee_complete_from") or 0))
    if now - start < 96 * HOUR - 1800:
        return None, None
    prev = fees_usd(state, key, now - 96 * HOUR, now - 48 * HOUR)
    cur = fees_usd(state, key, now - 48 * HOUR, now + HOUR)
    return (round((cur / prev - 1) * 100, 1) if prev >= 5 else None), prev


def refresh_eligibility(rpc, cid, state, now, log, every=6 * HOUR):
    """Marks gauges an ordinary position can vote: activated on the leaf and with a non-zero emission cap."""
    cid = int(cid)
    cs = chain_state(state, cid)
    if now - cs.get("eligibility_at", 0) < every:
        return
    registry = (cs.get("params") or {}).get("factory_registry") or (C.FACTORY_REGISTRY if cid == C.BASE_CHAIN
                                                                       else None)
    if not registry:
        return
    pref = f"{cid}:"
    keys = [k for k, m in state["gauges"].items() if k.startswith(pref) and m.get("vrm")]
    calls = []
    for k in keys:
        g = addr_of(k)
        calls.append((C.LEAF, C.S["isActivated"] + encode(["address"], [g])))
        calls.append((registry, C.S["emissionCap"] + encode(["address"], [g])))
    res = rpc.multicall(calls) if calls else []
    blocked = 0
    for i, k in enumerate(keys):
        act = C._dec(["bool"], *res[2 * i])
        cap = C._dec(["uint128"], *res[2 * i + 1])
        if act is None or cap is None:
            continue
        state["gauges"][k]["ok"] = bool(act) and cap > 0
        blocked += 0 if state["gauges"][k]["ok"] else 1
    cs["eligibility_at"] = now
    log(f"{C.chain_short(cid)}: eligibility {len(keys)} gauges checked, {blocked} not votable by ordinary positions")
