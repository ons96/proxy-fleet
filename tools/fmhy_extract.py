#!/usr/bin/env python3
"""Extract URLs from named FMHY raw-markdown sections.

Usage: python3 fmhy_extract.py <raw_dir> <out_json>
Reads storage.md, privacy.md, developer-tools.md from <raw_dir>.
Writes {"file": {"section": [urls...]}} JSON.
"""
import json
import re
import sys
from pathlib import Path

# anchor-name -> list of (file, heading regex)
SECTIONS = {
    "proxy-lists": [("storage.md", r"^## Proxy Lists$")],
    "free-vpn-configs": [("storage.md", r"^## Free VPN Configs$")],
    "privacy-proxy": [("privacy.md", r"^# ► Proxy$")],
    "privacy-proxy-servers": [("privacy.md", r"^## ▷ Proxy Servers$")],
    "privacy-proxy-clients": [("privacy.md", r"^## ▷ Proxy Clients$")],
    "privacy-proxy-sites": [("privacy.md", r"^## ▷ Proxy Sites$")],
    "dev-web-security": [("developer-tools.md", r"^## ▷ Web Security$")],
    "dev-encryption-certificates": [("developer-tools.md", r"^## ▷ Encryption / Certificates$")],
    "dev-developer-utilities": [("developer-tools.md", r"^## ▷ Developer Utilities$")],
}

URL_RE = re.compile(r"https?://[^\s)\]\|>\"']+")
HEADING_RE = re.compile(r"^#+ ")


def extract_section(lines, heading_re):
    """Return lines of one markdown section (heading until next same-or-higher level)."""
    start = None
    level = 0
    for i, line in enumerate(lines):
        if start is None:
            if HEADING_RE.match(line) and heading_re.match(line):
                start = i
                level = len(line) - len(line.lstrip("#"))
        elif HEADING_RE.match(line):
            lvl = len(line) - len(line.lstrip("#"))
            if lvl <= level:
                return lines[start:i]
    return lines[start:] if start is not None else []


def main(raw_dir, out_path):
    result = {}
    total = 0
    for name, specs in SECTIONS.items():
        for fname, heading in specs:
            path = Path(raw_dir) / fname
            lines = path.read_text(encoding="utf-8").splitlines()
            body = extract_section(lines, re.compile(heading))
            urls = []
            for u in URL_RE.findall("\n".join(body)):
                u = u.rstrip(".,;")
                if u not in urls:
                    urls.append(u)
            result.setdefault(fname, {})[name] = urls
            total += len(urls)
    Path(out_path).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"sections={sum(len(v) for v in result.values())} urls={total} -> {out_path}")
    for fname, secs in result.items():
        for sec, urls in secs.items():
            print(f"  {fname}:{sec}: {len(urls)} urls")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
