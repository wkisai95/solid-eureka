"""The Autopilot and the biggest sAERO positions.

Autopilot options are Relay contracts. A RelayFactory creates each one with its own sAERO position, which pools the
deposits and allocates them as a single allocator: the leaf voters show the relay's position number, never the
depositors. Each relay has a name in its configuration ("Autopilot USDC Maxi", "Autopilot Internal 004", ...).
The protocol-run ones ("Autopilot Internal") hold the protocol's own stakes: the Aerodrome Foundation deposited its
positions into one of them, the Public Goods Fund into another (see LABELS in config.json).

Every run this module
- reads the Allocation Power of the next slice of all sAERO positions (all of them every few hours) and keeps the
  biggest ones; positions are numbered in sequence, so the slices go by number up to a little past the highest number
  the leaf voters have seen,
- refreshes the biggest positions and every relay it knows: Allocation Power, staked AERO, owner, and whether the
  owner is a relay (relayType / relayConfig answer only on a relay),
- compares the allocation of the positions it follows with the previous run and records the moves: votes taken out
  of and put into each pool, and what that does to the yield per vote of the pools left behind.
"""
from eth_abi import decode, encode

from . import aero as A
from . import chain as C

HOUR = 3600
ESCROW = "0xbf7977e1521e9a3032bf7243ee4712f311f13915"     # sAERO (VotingEscrow) on Base
SEL = {k: C.sel(v) for k, v in {
    "balanceOfNFT": "balanceOfNFT(uint256)",
    "staked": "staked(uint256)",
    "ownerOf": "ownerOf(uint256)",
    "relayType": "relayType()",
    "relayConfig": "relayConfig()",
}.items()}
RELAY_TYPES = {0: "Maxi", 1: "Protocol", 2: "Protocol"}
# relayConfig() of the first relays (October) and of the newer ones (no minWithdrawal); the strings start right after
# the fixed fields, so the offset of the first one tells the two apart
RELAY_CONFIGS = {9: "(uint256,uint256,uint256,uint256,uint256,uint48,uint48,string,string)",
                 8: "(uint256,uint256,uint256,uint256,uint48,uint48,string,string)"}
OWNER_CHECK_V = 2          # owners checked with an older version of the relay check are checked again
SCAN_PER_RUN = 1500        # positions whose Allocation Power is read per run
ID_MARGIN = 300            # numbers past the highest one seen, for new positions that have not allocated yet
KEEP_MIN_AP = 50_000       # smaller positions are not kept from the scan
CANDIDATES = 40            # biggest by Allocation Power and by allocation, refreshed every run
TOP_N = 10
OWNER_TTL = 24 * HOUR      # how long an owner's relay check is trusted
MOVE_MIN_ABS = 50_000      # a move is recorded when at least this many votes changed pool...
MOVE_MIN_SHARE = 0.01      # ...and at least 1% of the position
MERGE_WINDOW = 2 * HOUR    # parts of one reallocation reach the other chains minutes apart
MOVES_KEEP = 40


def book(state):
    b = state.get("big")
    if not isinstance(b, dict):
        b = state["big"] = {}
    for k, v in (("scan", {"i": 0, "n": 0, "full_at": 0}), ("ap", {}), ("info", {}), ("owners", {}),
                 ("seen", {}), ("moves", []), ("new", [])):
        b.setdefault(k, v)
    return b


def _u(tid):
    return encode(["uint256"], [int(tid)])


def _max_id(state, b):
    m = max((int(t) for t in b["ap"]), default=0)
    for cs in state["chains"].values():
        m = max([m] + [int(t) for t in (cs.get("tokens") or {})])
    return m


