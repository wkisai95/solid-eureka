"""Temporary probe: what the monitor needs to know about Elysium (chain 99901), read from the real endpoints."""
import json
import sys
import time
import traceback
import urllib.request

from eth_abi import decode, encode

from monitor import chain as C
from monitor.rpc import Rpc

T0 = time.time()
out = {"at": int(T0), "steps": {}}
LEAF = C.LEAF
OFFICIAL = "https://rpc.elysium.kinetiq.xyz"
WHYPE = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"
USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"
UA = {"content-type": "application/json", "user-agent": "Mozilla/5.0 (monitor-saero probe)", "accept": "application/json"}


def raw(url, method, params, timeout=10):
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"user-agent": UA["user-agent"], "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def step(name):
    def deco(fn):
        t = time.time()
        try:
            out["steps"][name] = fn()
        except Exception:
            out["steps"][name] = {"error": traceback.format_exc()[-700:]}
        out["steps"].setdefault(name + "_s", round(time.time() - t, 1))
        return fn
    return deco


@step("rpc_candidates")
def _():
    res = {}
    for u in [OFFICIAL, "https://elysium.drpc.org", "https://99901.rpc.thirdweb.com", "https://elysium-rpc.publicnode.com",
              "https://elysium.gateway.tenderly.co", "https://elysium-mainnet.public.blastapi.io", "https://1rpc.io/elysium",
              "https://elysium.meowrpc.com", "https://rpc.ankr.com/elysium", "https://elysium.api.onfinality.io/public",
              "https://endpoints.omniatech.io/v1/elysium/mainnet/public", "https://elysium.lava.build",
              "https://rpc-elysium.kinetiq.xyz", "https://elysium-mainnet.rpc.conduit.xyz", "https://elysium.rpc.hypersync.xyz",
              "https://elysium.blockpi.network/v1/rpc/public", "https://elysium.therpc.io", "https://elysium-mainnet.gateway.tatum.io"]:
        t = time.time()
        try:
            r = raw(u, "eth_chainId", [], timeout=6)
            row = {"chain": r.get("result"), "ms": round((time.time() - t) * 1000)}
            if r.get("result") and int(r["result"], 16) == 99901:
                row["head"] = int(raw(u, "eth_blockNumber", [], timeout=6)["result"], 16)
                try:
                    row["client"] = raw(u, "web3_clientVersion", [], timeout=6).get("result")
                except Exception as e:
                    row["client"] = "err " + str(e)[:60]
            elif "error" in r:
                row["err"] = json.dumps(r["error"])[:120]
            res[u] = row
        except Exception as e:
            res[u] = {"err": f"{type(e).__name__}: {str(e)[:100]}"}
    return res


def blk(n):
    b = raw(OFFICIAL, "eth_getBlockByNumber", [hex(n) if isinstance(n, int) else n, False])["result"]
    return {"n": int(b["number"], 16), "ts": int(b["timestamp"], 16), "l1": int(b.get("l1BlockNumber", "0x0"), 16) if b.get("l1BlockNumber") else None,
            "txs": len(b.get("transactions") or []), "gasUsed": int(b.get("gasUsed", "0x0"), 16)}


HEAD = {}


@step("clock")
def _():
    h = blk("latest")
    HEAD.update(h)
    res = {"head": h, "now": int(time.time())}
    for back in (1, 10, 100, 1000, 36000, 864000):
        try:
            b = blk(max(1, h["n"] - back))
            res[f"back_{back}"] = {**b, "bt": round((h["ts"] - b["ts"]) / max(1, h["n"] - b["n"]), 5)}
        except Exception as e:
            res[f"back_{back}"] = "err " + str(e)[:100]
    for n in (0, 1, 2, 1000, 100000, 1000000):
        try:
            res[f"block_{n}"] = blk(n)
        except Exception as e:
            res[f"block_{n}"] = "err " + str(e)[:100]
    # how many consecutive blocks share a timestamp
    ts = [blk(h["n"] - i)["ts"] for i in range(30)]
    res["last30_ts_distinct"] = len(set(ts))
    return res


