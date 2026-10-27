# observability (in-cluster remnant)

Only the **collectors** live here. The stores, the rule evaluator, the alert router and the UI
all run outside the cluster, on dedicated LXC containers on the hypervisor tier.

| in-cluster | where it sends |
|---|---|
| `vmagent` | `http://obs-metrics.infra.asn.casa:8428/api/v1/write` |
| `vector` (DaemonSet, every node, control plane included; also a once-a-minute heartbeat per node that the out-of-cluster per-node alert watches) | `http://obs-logs.infra.asn.casa:9428/insert/elasticsearch` |
| `kube-state-metrics`, `node-exporter` | scraped by vmagent |
| VictoriaMetrics operator | reconciles the `VMAgent` CR |
| `otel-collector` (one-replica StatefulSet) | traces: `http://obs-traces.infra.asn.casa:10481/insert/opentelemetry/v1/traces`; product events: `http://obs-traces.infra.asn.casa:9481/insert/opentelemetry/v1/logs` (both insert-only ports; product events are OTLP protobuf only) |

All three collectors buffer to disk, so a CT reboot costs nothing.

## The OpenTelemetry collector

`otel-collector-app.yaml`. A forwarder in the same sense as vmagent and Vector
(Placement Doctrine, rule 3): it runs here because its senders do, keeps no
history, buffers to a small node-local disk and ships outward. The stores live
on the `obs-traces` container on the hypervisor tier, beside the other
observability containers, so a trace outlives the cluster it describes.

What it carries, from Spritz first (staging only until Spritz's tracing phase 5):

- **Traces** from the Rails processes (and, later, browser spans relayed by the
  app), to VictoriaTraces. On the way it scrubs token-shaped URL segments and
  drops query strings, fragments and headers (a second layer behind the app's
  own scrubber), then tail-samples: every error, every trace over 1 s and every
  Temporal workflow are kept; of the rest, 100% on staging and 20% elsewhere;
  recurring noise (`/up` probes, tick jobs) at 1%.
- **Product events**: OTLP log records on the `spritz.analytics` scope, never
  sampled, to a second VictoriaLogs instance on the same container. Any other
  OTLP log is dropped. Application logs do **not** come through here; they
  stay on Vector to obs-logs, which lifts the `trace=` tag of each Rails log
  line into a `trace_id` field so logs and traces link both ways in Grafana.

Senders use `http://otel-collector.observability:4318` (OTLP/HTTP; `:4317` is
gRPC). The Service is ClusterIP: nothing outside the cluster can post to it.

**One replica, on purpose.** Tail sampling decides per trace, so all of a
trace's spans must land on one collector. More replicas need a trace-id-keyed
load-balancing tier in front first.

**Off switches**, finest first, all reversible ConfigMap or values edits:

1. An app stops sending: remove `OTEL_EXPORTER_OTLP_ENDPOINT` from its
   ConfigMap (or set `OTEL_SDK_DISABLED=true`) and restart it.
2. The collector stops: `replicaCount: 0` in `otel-collector-app.yaml`. The
   apps' exporters then fail to connect and drop spans after their own small,
   capped in-memory queue; no request waits on telemetry.

**When obs-traces is away**, spans and events queue on the collector's disk,
capped in bytes: 512 MiB of traces (about a day at the assumed staging rate)
and 128 MiB of product events (about three days), with up to ~1.25 GiB peak
on the node while bbolt compacts at start-up. Batches are retried for as long
as the queue has room. When it is full, new data is refused and counted in
`otelcol_exporter_enqueue_failed_*`. The collector's own metrics are scraped by
vmagent; the alerts on them (enqueue and send failures, queue fill, refused
data, the collector absent) are in the asn-infra observability rules
(`obs-traces.yaml`), evaluated outside this cluster.

**Turning a sender on is its own change.** `spritz-staging` carries
`OTEL_SERVICE_NAME` and the protocol, but not `OTEL_EXPORTER_OTLP_ENDPOINT`.
That one line lands in a separate PR once obs-traces is verified reachable by
its internal name: until the internal record exists the name resolves through
a public wildcard, and spans would leave for somewhere that is not the store.
The collector itself can run before that; with no senders it sends nothing.

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
