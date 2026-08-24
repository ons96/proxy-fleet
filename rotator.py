#!/usr/bin/env python3
"""proxy-fleet rotator daemon.

Local forward proxy + OpenAI-compatible origin endpoint with direct-first
egress rotation:

  MODE DIRECT : connect to origin with own IP (default; own IP is best).
  MODE ROTATE : on rate-limit (429/403) or repeated origin errors, switch to
                the best-ranked proxy from the fleet list; rotate to the next
                proxy on failure; reclaim own IP after --direct-cooldown.

Host-scoped: only traffic for --target rotates; everything else goes direct,
so it is safe to set HTTP(S)_PROXY=http://127.0.0.1:5381 globally.

Also serves as an OpenAI-compatible base: requests to http://127.0.0.1:5381/*
are forwarded to --origin/* (drop-in provider baseURL for omp/opencode).

Stdlib-only asyncio. Usage:
    python3 rotator.py [--listen 127.0.0.1:5381] [--target api.kilo.ai]
                       [--origin https://api.kilo.ai/api/gateway]
                       [--list lists/all.txt] [--direct-cooldown 3300]
                       [--proxy-fail-limit 3] [--refresh 900]
"""
import argparse
import asyncio
import json
import os
import re
import time
import urllib.request

LIST_URL = "https://raw.githubusercontent.com/ons96/proxy-fleet/main/lists/all.txt"
LINE_RE = re.compile(r"^\s*(\d+)\s+(https?|socks4|socks5)://([0-9.]+):(\d+)\s+\S+ms\s*$")


