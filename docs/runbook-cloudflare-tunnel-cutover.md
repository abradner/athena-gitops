# Runbook — cutting a site over to Cloudflare Tunnel

Phase 0 of the second-AZ work (`docs/design-multi-az-boreas.md`). Removes the
dependency on the origin's static IP address, so that a WAN failover stops
taking the public sites down.

Placeholders — `<site>`, `<edge-proxy-host>`. Concrete addresses live in the
private infrastructure repo.

Today every public hostname is a **proxied A record pointing at the static ISP
address**, and the serving chain is:

    Cloudflare (proxied DNS) → HAProxy → cluster Gateway → HTTPRoute → pod

When the upstream link faults the router moves to its backup WAN, the public
address changes, and Cloudflare keeps sending traffic to an address nothing
answers on. After the cutover:

    Cloudflare → tunnel → cloudflared in-cluster → cluster Gateway :8443 → …

The connector dials out, so there is no origin address to go stale. HAProxy
stays exactly as it is and remains the rollback path.

---

## 1. One-time: create the two tunnels

A tunnel belongs to one Cloudflare account and the served zones span two — the
same split that gives `letsencrypt-issuers.yaml` its second DNS-01 solver. So
this is done twice, once logged into each account.

```bash
cloudflared tunnel login          # pick the account in the browser
cloudflared tunnel create athena-personal
```

That prints a UUID and writes `~/.cloudflared/<uuid>.json`. Repeat logged into
the other account as `athena-simplytics`.

Store each in 1Password, vault `Tooling - Athena`, as items
`cloudflare-tunnel-personal` and `cloudflare-tunnel-simplytics`, with two
fields each:

| Field | Value |
|---|---|
| `credentials-json` | the entire contents of `~/.cloudflared/<uuid>.json` |
| `tunnel-id` | the UUID |

Then delete the local JSON files — 1Password is the only copy that should
persist.

The UUID is in the Secret rather than in the repo so that one place describes a
tunnel; it is not itself sensitive, being the public CNAME target.

## 2. One-time: let the connectors come up

Merging `cluster/core/cloudflared/` gives four connector pods, two per tunnel.
Check both tunnels have registered before touching any DNS:

```bash
kubectl -n cloudflared get pods
cloudflared tunnel info athena-personal
```

Expect two connectors per tunnel. If a pod is crash-looping, read its log
before going further — a bad credentials field fails here, which is the cheap
place to find out.

The connectors talk to the Gateway on **port 8443**, a listener that sets no
hostname, which is what lets the tunnel config be a single rule instead of a
hostname list kept in step with `gateway.yaml` by hand.

Every public route also names `https-tunnel` in a second parentRef. That is
documentation and future-proofing, not a boundary: Cilium serves every route on
the Gateway through that listener regardless, internal ones included.

**So the deny rule is the only thing standing between a mistaken DNS record and
an exposed admin surface.** Treat it as load-bearing, and never assume the
routes are backing it up.

## 3. Cut one hostname over

Do `staging.<site>` before its production twin, always.

In the Cloudflare dashboard for the zone that owns the hostname, replace the
proxied **A** record with a proxied **CNAME** to `<uuid>.cfargotunnel.com`.
Keep the orange cloud on. Equivalent from the CLI:

```bash
cloudflared tunnel route dns athena-personal staging.<site>
```

Then verify the request actually traversed the tunnel rather than HAProxy:

```bash
curl -sS -D - -o /dev/null https://staging.<site>/
```

A 200 with `server: cloudflare` is necessary but not sufficient — both paths
produce that. Confirm the tunnel is carrying it by watching a connector:

```bash
kubectl -n cloudflared logs -l tunnel=personal --tail=20 -f
```

## 4. Prove it survives the failure it exists for

This is the whole point; do not skip it and do not do it for the first time on
a day that matters.

Disconnect the primary WAN for two minutes. Expect: the site keeps answering
200 within about 30 seconds, over the backup WAN, with **no DNS edit**. The
connectors log a reconnect. Restore the link and confirm nothing else changed.

Until this test has passed for one staging hostname, do not cut anything else
over.

## 5. Cut the rest

Repeat §3 per hostname, staging before production, one zone's tunnel per zone's
account. There is no config change for any of them — the single ingress rule
already covers whatever arrives.

**Nothing under `asn.casa` is ever tunnelled.** Not the admin surfaces, not
anything else in that zone. They are internal only because the edge proxy
refuses them from outside, and a tunnel record would go straight past that.

Do not rely on DNS to tell you which is which — it cannot. Both
`argocd.athena.asn.casa` and `rabalex.wedding` resolve to Cloudflare anycast,
while `kaff.asn.casa` publishes a private address. The zone is the rule.

The tunnel config denies the whole zone with a 404 rule ahead of the catch-all,
so a mistaken record fails closed rather than exposing a service — but the
record still should not exist.

The one hostname this costs you is `staging.kaff.asn.casa`, which is proxied
today and stays on the edge proxy, keeping the WAN-failover exposure until it
is renamed onto another domain.

## Rollback

Per hostname, change the CNAME back to the proxied A record. HAProxy has been
serving that path all along and is unchanged. Nothing in the cluster needs to
be touched, and the connectors can stay running.

## What to watch afterwards

`cloudflared_tunnel_ha_connections` is scraped from the connector pods
(`cluster/core/cloudflared/monitoring.yaml`). The number that matters is the
sum per tunnel: if it reaches zero while the sites still answer, the failover
path is gone and nothing else will say so. An alert on that belongs with the
rest of the rules in the private infrastructure repo.

The existing public-site probes (`vmprobe-public-sites.yaml`) need no change —
they probe the public hostname, which is exactly the path being altered.
