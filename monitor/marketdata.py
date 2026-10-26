"""Market data for the strategies and for a future predictive model.

Every hour:
- volume and liquidity of every Aero pool (DexScreener, by pool address);
- the volume of the same token pair on every DEX of the chain, so Aero's share of each pair is known. The pools come
  from DexScreener (up to 30 for each token of the Aero pools) and from GeckoTerminal (the 20 biggest pools of a
  chain, which DexScreener's per-token lists can miss; its free API allows few calls, so one chain per run in
  rotation and each list is used for a few hours);
- the 24-hour DEX volume of each chain and Aero's own volume there, both from DefiLlama, so the share of each chain
  compares like with like.
History: hourly for a few days, then one value per day for months.
"""
import re
import time
import urllib.error
import urllib.parse

from . import aero as A
from . import chain as C
from .rpc import http_json

HOUR = 3600
DAY = 86400
VERSION = 2
POOL_HOURS = 72          # hourly history per pool
CHAIN_HOURS = 7 * 24     # hourly history per chain
DAILY_DAYS = 120         # then one value per day
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
LLAMA = "https://api.llama.fi/overview/dexs"
LLAMA_Q = "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyVolume"
AERO_PARENT = "parent#aero-protocol"     # DefiLlama groups Aero, Aerodrome and Velodrome under this parent
CHAIN_ALIASES = {8453: ("base",), 10: ("optimism", "opmainnet"), 1: ("ethereum",), 42161: ("arbitrum", "arbitrumone"),
                 57073: ("ink",), 4663: ("robinhood", "robinhoodchain"), 5042: ("arc", "arcmainnet")}
GT_NETWORKS = {8453: "base", 10: "optimism", 1: "eth", 42161: "arbitrum", 57073: "ink", 4663: "robinhood",
               5042: "arc"}
