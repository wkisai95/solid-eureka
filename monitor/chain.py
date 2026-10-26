"""Aero contracts on Base: addresses, event topics, decoders and multicall readers."""
from eth_abi import decode, encode
from eth_hash.auto import keccak

LEAF = "0x382409fad8503FbCdbB27A4211551fa03e884078"   # LeafVoter (same address on every Aero chain)
ROOT = "0x49d7d4f17Ec56Ef8fA04E464AF5D1A4942CC31e6"   # root Voter on Base
FACTORY_REGISTRY = "0x7b2A2c12dC5Ea2a31455952a05031C3E4BB28F3E"   # leaf factory registry on Base
BASE_CHAIN = 8453
START_BLOCK = 52875727          # 2026-10-21 00:00 UTC, before the Aero launch
DEPLOY_BLOCK = 52688157         # root Voter and LeafVoter deployed on Base (2026-10-16)
BASE_GENESIS_TS = 1686789347    # Base has fixed 2-second blocks: ts = genesis + 2 * block
ZERO = "0x0000000000000000000000000000000000000000"

CHAIN_NAMES = {1: "Ethereum", 10: "OP Mainnet", 8453: "Base", 42161: "Arbitrum",
               57073: "Ink", 4663: "Robinhood Chain", 5042: "Arc"}


def chain_name(cid):
    return CHAIN_NAMES.get(int(cid), f"Chain {cid}")


def block_ts(block):
    return BASE_GENESIS_TS + 2 * int(block)


