"""Prices from DexScreener and routing share from public aggregator quotes, on every chain.

Tokens are keyed "chainId:address".
"""
import time
import urllib.error

from .rpc import http_json

# Stablecoins priced at $1 when no market price is available.
STABLES = {
    "8453:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 1.0,   # USDC
    "8453:0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 1.0,   # USDbC
    "8453:0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 1.0,   # USDT
    "8453:0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 1.0,   # DAI
    "10:0x0b2c639c533813f4aa9d7837caf62653d097ff85": 1.0,     # USDC
    "10:0x7f5c764cbc14f9669b88837ca1490cca17c31607": 1.0,     # USDC.e
    "10:0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": 1.0,     # USDT
    "10:0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": 1.0,     # DAI
    "1:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,      # USDC
    "1:0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,      # USDT
    "1:0x6b175474e89094c44da98b954eedeac495271d0f": 1.0,      # DAI
    "42161:0xaf88d065e77c8cc2239327c5edb3a432268e5831": 1.0,  # USDC
    "42161:0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 1.0,  # USDC.e
    "42161:0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 1.0,  # USDT
    "5042:0x3600000000000000000000000000000000000000": 1.0,   # USDC (Arc native)
}
# When a chain has no market data for a well-known token, its price is taken from the same asset on Base.
MAJORS = {"WETH": "eth", "ETH": "eth", "WBTC": "btc", "CBBTC": "btc", "USDC": "usd", "USDC.E": "usd",
          "USDT": "usd", "USDT0": "usd", "USD₮0": "usd", "DAI": "usd", "USDS": "usd", "USDBC": "usd"}
REFERENCE = {"eth": "8453:0x4200000000000000000000000000000000000006",
             "btc": "8453:0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"}


def _split(key):
    cid, addr = key.split(":", 1)
    return int(cid), addr.lower()


def _discover_slug(cid, addrs, taken, log):
    """DexScreener's name for a chain, found by searching one of its token addresses."""
    for a in addrs[:3]:
        try:
            res = http_json("https://api.dexscreener.com/latest/dex/search?q=" + a, timeout=20)
        except Exception as e:
            log(f"dexscreener search error: {e}")
            continue
        for p in (res or {}).get("pairs") or []:
            b = (p.get("baseToken") or {}).get("address", "").lower()
            q = (p.get("quoteToken") or {}).get("address", "").lower()
            slug = p.get("chainId")
            if a in (b, q) and slug and slug not in taken:
                return slug
        time.sleep(0.3)
    return None