class Router:
    def __init__(self, target, direct_cooldown, proxy_fail_limit):
        self.target = target
        self.direct_cooldown = direct_cooldown
        self.proxy_fail_limit = proxy_fail_limit
        self.mode = "direct"          # direct | rotate
        self.cooldown_until = 0.0     # when direct may be retried
        self.proxies = []             # [(score, proto, addr)]
        self.pidx = 0
        self.pfails = {}              # addr -> consecutive failures
        self.stats = {"direct": 0, "rotated": 0, "rotations": 0}

    def load_list(self, path):
        text = None
        try:
            req = urllib.request.Request(LIST_URL, headers={"User-Agent": "proxy-fleet/1.0"})
            text = urllib.request.urlopen(req, timeout=20).read().decode()
        except Exception as e:  # noqa: BLE001
            print(f"[rotator] list fetch failed ({e}); using local cache", flush=True)
        if text is None and os.path.exists(path):
            text = open(path, encoding="utf-8").read()
        if text is None:
            print("[rotator] no proxy list available", flush=True)
            return
        try:
            rows = []
            for line in text.splitlines():
                m = LINE_RE.match(line.strip())
                if m:
                    rows.append((int(m.group(1)), m.group(2), f"{m.group(3)}:{m.group(4)}"))
            if rows:
                self.proxies = rows
                self.pidx = 0
                self.pfails = {}
                print(f"[rotator] pool: {len(rows)} proxies", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[rotator] list parse failed: {e}", flush=True)

    def pick(self):
        """Return ('direct', None) or ('proxy', (proto, addr)) for target traffic."""
        if self.mode == "direct":
            return ("direct", None)
        if self.mode == "rotate" and time.monotonic() > self.cooldown_until:
            self.mode = "direct"
            self.stats["rotations"] += 1
            print("[rotator] direct cooldown expired; reclaimed own IP", flush=True)
            return ("direct", None)
        n = len(self.proxies)
        for i in range(n):
            idx = (self.pidx + i) % n
            score, proto, addr = self.proxies[idx]
            if self.pfails.get(addr, 0) >= self.proxy_fail_limit:
                continue
            self.pidx = (idx + 1) % n
            return ("proxy", (proto, addr))
        return ("direct", None)  # all proxies burned; fall back to direct

    def report(self, kind, addr=None, status=None):
        """kind: 'ok' | 'ratelimit' | 'error'."""
        if addr is None:  # direct path
            if kind == "ratelimit":
                self.mode = "rotate"
                self.cooldown_until = time.monotonic() + self.direct_cooldown
                self.stats["rotations"] += 1
                print(f"[rotator] rate-limit on own IP; rotating for {self.direct_cooldown}s", flush=True)
            return
        if kind == "ok":
            self.pfails[addr] = 0
            self.stats["rotated"] += 1
        elif kind in ("ratelimit", "error"):
            self.pfails[addr] = self.pfails.get(addr, 0) + 1
            self.stats["rotations"] += 1
            print(f"[rotator] proxy {addr} {kind}; switching", flush=True)


class ProxyRelay:
    """asyncio forward proxy + origin endpoint with egress rotation."""

    def __init__(self, router, target, origin, listen):
        self.router = router
        self.target = target
        self.origin = origin.rstrip("/")
        self.listen_host, self.listen_port = listen.rsplit(":", 1)
        self.listen_port = int(self.listen_port)

    async def handle(self, r, w):
        try:
            line = await asyncio.wait_for(r.readline(), 10)
            if not line:
                w.close()
                return
            parts = line.decode("latin1", "replace").strip().split(" ")
            if len(parts) < 3:
                w.close()
                return
            method, uri, ver = parts[0], parts[1], parts[2]
            headers = []
            while True:
                h = await asyncio.wait_for(r.readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)
            if method == "CONNECT":
                await self._connect(r, w, uri)
            else:
                await self._http(r, w, method, uri, ver, headers)
        except Exception as e:  # noqa: BLE001
            print(f"[rotator] conn error: {e}", flush=True)
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    def _split_target(self, uri, host_header):
        """Return (host, port, path). Handles absolute-form + authority + origin-form."""
        if uri.startswith("http://") or uri.startswith("https://"):
            from urllib.parse import urlsplit
            u = urlsplit(uri)
            port = u.port or (443 if u.scheme == "https" else 80)
            return u.hostname, port, (u.path or "/") + (("?" + u.query) if u.query else "")
        if ":" in uri and not uri.startswith("/"):
            # CONNECT authority-form: host:port
            host, _, p = uri.rpartition(":")
            return host, int(p), "/"
        # origin-form: host from Host header
        hh = host_header.decode("latin1", "replace").strip() if host_header else ""
        host = hh.split(":")[0] if hh else self.listen_host
        port = 80
        if ":" in hh:
            try:
                port = int(hh.split(":", 1)[1])
            except ValueError:
                pass
        return host, port, uri

    async def _connect(self, r, w, uri):
        """CONNECT host:port -> tunnel (direct or via proxy)."""
        host, port, _ = self._split_target(uri, None)
        target = (host == self.target)
        kind, proxy = self.router.pick() if target else ("direct", None)
        up = None
        try:
            if kind == "direct":
                up = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
            else:
                proto, addr = proxy
                up = await self._open_proxy(proto, addr, host, port)
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
            if target:
                self.router.stats["direct" if kind == "direct" else "rotated"] += 1
            await self._pump(r, w, up[0], up[1])
            if target and kind != "direct":
                self.router.report("ok", addr=proxy[1])
        except Exception as e:  # noqa: BLE001
            if target:
                self.router.report("error" if kind == "direct" else "error", addr=proxy[1] if kind != "direct" else None)
            print(f"[rotator] CONNECT {host}:{port} via {kind} failed: {e}", flush=True)
            try:
                w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await w.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if up:
                for s in up:
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001
                        pass

    async def _open_proxy(self, proto, addr, host, port):
        ip, p = addr.rsplit(":", 1)
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, int(p)), 10)
        if proto == "http":
            w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            await asyncio.wait_for(w.drain(), 10)
            buf = await asyncio.wait_for(r.read(64), 10)
            if not (buf.startswith(b"HTTP/1.1 200") or b" 200 " in buf):
                w.close()
                raise ConnectionError(f"proxy CONNECT rejected: {buf[:30]!r}")
        elif proto == "socks5":
            w.write(b"\x05\x01\x00")
            await asyncio.wait_for(w.drain(), 10)
            if await asyncio.wait_for(r.readexactly(2), 10) != b"\x05\x00":
                w.close()
                raise ConnectionError("socks5 greet failed")
            hb = host.encode()
            w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
            await asyncio.wait_for(w.drain(), 10)
            rep = await asyncio.wait_for(r.readexactly(4), 10)
            if rep[1] != 0:
                w.close()
                raise ConnectionError(f"socks5 connect failed: {rep[1]}")
        else:  # socks4
            w.write(b"\x04\x01" + port.to_bytes(2, "big") + b"\x00\x00\x00\x01\x00" + b"\x00")
            await asyncio.wait_for(w.drain(), 10)
            buf = await asyncio.wait_for(r.readexactly(8), 10)
            if buf[1] != 0x5A:
                w.close()
                raise ConnectionError("socks4 connect failed")
        return r, w

    async def _http(self, r, w, method, uri, ver, headers):
        """Forward HTTP request (absolute-form or origin endpoint)."""
        host_header = next((h for h in headers if h.lower().startswith(b"host:")), None)
        host, port, path = self._split_target(uri, host_header)
        if path == "/healthz":
            await self.health(r, w)
            return
        if path == "/ratelimit":
            self.router.report("ratelimit", addr=None)
            body = json.dumps({"mode": self.router.mode}).encode()
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                    str(len(body)).encode() + b"\r\n\r\n" + body)
            await w.drain()
            w.close()
            return
        to_origin = (host == self.listen_host)  # OpenAI-compatible base request
        if to_origin:
            host, port = self.target, 443
            path = self.origin + path
        target = (host == self.target)
        kind, proxy = self.router.pick() if target else ("direct", None)

        # body
        clen = 0
        for h in headers:
            if h.lower().startswith(b"content-length:"):
                try:
                    clen = int(h.split(b":", 1)[1])
                except ValueError:
                    pass
        body = await asyncio.wait_for(r.readexactly(clen), 10) if clen else b""

        up = None
        try:
            if kind == "direct":
                up = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
            else:
                proto, addr = proxy
                up = await self._open_proxy(proto, addr, host, port)
                # via HTTP proxy: absolute-form; via SOCKS: origin-form after tunnel
                if proto == "http":
                    path = f"http://{host}:{port}{path}"
            req = f"{method} {path} {ver}\r\n".encode() + b"".join(headers) + b"\r\n" + body
            up[1].write(req)
            await asyncio.wait_for(up[1].drain(), 10)

            status_line = await asyncio.wait_for(up[0].readline(), 10)
            status = int(status_line.split(b" ", 2)[1]) if status_line.split(b" ", 2) else 0
            w.write(status_line)
            resp_headers = []
            while True:
                h = await asyncio.wait_for(up[0].readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
                resp_headers.append(h)
                w.write(h)
            w.write(b"\r\n")
            await w.drain()
            # stream body
            while True:
                chunk = await asyncio.wait_for(up[0].read(65536), 30)
                if not chunk:
                    break
                w.write(chunk)
                await w.drain()
            if target:
                if status in (429, 403):
                    self.router.report("ratelimit", addr=proxy[1] if kind != "direct" else None)
                else:
                    self.router.report("ok", addr=proxy[1] if kind != "direct" else None)
        except Exception as e:  # noqa: BLE001
            if target:
                self.router.report("error", addr=proxy[1] if kind != "direct" else None)
            print(f"[rotator] HTTP {path} via {kind} failed: {e}", flush=True)
            try:
                w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await w.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if up:
                for s in up:
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    async def _pump(self, a_r, a_w, b_r, b_w):
        async def one(r, w):
            try:
                while True:
                    data = await asyncio.wait_for(r.read(65536), 60)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.gather(one(a_r, b_w), one(b_r, a_w))

    async def health(self, r, w):
        body = json.dumps({
            "mode": self.router.mode,
            "stats": self.router.stats,
            "pool": len(self.router.proxies),
        }).encode()
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                str(len(body)).encode() + b"\r\n\r\n" + body)
        await w.drain()
        w.close()

    async def serve(self):
        srv = await asyncio.start_server(self.handle, self.listen_host, self.listen_port)
        print(f"[rotator] listening on {self.listen_host}:{self.listen_port} "
              f"(target={self.target}, origin={self.origin})", flush=True)
        async with srv:
            await srv.serve_forever()


async def refresh_loop(router, path, interval):
    while True:
        await asyncio.sleep(interval)
        router.load_list(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:5381")
    ap.add_argument("--target", default="api.kilo.ai")
    ap.add_argument("--origin", default="https://api.kilo.ai/api/gateway")
    ap.add_argument("--list", default="lists/all.txt")
    ap.add_argument("--direct-cooldown", type=int, default=3300)
    ap.add_argument("--proxy-fail-limit", type=int, default=3)
    ap.add_argument("--refresh", type=int, default=900)
    args = ap.parse_args()

    router = Router(args.target, args.direct_cooldown, args.proxy_fail_limit)
    router.load_list(args.list)
    relay = ProxyRelay(router, args.target, args.origin, args.listen)

    async def amain():
        asyncio.create_task(refresh_loop(router, args.list, args.refresh))
        await relay.serve()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()