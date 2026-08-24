# proxy-fleet

Free-proxy super-aggregator: harvests every known free proxy source, validates each
proxy against a target API host, ranks by measured quality (speed, reliability,
uptime), and publishes fresh sorted lists hourly via GitHub Actions.

Built for rotating egress IPs when a free-tier API rate-limits the current IP
(e.g. Kilo Code's 200 req/h limit). Works for any OpenAI-compatible endpoint.

## How it works

```
sources.json --(harvest.py)--> data/pool_*.txt --(check.py)--> lists/{http,socks4,socks5,all}.txt
    37 sources                    ~130k unique          hourly GH Action
```

1. `harvest.py` fetches all 37 sources (GitHub raw lists + free APIs),
   extracts `ip:port` entries, dedupes across sources.
2. `check.py` opens a tunnel through each proxy to the target host
   (`api.kilo.ai:443` by default) and measures time-to-first-byte.
3. Each proxy gets a **quality score 0..1000**:
   - 65% current pass rate (reliability right now)
   - 35% historical pass rate persisted in `data/history.json` (uptime/stability)
   - minus latency penalty (speed)
4. Lists are sorted best-first: `lists/all.txt` is the final whitelist
   (format: `score protocol://ip:port latencyms`).

## Hourly refresh

`.github/workflows/refresh.yml` runs hourly (cron `12 * * * *`), re-harvests,
re-validates, commits changed lists, and uploads them as a CI artifact.
Raw URLs (usable directly):

- https://raw.githubusercontent.com/ons96/proxy-fleet/main/lists/all.txt
- https://raw.githubusercontent.com/ons96/proxy-fleet/main/lists/http.txt
- https://raw.githubusercontent.com/ons96/proxy-fleet/main/lists/socks5.txt

## Local use

```bash
python3 harvest.py --sources sources.json --outdir data
python3 check.py --pool data --out lists --target api.kilo.ai --port 443 --probes 2
```

Rotate egress IP for any API client:

```bash
export HTTPS_PROXY=http://127.0.0.1:5381   # rotator daemon, see rotator.py
```

## Sources

All free, no paid anything. GitHub raw lists (TheSpeedX/PROXY-List, monosans,
zevtyardt, roosterkid/openproxylist, proxifly, vakhov, zloi-user/hideip.me,
hookzof, jetkai, clarketm, gfpcom, Zaeem20, iplocate, sunny9577, MuRongPIG)
plus the free proxyscrape API. Add more in `sources.json`.

## Rotator daemon (direct-first egress rotation)

`rotator.py` is a local forward proxy + OpenAI-compatible origin endpoint that
uses your own IP by default and only switches to the fleet's proxies when your
IP gets rate-limited:

- **MODE DIRECT**: connects to the target with your own IP (best speed/reliability).
- **MODE ROTATE**: on 429/403 (rate limit) it switches to the best-ranked proxy;
  a proxy that fails, times out, or gets WAF-redirected (3xx) is auto-skipped;
  after `--direct-cooldown` (default 3300s) it reclaims your own IP.
- Host-scoped: only traffic for `--target` rotates; everything else goes direct,
  so it is safe to set `HTTP(S)_PROXY=http://127.0.0.1:5381` globally.
- Pool: auto-fetched from the repo's raw `lists/all.txt` every 15 min.

Run it (also supervised by the agent-session keeper, survives reboots):

```bash
python3 rotator.py --listen 127.0.0.1:5381 \
  --target api.kilo.ai --origin https://api.kilo.ai/api/gateway \
  --list ~/.proxy-fleet/lists/all.txt
```

Ops endpoints (all `http://127.0.0.1:5381/...`):
- `/healthz` — mode, rotation stats, pool size
- `/ratelimit` — force-enter rotate mode now (manual "I'm limited" trigger)
- `/dns?host=X`, `/conn?host=X` — diagnostics

**OMP wiring**: point any OpenAI-compatible provider at `http://127.0.0.1:5381/v1`
(e.g. a `kilo-rotator` provider entry mirroring `kiloproxy-lite`'s model list).
The rotator rewrites Host and forwards to the origin with egress rotation.

## Caveats

- Free proxies are flaky by nature: expect churn; hourly refresh mitigates.
- Some proxies block datacenter IPs (GitHub runners); the checker measures
  reality, so only working proxies survive.
- TLS is end-to-end; the proxy sees only the destination hostname, not payloads
  or API keys. Never disable certificate verification.
- Rotating IPs to exceed a service's rate limit may violate its ToS. Your call.