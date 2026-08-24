#!/usr/bin/env python3
"""Validate pooled proxies against the target host; rank by measured quality.

Quality model per proxy:
  - latency  : median ms to first byte through the tunnel (speed proxy)
  - passrate : fraction of probe rounds that succeeded (current reliability)
  - uptime   : historical pass rate persisted across runs (stability)
  - score    : 0..1000 blend of reliability and latency

HTTP(S) proxies: TCP connect + CONNECT <target>:443 + GET (any HTTP status = ok).
SOCKS4/5: handshake + CONNECT + GET through tunnel.
Outputs latency/quality-ordered lists/ files. Stdlib-only.

Usage:
    python3 check.py [--pool data] [--out lists] [--target api.kilo.ai] [--port 443]
                     [--probes 2] [--limit N] [--workers 400] [--timeout 8]
                     [--history data/history.json]
"""
import argparse
import asyncio
import json
import os
import time

PROBE_PATH = "/api/gateway/v1/models"
OUT = {"http": "http.txt", "socks4": "socks4.txt", "socks5": "socks5.txt"}


async def _tunnel(proto, addr, host, port, timeout):
    """Open tunneled connection through proxy; return (reader, writer) or None."""
    ip, p = addr.rsplit(":", 1)
    r, w = await asyncio.wait_for(asyncio.open_connection(ip, int(p)), timeout)
    if proto == "http":
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        w.write(req.encode())
        await asyncio.wait_for(w.drain(), timeout)
        buf = await asyncio.wait_for(r.read(64), timeout)
        if not (buf.startswith(b"HTTP/1.1 200") or b" 200 " in buf):
            w.close()
            return None
        return r, w
    if proto == "socks5":
        w.write(b"\x05\x01\x00")
        await asyncio.wait_for(w.drain(), timeout)
        if await asyncio.wait_for(r.readexactly(2), timeout) != b"\x05\x00":
            w.close()
            return None
        hb = host.encode()
        w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
        await asyncio.wait_for(w.drain(), timeout)
        rep = await asyncio.wait_for(r.readexactly(4), timeout)
        if rep[1] != 0:
            w.close()
            return None
        return r, w
    if proto == "socks4":
        w.write(b"\x04\x01" + port.to_bytes(2, "big") + b"\x00\x00\x00\x01\x00" + b"\x00")
        await asyncio.wait_for(w.drain(), timeout)
        buf = await asyncio.wait_for(r.readexactly(8), timeout)
        if buf[1] != 0x5A:
            w.close()
            return None
        return r, w
    return None


async def probe(proto, addr, host, port, timeout):
    """One probe round: tunnel + GET; return latency ms or None.

    Pass = any 2xx status. 3xx (bot-redirect/WAF), 4xx, 5xx, timeouts,
    and tunnel failures all count as fail — a proxy that gets redirected
    by the target's WAF is useless for rotation.
    """
    try:
        conn = await _tunnel(proto, addr, host, port, timeout)
        if conn is None:
            return None
        r, w = conn
        t0 = time.monotonic()
        w.write(f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await asyncio.wait_for(w.drain(), timeout)
        head = await asyncio.wait_for(r.read(256), timeout)  # status line + headers = first byte
        w.close()
        if not head.startswith(b"HTTP/1"):
            return None
        try:
            status = int(head.split(b" ", 2)[1])
        except (IndexError, ValueError):
            return None
        if not (200 <= status < 300):
            return None
        return (time.monotonic() - t0) * 1000
    except Exception:  # noqa: BLE001
        return None


async def run_pool(proto, addrs, host, port, workers, timeout, probes):
    sem = asyncio.Semaphore(workers)
    results = {}

    async def one(addr):
        async with sem:
            lats = [x for x in (await asyncio.gather(*(probe(proto, addr, host, port, timeout) for _ in range(probes)))) if x is not None]
            if lats:
                results[addr] = lats

    await asyncio.gather(*(one(a) for a in addrs))
    return results


def score(lats, history, now_ms):
    """0..1000 quality score: reliability-heavy, latency-penalized."""
    med = sorted(lats)[len(lats) // 2]
    passrate = len(lats) / 2.0 if False else None  # computed by caller; see below
    # passrate computed in main from probes; here blend with history
    h_ok, h_tot = history
    uptime = h_ok / h_tot if h_tot else None
    return med, uptime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="data")
    ap.add_argument("--out", default="lists")
    ap.add_argument("--target", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--probes", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=400)
    ap.add_argument("--timeout", type=float, default=8)
    ap.add_argument("--history", default="data/history.json")
    args = ap.parse_args()

    host = args.target
    port = args.port
    if host is None:
        cfg = json.load(open("sources.json", encoding="utf-8"))
        host = cfg["target"]["host"]
        port = cfg["target"]["port"]

    history = {}
    if os.path.exists(args.history):
        history = json.load(open(args.history, encoding="utf-8"))

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.dirname(args.history) or ".", exist_ok=True)

    merged = {}  # addr -> (proto, score, lat)
    for proto, outname in OUT.items():
        pool = f"{args.pool}/pool_{proto}.txt"
        if not os.path.exists(pool):
            continue
        addrs = [l.strip() for l in open(pool, encoding="utf-8") if l.strip()]
        if args.limit:
            addrs = addrs[: args.limit]
        if not addrs:
            continue
        res = asyncio.run(run_pool(proto, addrs, host, port, args.workers, args.timeout, args.probes))
        rows = []
        for addr, lats in res.items():
            med = sorted(lats)[len(lats) // 2]
            passrate = len(lats) / args.probes
            h_ok, h_tot = history.get(addr, (0, 0))
            uptime = h_ok / h_tot if h_tot else passrate
            rel = 0.65 * passrate + 0.35 * uptime
            s = max(0, min(1000, round(1000 * rel - min(med, 8000) * 0.1)))
            rows.append((s, addr, med))
            history[addr] = (h_ok + len(lats), h_tot + args.probes)
        rows.sort(key=lambda r: (-r[0], r[2]))
        with open(f"{args.out}/{outname}", "w", encoding="utf-8") as f:
            for s, addr, med in rows:
                f.write(f"{s:4d} {addr} {med:.0f}ms\n")
        ok = len(rows)
        print(f"{proto}: {ok}/{len(addrs)} ok" + (f" (fastest {rows[0][2]:.0f}ms)" if ok else ""))
        for s, addr, med in rows:
            merged[addr] = (proto, s, med)

    json.dump(history, open(args.history, "w", encoding="utf-8"), indent=0)

    if merged:
        all_rows = sorted(merged.items(), key=lambda kv: (-kv[1][1], kv[1][2]))
        with open(f"{args.out}/all.txt", "w", encoding="utf-8") as f:
            for addr, (proto, s, med) in all_rows:
                f.write(f"{s:4d} {proto}://{addr} {med:.0f}ms\n")
        with open(f"{args.out}/summary.json", "w", encoding="utf-8") as f:
            json.dump({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "target": f"{host}:{port}",
                "total_ok": len(merged),
                "by_proto": {p: sum(1 for v in merged.values() if v[0] == p) for p in OUT},
            }, f, indent=1)
        print(f"TOTAL ok: {len(merged)} -> lists/all.txt")


if __name__ == "__main__":
    main()