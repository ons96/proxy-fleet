#!/usr/bin/env python3
"""Harvest proxy IPs from all sources in sources.json; dedupe; write data/pool_<proto>.txt.

Stdlib-only so CI needs zero pip installs. Usage:
    python3 harvest.py [--sources sources.json] [--outdir data]
"""
import argparse
import concurrent.futures as cf
import json
import re
import sys
from urllib.request import Request, urlopen

IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
SCHEME_RE = re.compile(r"^(https?|socks[45])://", re.I)
UA = {"User-Agent": "proxy-fleet/1.0 (+https://github.com/ons96/proxy-fleet)"}


def fetch(url, timeout=40):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse(text, proto):
    """Parse a source body into [(protocol, 'ip:port'), ...]."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = proto
        if p == "mixed":
            p = "http"
        m = SCHEME_RE.match(line)
        if m:
            p = m.group(1).lower()
            line = line[m.end():]
        m2 = IP_RE.search(line)
        if not m2:
            continue
        port = int(m2.group(2))
        if not (0 < port < 65536):
            continue
        out.append((p, f"{m2.group(1)}:{port}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.json")
    ap.add_argument("--outdir", default="data")
    args = ap.parse_args()

    cfg = json.load(open(args.sources, encoding="utf-8"))
    sources = cfg["sources"]

    merged = {}  # (proto, addr) -> None
    per_source = {}
    failures = []

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fetch, s["url"]): s for s in sources}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                body = fut.result()
                entries = parse(body, s["type"])
                per_source[s["name"]] = len(entries)
                for p, addr in entries:
                    merged[(p, addr)] = None
            except Exception as e:  # noqa: BLE001
                failures.append((s["name"], repr(e)[:120]))

    pools = {}
    for (p, addr) in merged:
        pools.setdefault(p, []).append(addr)

    import os
    os.makedirs(args.outdir, exist_ok=True)
    for p, addrs in pools.items():
        with open(f"{args.outdir}/pool_{p}.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(addrs)) + "\n")

    print(f"unique proxies: {len(merged)}")
    for name, n in sorted(per_source.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d} {name}")
    for p, addrs in sorted(pools.items()):
        print(f"  pool_{p}.txt: {len(addrs)}")
    if failures:
        print("failed sources:")
        for name, err in failures:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()