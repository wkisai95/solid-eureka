"""JSON-RPC client with endpoint rotation, adaptive eth_getLogs chunking and Multicall3."""
import collections
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from eth_abi import decode, encode
from eth_hash.auto import keccak

UA = "radar-saero-monitor/1.0 (+github actions)"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGGREGATE3 = keccak(b"aggregate3((address,bool,bytes)[])")[:4]

RANGE_HINTS = (
    "range", "too many", "more than", "limit", "exceed", "10000", "response size",
    "too large", "query timeout", "-32005", "-32602", "block range", "max results",
)


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


def _is_range_error(msg):
    m = msg.lower()
    return any(k in m for k in RANGE_HINTS)


class Rpc:
    def __init__(self, urls, name="base", log=print, timeout=25):
        self.urls = list(urls)
        self.name = name
        self.log = log
        self.timeout = timeout
        self.cooldown = {}
        self.stats = collections.Counter()

    def _order(self):
        now = time.time()
        good = [u for u in self.urls if self.cooldown.get(u, 0) <= now]
        return good or list(self.urls)

    def call(self, method, params):
        errors, range_errors = [], 0
        for url in self._order():
            try:
                res = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=self.timeout)
            except Exception as e:  # network / HTTP error
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {e}")
                self.cooldown[url] = time.time() + 90
                self.stats["fail:" + url] += 1
                continue
            if isinstance(res, dict) and "error" in res:
                msg = json.dumps(res["error"])[:300]
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {msg}")
                if _is_range_error(msg):
                    range_errors += 1
                else:
                    self.cooldown[url] = time.time() + 60
                self.stats["err:" + url] += 1
                continue
            self.stats["ok:" + url] += 1
            return res["result"]
        if range_errors and range_errors == len(errors):
            raise RangeTooLarge("; ".join(errors[-3:]))
        raise RpcError(f"{method} failed on all endpoints: " + "; ".join(errors[-4:]))

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])

    def get_logs(self, from_block, to_block, address=None, topics=None, chunk=10000, max_chunk=20000):
        """Yields logs between from_block and to_block (inclusive), shrinking ranges on provider limits."""
        start, size = from_block, chunk
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
                if size <= 50:
                    raise
                size = max(50, size // 2)
                continue
            for lg in res:
                yield lg
            start = end + 1
            if size < max_chunk:
                size = min(max_chunk, size * 2)

    def multicall(self, calls, batch=180):
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
