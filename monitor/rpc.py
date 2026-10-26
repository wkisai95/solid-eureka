"""JSON-RPC client with endpoint rotation, adaptive eth_getLogs ranges and Multicall3."""
import collections
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from eth_abi import decode, encode
from eth_hash.auto import keccak

UA = "radar-saero-monitor/1.1 (+github actions)"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGGREGATE3 = keccak(b"aggregate3((address,bool,bytes)[])")[:4]

RANGE_HINTS = (
    "range", "too many", "more than", "limit", "exceed", "response size", "too large", "query timeout",
    "-32005", "block range", "max results", "results", "timeout", "10000", "returned more",
)
UNSUPPORTED_HINTS = ("not supported", "method not found", "not available", "-32601", "unsupported", "not allowed")
RATE_HINTS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "-32029", "throttl", "request limit",
              "exceeded the rate", "capacity exceeded", "try again later")
TINY_RANGE = re.compile(r"(?:limited to|up to|maximum of|max(?:imum)? range(?: is)?)\D{0,12}(?:0\s*-\s*)?(\d+)\s*blocks?",
                        re.I)


class RpcError(Exception):
    pass


class RangeTooLarge(RpcError):
    pass


class TimeBudget(RpcError):
    pass


class RateLimited(RpcError):
    pass


def retry_after(err, default=20):
    """Seconds a rate-limited endpoint asks us to wait (Retry-After), kept between 5 and 60."""
    try:
        v = float((err.headers or {}).get("Retry-After") or default)
    except (TypeError, ValueError):
        v = default
    return max(5.0, min(60.0, v))