def ts_block(ts):
    return max(0, (int(ts) - BASE_GENESIS_TS) // 2)


def topic(sig):
    return "0x" + keccak(sig.encode()).hex()


def sel(sig):
    return keccak(sig.encode())[:4]


T = {
    "leaf_alloc": topic("GaugesAllocated(uint256,(address,uint128,bytes)[],uint256)"),
    "leaf_reduction": topic("CooldownReductionApplied(uint256,uint48,uint48)"),
    "leaf_dealloc": topic("Deallocated(uint256,uint128)"),
    "leaf_emergency": topic("EmergencyDeallocationApplied(uint256)"),
    "leaf_registered": topic("GaugeRegistered(address,bool,uint48)"),
    "leaf_cooldown_set": topic("AllocationCooldownSet(uint48)"),
    "leaf_maxred_set": topic("MaxAccumulatedCooldownReductionSet(uint48)"),
    "root_alloc": topic("GaugesAllocated(uint256,uint256,(address,uint128,bytes)[])"),
    "root_cleared": topic("TokenCleared(uint256,uint256[])"),
    "fees": topic("FeesCollected(address,address,uint256)"),
    "incentive": topic("IncentiveCreated(uint256,address,address,uint256,uint48,uint48)"),
}

LEAF_TOPICS = [T["leaf_alloc"], T["leaf_reduction"], T["leaf_dealloc"], T["leaf_emergency"], T["leaf_registered"],
               T["leaf_cooldown_set"], T["leaf_maxred_set"]]
ROOT_TOPICS = [T["root_alloc"], T["root_cleared"]]
REWARD_TOPICS = [T["fees"], T["incentive"]]


def hexint(v):
    if v is None or v in ("0x", ""):
        return 0
    return int(v, 16) if isinstance(v, str) else int(v)


def addr_from_topic(t):
    return ("0x" + t[-40:]).lower()


def _data(lg):
    return bytes.fromhex(lg["data"][2:]) if lg.get("data") and lg["data"] != "0x" else b""


def decode_log(lg):
    """Returns a normalised event dict or None for unknown logs."""
    t0 = lg["topics"][0].lower() if lg.get("topics") else None
    block = hexint(lg["blockNumber"])
    base = {"block": block, "idx": hexint(lg.get("logIndex")), "ts": block_ts(block),
            "emitter": lg["address"].lower()}
    try:
        if t0 == T["leaf_alloc"]:
            allocs, _ = decode(["(address,uint128,bytes)[]", "uint256"], _data(lg))
            return {**base, "kind": "leaf_alloc", "token": int(lg["topics"][1], 16),
                    "allocs": {g.lower(): a / 1e18 for g, a, _ in allocs if g.lower() != ZERO and a > 0}}
        if t0 == T["leaf_reduction"]:
            reduction, accumulated = decode(["uint48", "uint48"], _data(lg))
            return {**base, "kind": "leaf_reduction", "token": int(lg["topics"][1], 16),
                    "accumulated": int(accumulated)}
        if t0 in (T["leaf_dealloc"], T["leaf_emergency"]):
            return {**base, "kind": "leaf_dirty", "token": int(lg["topics"][1], 16)}
        if t0 == T["leaf_registered"]:
            return {**base, "kind": "gauge_registered", "gauge": addr_from_topic(lg["topics"][1])}
        if t0 in (T["leaf_cooldown_set"], T["leaf_maxred_set"]):
            (value,) = decode(["uint48"], _data(lg))
            return {**base, "kind": "param", "value": int(value),
                    "name": "cooldown" if t0 == T["leaf_cooldown_set"] else "max_reduction"}
        if t0 == T["root_alloc"]:
            (allocs,) = decode(["(address,uint128,bytes)[]"], _data(lg))
            return {**base, "kind": "root_alloc", "token": int(lg["topics"][1], 16),
                    "chain": int(lg["topics"][2], 16),
                    "allocs": {g.lower(): a / 1e18 for g, a, _ in allocs if g.lower() != ZERO and a > 0}}
        if t0 == T["root_cleared"]:
            (chains,) = decode(["uint256[]"], _data(lg))
            return {**base, "kind": "root_cleared", "token": int(lg["topics"][1], 16),
                    "chains": [int(c) for c in chains]}
        if t0 == T["fees"]:
            (amount,) = decode(["uint256"], _data(lg))
            return {**base, "kind": "fees", "gauge": addr_from_topic(lg["topics"][1]),
                    "token": addr_from_topic(lg["topics"][2]), "amount": int(amount)}
        if t0 == T["incentive"]:
            amount, start, duration = decode(["uint256", "uint48", "uint48"], _data(lg))
            return {**base, "kind": "incentive", "program": int(lg["topics"][1], 16),
                    "token": addr_from_topic(lg["topics"][2]), "creator": addr_from_topic(lg["topics"][3]),
                    "amount": int(amount), "start": int(start), "end": int(start) + int(duration)}
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- multicall readers

S = {k: sel(v) for k, v in {
    "allocationCooldown": "allocationCooldown()",
    "maxReduction": "maxAccumulatedCooldownReduction()",
    "maxGauges": "maxGauges()",
    "incentiveCount": "incentiveCount()",
    "incentive": "incentive(uint256)",
    "isActivated": "isActivated(address)",
    "emissionCap": "emissionCap(address)",
    "tokenStates": "tokenStates(uint256)",
    "reduction": "accumulatedCooldownReduction(uint256)",
    "allocatedGauges": "allocatedGauges(uint256)",
    "allocations": "allocations(uint256,address)",
    "TOKEN": "TOKEN()",
    "chains": "chains()",
    "stakingToken": "stakingToken()",
    "pool": "pool()",
    "token0": "token0()",
    "token1": "token1()",
    "vrm": "votingRewardsManager()",
    "stable": "stable()",
    "tickSpacing": "tickSpacing()",
    "fee": "fee()",
    "symbol": "symbol()",
    "decimals": "decimals()",
}.items()}


def _dec(types, ok, data):
    if not ok or not data:
        return None
    try:
        out = decode(types, data)
        return out[0] if len(out) == 1 else out
    except Exception:
        return None


def _dec_addr(ok, data):
    a = _dec(["address"], ok, data)
    return a.lower() if a and a.lower() != ZERO else None


def _dec_symbol(ok, data):
    s = _dec(["string"], ok, data)
    if s is None and ok and len(data) == 32:
        s = data.rstrip(b"\0").decode("utf-8", "ignore")
    if isinstance(s, str):
        s = "".join(ch for ch in s if ch.isprintable()).strip()[:24]
    return s or None


def read_params(rpc):
    res = rpc.multicall([(LEAF, S["allocationCooldown"]), (LEAF, S["maxReduction"]),
                         (ROOT, S["TOKEN"]), (ROOT, S["chains"]), (LEAF, S["maxGauges"])])
    cooldown = _dec(["uint48"], *res[0])
    max_red = _dec(["uint48"], *res[1])
    token = _dec_addr(*res[2])
    chains = _dec(["uint256[]"], *res[3])
    max_gauges = _dec(["uint256"], *res[4])
    return {"cooldown": int(cooldown) if cooldown else 47 * 3600,
            "max_reduction": int(max_red) if max_red is not None else 0,
            "aero": token or "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
            "chains": [int(c) for c in chains] if chains else [],
            "max_gauges": int(max_gauges) if max_gauges else 20}


def read_token_states(rpc, token_ids):
    calls = []
    for tid in token_ids:
        arg = encode(["uint256"], [int(tid)])
        calls.append((LEAF, S["tokenStates"] + arg))
        calls.append((LEAF, S["reduction"] + arg))
    res = rpc.multicall(calls)
    out = {}
    for i, tid in enumerate(token_ids):
        st = _dec(["address", "uint48", "bool", "uint128"], *res[2 * i])
        red = _dec(["uint48"], *res[2 * i + 1])
        if st is not None:
            out[str(tid)] = {"last": int(st[1]), "budget": st[3] / 1e18,
                             "red": int(red) if red is not None else 0}
    return out


def read_token_allocations(rpc, token_ids):
    """Rebuilds the current Base allocation of each token straight from the leaf voter."""
    res = rpc.multicall([(LEAF, S["allocatedGauges"] + encode(["uint256"], [int(t)])) for t in token_ids])
    gauges = {}
    calls = []
    for tid, r in zip(token_ids, res):
        gs = _dec(["address[]"], *r)
        if gs is None:
            continue
        gauges[str(tid)] = [g.lower() for g in gs if g.lower() != ZERO]
        for g in gauges[str(tid)]:
            calls.append((str(tid), g))
    amounts = rpc.multicall([(LEAF, S["allocations"] + encode(["uint256", "address"], [int(t), g]))
                             for t, g in calls]) if calls else []
    out = {t: {} for t in gauges}
    for (tid, g), r in zip(calls, amounts):
        a = _dec(["uint128"], *r)
        if a:
            out[tid][g] = a / 1e18
    return out


def read_gauge_meta(rpc, gauges):
    """Pool, tokens and voting-rewards manager for each gauge (Base)."""
    calls = []
    for g in gauges:
        for k in ("stakingToken", "pool", "token0", "token1", "vrm"):
            calls.append((g, S[k]))
    res = rpc.multicall(calls)
    meta = {}
    for i, g in enumerate(gauges):
        r = res[5 * i:5 * i + 5]
        pool = _dec_addr(*r[0]) or _dec_addr(*r[1])
        meta[g] = {"pool": pool, "t0": _dec_addr(*r[2]), "t1": _dec_addr(*r[3]), "vrm": _dec_addr(*r[4])}
    # pool details
    pools = sorted({m["pool"] for m in meta.values() if m["pool"]})
    calls = []
    for p in pools:
        for k in ("token0", "token1", "stable", "tickSpacing", "fee"):
            calls.append((p, S[k]))
    res = rpc.multicall(calls) if calls else []
    pinfo = {}
    for i, p in enumerate(pools):
        r = res[5 * i:5 * i + 5]
        pinfo[p] = {"t0": _dec_addr(*r[0]), "t1": _dec_addr(*r[1]),
                    "stable": _dec(["bool"], *r[2]), "tick": _dec(["int24"], *r[3]), "fee": _dec(["uint24"], *r[4])}
    for g, m in meta.items():
        p = pinfo.get(m["pool"]) or {}
        m["t0"] = m["t0"] or p.get("t0")
        m["t1"] = m["t1"] or p.get("t1")
        if p.get("tick") is not None:
            m["kind"] = f"CL{p['tick']}"
        elif p.get("stable") is not None:
            m["kind"] = "stable" if p["stable"] else "volatile"
        else:
            m["kind"] = "?"
        if p.get("fee") is not None:
            m["fee_raw"] = int(p["fee"])
    return meta


def read_erc20(rpc, tokens):
    calls = []
    for t in tokens:
        calls.append((t, S["symbol"]))
        calls.append((t, S["decimals"]))
    res = rpc.multicall(calls) if calls else []
    out = {}
    for i, t in enumerate(tokens):
        sym = _dec_symbol(*res[2 * i])
        dec = _dec(["uint8"], *res[2 * i + 1])
        out[t] = {"sym": sym or t[:6] + "…" + t[-4:], "dec": int(dec) if dec is not None else 18}
    return out