def code_at(addr, block="latest"):
    r = raw(OFFICIAL, "eth_getCode", [addr, block if isinstance(block, str) else hex(block)])
    if "error" in r:
        raise RuntimeError(json.dumps(r["error"])[:200])
    return (len(r["result"]) - 2) // 2


@step("contracts")
def _():
    res = {}
    for name, a in (("LeafVoter", LEAF), ("Multicall3", "0xcA11bde05977b3631167028862bE2a173976CA11"), ("root voter", C.ROOT),
                    ("WHYPE", WHYPE), ("USDC", USDC), ("Base factory registry", C.FACTORY_REGISTRY)):
        try:
            res[name] = code_at(a)
        except Exception as e:
            res[name] = "err " + str(e)[:120]
    return res


RPC = Rpc([OFFICIAL], name="Elysium", log=lambda m: out.setdefault("rpc_log", []).append(str(m)[:240]))


@step("leaf_params")
def _():
    return C.read_leaf_params(RPC)


DEPLOY = {}


@step("deploy_block")
def _():
    """First block where the LeafVoter has code: bisect on historical eth_getCode; if the node keeps no old state,
    the first LeafVoter log found by reading forward."""
    head = HEAD["n"]
    res = {}
    try:
        assert code_at(LEAF, 1) == 0
        lo, hi = 1, head          # code at hi, none at lo
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if code_at(LEAF, mid) > 0:
                hi = mid
            else:
                lo = mid
        res["by_code"] = hi
        res["by_code_ts"] = blk(hi)["ts"]
        DEPLOY["block"] = hi
    except Exception as e:
        res["by_code_error"] = str(e)[:300]
    return res


def logs(frm, to, topics=None, address=LEAF, timeout=40):
    p = {"fromBlock": hex(frm), "toBlock": hex(to)}
    if address:
        p["address"] = address
    if topics:
        p["topics"] = topics
    t = time.time()
    r = raw(OFFICIAL, "eth_getLogs", [p], timeout=timeout)
    ms = round((time.time() - t) * 1000)
    if "error" in r:
        return None, json.dumps(r["error"])[:260], ms
    return r["result"], None, ms


@step("getlogs_limits")
def _():
    head = HEAD["n"] - 100
    res = {}
    for n in (1000, 10000, 50000, 200000, 1000000, 5000000):
        try:
            lg, err, ms = logs(max(1, head - n), head)
            res[str(n)] = {"logs": len(lg) if lg is not None else None, "err": err, "ms": ms}
        except Exception as e:
            res[str(n)] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    # the same without an address filter (the fee events are read by topic only)
    for n in (1000, 20000, 150000):
        try:
            lg, err, ms = logs(max(1, head - n), head, topics=[[C.T["fees"]]], address=None)
            res[f"fees_{n}"] = {"logs": len(lg) if lg is not None else None, "err": err, "ms": ms}
        except Exception as e:
            res[f"fees_{n}"] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    return res


GAUGES = []


@step("leaf_events")
def _():
    """Every LeafVoter event since its deployment, read the way the monitor would: counts per kind, the gauges."""
    head = HEAD["n"] - 100
    start = DEPLOY.get("block") or 1
    got, t, n_req = [], time.time(), 0
    for lg in RPC.get_logs(start, head, address=LEAF, topics=[C.LEAF_TOPICS], chunk=200000, max_chunk=2000000,
                           deadline=time.time() + 240):
        got.append(lg)
    kinds = {}
    names = {v: k for k, v in C.T.items()}
    first = None
    for lg in got:
        k = names.get(lg["topics"][0], lg["topics"][0][:10])
        kinds[k] = kinds.get(k, 0) + 1
        b = int(lg["blockNumber"], 16)
        first = b if first is None else min(first, b)
        if lg["topics"][0] == C.T["leaf_registered"]:
            try:
                ev = C.decode_log(lg, lambda b: 0)
                GAUGES.append(ev.get("gauge"))
            except Exception as e:
                out.setdefault("decode_errors", []).append(str(e)[:120])
    return {"from": start, "to": head, "logs": len(got), "kinds": kinds, "first_log_block": first, "last_chunk": RPC.last_chunk,
            "s": round(time.time() - t, 1), "gauges": len(GAUGES), "sample_event": (C.decode_log(got[0], lambda b: 0) if got else None)}


