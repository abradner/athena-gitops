# observability (in-cluster remnant)

Only the **collectors** live here. The stores, the rule evaluator, the alert router and the UI
all run outside the cluster, on dedicated LXC containers on the hypervisor tier.

| in-cluster | where it sends |
|---|---|
| `vmagent` | `http://obs-metrics.infra.asn.casa:8428/api/v1/write` |
| `vector` (DaemonSet, all 7 nodes) | `http://obs-logs.infra.asn.casa:9428/insert/elasticsearch` |
| `kube-state-metrics`, `node-exporter` | scraped by vmagent |
| VictoriaMetrics operator | reconciles the `VMAgent` CR |

Both collectors buffer to disk, so a CT reboot costs nothing.

## Why the stateful half left

Observability that dies with the cluster is useless exactly when it is needed. During the
mid-July to 2026-08-21 outage there would have been no metrics, no logs and no alerts — and
Alertmanager, running inside the thing it monitored, would have gone silent without saying so.
`vmalert` went out with them specifically so `absent()` rules can fire when athena stops
reporting; a rule that only evaluates while the cluster is healthy cannot tell you it is not.

The stores' PVCs left the cluster with them. One deliberate exception remains: vmagent
runs `statefulMode` with a small PVC purely as a remote-write buffer, so a temporary
obs-metrics outage loses nothing. That buffer is disposable — losing it costs at most
the buffered window, never history.

## Where things are now

- Dashboards + alert rules: `asn-infra/observability/{dashboards,rules}`
- Runbook and as-built inventory: `asn-infra/observability/PROVISION.md`
- Alertmanager config: `asn-infra/observability/configs/alertmanager.yaml.tpl` (rendered with
  `op inject`, so no token is ever written to disk)
- UIs: behind the HAProxy front door on `*.infra.asn.casa`, over TLS. Names are
  in `asn-infra/observability/firewall/README.md`. The store ports listed here
  previously answered any LAN client with no credentials — a regression from
  moving observability out of the cluster, where HAProxy had gated the
  equivalent UIs. They are no longer reachable directly; use the front door,
  or Grafana Explore, which proxies both stores.

The `grafana.athena.asn.casa` / `logs.athena.asn.casa` HTTPRoutes were removed with this change —
those Services no longer exist. Neither hostname ever had a UniFi DNS record, so nothing that
worked stops working.
