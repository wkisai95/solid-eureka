"""Market data for the strategies and for a future predictive model.

Once an hour: volume and liquidity of every Aero pool (DexScreener), the volume of the same token pair on every
DEX of that chain (so Aero's share of each pair is known) and the 24-hour DEX volume of each chain (DefiLlama),
which gives Aero's share of each chain. History: hourly for a few days, then one value per day for months.
"""
import re
import time
import urllib.parse

from . import aero as A
from . import chain as C
from .rpc import http_json

HOUR = 3600
DAY = 86400
POOL_HOURS = 72          # hourly history per pool
CHAIN_HOURS = 7 * 24     # hourly history per chain
DAILY_DAYS = 120         # then one value per day
DS = "https://api.dexscreener.com"
LLAMA = "https://api.llama.fi/overview/dexs"
LLAMA_Q = "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyVolume"
CHAIN_ALIASES = {8453: ("base",), 10: ("optimism", "opmainnet"), 1: ("ethereum",), 42161: ("arbitrum", "arbitrumone"),
                 57073: ("ink",), 4663: ("robinhood", "robinhoodchain"), 5042: ("arc", "arcmainnet")}
# pool fields kept in state["market"]["pools"][gauge]
FIELDS = ("vol_1h", "vol_6h", "vol_24h", "liq", "pair_vol_24h", "pair_liq")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def new_market():
    return {"at": 0, "pools": {}, "chains": {}, "hist": {"pools": {}, "chains": {}},
            "daily": {"pools": {}, "chains": {}}}


def market_state(state):
    mk = state.setdefault("market", new_market())
    for k, v in new_market().items():
        mk.setdefault(k, v)
    return mk


# ---------------------------------------------------------------- sources

def chain_volumes(chain_ids, log):
    """24-hour DEX volume of each chain from DefiLlama: {chainId: usd}."""
    out = {}
    try:
        res = http_json(LLAMA + LLAMA_Q, timeout=45)
    except Exception as e:
        log(f"defillama error: {str(e)[:160]}")
        return out
    totals = {}
    for p in res.get("protocols") or []:
        for chain, versions in (p.get("breakdown24h") or {}).items():
            v = sum(_num(x) for x in versions.values()) if isinstance(versions, dict) else _num(versions)
            totals[_norm(chain)] = totals.get(_norm(chain), 0.0) + v
    for cid in chain_ids:
        for a in CHAIN_ALIASES.get(int(cid), ()):
            if totals.get(a):
                out[int(cid)] = totals[a]
                break
    # chains missing from the breakdown: ask DefiLlama for each of them alone
    names = {_norm(n): n for n in res.get("allChains") or []}
    for cid in chain_ids:
        if int(cid) in out:
            continue
        name = next((names[a] for a in CHAIN_ALIASES.get(int(cid), ()) if a in names), None)
        if not name:
            continue
        try:
            r = http_json(f"{LLAMA}/{urllib.parse.quote(name)}{LLAMA_Q}", timeout=45)
            if _num(r.get("total24h")) > 0:
                out[int(cid)] = _num(r["total24h"])
        except Exception as e:
            log(f"defillama {name}: {str(e)[:120]}")
        time.sleep(0.5)
    miss = [C.chain_short(c) for c in chain_ids if int(c) not in out]
    if miss:
        log(f"defillama: no DEX volume for {miss}")
    return out


def _pairs_by_address(slug, addrs, log):
    out = {}
    for i in range(0, len(addrs), 30):
        part = addrs[i:i + 30]
        try:
            res = http_json(f"{DS}/latest/dex/pairs/{slug}/" + ",".join(part), timeout=20)
        except Exception as e:
            log(f"dexscreener pairs ({slug}): {str(e)[:120]}")
            continue
        for p in (res or {}).get("pairs") or []:
            a = (p.get("pairAddress") or "").lower()
            if a in part:
                out[a] = p
        time.sleep(0.2)
    return out


