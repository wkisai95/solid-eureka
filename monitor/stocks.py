"""Tokenized stocks on Base and Robinhood Chain, hour by hour.

Which tokens: the stocks and funds among the tokens of Aero's pools on the two chains. On Base, the stock tokens
that end in "c" (AAPLc, NVDAc...: 8 decimals, addresses that start with 0xb2 and twenty zeros, a prefix other tokens
of the same factory share) and WisdomTree's funds (wt...); on Robinhood Chain the tokens whose symbol is a US ticker
(TICKERS). config.json "stocks": {"add": [keys], "skip": [keys]} changes the list.

Once an hour, for each token:
- its trading in the last hour on every DEX of the chain, from DexScreener's list of its pools: volume, buys and
  sells, price, liquidity, and the 24-hour volume;
- its supply on chain (totalSupply): what is minted or burned from one hour to the next is stock brought on chain
  or taken off it.
The history is hourly for 7 days; export() gives the page the last 72 hours and the change from hour to hour.
"""
import re
import time

from . import chain as C
from .marketdata import DS, LOOKUP_PACE_S, Limited, _get, _num

HOUR = 3600
CHAINS = {8453: "base", 4663: "robinhood"}
GAP = 40 * 60               # one reading per clock hour, at least this long after the previous one
LATE = 80 * 60              # after this long it reads even in a run that already read the other markets
KEEP_H = 7 * 24
EXPORT_H = 72               # hours of each chain's totals in the export
WINDOWS = (1, 2, 6, 12)     # hours the page can show the trading of
COINBASE_PREFIX = "0xb2" + "0" * 20
SEL_SUPPLY = C.sel("totalSupply()")

# US stocks and ETFs, as Robinhood Chain names its tokens ("U" is left out: on Robinhood Chain it is a stablecoin)
TICKERS = set("""
AAPL ABBV ABNB ACN ADBE ADP AMAT AMC AMD AMGN AMZN ANET APP ARKK ARM ASML AVGO AXP BA BABA BAC BBAI BIDU BLK BMY
BRK.B BRKB BX C CAT CCJ CEG CHWY CMCSA CMG COIN COP COST CRCL CRM CRWD CSCO CVNA CVS CVX DAL DDOG DE DELL DIA DIS
DJT DKNG DOCU DUOL EEM EFA ETHA F FCX FDX FIG GD GE GEV GILD GLD GLXY GM GME GOOG GOOGL GS HD HIMS HON HOOD HYG IBIT
IBM INTC INTU IONQ ISRG IVV IWM JD JNJ JPM KO KRE LCID LIN LLY LMT LOW LRCX LULU MA MARA MCD MDT MELI META MMM MO
MRK MRVL MS MSFT MSTR MU NBIS NET NFLX NIO NKE NOW NU NVDA NVO OKLO ON ORCL OXY PANW PDD PEP PFE PG PINS PLTR PM
PYPL QCOM QQQ QS QUBT RBLX RDDT RGTI RIOT RIVN RKLB ROKU SBUX SCHW SGOV SHOP SLV SMCI SMR SNAP SNOW SOFI SONY SOXL
SOXX SPCX SPOT SPY SQ T TGT TLT TQQQ TSLA TSM TTD TXN UBER UNH UNP UPS UPST USO V VOO VTI VZ WFC WMT XLE XLF XOM
XYZ ZM
""".split())


def _is_stock(cid, addr, sym, dec):
    sym = sym or ""
    if cid == 8453:
        stock = (addr.startswith(COINBASE_PREFIX) and dec == 8 and sym.endswith("c") and not sym.startswith("cb")
                 and sym[:-1] == sym[:-1].upper())
        return stock or bool(re.fullmatch(r"wt[A-Z]{2,6}", sym))
    if cid == 4663:
        return sym in TICKERS
    return False


def universe(state, cfg):
    """{key: {"sym", "dec"}} of the stock tokens of the two chains."""
    opt = cfg.get("stocks") or {}
    add, skip = {k.lower() for k in opt.get("add") or []}, {k.lower() for k in opt.get("skip") or []}
    out = {}
    for k, m in (state.get("erc20") or {}).items():
        cid, _, addr = k.partition(":")
        if not cid.isdigit() or int(cid) not in CHAINS or k in skip:
            continue
        sym = m.get("sym") or ""
        if k in add or _is_stock(int(cid), addr, sym, int(m.get("dec") or 18)):
            out[k] = {"sym": sym, "dec": int(m.get("dec") or 18)}
    return out


