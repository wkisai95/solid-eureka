"""Prices from DexScreener and routing share from public aggregator quotes."""
import time
import urllib.error

from .rpc import http_json

STABLES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 1.0,   # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 1.0,   # USDbC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 1.0,   # USDT
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 1.0,   # DAI
}


def fetch_prices(tokens, cache, now, log, max_age=1800):
    """USD price per token. Uses the deepest DexScreener pair; keeps recent prices as a fallback."""
    prices = {}
    todo = []
    for t in sorted(set(tokens)):
        c = cache.get(t)
        if c and now - c[1] < max_age:
            prices[t] = c[0]
        else:
            todo.append(t)
    fails = 0
    for i in range(0, len(todo), 30):
        part = todo[i:i + 30]
        try:
            pairs = http_json("https://api.dexscreener.com/tokens/v1/base/" + ",".join(part), timeout=20)
        except Exception as e:
            fails += 1
            log(f"dexscreener error: {e}")
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
                prices[t] = best[0]
                cache[t] = [best[0], now]
            elif t in STABLES:
                prices[t] = STABLES[t]
            elif t in cache:
                prices[t] = cache[t][0]
        time.sleep(0.25)
    for t, p in STABLES.items():
        prices.setdefault(t, p)
    for t in list(cache):
        if now - cache[t][1] > 7 * 86400:
            del cache[t]
    log(f"prices: {len(prices)} tokens priced, {len(todo)} looked up, {fails} failed requests")
    return prices


# ---------------------------------------------------------------- routing share

def _kyber(t_in, t_out, amount):
    url = ("https://aggregator-api.kyberswap.com/base/api/v1/routes"
           f"?tokenIn={t_in}&tokenOut={t_out}&amountIn={amount}&gasInclude=true")
    res = http_json(url, headers={"x-client-id": "radar-saero"}, timeout=20)
    rs = (res.get("data") or {}).get("routeSummary") or {}
    total = int(rs.get("amountIn") or 0)
    legs = []
    for path in rs.get("route") or []:
        if not path or not total:
            continue
        frac = int(path[0].get("swapAmount") or 0) / total
        legs.append({"frac": frac, "swaps": [[s.get("pool", "").lower()] for s in path],
                     "dex": sorted({s.get("exchange") or "?" for s in path})})
    return legs


def _velora(t_in, d_in, t_out, d_out, amount):
    url = ("https://api.paraswap.io/prices"
           f"?srcToken={t_in}&srcDecimals={d_in}&destToken={t_out}&destDecimals={d_out}"
           f"&amount={amount}&side=SELL&network=8453&version=6.2&partner=radar-saero")
    res = http_json(url, timeout=20)
    legs = []
    for route in (res.get("priceRoute") or {}).get("bestRoute") or []:
        rp = float(route.get("percent") or 0) / 100
        for sw in route.get("swaps") or []:
            for ex in sw.get("swapExchanges") or []:
                legs.append({"frac": rp * float(ex.get("percent") or 0) / 100, "route": id(route),
                             "swaps": [[a.lower() for a in (ex.get("poolAddresses") or [])]],
                             "dex": [ex.get("exchange") or "?"]})
    return legs


def _shares(legs, pool, aero_pools):
    """Share of the trade that touches this pool and any Aero pool."""
    s_pool = sum(l["frac"] for l in legs if any(pool in sw for sw in l["swaps"]))
    s_aero = sum(l["frac"] for l in legs if any(p in aero_pools for sw in l["swaps"] for p in sw))
    return min(1.0, s_pool), min(1.0, s_aero)


def routing_share(targets, aero_pools, prices, erc20, sizes, log):
    """targets: list of (gauge_key, pool, token0, token1). Returns {gauge_key: {agg: {size: [pool, aero]}}}."""
    out, errors, dexes = {}, {"kyber": 0, "velora": 0}, {}
    for key, pool, t0, t1 in targets:
        if not pool or not t0 or not t1:
            continue
        t_in, t_out = (t0, t1) if prices.get(t0) else (t1, t0)
        p_in = prices.get(t_in)
        if not p_in:
            continue
        d_in = (erc20.get(t_in) or {}).get("dec", 18)
        d_out = (erc20.get(t_out) or {}).get("dec", 18)
        res = {}
        for size in sizes:
            amount = int(size / p_in * 10 ** d_in)
            if amount <= 0:
                continue
            for agg in ("kyber", "velora"):
                try:
                    legs = _kyber(t_in, t_out, amount) if agg == "kyber" else _velora(t_in, d_in, t_out, d_out, amount)
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
                sp, sa = _shares(legs, pool, aero_pools)
                res.setdefault(agg, {})[str(size)] = [round(sp, 4), round(sa, 4)]
                for l in legs:
                    for d in l["dex"]:
                        dexes[d] = dexes.get(d, 0) + l["frac"]
                time.sleep(0.7)
        if res:
            out[key] = res
    log(f"routing: {len(out)} pools quoted, errors {errors}, venues seen "
        f"{sorted(dexes.items(), key=lambda kv: -kv[1])[:8]}")
    return out, errors
