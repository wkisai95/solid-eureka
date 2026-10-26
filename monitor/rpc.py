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
TINY_RANGE = re.compile(r"(?:limited to|up to|maximum of|max(?:imum)? range(?: is)?)\D{0,12}(?:0\s*-\s*)?(\d+)\s*blocks?",
                        re.I)


class RpcError(Exception):
    pass


class RangeTooLarge(RpcError):
    pass


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
    def __init__(self, urls, name="base", log=print, timeout=25):
        self.urls = list(urls)
        self.name = name
        self.log = log
        self.timeout = timeout
        self.cooldown = {}
        self.no_logs = {}          # endpoint -> reason it cannot serve eth_getLogs
        self.stats = collections.Counter()

    def _order(self, method):
        now = time.time()
        urls = [u for u in self.urls if not (method == "eth_getLogs" and u in self.no_logs)]
        good = [u for u in urls if self.cooldown.get(u, 0) <= now] or urls
        # endpoints that already served this method come first
        return sorted(good, key=lambda u: -self.stats[f"ok:{method}:{host(u)}"])

    def call(self, method, params):
        errors, range_errors = [], 0
        for url in self._order(method):
            h = host(url)
            try:
                res = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=self.timeout)
            except urllib.error.HTTPError as e:
                errors.append(f"{h}: HTTP {e.code}")
                self.stats[f"err:{method}:{h}"] += 1
                if method == "eth_getLogs" and e.code in (400, 413):
                    range_errors += 1
                else:
                    self.cooldown[url] = time.time() + 90
                continue
            except Exception as e:  # network error, timeout, TLS
                errors.append(f"{h}: {type(e).__name__} {e}"[:160])
                self.stats[f"err:{method}:{h}"] += 1
                self.cooldown[url] = time.time() + 90
                if method == "eth_getLogs" and "timed out" in str(e).lower():
                    range_errors += 1
                continue
            if isinstance(res, dict) and "error" in res:
                msg = json.dumps(res["error"])[:300]
                low = msg.lower()
                errors.append(f"{h}: {msg}")
                self.stats[f"err:{method}:{h}"] += 1
                if method == "eth_getLogs":
                    tiny = TINY_RANGE.search(msg)
                    if any(k in low for k in UNSUPPORTED_HINTS) or (tiny and int(tiny.group(1)) < 1000):
                        self.no_logs[url] = msg[:120]
                        continue
                    if any(k in low for k in RANGE_HINTS):
                        range_errors += 1
                        continue
                self.cooldown[url] = time.time() + 60
                continue
            if not isinstance(res, dict) or "result" not in res:
                errors.append(f"{h}: unexpected response")
                continue
            self.stats[f"ok:{method}:{h}"] += 1
            return res["result"]
        if range_errors:
            raise RangeTooLarge("; ".join(errors[-4:]))
        raise RpcError(f"{method} failed on all endpoints: " + "; ".join(errors[-5:]))

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])

    def get_logs(self, from_block, to_block, address=None, topics=None, chunk=5000, max_chunk=20000, min_chunk=100):
        """Yields logs between from_block and to_block (inclusive), shrinking ranges on provider limits."""
        start, size, failures = from_block, chunk, 0
        while start <= to_block:
            end = min(to_block, start + size - 1)
            flt = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                res = self.call("eth_getLogs", [flt])
            except RangeTooLarge:
                if size <= min_chunk:
                    raise
                size = max(min_chunk, size // 2)
                continue
            except RpcError:
                failures += 1
                if failures > 4 or size <= min_chunk:
                    raise
                time.sleep(2 * failures)
                size = max(min_chunk, size // 2)
                continue
            failures = 0
            for lg in res:
                yield lg
            start = end + 1
            if size < max_chunk:
                size = min(max_chunk, size * 2)

    def multicall(self, calls, batch=150):
        """calls: list of (target, calldata bytes). Returns list of (ok, returndata bytes)."""
        out = []
        for i in range(0, len(calls), batch):
            part = calls[i:i + batch]
            payload = AGGREGATE3 + encode(["(address,bool,bytes)[]"], [[(t, True, d) for t, d in part]])
            for attempt in range(3):
                try:
                    raw = self.eth_call(MULTICALL3, payload)
                    res = decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]
                    out.extend(res)
                    break
                except RpcError as e:
                    if attempt == 2:
                        if len(part) > 20:
                            # split a batch that may exceed a provider's gas or size cap
                            out.extend(self.multicall(part, batch=max(10, len(part) // 3)))
                            break
                        self.log(f"multicall batch failed: {e}")
                        out.extend([(False, b"")] * len(part))
                    time.sleep(1.5 * (attempt + 1))
        return out

    def summary(self):
        by_host = collections.defaultdict(lambda: collections.Counter())
        for k, v in self.stats.items():
            kind, method, h = k.split(":", 2)
            by_host[h][f"{kind}:{method}"] += v
        return {"endpoints": {h: dict(c) for h, c in by_host.items()},
                "no_logs": {host(u): r for u, r in self.no_logs.items()}}