def book(state):
    st = state.get("stocks")
    if not isinstance(st, dict) or "h" not in st:
        st = state["stocks"] = {"at": 0, "tok": {}, "h": {}, "t": {}}
    return st


def _pools(slug, addr, log):
    """Every pool of the token on the chain, from DexScreener."""
    try:
        res = _get(f"{DS}/token-pairs/v1/{slug}/{addr}")
    except Limited:
        raise
    except Exception as e:
        log(f"stocks: dexscreener {slug} {addr[:10]}: {str(e)[:100]}")
        return None
    return res if isinstance(res, list) else []


def _trading(addr, pairs):
    """[volume of the last hour, buys, sells, price, liquidity, volume of the last 24 hours, price change in the last
    hour (%)] of one token over its pools; buys and sells are of the token, whichever side of the pair it is."""
    vol = v24 = liq = 0.0
    buys = sells = 0
    best, px, chg = -1.0, None, None
    for p in pairs:
        b = ((p.get("baseToken") or {}).get("address") or "").lower()
        q = ((p.get("quoteToken") or {}).get("address") or "").lower()
        if addr not in (b, q):
            continue
        tx = ((p.get("txns") or {}).get("h1") or {})
        vol += _num((p.get("volume") or {}).get("h1"))
        v24 += _num((p.get("volume") or {}).get("h24"))
        lq = _num((p.get("liquidity") or {}).get("usd"))
        liq += lq
        if addr == b:
            buys += int(_num(tx.get("buys")))
            sells += int(_num(tx.get("sells")))
            price = _num(p.get("priceUsd"))
            change = (p.get("priceChange") or {}).get("h1")
        else:
            buys += int(_num(tx.get("sells")))
            sells += int(_num(tx.get("buys")))
            native = _num(p.get("priceNative"))
            price = _num(p.get("priceUsd")) / native if native > 0 else 0.0
            change = None
        if price > 0 and lq > best:            # the price of the pool with the most liquidity
            best, px, chg = lq, price, (round(_num(change), 2) if change is not None else None)
    return [round(vol, 2), buys, sells, float(f"{px:.6g}") if px else None, round(liq), round(v24), chg]


def _supply(rpc, toks):
    """{key: supply in tokens} from totalSupply on chain."""
    keys = list(toks)
    res = rpc.multicall([(k.split(":")[1], SEL_SUPPLY) for k in keys], batch=100)
    out = {}
    for k, (ok, data) in zip(keys, res):
        v = C._dec(["uint256"], ok, data)
        if v is not None:
            out[k] = float(f"{v / 10 ** toks[k]['dec']:.8g}")
    return out


def refresh(state, cfg, rpc_for, now, log, markets_read=False):
    """One reading per clock hour (a fixed interval drifted across the runs, leaving some hours with two readings and
    others with none); markets_read: this run already read the other markets from DexScreener, so the reading waits
    for the next run unless the last one is getting late. Returns True when it read."""
    st = book(state)
    age = now - int(st.get("at") or 0)
    if str(now - now % HOUR) in st["h"] or age < GAP or (markets_read and age < LATE):
        return False
    t0 = time.time()
    toks = universe(state, cfg)
    st["tok"] = toks
    hour = str(now - now % HOUR)
    row, read, failed = {}, 0, 0
    for cid, slug in CHAINS.items():
        mine = {k: m for k, m in toks.items() if k.startswith(f"{cid}:")}
        if not mine:
            continue
        try:
            sup = _supply(rpc_for(cid), mine)
        except Exception as e:
            log(f"stocks: supply on {C.chain_short(cid)} not read: {str(e)[:120]}")
            sup = {}
        last = 0.0
        for k in sorted(mine):
            wait = LOOKUP_PACE_S - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()
            try:
                pairs = _pools(slug, k.split(":")[1], log)
            except Limited:
                log("stocks: dexscreener rate limit, the rest next hour")
                pairs = None
                failed += 1
                break
            if pairs is None:
                failed += 1
                continue
            read += 1
            row[k] = _trading(k.split(":")[1], pairs) + [sup.get(k)]
    if row:
        st["h"][hour] = row
        st["t"][hour] = now
        st["at"] = now
    cut = now - KEEP_H * HOUR
    st["h"] = {h: v for h, v in st["h"].items() if int(h) >= cut}
    st["t"] = {h: v for h, v in st["t"].items() if int(h) >= cut}
    by = {C.chain_short(c): sum(1 for k in row if k.startswith(f"{c}:")) for c in CHAINS}
    log(f"stocks: {len(toks)} tokens, {read} read {by}, {failed} not read, {time.time() - t0:.1f}s")
    return bool(row)