def _scan(rpc, state, b, now, log):
    """Allocation Power of the next slice of all positions, by number."""
    sc = b["scan"]
    if not sc.get("i") or not sc.get("n"):
        sc.update({"i": 1, "n": _max_id(state, b) + ID_MARGIN, "cnt": 0})
    lo, hi = sc["i"], min(sc["n"], sc["i"] + SCAN_PER_RUN - 1)
    ids = list(range(lo, hi + 1))
    aps = rpc.multicall([(ESCROW, SEL["balanceOfNFT"] + _u(t)) for t in ids], batch=150)
    kept = live = 0
    for t, r in zip(ids, aps):
        v = C._dec(["uint256"], *r)
        if v is None:
            continue                  # not readable now (or no such position): left as it was
        ap = v / 1e18
        live += ap > 0
        if ap >= KEEP_MIN_AP:
            b["ap"][str(t)] = round(ap, 2)
            kept += 1
        else:
            b["ap"].pop(str(t), None)
    sc["cnt"] = sc.get("cnt", 0) + live
    if hi >= sc["n"]:
        sc.update({"i": 0, "full_at": now, "live": sc["cnt"]})     # the next run starts over with a new last number
    else:
        sc["i"] = hi + 1
    log(f"big: positions {lo}-{hi} of {sc['n']} read, {live} with votes, {kept} above {KEEP_MIN_AP:,}, "
        f"{len(b['ap'])} kept")


def _relay_config(ok, data):
    if not ok or not data or len(data) < 64:
        return None
    try:
        at = int.from_bytes(data[:32], "big")                      # where the struct starts
        word = lambda k: int.from_bytes(data[at + 32 * k:at + 32 * k + 32], "big")
        for n, layout in RELAY_CONFIGS.items():
            if len(data) >= at + 32 * n and word(n - 2) == 32 * n:  # offset of the name = size of the fixed part
                c = decode([layout], data)[0]
                name = "".join(ch for ch in c[n - 2] if ch.isprintable()).strip()[:60]
                return {"token": int(c[0]), "name": name}
    except Exception:
        pass
    return None


def allocation_of(state, tid):
    """{gauge: votes} of one position on every chain."""
    out = {}
    for cs in state["chains"].values():
        t = (cs.get("tokens") or {}).get(str(tid))
        for g, a in ((t or {}).get("a") or {}).items():
            if a > 0:
                out[g] = out.get(g, 0.0) + a
    return out


def _allocated(state):
    out = {}
    for cs in state["chains"].values():
        for tid, t in (cs.get("tokens") or {}).items():
            s = sum(t["a"].values())
            if s > 0:
                out[tid] = out.get(tid, 0.0) + s
    return out


def _refresh(rpc, state, b, cfg, now, log):
    alloc = _allocated(state)
    by_ap = [t for t, _ in sorted(b["ap"].items(), key=lambda kv: -kv[1])[:CANDIDATES]]
    by_alloc = [t for t, _ in sorted(alloc.items(), key=lambda kv: -kv[1])[:CANDIDATES]]
    relays = [t for t, i in b["info"].items() if i.get("relay")]
    cand = sorted(set(by_ap) | set(by_alloc) | set(relays), key=int)
    calls = []
    for t in cand:
        calls += [(ESCROW, SEL["balanceOfNFT"] + _u(t)), (ESCROW, SEL["staked"] + _u(t)), (ESCROW, SEL["ownerOf"] + _u(t))]
    res = rpc.multicall(calls, batch=150)
    for k, t in enumerate(cand):
        ap = C._dec(["uint256"], *res[3 * k])
        st = C._dec(["uint128", "uint48", "bool"], *res[3 * k + 1])
        owner = C._dec_addr(*res[3 * k + 2])
        if owner is None and ap is None:
            b["info"].pop(t, None)        # burned (merged, withdrawn) or not readable now
            b["ap"].pop(t, None)
            continue
        i = b["info"].setdefault(t, {})
        if ap is not None:
            i["ap"] = round(ap / 1e18, 2)
            if i["ap"] >= KEEP_MIN_AP:
                b["ap"][t] = i["ap"]
        if st is not None:
            i.update({"amount": round(st[0] / 1e18, 2), "end": int(st[1]), "perm": bool(st[2])})
        if owner:
            i["owner"] = owner
        i["at"] = now
    # which owners are relays: relayType() and relayConfig() answer only on a relay
    owners = sorted({i["owner"] for i in b["info"].values() if i.get("owner")})
    todo = [o for o in owners if now - (b["owners"].get(o) or {}).get("at", 0) > OWNER_TTL
            or (b["owners"].get(o) or {}).get("v") != OWNER_CHECK_V]
    if todo:
        res = rpc.multicall([(o, SEL["relayType"]) for o in todo] + [(o, SEL["relayConfig"]) for o in todo])
        for j, o in enumerate(todo):
            rt = C._dec(["uint8"], *res[j])
            rc = _relay_config(*res[len(todo) + j])
            rec = {"at": now, "v": OWNER_CHECK_V}
            if rt is not None and rc is not None:
                rec.update({"relay": True, "type": RELAY_TYPES.get(int(rt), str(rt)), "name": rc["name"],
                            "token": rc["token"]})
            b["owners"][o] = rec
    labels = {k.lower(): v for k, v in (cfg.get("labels") or {}).items()}
    for t, i in b["info"].items():
        o = b["owners"].get(i.get("owner") or "") or {}
        i["relay"] = bool(o.get("relay")) and o.get("token") == int(t)
        i["name"] = o.get("name") if i["relay"] else None
        i["type"] = o.get("type") if i["relay"] else None
        i["label"] = labels.get(i.get("owner") or "")
    # positions that are neither big nor relays any more
    keep = set(cand) | {t for t, i in b["info"].items() if i.get("relay")}
    for t in [t for t in b["info"] if t not in keep]:
        del b["info"][t]
    n_rel = sum(1 for i in b["info"].values() if i.get("relay"))
    log(f"big: {len(cand)} positions refreshed, {n_rel} Autopilot relays, {len(todo)} owners checked")