NATIVE = ("0x0000000000000000000000000000000000000000", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
GT_EVERY = HOUR          # a chain's GeckoTerminal list is refreshed when older than this (one chain per run)
GT_MAX_AGE = 4 * HOUR    # older lists are not used
LOOKUP_MIN_USD = 10_000  # Aero pools below this volume and liquidity get no per-token lookup
LOOKUP_PACE_S = 0.22     # DexScreener allows 300 requests a minute
LOOKUP_BUDGET_S = 100
TOP_PAIRS = 12
# pool fields kept in state["market"]["pools"][gauge]
FIELDS = ("vol_1h", "vol_6h", "vol_24h", "liq", "pair_vol_24h", "pair_liq", "pair_pools")
# chain fields kept in state["market"]["chains"][chain id]
CHAIN_FIELDS = ("aero_vol_24h_dexscreener", "dex_vol_24h", "aero_vol_24h", "legacy_vol_24h")


class Limited(Exception):
    """The API keeps answering 429."""


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def new_market():
    return {"v": VERSION, "at": 0, "pools": {}, "chains": {}, "top": {}, "gt": {},
            "hist": {"pools": {}, "chains": {}}, "daily": {"pools": {}, "chains": {}}}


def market_state(state):
    mk = state.setdefault("market", new_market())
    if mk.get("v", 1) < VERSION:
        # version 1 measured a pair only through one pool per token and Aero's chain share against another source:
        # its hour or two of history would look like a jump, so the history starts again
        mk.clear()
        mk.update(new_market())
    for k, v in new_market().items():
        mk.setdefault(k, v)
    return mk


def _get(url, timeout=20, retries=1):
    """JSON from a public API; after a 429 waits and retries, then raises Limited."""
    for i in range(retries + 1):
        try:
            return http_json(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if i == retries:
                raise Limited(url) from e
            time.sleep(min(30.0, max(3.0, _num(e.headers.get("retry-after")))))
    return None


# ---------------------------------------------------------------- DefiLlama: chain volume and Aero's part of it

def _aero_kind(p):
    """'aero' for the Aero DEX, 'legacy' for Aerodrome and Velodrome, None for any other protocol."""
    if p.get("parentProtocol") != AERO_PARENT:
        return None
    name = _norm(p.get("name") or p.get("displayName") or "")
    return "legacy" if name.startswith(("aerodrome", "velodrome")) else "aero"


def chain_volumes(chain_ids, log):
    """24-hour DEX volume from DefiLlama: {chainId: [every DEX, Aero, Aerodrome and Velodrome]}."""
    out = {}
    try:
        res = http_json(LLAMA + LLAMA_Q, timeout=45)
    except Exception as e:
        log(f"defillama error: {str(e)[:160]}")
        return out
    totals = {}
    for p in res.get("protocols") or []:
        kind = _aero_kind(p)
        for chain, versions in (p.get("breakdown24h") or {}).items():
            v = sum(_num(x) for x in versions.values()) if isinstance(versions, dict) else _num(versions)
            t = totals.setdefault(_norm(chain), [0.0, 0.0, 0.0])
            t[0] += v
            if kind == "aero":
                t[1] += v
            elif kind == "legacy":
                t[2] += v
    for cid in chain_ids:
        for a in CHAIN_ALIASES.get(int(cid), ()):
            if totals.get(a) and totals[a][0] > 0:
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
                t = [_num(r["total24h"]), 0.0, 0.0]
                for p in r.get("protocols") or []:
                    kind = _aero_kind(p)
                    if kind:
                        t[1 if kind == "aero" else 2] += _num(p.get("total24h"))
                out[int(cid)] = t
        except Exception as e:
            log(f"defillama {name}: {str(e)[:120]}")
        time.sleep(0.5)
    miss = [C.chain_short(c) for c in chain_ids if int(c) not in out]
    if miss:
        log(f"defillama: no DEX volume for {miss}")
    return out


# ---------------------------------------------------------------- pools on every DEX

def _pairs_by_address(slug, addrs, log):
    out = {}
    for i in range(0, len(addrs), 30):
        part = addrs[i:i + 30]
        try:
            res = _get(f"{DS}/latest/dex/pairs/{slug}/" + ",".join(part))
        except Exception as e:
            log(f"dexscreener pairs ({slug}): {str(e)[:120]}")
            continue
        for p in (res or {}).get("pairs") or []:
            a = (p.get("pairAddress") or "").lower()
            if a in part:
                out[a] = p
        time.sleep(LOOKUP_PACE_S)
    return out


def discover(state, now, log):
    """The 20 biggest pools (any DEX) of one chain from GeckoTerminal: the chain whose list is the oldest.

    One request per run keeps well inside GeckoTerminal's free limit; every chain is refreshed every couple of hours.
    """
    mk = market_state(state)
    gt = mk["gt"]
    chains = sorted({A.chain_of(k) for k in state["gauges"]} & set(GT_NETWORKS))
    if not chains:
        return False
    cid = min(chains, key=lambda c: (gt.get(str(c)) or {}).get("tried", 0))
    cur = gt.get(str(cid)) or {}
    if now - cur.get("at", 0) < GT_EVERY:
        return False
    gt[str(cid)] = {**cur, "tried": now}
    net = GT_NETWORKS[cid]
    try:
        res = _get(f"{GT}/networks/{net}/pools?page=1&sort=h24_volume_usd_desc&include=base_token,quote_token",
                   retries=0)
    except Limited:
        log(f"geckoterminal ({net}): limit reached, next run")
        return False
    except Exception as e:
        log(f"geckoterminal ({net}): {str(e)[:120]}")
        return False
    syms = {d.get("id"): (d.get("attributes") or {}).get("symbol") or ""
            for d in (res or {}).get("included") or [] if d.get("type") == "token"}
    pools = []
    for d in (res or {}).get("data") or []:
        a, rel = d.get("attributes") or {}, d.get("relationships") or {}
        ids = [((rel.get(r) or {}).get("data") or {}).get("id") or "" for r in ("base_token", "quote_token")]
        toks = [i.rsplit("_", 1)[-1].lower() for i in ids]
        if not (a.get("address") and toks[0].startswith("0x") and toks[1].startswith("0x")):
            continue
        pools.append([a["address"].lower(), toks[0], toks[1], syms.get(ids[0], ""), syms.get(ids[1], ""),
                      round(_num((a.get("volume_usd") or {}).get("h24"))), round(_num(a.get("reserve_in_usd"))),
                      ((rel.get("dex") or {}).get("data") or {}).get("id") or ""])
    gt[str(cid)] = {"at": now, "tried": now, "pools": pools}
    log(f"geckoterminal ({net}): {len(pools)} biggest pools")
    return True


def _chain_tokens(state, cid):
    """Symbol -> address of the tokens in the chain's Aero pools (to map a native coin to its wrapped token)."""
    out = {}
    for k, m in state["gauges"].items():
        if A.chain_of(k) != cid:
            continue
        for t in (m.get("t0"), m.get("t1")):
            if t:
                sym = ((state["erc20"].get(A.tkey(cid, t)) or {}).get("sym") or "").upper()
                if sym:
                    out.setdefault(sym, t.lower())
    return out


def _token(addr, sym, wrapped):
    """Pool token as a lowercase address; the native coin (ETH, or USDC on Arc) becomes its wrapped token."""
    a = (addr or "").lower()
    if a in NATIVE:
        s = (sym or "").upper()
        return wrapped.get("W" + s) or wrapped.get(s) or a
    return a


class _Market:
    """Every pool seen on one chain, by pool address, and the pairs they add up to."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.pools = {}     # address -> (pair, volume 24h, liquidity)
        self.syms = {}

    def add(self, addr, a, asym, b, bsym, vol, liq, replace=True):
        addr = (addr or "").lower()
        a, b = _token(a, asym, self.wrapped), _token(b, bsym, self.wrapped)
        if not addr or not a or not b or a == b or (addr in self.pools and not replace):
            return
        self.syms.setdefault(a, asym)
        self.syms.setdefault(b, bsym)
        self.pools[addr] = (tuple(sorted((a, b))), _num(vol), _num(liq))

    def add_ds(self, p):
        bt, qt = p.get("baseToken") or {}, p.get("quoteToken") or {}
        self.add(p.get("pairAddress"), bt.get("address"), bt.get("symbol"), qt.get("address"), qt.get("symbol"),
                 (p.get("volume") or {}).get("h24"), (p.get("liquidity") or {}).get("usd"))

    def pairs(self):
        out = {}
        for addr, (pair, vol, liq) in self.pools.items():
            v = out.setdefault(pair, [0.0, 0.0, 0, []])
            v[0] += vol
            v[1] += liq
            v[2] += 1
            v[3].append(addr)
        return out


def refresh(state, chains_cfg, now, log, every=55 * 60):
    """Hourly snapshot of every Aero pool and chain. Returns True when it refreshed."""
    mk = market_state(state)
    if now - mk.get("at", 0) < every:
        return False
    t_start = time.time()
    by_chain = {}
    for k, m in state["gauges"].items():
        if m.get("pool") and m.get("t0") and m.get("t1"):
            by_chain.setdefault(A.chain_of(k), []).append((k, m["pool"].lower(), m["t0"].lower(), m["t1"].lower()))
    slugs = state.get("dex_slugs", {})
    own, markets = {}, {}
    for cid, rows in sorted(by_chain.items()):
        slug = (chains_cfg.get(str(cid)) or {}).get("dexscreener") or (slugs.get(str(cid)) or {}).get("slug")
        if slug:
            own[cid] = (slug, _pairs_by_address(slug, sorted({r[1] for r in rows}), log))
            markets[cid] = _Market(_chain_tokens(state, cid))

    # every pool of the tokens of the Aero pools that trade, the most traded first, within a time budget
    want = {}
    for cid, (slug, found) in own.items():
        for k, pool, t0, t1 in by_chain[cid]:
            p = found.get(pool)
            vol = _num(((p or {}).get("volume") or {}).get("h24"))
            if p and max(vol, _num(((p or {}).get("liquidity") or {}).get("usd"))) < LOOKUP_MIN_USD:
                continue
            if not p and not _both_majors(state, k):
                continue
            for t in (t0, t1):
                want[(cid, t)] = want.get((cid, t), 0.0) + vol + 1.0
    lookups, stopped, looked = 0, "", set()
    deadline = time.time() + LOOKUP_BUDGET_S
    for (cid, tok), _w in sorted(want.items(), key=lambda kv: -kv[1]):
        if time.time() > deadline:
            stopped = "time budget reached"
            break
        try:
            res = _get(f"{DS}/token-pairs/v1/{own[cid][0]}/{tok}")
        except Limited:
            stopped = "rate limited"
            break
        except Exception as e:
            log(f"dexscreener token pairs ({C.chain_short(cid)} {tok[:10]}): {str(e)[:100]}")
            res = None
        lookups += 1
        if isinstance(res, list):
            looked.add((cid, tok))
            for p in res:
                markets[cid].add_ds(p)
        time.sleep(LOOKUP_PACE_S)

    # the biggest pools of the chain from GeckoTerminal, where DexScreener did not already give them
    gt_used = []
    for cid, market in markets.items():
        g = mk["gt"].get(str(cid)) or {}
        if g.get("pools") and now - g.get("at", 0) <= GT_MAX_AGE:
            gt_used.append(C.chain_short(cid))
            for addr, b, q, bsym, qsym, vol, liq, _dex in g["pools"]:
                market.add(addr, b, bsym, q, qsym, vol, liq, replace=False)

    pools, aero_vol, top, seen = {}, {}, {}, 0
    prev = mk.get("pools") or {}
    for cid, (slug, found) in own.items():
        market = markets[cid]
        for p in found.values():
            market.add_ds(p)        # the Aero pools themselves, with their own figures
        pairs = market.pairs()
        seen += len(market.pools)
        aero_addrs = set(found)
        for k, pool, t0, t1 in by_chain[cid]:
            p = found.get(pool)
            if not p:
                continue
            vol = p.get("volume") or {}
            pm = pairs.get(tuple(sorted((t0, t1))))
            pools[k] = [round(_num(vol.get("h1"))), round(_num(vol.get("h6"))), round(_num(vol.get("h24"))),
                        round(_num((p.get("liquidity") or {}).get("usd"))),
                        round(pm[0]) if pm else None, round(pm[1]) if pm else None, pm[2] if pm else None]
            old = prev.get(k)
            if ((cid, t0) in want or (cid, t1) in want) and (cid, t0) not in looked and (cid, t1) not in looked \
                    and old and len(old) > 6 and old[4]:
                # lookups cut short this hour: the pair keeps last hour's figures rather than a sudden drop
                pools[k][4:7] = [max(old[4], pools[k][2]), old[5], old[6]]
            aero_vol[cid] = aero_vol.get(cid, 0.0) + _num(vol.get("h24"))
        # the chain's biggest pairs on every DEX, with Aero's part of each
        rows = []
        for pair, (v, liq, n, addrs) in sorted(pairs.items(), key=lambda kv: -kv[1][0])[:TOP_PAIRS]:
            av = sum(market.pools[a][1] for a in addrs if a in aero_addrs)
            gauges = sum(1 for r in by_chain[cid] if tuple(sorted((r[2], r[3]))) == pair)
            syms = [_sym(state, cid, t) or market.syms.get(t) or t[:8] for t in pair]
            rows.append([f"{syms[0]}/{syms[1]}", round(v), round(liq), round(av), gauges])
        top[str(cid)] = rows

    dex = chain_volumes(list(by_chain), log)
    chains = {}
    for cid in by_chain:
        d = dex.get(cid)
        chains[str(cid)] = [round(aero_vol.get(cid, 0.0)), round(d[0]) if d else None,
                            round(d[1]) if d else None, round(d[2]) if d else None]
    mk["pools"], mk["chains"], mk["top"], mk["at"] = pools, chains, top, now
    record(mk, now)
    shares = []
    for c, v in sorted(chains.items(), key=lambda kv: -(kv[1][1] or 0)):
        s = _share(v)
        shares.append(f"{C.chain_short(int(c))} {s * 100:.1f}%" if s is not None else f"{C.chain_short(int(c))} n/d")
    log(f"market: {len(pools)} Aero pools; {seen} pools on every DEX from {lookups} token lookups"
        f"{' (' + stopped + ')' if stopped else ''} and GeckoTerminal lists of {', '.join(gt_used) or 'no chain yet'}; "
        f"Aero share of DEX volume (DefiLlama): {', '.join(shares)}; {time.time() - t_start:.0f}s")
    return True


def _sym(state, cid, t):
    return (state["erc20"].get(A.tkey(cid, t)) or {}).get("sym")


# ---------------------------------------------------------------- history

def record(mk, now):
    hour = str(now - now % HOUR)
    hp, hc = mk["hist"]["pools"], mk["hist"]["chains"]
    for k, v in mk["pools"].items():
        hp.setdefault(k, {})[hour] = [v[0], v[2], v[3], v[4]]       # volume 1h, volume 24h, liquidity, pair volume
    for c, v in mk["chains"].items():
        hc.setdefault(c, {})[hour] = v                              # see CHAIN_FIELDS
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


def _share(v):
    """Aero's share of a chain's DEX volume, both from DefiLlama."""
    if not v or len(v) < 3 or not v[1] or v[2] is None:
        return None
    return v[2] / v[1]


def chain_share(mk, cid, ago=0, now=None):
    """Aero's share of the chain's DEX volume now, or about `ago` seconds earlier from the history."""
    if not ago:
        return _share((mk.get("chains") or {}).get(str(cid)))
    series = (mk.get("hist") or {}).get("chains", {}).get(str(cid)) or {}
    target = (now or mk.get("at") or 0) - ago
    best = min(series.items(), key=lambda kv: abs(int(kv[0]) - target), default=None)
    if not best or abs(int(best[0]) - target) > 3 * HOUR:
        daily = (mk.get("daily") or {}).get("chains", {}).get(str(cid)) or {}
        best = min(daily.items(), key=lambda kv: abs(int(kv[0]) - target), default=None)
        if not best or abs(int(best[0]) - target) > DAY:
            return None
    return _share(best[1])


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
    day, with every Aero pool of the pair; pools DexScreener does not list yet count when both tokens are blue chips.
    """
    r = {**NEW_CHAIN_RULES, **(rules or {})}
    mk = market_state(state)
    home = {int(c) for c in r["home_chains"]}
    ok = set()
    for cid in {A.chain_of(k) for k in state["gauges"]} - home:
        share = chain_share(mk, cid)
        if share is None or share < float(r["max_chain_share"]):
            ok.add(cid)
    pairs = {}
    for k, v in (mk.get("pools") or {}).items():
        cid = A.chain_of(k)
        if cid not in ok or not v[4] or v[4] < float(r["min_pair_volume_usd"]):
            continue
        m = state["gauges"].get(k) or {}
        pair = (cid, tuple(sorted(((m.get("t0") or "").lower(), (m.get("t1") or "").lower()))))
        e = pairs.setdefault(pair, [0.0, []])
        e[0] = max(e[0], v[4])
        e[1].append(k)
    by_chain = {}
    for (cid, _pair), (vol, keys) in pairs.items():
        by_chain.setdefault(cid, []).append((vol, keys))
    out = set()
    for lst in by_chain.values():
        for _vol, keys in sorted(lst, key=lambda x: -x[0])[: int(r["pairs_per_chain"])]:
            out |= set(keys)
    covered = set(mk.get("pools") or {})
    out |= {k for k in state["gauges"] if A.chain_of(k) in ok and k not in covered and _both_majors(state, k)}
    return out, ok