def _token_pairs(slug, tokens, log):
    """Every pair DexScreener lists for these tokens on one chain, any DEX, by pair address."""
    out = {}
    for i in range(0, len(tokens), 30):
        part = tokens[i:i + 30]
        try:
            res = http_json(f"{DS}/tokens/v1/{slug}/" + ",".join(part), timeout=20)
        except Exception as e:
            log(f"dexscreener tokens ({slug}): {str(e)[:120]}")
            continue
        for p in res if isinstance(res, list) else []:
            a = (p.get("pairAddress") or "").lower()
            if a:
                out[a] = p
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- refresh and history

def refresh(state, chains_cfg, now, log, every=55 * 60):
    """Hourly snapshot of every Aero pool and chain. Returns True when it refreshed."""
    mk = market_state(state)
    if now - mk.get("at", 0) < every:
        return False
    t0 = time.time()
    by_chain = {}
    for k, m in state["gauges"].items():
        if m.get("pool"):
            by_chain.setdefault(A.chain_of(k), []).append(
                (k, m["pool"].lower(), (m.get("t0") or "").lower(), (m.get("t1") or "").lower()))
    slugs = state.get("dex_slugs", {})
    pools, aero_vol, seen = {}, {}, 0
    for cid, rows in sorted(by_chain.items()):
        ccfg = chains_cfg.get(str(cid)) or {}
        slug = ccfg.get("dexscreener") or (slugs.get(str(cid)) or {}).get("slug")
        if not slug:
            continue
        own = _pairs_by_address(slug, sorted({r[1] for r in rows}), log)
        every_dex = _token_pairs(slug, sorted({t for r in rows for t in r[2:] if t}), log)
        every_dex.update(own)
        seen += len(every_dex)
        pair_market = {}
        for p in every_dex.values():
            b = ((p.get("baseToken") or {}).get("address") or "").lower()
            q = ((p.get("quoteToken") or {}).get("address") or "").lower()
            if b and q:
                v = pair_market.setdefault(tuple(sorted((b, q))), [0.0, 0.0])
                v[0] += _num((p.get("volume") or {}).get("h24"))
                v[1] += _num((p.get("liquidity") or {}).get("usd"))
        for k, pool, tok0, tok1 in rows:
            p = own.get(pool)
            if not p:
                continue
            vol = p.get("volume") or {}
            mkt = pair_market.get(tuple(sorted((tok0, tok1)))) if tok0 and tok1 else None
            pools[k] = [round(_num(vol.get("h1"))), round(_num(vol.get("h6"))), round(_num(vol.get("h24"))),
                        round(_num((p.get("liquidity") or {}).get("usd"))),
                        round(mkt[0]) if mkt else None, round(mkt[1]) if mkt else None]
            aero_vol[cid] = aero_vol.get(cid, 0.0) + _num(vol.get("h24"))
    dex = chain_volumes(list(by_chain), log)
    chains = {str(cid): [round(aero_vol.get(cid, 0.0)), round(dex[cid]) if cid in dex else None] for cid in by_chain}
    mk["pools"], mk["chains"], mk["at"] = pools, chains, now
    record(mk, now)
    shares = ", ".join(f"{C.chain_short(int(c))} {v[0] / v[1] * 100:.1f}%" if v[1] else f"{C.chain_short(int(c))} n/d"
                       for c, v in sorted(chains.items(), key=lambda kv: -(kv[1][0] or 0)))
    log(f"market: volume for {len(pools)} pools, {seen} DEX pairs seen; Aero share of DEX volume: {shares}; "
        f"{time.time() - t0:.0f}s")
    return True


def record(mk, now):
    hour = str(now - now % HOUR)
    hp, hc = mk["hist"]["pools"], mk["hist"]["chains"]
    for k, v in mk["pools"].items():
        hp.setdefault(k, {})[hour] = [v[0], v[2], v[3], v[4]]       # volume 1h, volume 24h, liquidity, pair volume
    for c, v in mk["chains"].items():
        hc.setdefault(c, {})[hour] = v                              # Aero volume 24h, chain DEX volume 24h
    _roll(hp, mk["daily"]["pools"], now - POOL_HOURS * HOUR, now)
    _roll(hc, mk["daily"]["chains"], now - CHAIN_HOURS * HOUR, now)


