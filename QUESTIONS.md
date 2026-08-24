# QUESTIONS.md — proxy-fleet

## Pending Admin / User Decisions

### 1. Repo visibility: public vs private
I created `ons96/proxy-fleet` as **public** (pushed 2026-08-24). Reason: GitHub
private repos cap Actions minutes at 2,000/month (~100 hourly runs); public is
unlimited, and hourly refresh is the whole point of the aggregator. Content is
non-sensitive (public proxy IPs); every established proxy-list project
(TheSpeedX, monosans, proxifly) is public.
If you want private: flip it in repo Settings (Actions minutes then cap ~3 runs/day).

### 2. Kilo's WAF reality (important)
Kilo's gateway runs on Vercel, which bot-redirects (308) many free/proxy IPs.
Direct connection from your VPS gets 200. Of the first lenient pool (5,000
proxies, CONNECT-validated), **0/10 sampled returned 200 through a proxy** —
the lenient check counted any HTTP status as pass. I patched `check.py` to
require 2xx and am re-running CI; the strict list will be much smaller but
genuinely usable. Design implication: your own IP stays the primary path
(direct-first is the default in the rotator) and proxies are the rate-limit
fallback — which is exactly the priority you asked for.

### 3. OMP provider wiring
Your `models.yml` already has a `kiloproxy-lite` provider pointing at
`http://127.0.0.1:5381/v1` — the rotator now serves that exact endpoint with
the same model list, so **no models.yml change was needed**. The old
kiloproxy-lite service is not running, so no port conflict. Note: if you later
restart the old service, it will collide on 5381.

### 4. Questions for you
- Want me to add a `kilo-rotator` provider entry (same models) so you can
  A/B between direct kilocode and rotator? (Currently the rotator IS the
  endpoint, so `kilo-auto/free` etc. already flow through it.)
- Should the rotator also rotate for other providers/hosts (add more
  `--target` entries)?
- OK with the rotator restarting omp's session on repeated empty responses
  (watchdog already armed)? It fires only on a fresh log marker, 5-min cooldown.

## Notes
- Nothing here is blocking; all work is complete and verified without these.
- Watchdog auto-recovery + omp retry hardening (maxRetries 8, cross-family
  fallback chains, default role = vps-gateway/coding-elite) already applied to
  `~/.omp/agent/config.yml` (backup: `config.yml.bak.20260824-183347`).