def followed(b):
    """Position numbers whose moves are recorded: every relay and the top positions by Allocation Power."""
    top = [t for t, _ in sorted(((t, i.get("ap") or 0) for t, i in b["info"].items()), key=lambda kv: -kv[1])[:TOP_N]]
    return sorted(set(top) | {t for t, i in b["info"].items() if i.get("relay")}, key=int)


def _moves(state, b, now, log):
    """Compares the allocation of the followed positions with the last run; returns the new move records."""
    new = []
    follow = followed(b)
    seen = {}
    for t in follow:
        cur = allocation_of(state, t)
        prev = b["seen"].get(t)
        seen[t] = {g: round(a, 2) for g, a in cur.items()}
        if prev is None:
            continue
        delta = {g: cur.get(g, 0.0) - prev.get(g, 0.0) for g in set(cur) | set(prev)}
        delta = {g: d for g, d in delta.items() if abs(d) >= 1}
        put = sum(d for d in delta.values() if d > 0)
        took = -sum(d for d in delta.values() if d < 0)
        size = max(sum(cur.values()), sum(prev.values()), 1.0)
        if max(put, took) < max(MOVE_MIN_ABS, MOVE_MIN_SHARE * size):
            continue
        i = b["info"].get(t) or {}
        last = next((m for m in reversed(b["moves"]) if m["id"] == t), None)
        if last and now - last.get("at", last["t"]) <= MERGE_WINDOW:
            # another part of the same reallocation, from a chain read later: merge it
            for g, d in delta.items():
                last["d"][g] = round(last["d"].get(g, 0.0) + d, 2)
            last["t_last"] = now
            continue
        # when it happened: the position's last allocation on chain, if it is from the last few hours
        on_chain = max((int(((cs.get("tokens") or {}).get(t) or {}).get("last") or 0) for cs in state["chains"].values()),
                       default=0)
        rec = {"t": on_chain if 0 < now - on_chain <= 6 * HOUR else now, "at": now, "id": t,
               "d": {g: round(d, 2) for g, d in delta.items()},
               "relay": bool(i.get("relay")), "name": i.get("name"), "label": i.get("label")}
        b["moves"].append(rec)
        new.append(t)
    b["seen"] = seen
    b["moves"] = b["moves"][-MOVES_KEEP:]
    b["new"] = new
    if new:
        log(f"big: moves of {', '.join('#' + t for t in new)}")
    return new