META = {}


@step("gauges")
def _():
    gs = [g for g in dict.fromkeys(GAUGES) if g]
    meta = C.read_gauge_meta(RPC, gs)
    toks = sorted({m[x] for m in meta.values() for x in ("t0", "t1") if m.get(x)})
    erc = C.read_erc20(RPC, toks)
    pend = C.read_pending_fees(RPC, gs)
    lp = C.read_leaf_params(RPC)
    reg = lp.get("factory_registry")
    caps = {}
    if reg:
        res = RPC.multicall([(reg, C.S["emissionCap"] + encode(["address"], [g])) for g in gs] + [(LEAF, C.S["isActivated"] + encode(["address"], [g])) for g in gs])
        for i, g in enumerate(gs):
            cap = C._dec(["uint128"], *res[i])
            act = C._dec(["bool"], *res[len(gs) + i])
            caps[g] = [None if cap is None else cap / 1e18 * 86400, act]
    pools = sorted({m["pool"] for m in meta.values() if m.get("pool")})
    facts = C.read_pool_facts(RPC, [p for p in pools if str(next(m for m in meta.values() if m.get("pool") == p).get("kind", "")).startswith("CL")],
                              [p for p in pools if not str(next(m for m in meta.values() if m.get("pool") == p).get("kind", "")).startswith("CL")])
    rows = []
    for g in gs:
        m = meta.get(g) or {}
        s0 = (erc.get(m.get("t0")) or {}).get("sym") if m.get("t0") else None
        s1 = (erc.get(m.get("t1")) or {}).get("sym") if m.get("t1") else None
        rows.append({"gauge": g, "pool": m.get("pool"), "pair": f"{s0}/{s1}", "kind": m.get("kind"), "fee_raw": m.get("fee_raw"), "facts": facts.get(m.get("pool")),
                     "t0": m.get("t0"), "t1": m.get("t1"), "vrm": m.get("vrm"), "pending": [str(x) for x in (pend.get(g) or [])], "cap_day_active": caps.get(g)})
    META.update({"rows": rows, "erc": erc, "pools": pools})
    return {"n": len(gs), "tokens": {t: erc.get(t) for t in toks}, "rows": rows, "multicall3": RPC.multicall3}


@step("votes")
def _():
    """Votes per gauge, the way the monitor reads them (refresh of the allocation power)."""
    from monitor import aero as A
    st = A.new_state(int(time.time()))
    cs = A.chain_state(st, 99901)
    for r in META.get("rows", []):
        st["gauges"][A.gkey(99901, r["gauge"])] = {"pool": r["pool"], "t0": r["t0"], "t1": r["t1"], "vrm": r["vrm"], "kind": r["kind"]}
    fn = getattr(A, "refresh_allocation_power", None)
    if not fn:
        return {"error": "no refresh_allocation_power in aero"}
    lg = []
    fn(RPC, 99901, st, int(time.time()), lg.append)
    return {"log": lg, "ap": {k: round(v) for k, v in st["ap"].items() if k.startswith("99901:")}}


