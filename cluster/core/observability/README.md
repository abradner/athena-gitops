# observability (in-cluster remnant)

Only the **collectors** live here. The stores, the rule evaluator, the alert router and the UI
all run outside the cluster, on CTs `140-143` on `pve0/1/2`.

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

It also makes every worker genuinely stateless again: these were the only PVCs in the cluster.

## Where things are now

- Dashboards + alert rules: `asn-infra/observability/{dashboards,rules}`
- Runbook and as-built inventory: `asn-infra/observability/PROVISION.md`
- Alertmanager config: `asn-infra/observability/configs/alertmanager.yaml.tpl` (rendered with
  `op inject`, so no token is ever written to disk)
- UIs: `obs-grafana.infra.asn.casa:3000`, `obs-logs.infra.asn.casa:9428`,
  `obs-alerts.infra.asn.casa:9093`

The `grafana.athena.asn.casa` / `logs.athena.asn.casa` HTTPRoutes were removed with this change —
those Services no longer exist. Neither hostname ever had a UniFi DNS record, so nothing that
worked stops working.