def refresh(rpc, state, cfg, now, log):
    """One run: scan, refresh and moves. Returns the position numbers that moved in this run."""
    b = book(state)
    _scan(rpc, state, b, now, log)
    _refresh(rpc, state, b, cfg, now, log)
    return _moves(state, b, now, log)


# ---------------------------------------------------------------- report

def _who(i):
    """How a position is called on the page: label, relay name, or nothing."""
    if i.get("relay"):
        return i.get("label") or i.get("name") or "Autopilot"
    return i.get("label")


def _name(state, g):
    """Pool name with its kind (CL100, sAMM, ...): the same pair often has several pools."""
    kind = ((state["gauges"].get(g) or {}).get("kind") or "").strip()
    name = A.gauge_name(state, g)
    return f"{name} {kind}" if kind and kind != "?" else name


def _pools(state, alloc, limit=None):
    ap = state["ap"]
    rows = []
    for g, a in sorted(alloc.items(), key=lambda kv: -kv[1])[:limit]:
        tot = float(ap.get(g, 0.0))
        rows.append([g, _name(state, g), C.chain_short(A.chain_of(g)), round(a), round(tot),
                     round(a / tot, 4) if tot > 0 else None])
    return rows


def _position(state, t, i, cooldown_of, cooldown_end):
    alloc = allocation_of(state, t)
    ends, last, chains = [], 0, []
    for cid, cs in state["chains"].items():
        tok = (cs.get("tokens") or {}).get(t)
        if tok and sum(tok["a"].values()) > 0:
            chains.append(int(cid))
            ends.append(cooldown_end(tok, cooldown_of(cid)))
            last = max(last, int(tok.get("last") or 0))
    return {"id": int(t), "ap": round(i.get("ap") or 0), "amount": round(i.get("amount") or 0),
            "perm": bool(i.get("perm")), "end": i.get("end") or 0, "owner": i.get("owner"),
            "relay": bool(i.get("relay")), "type": i.get("type"), "name": i.get("name"), "label": i.get("label"),
            "who": _who(i), "alloc": round(sum(alloc.values())), "last": last,
            "ends": min(ends) if ends else 0, "ends_max": max(ends) if ends else 0, "chains": chains,
            "pools": _pools(state, alloc)}


def report(state, now, cooldown_of, cooldown_end):
    b = state.get("big")
    if not isinstance(b, dict) or not b.get("info"):
        return None
    total = round(sum(state["ap"].values()))
    info = b["info"]
    relays = sorted((t for t, i in info.items() if i.get("relay")), key=lambda t: -(info[t].get("ap") or 0))
    top = [t for t, _ in sorted(((t, i.get("ap") or 0) for t, i in info.items()), key=lambda kv: -kv[1])[:TOP_N]]
    ap = state["ap"]
    moves = []
    for m in reversed(b.get("moves") or []):
        rows = []
        for g, d in sorted(m["d"].items(), key=lambda kv: kv[1]):
            after = float(ap.get(g, 0.0))
            rows.append([g, _name(state, g), C.chain_short(A.chain_of(g)), round(d), round(after)])
        moves.append({"t": m["t"], "t_last": m.get("t_last"), "id": int(m["id"]), "relay": m.get("relay"),
                      "who": m.get("label") or m.get("name") or (("Autopilot" if m.get("relay") else None)),
                      "name": m.get("name"), "new": m["id"] in (b.get("new") or []), "rows": rows})
    return {
        "generated": now, "total": total,
        "autopilot": [_position(state, t, info[t], cooldown_of, cooldown_end) for t in relays
                      if (info[t].get("ap") or 0) >= 1 or allocation_of(state, t)],
        "top": [dict(_position(state, t, info[t], cooldown_of, cooldown_end), rank=k + 1) for k, t in enumerate(top)],
        "moves": moves[:15],
        "scan": {"full_at": b["scan"].get("full_at") or 0, "live": b["scan"].get("live") or 0},
    }