@step("dexscreener")
def _():
    res = {}
    for label, q in (("search_whype", WHYPE), ("search_usdc", USDC)) + tuple((f"search_pool_{i}", p) for i, p in enumerate(META.get("pools", [])[:2])):
        try:
            st, body = get("https://api.dexscreener.com/latest/dex/search?q=" + q)
            d = json.loads(body)
            ps = d.get("pairs") or []
            res[label] = {"n": len(ps), "chains": sorted({p.get("chainId") for p in ps}), "dexes": sorted({p.get("dexId") for p in ps})[:12],
                          "sample": [{k: p.get(k) for k in ("chainId", "dexId", "labels", "pairAddress", "priceUsd")} | {"b": (p.get("baseToken") or {}).get("symbol"), "q": (p.get("quoteToken") or {}).get("symbol"),
                                     "liq": (p.get("liquidity") or {}).get("usd"), "v24": (p.get("volume") or {}).get("h24")} for p in ps[:6]]}
        except Exception as e:
            res[label] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
        time.sleep(0.4)
    for slug in ("elysium", "elysiumchain", "hyperliquid-elysium", "kinetiq"):
        for label, url in ((f"tokens_{slug}", f"https://api.dexscreener.com/tokens/v1/{slug}/{WHYPE},{USDC}"),
                           (f"tokenpairs_{slug}", f"https://api.dexscreener.com/token-pairs/v1/{slug}/{WHYPE}")):
            try:
                st, body = get(url)
                d = json.loads(body)
                ps = d if isinstance(d, list) else (d.get("pairs") or [])
                res[label] = {"status": st, "n": len(ps), "dexes": sorted({p.get("dexId") for p in ps})[:12],
                              "sample": [{k: p.get(k) for k in ("chainId", "dexId", "labels", "pairAddress", "priceUsd")} | {"b": (p.get("baseToken") or {}).get("symbol"), "q": (p.get("quoteToken") or {}).get("symbol"),
                                         "liq": (p.get("liquidity") or {}).get("usd"), "v24": (p.get("volume") or {}).get("h24")} for p in ps[:8]]}
            except Exception as e:
                res[label] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
            time.sleep(0.4)
    pools = META.get("pools", [])
    if pools:
        for slug in ("elysium",):
            try:
                st, body = get(f"https://api.dexscreener.com/latest/dex/pairs/{slug}/" + ",".join(pools[:30]))
                ps = json.loads(body).get("pairs") or []
                res[f"aero_pools_{slug}"] = {"asked": len(pools[:30]), "found": len(ps), "sample": [{k: p.get(k) for k in ("dexId", "labels", "pairAddress", "priceUsd")} | {"b": (p.get("baseToken") or {}).get("symbol"), "q": (p.get("quoteToken") or {}).get("symbol"),
                                             "liq": (p.get("liquidity") or {}).get("usd"), "v24": (p.get("volume") or {}).get("h24")} for p in ps[:10]]}
            except Exception as e:
                res[f"aero_pools_{slug}"] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    try:
        st, body = get("https://api.dexscreener.com/tokens/v1/hyperevm/0x5555555555555555555555555555555555555555")
        ps = json.loads(body)
        ps = ps if isinstance(ps, list) else []
        top = sorted(ps, key=lambda p: -float((p.get("liquidity") or {}).get("usd") or 0))[:5]
        res["hyperevm_whype"] = [{"dex": p.get("dexId"), "b": (p.get("baseToken") or {}).get("symbol"), "q": (p.get("quoteToken") or {}).get("symbol"), "priceUsd": p.get("priceUsd"), "priceNative": p.get("priceNative"),
                                  "liq": (p.get("liquidity") or {}).get("usd")} for p in top]
    except Exception as e:
        res["hyperevm_whype"] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    return res


