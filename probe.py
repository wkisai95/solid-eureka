"""Temporary probe: what DexScreener, GeckoTerminal and DefiLlama return for pair and chain volumes."""
import json, re, sys, time, urllib.request, urllib.error

def get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (monitor-saero probe)", "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            hdr = {k: v for k, v in r.headers.items() if re.match(r"(x-)?rate|retry|cf-cache|age$|x-cache", k, re.I)}
            return {"status": r.status, "ms": int((time.time() - t0) * 1000), "headers": hdr, "json": json.loads(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read()[:400].decode("utf-8", "replace"), "headers": dict(e.headers)}
    except Exception as e:
        return {"status": None, "error": str(e)[:300]}

def ds_pairs(res):
    j = res.get("json")
    pairs = j if isinstance(j, list) else (j or {}).get("pairs") or []
    rows = []
    for p in pairs:
        rows.append([p.get("chainId"), p.get("dexId"), ",".join(p.get("labels") or []), p["baseToken"]["symbol"], p["quoteToken"]["symbol"],
                     p["baseToken"]["address"][:10], p["quoteToken"]["address"][:10],
                     round(float((p.get("volume") or {}).get("h24") or 0)), round(float((p.get("liquidity") or {}).get("usd") or 0))])
    return {"status": res.get("status"), "ms": res.get("ms"), "headers": res.get("headers"), "error": res.get("error"),
            "count": len(pairs), "rows": rows[:80]}

out = {}
DS = "https://api.dexscreener.com"
E = {"USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7", "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
     "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"}
A = {"WBTC": "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f", "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
     "USDT0": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "WETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"}
probes = {
    "ds_tokens_usdt_eth": f"{DS}/tokens/v1/ethereum/{E['USDT']}",
    "ds_tokenpairs_usdt_eth": f"{DS}/token-pairs/v1/ethereum/{E['USDT']}",
    "ds_tokens_4_eth": f"{DS}/tokens/v1/ethereum/{E['USDT']},{E['WETH']},{E['USDC']},{E['WBTC']}",
    "ds_tokenpairs_weth_eth": f"{DS}/token-pairs/v1/ethereum/{E['WETH']}",
    "ds_old_tokens_usdt": f"{DS}/latest/dex/tokens/{E['USDT']}",
    "ds_search_weth_usdt": f"{DS}/latest/dex/search?q=WETH%20USDT",
    "ds_tokenpairs_wbtc_arb": f"{DS}/token-pairs/v1/arbitrum/{A['WBTC']}",
    "ds_tokenpairs_usdt0_arb": f"{DS}/token-pairs/v1/arbitrum/{A['USDT0']}",
}
for k, u in probes.items():
    out[k] = ds_pairs(get(u))
    time.sleep(0.4)

# GeckoTerminal
GT = "https://api.geckoterminal.com/api/v2"
nets = []
for page in range(1, 8):
    r = get(f"{GT}/networks?page={page}")
    data = (r.get("json") or {}).get("data") or []
    if not data:
        out.setdefault("gt_networks_err", []).append({"page": page, "status": r.get("status"), "error": r.get("error")})
        break
    nets += [[d["id"], d["attributes"].get("name"), d["attributes"].get("coingecko_asset_platform_id")] for d in data]
    time.sleep(2.2)
out["gt_networks_match"] = [n for n in nets if re.search(r"base|ether|eth$|arbitrum|optimism|^op|ink|robinhood|arc", " ".join(str(x) for x in n), re.I)]
out["gt_networks_total"] = len(nets)

def gt_pools(res):
    j = res.get("json") or {}
    inc = {d["id"]: d["attributes"].get("symbol") for d in j.get("included") or [] if d.get("type") == "token"}
    rows = []
    for d in j.get("data") or []:
        a, rel = d["attributes"], d.get("relationships") or {}
        b = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        q = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
        rows.append([a.get("name"), dex, inc.get(b, b), inc.get(q, q), round(float((a.get("volume_usd") or {}).get("h24") or 0)),
                     round(float(a.get("reserve_in_usd") or 0)), a.get("address", "")[:12]])
    return {"status": res.get("status"), "ms": res.get("ms"), "headers": res.get("headers"), "error": res.get("error"),
            "count": len(rows), "rows": rows, "links": j.get("links"), "meta": j.get("meta")}

for net in ("eth", "arbitrum", "base"):
    time.sleep(2.2)
    out[f"gt_top_{net}"] = gt_pools(get(f"{GT}/networks/{net}/pools?page=1&sort=h24_volume_usd_desc&include=base_token,quote_token"))
time.sleep(2.2)
out["gt_top_eth_p5"] = gt_pools(get(f"{GT}/networks/eth/pools?page=5&sort=h24_volume_usd_desc&include=base_token,quote_token"))
time.sleep(2.2)
out["gt_token_pools_usdt_eth"] = gt_pools(get(f"{GT}/networks/eth/tokens/{E['USDT']}/pools?page=1&sort=h24_volume_usd_desc&include=base_token,quote_token"))
for n in out["gt_networks_match"]:
    if re.search(r"robinhood|arc|ink", f"{n[0]} {n[1]}", re.I):
        time.sleep(2.2)
        out[f"gt_top_{n[0]}"] = gt_pools(get(f"{GT}/networks/{n[0]}/pools?page=1&sort=h24_volume_usd_desc&include=base_token,quote_token"))

# DefiLlama
LL = "https://api.llama.fi/overview/dexs"
Q = "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyVolume"
r = get(LL + Q, timeout=60)
j = r.get("json") or {}
out["llama_status"] = r.get("status")
out["llama_allChains_match"] = [c for c in j.get("allChains") or [] if re.search(r"robin|arc|ink|optim|op |arbitrum|base|ethereum", c, re.I)]
norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
want = {"base", "optimism", "opmainnet", "ethereum", "arbitrum", "ink", "robinhood", "robinhoodchain", "arc", "arcmainnet"}
sums, tops = {}, {}
aero = []
for p in j.get("protocols") or []:
    nm = f"{p.get('name')} | {p.get('displayName')} | parent={p.get('parentProtocol')}"
    if re.search(r"aero|velo|slipstream", nm, re.I):
        aero.append({"name": nm, "chains": p.get("chains"), "total24h": p.get("total24h"), "breakdown24h": p.get("breakdown24h")})
    for ch, vers in (p.get("breakdown24h") or {}).items():
        v = sum(float(x or 0) for x in vers.values()) if isinstance(vers, dict) else float(vers or 0)
        sums[norm(ch)] = sums.get(norm(ch), 0) + v
        if norm(ch) in want:
            tops.setdefault(norm(ch), []).append([p.get("name"), round(v)])
out["llama_aero_protocols"] = aero
out["llama_sum_by_chain"] = {k: round(v) for k, v in sums.items() if k in want}
out["llama_top_by_chain"] = {k: sorted(v, key=lambda x: -x[1])[:12] for k, v in tops.items()}
out["llama_breakdown_keys_sample"] = sorted({k for p in j.get("protocols") or [] for k in (p.get("breakdown24h") or {})})[:400]
for ch in ("Arbitrum", "Ethereum", "Base", "OP Mainnet", "Optimism", "Robinhood", "Robinhood Chain", "Arc", "Ink"):
    time.sleep(0.5)
    rr = get(f"{LL}/{urllib.request.quote(ch)}{Q}", timeout=60)
    jj = rr.get("json") or {}
    out[f"llama_chain_{ch}"] = {"status": rr.get("status"), "total24h": jj.get("total24h"), "change_1d": jj.get("change_1d"),
                                "n_protocols": len(jj.get("protocols") or []), "error": rr.get("error")}
json.dump(out, sys.stdout, indent=1, default=str)