def fetch_prices(token_keys, cache, now, log, chains_cfg, erc20, slugs, max_age=1800):
    """USD price per token key. Uses the deepest DexScreener pair; keeps recent prices as a fallback."""
    prices = {}
    want = set(token_keys) | set(REFERENCE.values())
    by_chain = {}
    for k in sorted(want):
        c = cache.get(k)
        if c and now - c[1] < max_age:
            prices[k] = c[0]
            continue
        cid, addr = _split(k)
        by_chain.setdefault(cid, []).append(addr)
    fails, looked = 0, 0
    taken = {str(c.get("dexscreener")) for c in chains_cfg.values() if c.get("dexscreener")}
    for cid, addrs in by_chain.items():
        ccfg = chains_cfg.get(str(cid)) or {}
        slug = ccfg.get("dexscreener") or (slugs.get(str(cid)) or {}).get("slug")
        if not slug:
            miss = slugs.get(str(cid)) or {}
            if now - miss.get("tried", 0) > 6 * 3600:
                slug = _discover_slug(cid, addrs, taken, log)
                slugs[str(cid)] = {"slug": slug, "tried": now}
                if slug:
                    log(f"dexscreener: chain {cid} is '{slug}'")
        if not slug:
            continue
        for i in range(0, len(addrs), 30):
            part = addrs[i:i + 30]
            looked += len(part)
            try:
                pairs = http_json(f"https://api.dexscreener.com/tokens/v1/{slug}/" + ",".join(part), timeout=20)
            except Exception as e:
                fails += 1
                log(f"dexscreener error ({slug}): {e}")
                pairs = []
            as_base, as_quote = {}, {}
            for p in pairs if isinstance(pairs, list) else []:
                try:
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    pu, pn = float(p.get("priceUsd") or 0), float(p.get("priceNative") or 0)
                except (TypeError, ValueError):
                    continue
                b = (p.get("baseToken") or {}).get("address", "").lower()
                q = (p.get("quoteToken") or {}).get("address", "").lower()
                if b in part and pu > 0 and liq > as_base.get(b, (0, 0))[1]:
                    as_base[b] = (pu, liq)
                if q in part and pu > 0 and pn > 0 and liq > as_quote.get(q, (0, 0))[1]:
                    as_quote[q] = (pu / pn, liq)
            for t in part:
                best = as_base.get(t) if (as_base.get(t) or (0, 0))[1] >= 500 else as_quote.get(t)
                if best and best[1] >= 500:
                    k = f"{cid}:{t}"
                    prices[k] = best[0]
                    cache[k] = [best[0], now]
            time.sleep(0.25)
    # fallbacks: old prices, $1 stables, the same asset on Base
    ref = {name: prices.get(k) or (cache.get(k) or [None])[0] for name, k in REFERENCE.items()}
    ref["usd"] = 1.0
    for k in sorted(want):
        if k in prices:
            continue
        if k in cache and now - cache[k][1] < 7 * 86400:
            prices[k] = cache[k][0]
        elif k in STABLES:
            prices[k] = STABLES[k]
        else:
            sym = ((erc20.get(k) or {}).get("sym") or "").upper()
            asset = MAJORS.get(sym)
            if asset and ref.get(asset):
                prices[k] = ref[asset]
    for k in list(cache):
        if now - cache[k][1] > 7 * 86400:
            del cache[k]
    counts = {}
    for k in prices:
        counts[k.split(":")[0]] = counts.get(k.split(":")[0], 0) + 1
    log(f"prices: {len(prices)} tokens priced {counts}, {looked} looked up, {fails} failed requests")
    return prices


# ---------------------------------------------------------------- routing share

def _kyber(slug, t_in, t_out, amount):
    url = (f"https://aggregator-api.kyberswap.com/{slug}/api/v1/routes"
           f"?tokenIn={t_in}&tokenOut={t_out}&amountIn={amount}&gasInclude=true")
    res = http_json(url, headers={"x-client-id": "radar-saero"}, timeout=20)
    rs = (res.get("data") or {}).get("routeSummary") or {}
    paths = [p for p in (rs.get("route") or []) if p]
    # each path starts from the input token; its first swap carries the share of the trade
    first = [(int(p[0].get("swapAmount") or 0), p) for p in paths
             if (p[0].get("tokenIn") or "").lower() == t_in.lower()] or [(int(p[0].get("swapAmount") or 0), p) for p in paths]
    total = sum(a for a, _ in first)
    legs = []
    for amt, path in first:
        if total <= 0:
            continue
        legs.append({"frac": amt / total, "swaps": [[(s.get("pool") or "").lower()] for s in path],
                     "dex": sorted({s.get("exchange") or "?" for s in path})})
    _sample("kyber", legs)
    return legs


SAMPLES = {}


def _sample(agg, legs):
    """Keeps one routed trade per aggregator for the run log."""
    if agg not in SAMPLES and legs:
        SAMPLES[agg] = [(round(l["frac"], 3), l["dex"], [p for sw in l["swaps"] for p in sw][:3]) for l in legs[:6]]