@step("geckoterminal")
def _():
    res = {"networks": []}
    for page in range(1, 8):
        try:
            st, body = get(f"https://api.geckoterminal.com/api/v2/networks?page={page}")
            data = json.loads(body).get("data") or []
        except Exception as e:
            res[f"page_{page}"] = f"{type(e).__name__}: {str(e)[:120]}"
            break
        if not data:
            break
        for d in data:
            name = ((d.get("attributes") or {}).get("name") or "")
            if any(w in (d.get("id", "") + " " + name).lower() for w in ("elysium", "hyper", "kinetiq")):
                res["networks"].append({"id": d.get("id"), "name": name, "cg": (d.get("attributes") or {}).get("coingecko_asset_platform_id")})
        res["pages"] = page
        time.sleep(2.2)
    for net in [n["id"] for n in res["networks"] if "elysium" in (n["id"] + n["name"]).lower()][:2]:
        try:
            st, body = get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools?page=1&sort=h24_volume_usd_desc&include=base_token,quote_token,dex")
            d = json.loads(body)
            res[f"pools_{net}"] = [{"name": (p.get("attributes") or {}).get("name"), "addr": (p.get("attributes") or {}).get("address"), "v24": ((p.get("attributes") or {}).get("volume_usd") or {}).get("h24"),
                                    "liq": (p.get("attributes") or {}).get("reserve_in_usd"), "dex": (((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id")} for p in (d.get("data") or [])[:20]]
            time.sleep(2.2)
            st, body = get(f"https://api.geckoterminal.com/api/v2/networks/{net}/dexes")
            res[f"dexes_{net}"] = [(x.get("id"), (x.get("attributes") or {}).get("name")) for x in (json.loads(body).get("data") or [])]
            time.sleep(2.2)
            pools = META.get("pools", [])
            if pools:
                st, body = get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/multi/" + ",".join(pools[:20]))
                d = json.loads(body)
                res[f"aero_pools_{net}"] = {"asked": len(pools[:20]), "found": len(d.get("data") or []), "sample": [{"name": (p.get("attributes") or {}).get("name"), "v24": ((p.get("attributes") or {}).get("volume_usd") or {}).get("h24"),
                                            "liq": (p.get("attributes") or {}).get("reserve_in_usd"), "dex": (((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id")} for p in (d.get("data") or [])[:10]]}
                time.sleep(2.2)
                st, body = get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pools[0]}/ohlcv/hour?aggregate=1&limit=5&currency=usd")
                res[f"ohlcv_{net}"] = json.loads(body).get("data", {}).get("attributes", {}).get("ohlcv_list", [])[:3]
        except Exception as e:
            res[f"pools_{net}_err"] = f"{type(e).__name__}: {str(e)[:160]}"
    return res


@step("defillama")
def _():
    st, body = get("https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyVolume", timeout=40)
    d = json.loads(body)
    chains = [c for c in d.get("allChains") or [] if any(w in c.lower() for w in ("elysium", "hyper"))]
    aero = [{k: p.get(k) for k in ("name", "displayName", "parentProtocol", "total24h", "chains")} for p in d.get("protocols") or []
            if "aero" in (str(p.get("name")) + str(p.get("parentProtocol"))).lower()]
    res = {"chains_like": chains, "aero": aero}
    for c in chains:
        if "elysium" in c.lower():
            try:
                st, body = get(f"https://api.llama.fi/overview/dexs/{c}?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyVolume", timeout=40)
                dd = json.loads(body)
                res[f"chain_{c}"] = {"total24h": dd.get("total24h"), "protocols": sorted(((p.get("name"), p.get("total24h")) for p in dd.get("protocols") or []), key=lambda x: -(x[1] or 0))[:12]}
            except Exception as e:
                res[f"chain_{c}_err"] = str(e)[:160]
    return res


@step("explorer")
def _():
    res = {}
    for u in (f"https://elysium.kinetiq.xyz/explorer/api/v2/addresses/{LEAF}", f"https://elysium.kinetiq.xyz/explorer/api/v2/stats",
              f"https://elysium.kinetiq.xyz/explorer/api?module=contract&action=getcontractcreation&contractaddresses={LEAF}",
              f"https://explorer.elysium.kinetiq.xyz/api/v2/addresses/{LEAF}", "https://elysium.kinetiq.xyz/explorer"):
        try:
            st, body = get(u, timeout=15)
            res[u] = {"status": st, "body": body[:400]}
        except Exception as e:
            res[u] = {"err": f"{type(e).__name__}: {str(e)[:160]}"}
    return res


out["total_s"] = round(time.time() - T0, 1)
json.dump(out, sys.stdout, indent=1, default=str)