def _span(xs):
    """Total of an hourly series over a span; hours without a reading count at the average of the others."""
    have = [x for x in xs if x is not None]
    return float(sum(have)) * len(xs) / len(have) if have else None


def export(state, now):
    """The page's stocks.json: the last 72 hours of every stock token and of each chain.

    {"generated", "hours": [hour starts, oldest first], "read": {hour: time of the reading}, "end": index in hours
     of the last reading, "windows": [1, 2, 6, 12],
     "chains": {cid: {"vol": [72], "tx": [72], "mint": [USD minted - burned in the hour], "n": tokens}},
     "tokens": [{"k", "sym", "c", "vol": [the last 24 hours], "px", "liq", "v24", "chg", "buys", "sells",
                 "w": {hours: [volume, volume of the hours before, volume of the same hours yesterday, operations,
                               operations of the hours before, USD of supply brought on chain (minus taken off),
                               price change %]}}]}
    The token's px, liq, v24, chg, buys and sells are those of its last reading; the windows end at the last reading."""
    st = book(state)
    h0 = now - now % HOUR
    hours = [h0 - i * HOUR for i in range(EXPORT_H - 1, -1, -1)]
    rows = [st["h"].get(str(h)) or {} for h in hours]
    end = max((i for i, x in enumerate(rows) if x), default=len(rows) - 1)
    chains = {str(c): {"vol": [], "tx": [], "mint": [], "n": 0} for c in CHAINS}
    tokens = []
    for k, m in sorted((st.get("tok") or {}).items()):
        cid = k.split(":")[0]
        r = [x.get(k) for x in rows]
        last = next((x for x in reversed(r) if x), None)
        if not last:
            continue
        chains[cid]["n"] += 1
        vol = [x[0] if x else None for x in r]
        tx = [(x[1] + x[2]) if x else None for x in r]
        px = [x[3] if x else None for x in r]
        sup = [x[7] if x and len(x) > 7 else None for x in r]
        win = {}
        for n in WINDOWS:
            lo = end - n + 1
            if lo - n < 0:
                continue
            cur, before = _span(vol[lo:end + 1]), _span(vol[lo - n:lo])
            yday = _span(vol[lo - 24:end - 23]) if lo - 24 >= 0 else None
            t_cur, t_before = _span(tx[lo:end + 1]), _span(tx[lo - n:lo])
            # the reading just before the span, or the closest one up to two hours earlier when it is missing
            before_i = next((j for j in (lo - 1, lo - 2, lo - 3) if j >= 0 and r[j]), None)
            s_end, p_end = sup[end], px[end]
            s_start, p_start = (sup[before_i], px[before_i]) if before_i is not None else (None, None)
            supply = (s_end - s_start) * p_end if None not in (s_end, s_start, p_end) else None
            change = (p_end / p_start - 1) * 100 if p_end and p_start else (last[6] if n == 1 else None)
            win[str(n)] = [None if v is None else round(v) for v in (cur, before, yday, t_cur, t_before, supply)] + \
                          [None if change is None else round(change, 2)]
        tokens.append({"k": k, "sym": m["sym"], "c": int(cid),
                       "vol": [None if v is None else round(v) for v in vol[-24:]],
                       "px": last[3], "liq": last[4], "v24": last[5], "chg": last[6], "buys": last[1], "sells": last[2],
                       "w": win})
    listed = st.get("tok") or {}
    for i, x in enumerate(rows):
        prev = rows[i - 1] if i else {}
        for cid, ch in chains.items():
            # only the tokens still on the list: one dropped from it leaves its past readings out of the totals too
            ks = [k for k in x if k.startswith(f"{cid}:") and k in listed]
            ch["vol"].append(round(sum(x[k][0] for k in ks)) if ks else None)
            ch["tx"].append(sum(x[k][1] + x[k][2] for k in ks) if ks else None)
            mint = None
            for k in ks:
                a, b = (prev.get(k) or [None] * 8), x[k]
                if len(a) > 7 and len(b) > 7 and a[7] is not None and b[7] is not None and b[3]:
                    mint = (mint or 0.0) + (b[7] - a[7]) * b[3]
            ch["mint"].append(round(mint) if mint is not None else None)
    return {"generated": now, "hours": hours, "end": end, "windows": list(WINDOWS),
            "read": {h: t for h, t in st.get("t", {}).items() if int(h) >= hours[0]},
            "chains": chains, "tokens": tokens}