def _velora(network, t_in, d_in, t_out, d_out, amount):
    url = ("https://api.paraswap.io/prices"
           f"?srcToken={t_in}&srcDecimals={d_in}&destToken={t_out}&destDecimals={d_out}"
           f"&amount={amount}&side=SELL&network={network}&version=6.2&partner=radar-saero")
    res = http_json(url, timeout=20)
    legs = []
    for route in (res.get("priceRoute") or {}).get("bestRoute") or []:
        rp = float(route.get("percent") or 0) / 100
        swaps = route.get("swaps") or []
        # later hops carry the whole share of their route: count them once for every first-hop split
        later, dexes = [], set()
        for sw in swaps[1:]:
            for ex in sw.get("swapExchanges") or []:
                later += [a.lower() for a in (ex.get("poolAddresses") or [])]
                dexes.add(ex.get("exchange") or "?")
        for ex in (swaps[0].get("swapExchanges") or []) if swaps else []:
            share = rp * float(ex.get("percent") or 0) / 100
            legs.append({"frac": share, "swaps": [[a.lower() for a in (ex.get("poolAddresses") or [])], later],
                         "dex": sorted(dexes | {ex.get("exchange") or "?"})})
    _sample("velora", legs)
    return legs


def _shares(legs, pool, aero_pools):
    """Share of the trade that touches this pool and any Aero pool."""
    s_pool = sum(l["frac"] for l in legs if any(pool in sw for sw in l["swaps"]))
    s_aero = sum(l["frac"] for l in legs if any(p in aero_pools for sw in l["swaps"] for p in sw))
    return min(1.0, s_pool), min(1.0, s_aero)


def routing_share(targets, aero_pools, prices, erc20, sizes, chains_cfg, log):
    """targets: (gauge_key, chainId, pool, token0, token1). aero_pools: {chainId: {pool}}.

    Returns {gauge_key: {agg: {size: [pool share, Aero share]}}} and error counts.
    """
    out, errors, dexes = {}, {"kyber": 0, "velora": 0}, {}
    for key, cid, pool, t0, t1 in targets:
        ccfg = chains_cfg.get(str(cid)) or {}
        aggs = [a for a in ("kyber", "velora") if ccfg.get(a)]
        if not pool or not t0 or not t1 or not aggs:
            continue
        k0, k1 = f"{cid}:{t0}", f"{cid}:{t1}"
        t_in, t_out, k_in, k_out = (t0, t1, k0, k1) if prices.get(k0) else (t1, t0, k1, k0)
        p_in = prices.get(k_in)
        if not p_in:
            continue
        d_in = (erc20.get(k_in) or {}).get("dec", 18)
        d_out = (erc20.get(k_out) or {}).get("dec", 18)
        res = {}
        for size in sizes:
            amount = int(size / p_in * 10 ** d_in)
            if amount <= 0:
                continue
            for agg in aggs:
                try:
                    legs = (_kyber(ccfg["kyber"], t_in, t_out, amount) if agg == "kyber" else
                            _velora(ccfg["velora"], t_in, d_in, t_out, d_out, amount))
                except urllib.error.HTTPError as e:
                    errors[agg] += 1
                    if e.code == 429:
                        time.sleep(3)
                    continue
                except Exception:
                    errors[agg] += 1
                    continue
                if not legs:
                    errors[agg] += 1
                    continue
                sp, sa = _shares(legs, pool, aero_pools.get(int(cid), set()))
                res.setdefault(agg, {})[str(size)] = [round(sp, 4), round(sa, 4)]
                for l in legs:
                    for d in l["dex"]:
                        dexes[d] = dexes.get(d, 0) + l["frac"]
                time.sleep(0.7)
        if res:
            out[key] = res
    log(f"routing: {len(out)} pools quoted, errors {errors}, venues seen "
        f"{sorted(((d, round(v, 2)) for d, v in dexes.items()), key=lambda kv: -kv[1])[:10]}")
    for agg, smp in SAMPLES.items():
        log(f"routing sample {agg}: {smp}")
    return out, errors