def _roll(hourly, daily, cutoff, now):
    """Hours past the cutoff become one value per day (the last hour of that day); old days are dropped."""
    for k, series in list(hourly.items()):
        for h in sorted(x for x in series if int(x) < cutoff):
            daily.setdefault(k, {})[str(int(h) - int(h) % DAY)] = series.pop(h)
        if not series:
            del hourly[k]
    dcut = now - DAILY_DAYS * DAY
    for k, series in list(daily.items()):
        for d in [x for x in series if int(x) < dcut]:
            del series[d]
        if not series:
            del daily[k]


def chain_share(mk, cid, ago=0, now=None):
    """Aero's share of the chain's DEX volume now, or about `ago` seconds earlier from the history."""
    if not ago:
        v = (mk.get("chains") or {}).get(str(cid))
        return v[0] / v[1] if v and v[1] else None
    series = (mk.get("hist") or {}).get("chains", {}).get(str(cid)) or {}
    target = (now or mk.get("at") or 0) - ago
    best = min(series.items(), key=lambda kv: abs(int(kv[0]) - target), default=None)
    if not best or abs(int(best[0]) - target) > 3 * HOUR:
        daily = (mk.get("daily") or {}).get("chains", {}).get(str(cid)) or {}
        best = min(daily.items(), key=lambda kv: abs(int(kv[0]) - target), default=None)
        if not best or abs(int(best[0]) - target) > DAY:
            return None
    v = best[1]
    return v[0] / v[1] if v and v[1] else None


# ---------------------------------------------------------------- "Chain nuove" universe

MAJORS = {"ETH", "WETH", "WBTC", "CBBTC", "TBTC", "LBTC", "BTC", "USDC", "USDC.E", "USDT", "USDT0", "USD₮0", "DAI",
          "USDS", "EURC", "USDG", "PYUSD", "GHO", "FRAX", "USDE"}
NEW_CHAIN_RULES = {"home_chains": [8453, 10], "max_chain_share": 0.15, "pairs_per_chain": 10,
                   "min_pair_volume_usd": 250000}


def _both_majors(state, key):
    m = state["gauges"].get(key) or {}
    cid = A.chain_of(key)
    syms = [((state["erc20"].get(A.tkey(cid, t)) or {}).get("sym") or "").upper() for t in (m.get("t0"), m.get("t1"))
            if t]
    return len(syms) == 2 and all(s in MAJORS for s in syms)


def new_chain_universe(state, rules=None):
    """Big pairs on the chains where Aero is still small: the pools the 'Chain nuove' method may choose.

    Chains: outside the home chains (Base and OP by default) and with Aero below max_chain_share of the chain's DEX
    volume. Pairs: the pairs_per_chain largest by volume on every DEX of that chain, at least min_pair_volume_usd a
    day; pools DexScreener does not list yet count when both tokens are blue chips.
    """
    r = {**NEW_CHAIN_RULES, **(rules or {})}
    mk = market_state(state)
    home = {int(c) for c in r["home_chains"]}
    gauges_chains = {A.chain_of(k) for k in state["gauges"]} - home
    ok = set()
    for cid in gauges_chains:
        share = chain_share(mk, cid)
        if share is None or share < float(r["max_chain_share"]):
            ok.add(cid)
    by_chain = {}
    for k, v in (mk.get("pools") or {}).items():
        cid = A.chain_of(k)
        if cid in ok and v[4] and v[4] >= float(r["min_pair_volume_usd"]):
            by_chain.setdefault(cid, []).append((v[4], k))
    out = set()
    for lst in by_chain.values():
        out |= {k for _, k in sorted(lst, reverse=True)[: int(r["pairs_per_chain"])]}
    covered = set(mk.get("pools") or {})
    out |= {k for k in state["gauges"] if A.chain_of(k) in ok and k not in covered and _both_majors(state, k)}
    return out, ok
