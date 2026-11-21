"""Probe: every allocation event of the LeafVoter on every Aero chain since deployment, as a compact log.

Output (stdout, JSON): {chain: {"start", "head", "secs", "windows", "g": [gauge addresses],
  "ev": [[ts, block, logIndex, kind, token, payload]]}}  kind: a = GaugesAllocated (payload [[gauge index, votes]]),
  d = Deallocated (payload amount), e = EmergencyDeallocationApplied, r = CooldownReductionApplied (accumulated).
"""
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from eth_abi import decode

from monitor import chain as C
from monitor.rpc import Rpc

cfg = json.load(open("config.json"))
TOP = {C.T["leaf_alloc"]: "a", C.T["leaf_dealloc"]: "d", C.T["leaf_emergency"]: "e", C.T["leaf_reduction"]: "r"}
T_END = time.time() + 12 * 60


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def one(cid):
    ccfg = cfg["chains"][str(cid)]
    rpc = Rpc(ccfg["rpc"], name=str(cid), log=err, pace=float(ccfg.get("pace_s", 0)))
    t0 = time.time()
    head = rpc.block_number() - 50
    head_ts = rpc.block_ts(head)
    base = cid == C.BASE_CHAIN
    if ccfg.get("deploy_block"):
        start = int(ccfg["deploy_block"])
    else:
        b2 = head - 20000
        bt = (head_ts - rpc.block_ts(b2)) / 20000
        start = rpc.block_at_ts(1792160000, head, head_ts, bt)
    win = 100_000 if base else 1_000_000
    gi, g_of, ev, n_win = [], {}, [], 0
    lo = start
    while lo <= head and time.time() < T_END:
        hi = min(head, lo + win - 1)
        t_lo = C.block_ts(lo) if base else rpc.block_ts(lo)
        t_hi = C.block_ts(hi) if base else rpc.block_ts(hi)

        def ts_of(b, lo=lo, hi=hi, t_lo=t_lo, t_hi=t_hi):
            if base:
                return C.block_ts(b)
            return int(t_lo + (b - lo) * (t_hi - t_lo) / max(1, hi - lo))
        for lg in rpc.get_logs(lo, hi, address=C.LEAF, topics=[list(TOP)], chunk=win, max_chunk=win):
            k = TOP.get(lg["topics"][0].lower())
            if not k:
                continue
            blk = C.hexint(lg["blockNumber"])
            row = [ts_of(blk), blk, C.hexint(lg.get("logIndex")), k, int(lg["topics"][1], 16)]
            data = bytes.fromhex(lg["data"][2:]) if lg.get("data") and lg["data"] != "0x" else b""
            if k == "a":
                allocs, epv = decode(["(address,uint128,bytes)[]", "uint256"], data)
                pay = []
                for g, a, _ in allocs:
                    g = g.lower()
                    if a <= 0 or g == C.ZERO:
                        continue
                    if g not in g_of:
                        g_of[g] = len(gi)
                        gi.append(g)
                    pay.append([g_of[g], round(a / 1e18, 3)])
                row.append(pay)
            elif k == "d":
                (amt,) = decode(["uint128"], data)
                row.append(round(amt / 1e18, 3))
            elif k == "r":
                red, acc = decode(["uint48", "uint48"], data)
                row.append(int(acc))
            else:
                row.append(None)
            ev.append(row)
        n_win += 1
        lo = hi + 1
    return {"start": start, "head": head, "head_ts": head_ts, "read_to": lo - 1, "secs": round(time.time() - t0, 1),
            "windows": n_win, "g": gi, "ev": ev}


def safe(cid):
    try:
        return one(cid)
    except Exception:
        return {"error": traceback.format_exc()[-1500:]}


ids = [int(c) for c in cfg["chains"]]
with ThreadPoolExecutor(max_workers=len(ids)) as ex:
    res = dict(zip(ids, ex.map(safe, ids)))
for cid, r in res.items():
    err(cid, {k: v for k, v in r.items() if k not in ("ev", "g")}, len(r.get("ev") or []))
json.dump({str(k): v for k, v in res.items()}, sys.stdout, separators=(",", ":"))
