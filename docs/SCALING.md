# Scaling the LinkedIn scraper

This service is **already horizontally scalable**: it is stateless per request
(the only in-process state is the LinkedIn login session + the result cache,
both of which are per-instance and self-healing). So the way to handle heavy,
sustained load is to run **N replicas**, each with its own LinkedIn account,
behind a load balancer — not to push one process harder.

This doc covers the topology, a working multi-replica `docker-compose` +
Caddy example, the operational implications, and the future single-process
multi-identity direction.

---

## Why scale out, not up

- One LinkedIn identity from one IP draws anti-bot scrutiny as concurrency
  rises. The in-process scrape pool (`SCRAPE_POOL_SIZE`) is deliberately
  conservative (start at 2) for exactly this reason.
- N identities from N containers spread the load across N sessions and N
  source IPs (or proxies), each doing modest concurrent work — far safer for
  stealth and far higher aggregate throughput.
- The cache, single-flight, circuit breaker, deadline, and backpressure are
  all **per-instance**, so adding replicas multiplies capacity linearly with
  none of the per-IP risk.

## Topology

```
                 ┌──────────────────────────────┐
   clients ───▶  │  Caddy (round-robin LB)      │  :443 / :80
                 │  health-aware, sticky-opt.   │
                 └──────────────┬───────────────┘
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
     ┌────────────┐      ┌────────────┐      ┌────────────┐
     │ scraper-0  │      │ scraper-1  │      │ scraper-2  │
     │ acct A     │      │ acct B     │      │ acct C     │
     │ profile_A  │      │ profile_B  │      │ profile_C  │
     │ SCRAPE_POOL│      │ SCRAPE_POOL│      │ SCRAPE_POOL│
     │  =2        │      │  =2        │      │  =2        │
     └────────────┘      └────────────┘      └────────────┘
```

Each replica:
- has its own `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` (a **distinct** account),
- has its own named profile volume (so its persisted session survives restarts),
- shares the same `API_AUTH_SHA256` (one client secret works across all),
- exposes `/health` with a unique `instance` id + login/breaker/cache state.

## What each replica needs

| env / volume            | per-replica? | notes                                                    |
|-------------------------|:------------:|----------------------------------------------------------|
| `LINKEDIN_EMAIL`        | ✅ unique    | a different LinkedIn account per replica                 |
| `LINKEDIN_PASSWORD`     | ✅ unique    | matching password                                        |
| `API_AUTH_SHA256`       | ❌ shared    | one client secret across all replicas                    |
| profile volume          | ✅ unique    | one named volume per replica (`linkedin_profile_0`, …)   |
| `SCRAPE_POOL_SIZE`      | either       | 2 is sane; total concurrency ≈ N × pool size             |
| `INSTANCE_ID`           | optional     | else auto-generated; helps LB/debugging                  |

> Each account must complete its own first-run login (possibly a confirmation
> code via `POST /login/code`). After that the session persists in the volume.

## Working example: 3 replicas behind Caddy

`docker-compose.scale.yml`:

```yaml
services:
  caddy:
    image: caddy:2
    depends_on: [scraper]
    ports: ["80:80", "443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile:ro"]
    networks: [proxy-network]

  scraper:
    build: .
    env_file: [.env]                 # shared API_AUTH_SHA256
    environment:
      - SCRAPE_POOL_SIZE=2
      - SCRAPE_DEADLINE_SECONDS=180
    shm_size: "1gb"
    deploy:
      replicas: 3
    # per-replica creds + volume are injected by an env_file per replica, OR by
    # an orchestrator (k8s: one Secret per replica). For a simple compose demo
    # you can use a list of services instead of `replicas` (see below).
    volumes:
      - linkedin_profile:/app/profile
    networks: [proxy-network]
    restart: unless-stopped

networks:
  proxy-network:
    external: true

volumes:
  linkedin_profile:
```

For **distinct accounts per replica** with plain compose (which can't template
env per replica), enumerate the services explicitly:

```yaml
services:
  scraper-0:
    build: .
    env_file: [.env, .env.acct-0]     # .env.acct-0 = LINKEDIN_EMAIL/PASSWORD for acct A
    environment: { SCRAPE_POOL_SIZE: "2", INSTANCE_ID: "scraper-0" }
    volumes: ["linkedin_profile_0:/app/profile"]
    networks: [proxy-network]
    restart: unless-stopped
  scraper-1:
    build: .
    env_file: [.env, .env.acct-1]
    environment: { SCRAPE_POOL_SIZE: "2", INSTANCE_ID: "scraper-1" }
    volumes: ["linkedin_profile_1:/app/profile"]
    networks: [proxy-network]
    restart: unless-stopped
  # … scraper-2 …
```

`Caddyfile` (round-robin + health-aware):

```caddy
scraper.example.com {
    reverse_proxy scraper-0:5000 scraper-1:5000 scraper-2:5000 {
        lb_policy round_robin
        health_uri /health
        health_interval 10s
        health_timeout 3s
    }
}
```

Caddy will pull a replica out of rotation if its `/health` fails (e.g. login
state `failed` or the breaker is open and you choose to fail the health check).

## Client contract across replicas

- `POST /extract`, `/profile`, `/company` behave identically on every replica.
- The **result cache is per-replica**, so the same URL hit on two different
  replicas will scrape twice. If that matters, front it with a shared cache
  (Redis) or enable `?force` discipline; for most workloads the per-replica
  cache is plenty.
- **Async jobs** (`?async=1` → `GET /jobs/{id}`) are per-replica: the LB must
  route the `GET /jobs/{id}` to the SAME replica that created it (the 202
  response carries a `Location: /jobs/{id}` header — enable LB session
  affinity on that path, or have the client follow the absolute job URL). For
  blocking requests there is no affinity requirement.

## Sizing

- Each replica ≈ 1 cloakbrowser (login) + `SCRAPE_POOL_SIZE` ephemeral
  browsers under load ≈ `(1 + pool_size) × ~250MB` RSS at peak. With
  `SCRAPE_POOL_SIZE=2` that's ~750MB per replica; size the host accordingly.
- Aggregate concurrency ≈ `replicas × SCRAPE_POOL_SIZE`. 3 replicas × 2 = 6
  concurrent scrapes with 6 distinct identities — comfortably more than the
  "10 requests" scenario, with far less anti-bot risk than 6-in-one-process.

## Future: single-process multi-identity (not yet implemented)

Today every pool worker re-seeds from the **same** `_cookies` snapshot (one
identity per process). A generalized `SessionPool` that manages N
`{profile_dir, cookies, login_state}` tuples and hands each scrape worker a
*different* identity would let one process run N identities — useful when you
want the parallelism but not N containers. The seam is small and isolated
(`browser._cookies` / `_cookie_gen` are the single point), but it is **not
implemented** because it needs N real LinkedIn accounts to validate end-to-end
and risks half-baked behavior. Prefer N containers (above) until you have a
reason to collapse them.