def http_json(url, payload=None, headers=None, timeout=25):
    h = {"user-agent": UA, "accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def host(url):
    return urllib.parse.urlparse(url).netloc


class Rpc:
    def __init__(self, urls, name="base", log=print, timeout=20, pace=0.0):
        self.urls = list(urls)
        self.name = name
        self.log = log
        self.timeout = timeout
        self.pace = float(pace or 0)          # minimum seconds between two requests to the same endpoint
        self.last_req = {}
        self.cooldown = {}
        self.no_logs = {}          # endpoint -> reason it cannot serve eth_getLogs
        self.stats = collections.Counter()
        self.streak = collections.Counter()   # consecutive failures per (method, endpoint)
        self.dead = set()                      # (method, endpoint) pairs given up for this run
        self.multicall3 = None                 # unknown until the first multicall
        self.last_chunk = None

    def _order(self, method):
        now = time.time()
        urls = [u for u in self.urls if not (method == "eth_getLogs" and u in self.no_logs)
                and (method, u) not in self.dead]
        good = [u for u in urls if self.cooldown.get(u, 0) <= now] or urls
        # endpoints that already served this method come first
        return sorted(good, key=lambda u: -self.stats[f"ok:{method}:{host(u)}"])

    def _fail(self, method, url, cool):
        """A hard failure (not a range limit): cool the endpoint down and give up on it after a streak."""
        self.streak[(method, url)] += 1
        if self.streak[(method, url)] >= 5:
            self.dead.add((method, url))
        self.cooldown[url] = time.time() + cool

    def call(self, method, params):
        errors, range_errors, rate_limited = [], 0, 0
        span = None
        if method == "eth_getLogs":
            try:
                span = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
            except Exception:
                span = None
        order = self._order(method)
        if not order:
            raise RpcError(f"{method}: no usable endpoint left")
        # every endpoint is cooling down after a rate limit: wait for the first one rather than hammering them
        soonest = min(self.cooldown.get(u, 0) for u in order) - time.time()
        if soonest > 0:
            if soonest > 20:
                raise RateLimited(f"{method}: every endpoint is rate limited for {soonest:.0f} s more")
            time.sleep(soonest)
        for url in order:
            h = host(url)
            if self.pace:
                wait = self.last_req.get(url, 0) + self.pace - time.time()
                if wait > 0:
                    time.sleep(wait)
            self.last_req[url] = time.time()
            try:
                res = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=self.timeout)
            except urllib.error.HTTPError as e:
                errors.append(f"{h}: HTTP {e.code}")
                self.stats[f"err:{method}:{h}"] += 1
                if e.code == 429:
                    rate_limited += 1
                    self.stats[f"429:{method}:{h}"] += 1
                    self.cooldown[url] = time.time() + retry_after(e)   # try the others for a while
                elif method == "eth_getLogs" and e.code in (400, 413):
                    if span is not None and span <= 2000:
                        self.no_logs[url] = f"HTTP {e.code} on {span} blocks"
                    else:
                        range_errors += 1
                else:
                    self._fail(method, url, 90)
                continue
            except Exception as e:  # network error, timeout, TLS
                errors.append(f"{h}: {type(e).__name__} {e}"[:160])
                self.stats[f"err:{method}:{h}"] += 1
                if method == "eth_getLogs" and "timed out" in str(e).lower():
                    range_errors += 1
                    self.cooldown[url] = time.time() + 30
                else:
                    self._fail(method, url, 90)
                continue
            if isinstance(res, dict) and "error" in res:
                msg = json.dumps(res["error"])[:300]
                low = msg.lower()
                errors.append(f"{h}: {msg}")
                self.stats[f"err:{method}:{h}"] += 1
                if any(k in low for k in RATE_HINTS):
                    rate_limited += 1
                    self.stats[f"429:{method}:{h}"] += 1
                    self.cooldown[url] = time.time() + 20
                    continue
                if method == "eth_getLogs":
                    tiny = TINY_RANGE.search(msg)
                    if any(k in low for k in UNSUPPORTED_HINTS) or (tiny and int(tiny.group(1)) < 1000):
                        self.no_logs[url] = msg[:120]
                        continue
                    if any(k in low for k in RANGE_HINTS):
                        if span is not None and span <= 200:
                            self.no_logs[url] = msg[:120]
                        else:
                            range_errors += 1
                        continue
                self._fail(method, url, 60)
                continue
            if not isinstance(res, dict) or "result" not in res:
                errors.append(f"{h}: unexpected response")
                self._fail(method, url, 60)
                continue
            self.stats[f"ok:{method}:{h}"] += 1
            self.streak[(method, url)] = 0
            return res["result"]
        if range_errors:
            raise RangeTooLarge("; ".join(errors[-4:]))
        if rate_limited:
            raise RateLimited(f"{method} rate limited: " + "; ".join(errors[-5:]))
        raise RpcError(f"{method} failed on all endpoints: " + "; ".join(errors[-5:]))

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, number):
        """Timestamp of a block (headers are kept by every node, no archive state needed)."""
        blk = self.call("eth_getBlockByNumber", [hex(int(number)), False])
        if not blk or not blk.get("timestamp"):
            raise RpcError(f"no header for block {number}")
        return int(blk["timestamp"], 16)

    def block_at_ts(self, ts, head, head_ts, block_time):
        """Highest block with timestamp <= ts: estimate from the block time, gallop to a bracket, bisect."""
        if ts >= head_ts:
            return head
        bt = max(block_time, 0.01)
        est = max(0, min(head, int(head - (head_ts - ts) / bt)))
        t = self.block_ts(est)
        if t <= ts:
            lo, step = est, int((ts - t) / bt) + 16
            hi = min(head, lo + step)
            while hi < head and self.block_ts(hi) <= ts:
                lo, step = hi, step * 2
                hi = min(head, lo + step)
        else:
            hi, step = est, int((t - ts) / bt) + 16
            lo = max(0, hi - step)
            while lo > 0 and self.block_ts(lo) > ts:
                hi, step = lo, step * 2
                lo = max(0, hi - step)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.block_ts(mid) <= ts:
                lo = mid
            else:
                hi = mid
        return lo

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])

    def has_code(self, address):
        code = self.call("eth_getCode", [address, "latest"])
        return bool(code) and code not in ("0x", "0x0")

    def get_logs(self, from_block, to_block, address=None, topics=None, chunk=5000, max_chunk=20000, min_chunk=100,
                 deadline=None):
        """Yields logs between from_block and to_block (inclusive), shrinking ranges on provider limits.

        The largest range that worked is kept in self.last_chunk, so the caller can start from it next time.
        """
        start, size, failures, cap = from_block, max(min_chunk, int(chunk)), 0, max(int(chunk), int(max_chunk))
        limited = 0
        self.last_chunk = size
        while start <= to_block:
            if deadline and time.time() > deadline:
                raise TimeBudget(f"time budget reached at block {start}")
            end = min(to_block, start + size - 1)
            flt = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                res = self.call("eth_getLogs", [flt])
            except RateLimited:
                # the range is fine, the endpoints want us to slow down
                limited += 1
                if limited > 5:
                    raise
                time.sleep(min(20, 4 * limited))
                continue
            except RangeTooLarge:
                if size <= min_chunk:
                    raise
                size = max(min_chunk, size // 2)
                cap = size              # do not grow back past a range a provider refused
                continue
            except RpcError:
                failures += 1
                if failures > 4 or size <= min_chunk:
                    raise
                time.sleep(2 * failures)
                size = max(min_chunk, size // 2)
                continue
            failures = limited = 0
            self.last_chunk = max(min_chunk, min(size, cap))
            for lg in res:
                yield lg
            start = end + 1
            if size < cap:
                size = min(cap, size * 2)

    def multicall(self, calls, batch=150):
        """calls: list of (target, calldata bytes). Returns list of (ok, returndata bytes).

        Uses Multicall3 when the chain has it, otherwise plain eth_call one by one.
        """
        if self.multicall3 is None:
            try:
                self.multicall3 = self.has_code(MULTICALL3)
            except RpcError:
                self.multicall3 = True     # assume the usual deployment; individual batches fall back below
            if not self.multicall3:
                self.log(f"{self.name}: no Multicall3, using single calls")
        if not self.multicall3:
            return self._single_calls(calls)
        out = []
        for i in range(0, len(calls), batch):
            part = calls[i:i + batch]
            payload = AGGREGATE3 + encode(["(address,bool,bytes)[]"], [[(t, True, d) for t, d in part]])
            for attempt in range(3):
                try:
                    raw = self.eth_call(MULTICALL3, payload)
                    if not raw or raw == "0x":
                        raise RpcError("empty multicall result")
                    res = decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]
                    out.extend(res)
                    break
                except (RpcError, ValueError, TypeError) as e:
                    if attempt == 2:
                        if len(part) > 20:
                            # split a batch that may exceed a provider's gas or size cap
                            out.extend(self.multicall(part, batch=max(10, len(part) // 3)))
                            break
                        self.log(f"{self.name}: multicall batch failed: {str(e)[:200]}")
                        out.extend([(False, b"")] * len(part))
                    time.sleep(1.5 * (attempt + 1))
        return out

    def _single_calls(self, calls):
        out = []
        for target, data in calls:
            try:
                raw = self.eth_call(target, data)
                out.append((True, bytes.fromhex(raw[2:])) if raw and raw != "0x" else (False, b""))
            except RpcError:
                out.append((False, b""))
        return out

    def summary(self):
        by_host = collections.defaultdict(lambda: collections.Counter())  # kinds: ok, err, 429
        for k, v in self.stats.items():
            kind, method, h = k.split(":", 2)
            by_host[h][f"{kind}:{method}"] += v
        return {"endpoints": {h: dict(c) for h, c in by_host.items()},
                "no_logs": {host(u): r for u, r in self.no_logs.items()},
                "gave_up": sorted(f"{m}:{host(u)}" for m, u in self.dead)}
