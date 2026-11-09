#!/usr/bin/env python3
"""zagreus node waker: power the amd64 runner nodes on and off, and drive the
ARC scale sets' maxRunners from the slots on the nodes that are awake.

README.md beside this file is the design. In short:

- maxRunners is what GitHub offers a scale set, whether or not its pods can
  schedule, so a set must claim nothing while its nodes sleep. The caps are
  always "base + slots of the awake, schedulable nodes", never above the
  ceiling in the set's Argo Application; except a set's floor, held while
  its nodes sleep (README, "Floor").
- Wake when a job a driven set could take has waited in GitHub's queue past
  the grace (shared labels: a classic runner would have taken it by now) or
  names a set outright (no classic runner can take it).
- Sleep a node after it has hosted nothing but DaemonSet pods for the idle
  timeout: lower the caps, cordon, re-check, tell the log alerts the node is
  asleep, then `talosctl shutdown` and confirm the power state through AMT.

Python standard library only, plus the talosctl binary (mounted from the
official image). One thread; one wake or sleep at a time.

Modes (config "mode"):
  observe  read everything, log what it would do, write nothing
  caps     also drive maxRunners; never power anything
  full     also wake and sleep nodes
"""

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

LOG = logging.getLogger("waker")

ANNOTATION_STATE = "zagreus.asn.casa/waker-state"
ANNOTATION_CORDON = "zagreus.asn.casa/waker-cordon"
# On exactly the nodes on the asleep list. A DaemonSet whose rollout Argo waits
# on (node-exporter) says "not on a node with this label", so the DaemonSet
# controller stops counting a sleeping node as one it must run on; otherwise
# its dead pod keeps the DaemonSet, and with it the whole zagreus-core sync,
# Progressing for as long as the node sleeps (README, "Asleep label").
LABEL_ASLEEP = "zagreus.asn.casa/asleep"
# Set just before `talosctl shutdown`. A Ready condition whose last transition
# is older than this is the API server's stale view of a node that is already
# going down (it keeps Ready for ~40 s after the kubelet stops), not a node
# that came back.
ANNOTATION_SLEPT_AT = "zagreus.asn.casa/waker-slept-at"
# States that leave a node off the asleep list (so its alert can fire) but
# still eligible for another wake once its backoff has passed.
RETRY_STATES = ("wake-failed", "sleep-unconfirmed", "on-not-ready")
# A stale Ready lasts until the node controller's grace runs out (~40 s).
# Still Ready this long after the waker's shutdown time, the node never went
# down (the waker stopped between stamping the time and shutting it down).
STALE_READY_LIMIT_S = 600
# The kubelet renews its node Lease every 10 s. A Lease not renewed for this
# long means the kubelet is gone (powered off, crashed, unplugged), whatever
# the Ready condition says: the node controller takes ~40 s to notice, and
# never does if kube-controller-manager is down. No fresh Lease, no slots.
# The renewal is timed by the API server's stamp on the kubelet's update
# (metadata.managedFields[].time), not spec.renewTime: renewTime is the
# worker's clock, which may be skewed (a box just woken, before NTP), while
# the stamp is the control plane's clock, the one the waker runs on.
LEASE_FRESH_S = 25
# A failed Lease read keeps the last good answer while the renewal it
# describes is under this old, so a transient API error does not flap the
# caps; past it, the node is not alive.
LEASE_HOLD_S = 60
SCALE_SET_LABEL = "actions.github.com/scale-set-name"
NODE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

MODES = ("observe", "caps", "full")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULTS = {
    "mode": "observe",
    "pollSeconds": 30,
    # Shared-label jobs: how long one may sit queued before it counts as
    # demand. An idle classic runner with the labels takes a job within
    # seconds, so a job still queued after this has no classic runner free.
    "saturationGraceSeconds": 60,
    # Jobs that name a scale set (runs-on: zagreus-ci) can only run on
    # zagreus: no classic runner carries a set's name.
    "namedGraceSeconds": 0,
    # Only runs created this recently are read (a forgotten queued run from
    # last week is not demand).
    "runLookbackHours": 72,
    "idleMinutes": 60,
    # Caps from each awake node's free room (true) or from its configured
    # slots alone (false). Room tracks every pod that comes and goes, and
    # every cap change restarts the set's listener, after which GitHub can
    # hold queued jobs (README, "Caps from slots").
    "roomCaps": True,
    "wakeCooldownSeconds": 180,
    # A job a set turned down for want of room is held about ten minutes
    # before GitHub offers it again, even once the set has room (26 Oct
    # 2026). One still queued this long beside spare slots is taken to be
    # held: it keeps the nodes that can run it awake.
    "heldAfterSeconds": 180,
    # ...and only for this long after that: GitHub offers it again in about
    # ten minutes, and a queued job that never dispatches (an orphan the
    # 72 h lookback still reads) must not keep nodes awake for days.
    "heldForSeconds": 900,
    # A job set aside as an orphan counts as demand again after this long:
    # if dispatch to the set was broken rather than the job abandoned, it
    # comes back (and can wake a node) once dispatch may have recovered.
    # A true orphan costs one wake and about an idle period per window.
    "orphanForSeconds": 21600,
    "listenerSettleSeconds": 15,
    "drainRecheckSeconds": 30,
    # Time between publishing "asleep" and shutting the node down, so the
    # line has reached the log store before the node's heartbeats stop:
    # kubelet refreshes the mounted ConfigMap within about a minute, the
    # heartbeat pod writes once a minute, the recording rule runs once a
    # minute. The alert needs 150 s of silence plus its 1 m `for`.
    "assertLeadSeconds": 180,
    # The heartbeat pod stops asserting "asleep" when the waker has not
    # renewed it for this long, so a dead waker makes sleeping nodes alert.
    "assertTtlSeconds": 300,
    # GitHub unreadable this long: the waker can no longer see demand, so
    # it stops vouching for sleeping nodes (they alert) rather than leave
    # them asleep and silent while jobs that name a set wait for ever.
    "demandBlindSeconds": 900,
    "powerOnTimeoutSeconds": 90,
    "readyTimeoutSeconds": 600,
    "heartbeatPodTimeoutSeconds": 120,
    "offTimeoutSeconds": 300,
    "failureBackoffSeconds": 1800,
    "githubApi": "https://api.github.com",
    "sets": [],
    "nodes": [],
    "asleepConfigMap": {"namespace": "log-heartbeat", "name": "waker-asleep"},
    "eventsConfigMap": {"namespace": "zagreus-waker", "name": "waker-events"},
    # The queue-age gauges, as a node_exporter textfile: the node exporter
    # (core/node-power-exporter.yaml) mounts this ConfigMap, and the metrics
    # store already scrapes it. See "Queue watchdog" in README.md.
    "metricsConfigMap": {"namespace": "node-power", "name": "waker-metrics"},
    "heartbeat": {"namespace": "log-heartbeat", "label": "app=kmsg-heartbeat"},
    "applicationsNamespace": "argocd",
    "evictableNamespaces": ["kube-system"],
    # Taint keys the runner pods tolerate (none do today). Any other
    # NoSchedule or NoExecute taint takes a node out of the caps.
    "toleratedTaintKeys": [],
    # GitHub Apps whose installation tokens read a set's queue (a set's
    # "github"), by name: {"name": {"dir": path}}. The directory holds the
    # App's Secret as ARC's own (github_app_id, github_app_installation_id,
    # github_app_private_key). "default" is the GITHUB_TOKEN.
    "githubApps": {},
    # A set sharing a pool keeps, for this long, as many slots as it last
    # used, while no other set's queued jobs want them: a run's next job
    # then finds the slot already offered instead of restarting both sets'
    # listeners (README, "Sharing").
    "shareHoldSeconds": 600,
}


def load_config(path):
    with open(path) as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    if cfg["mode"] not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {cfg['mode']!r}")
    names = [s["name"] for s in cfg["sets"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate scale set in sets")
    by_name = {s["name"]: s for s in cfg["sets"]}
    credentials = {"default"} | set(cfg.get("githubApps") or {})
    for s in cfg["sets"]:
        for key in ("name", "namespace", "application"):
            if not s.get(key):
                raise ValueError(f"set {s!r} needs {key}")
        # The queue it reads: one repository, or every repository of an org
        # (an org-level scale set serves them all).
        if bool(s.get("repo")) == bool(s.get("org")):
            raise ValueError(f"set {s['name']}: needs exactly one of repo and org")
        if s.get("github", "default") not in credentials:
            raise ValueError(f"set {s['name']}: unknown github credential {s.get('github')!r}")
        if int(s.get("base", 0)) < 0:
            raise ValueError(f"set {s['name']}: base must be >= 0")
        if int(s.get("floor", 0)) < 0:
            raise ValueError(f"set {s['name']}: floor must be >= 0")
        shared = s.get("sharesSlotsOf")
        if shared is not None:
            if shared == s["name"] or shared not in by_name or by_name[shared].get("sharesSlotsOf"):
                raise ValueError(f"set {s['name']}: sharesSlotsOf must name another set that shares no one's")
    for key, members in shared_pools(cfg).items():
        for m in members:
            # A pin is a promise about where one set's pods land; a pool's
            # slots may be handed to any of its sets. (A floor is fine: it
            # is a claim on top of the room, not room, README "Floor".)
            if by_name[m].get("pinnedToSlots"):
                raise ValueError(f"set {m}: no pinnedToSlots in a shared pool")
    nodes = [n["name"] for n in cfg["nodes"]]
    if len(set(nodes)) != len(nodes):
        raise ValueError("duplicate node in nodes")
    for n in cfg["nodes"]:
        if not NODE_NAME_RE.match(n["name"]):
            raise ValueError(f"node name {n['name']!r} is not a DNS label")
        for set_name, slots in n.get("slots", {}).items():
            if set_name not in names:
                raise ValueError(f"node {n['name']}: slots for unknown set {set_name}")
            if by_name[set_name].get("sharesSlotsOf"):
                raise ValueError(f"node {n['name']}: {set_name} uses {by_name[set_name]['sharesSlotsOf']}'s "
                                 "slots and has none of its own")
            if int(slots) < 0:
                raise ValueError(f"node {n['name']}: negative slots for {set_name}")
        if not isinstance(n.get("alwaysOn", False), bool):
            raise ValueError(f"node {n['name']}: alwaysOn must be true or false")


def always_on(cfg, name):
    """The node is always on (README, "Always-on node"): the waker counts its
    slots whenever it is Ready, and never powers, cordons or sleeps it."""
    return any(n["name"] == name and n.get("alwaysOn") is True for n in cfg["nodes"])


def unwakeable_nodes(cfg, wakeable):
    """Configured nodes the waker could not power on again, so will never put
    to sleep: worth an error at start. An always-on node is never slept by
    design, so it is not one of them."""
    return sorted(n["name"] for n in cfg["nodes"] if not n.get("alwaysOn") and n["name"] not in wakeable)


def slot_key(s):
    """The node slots a set draws on: its own, or (sharesSlotsOf) another
    set's, shared with it (README, "Sharing")."""
    return s.get("sharesSlotsOf") or s["name"]


def node_slots(cfg, node, set_name):
    """The node's configured slots a set may use. For a set in a shared
    pool, the pool's slots, which its other sets may also be using."""
    s = next((x for x in cfg["sets"] if x["name"] == set_name), None)
    return int(node.get("slots", {}).get(slot_key(s) if s else set_name, 0))


def shared_pools(cfg):
    """{slot key: [set names, waker.conf order]} for every pool of slots
    that more than one set draws on."""
    pools = {}
    for s in cfg["sets"]:
        pools.setdefault(slot_key(s), []).append(s["name"])
    return {k: v for k, v in pools.items() if len(v) > 1}


def owns(s, job):
    """The job is in the set's queue: its repository, or any repository of
    its org."""
    repo = job["repo"].lower()
    if s.get("org"):
        return repo.split("/", 1)[0] == s["org"].lower()
    return repo == s["repo"].lower()


def share_need(cfg, demand, unstarted):
    """Per set in a shared pool: queued jobs it has not claimed yet, the
    slots it would take now. A claimed job's runner already holds its slot
    (compute_caps counts it as the set's own)."""
    pooled = {m for members in shared_pools(cfg).values() for m in members}
    return {name: max(0, d - min(unstarted.get(name, 0), d))
            for name, d in demand.items() if name in pooled}


# --------------------------------------------------------------------------
# Pure decisions (unit-tested in tests/test_waker.py)
# --------------------------------------------------------------------------

def job_match(job_labels, set_name, set_labels):
    """(matches, by_name) for one queued job against one scale set.

    GitHub hands a job to a runner that carries every one of its labels. A
    scale set carries its labels plus its own name. by_name is True when the
    job asks for the set's name, which no classic runner carries."""
    want = {l.lower() for l in job_labels}
    have = {l.lower() for l in set_labels} | {set_name.lower()}
    if not want or not want <= have:
        return False, False
    return True, set_name.lower() in want


def parse_time(value):
    """GitHub's ISO 8601 (with Z) to an aware datetime."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def first_set(cfg, job, set_labels):
    """The set a job counts for: the first in waker.conf order it matches."""
    for s in cfg["sets"]:
        if owns(s, job) and job_match(job["labels"], s["name"], set_labels.get(s["name"], []))[0]:
            return s["name"]
    return None


def demand_by_set(cfg, jobs, set_labels, now, max_age=None, alts=None):
    """Queued jobs per driven set that have waited past their grace (and,
    with max_age, no longer than that). Each job counts once, for the first
    set in waker.conf order that it matches. alts, when given, is filled
    with {(counted set, other set it matches): jobs}, for share_overlap.

    jobs: dicts with repo, labels, created_at (aware datetime)."""
    out = {s["name"]: 0 for s in cfg["sets"]}
    for job in jobs:
        age = (now - job["created_at"]).total_seconds()
        if max_age is not None and age >= max_age:
            continue
        for s in cfg["sets"]:
            if not owns(s, job):
                continue
            ok, by_name = job_match(job["labels"], s["name"], set_labels.get(s["name"], []))
            if not ok:
                continue
            grace = cfg["namedGraceSeconds"] if by_name else cfg["saturationGraceSeconds"]
            if age >= grace:
                out[s["name"]] += 1
                if alts is not None:
                    for o in cfg["sets"]:
                        if o["name"] != s["name"] and owns(o, job) and \
                                job_match(job["labels"], o["name"], set_labels.get(o["name"], []))[0]:
                            key = (s["name"], o["name"])
                            alts[key] = alts.get(key, 0) + 1
            # One job runs once: it counts for the first set it matches, in
            # waker.conf order. Counted for every match, a spritz-ci job
            # would be demand on both zagreus-ci and zagreus-ci-x64 once x64
            # carries the class labels (26 Oct 2026).
            break
    return out


QUEUE_METRIC_FILE = "waker.prom"


def oldest_queued_by_set(cfg, jobs, set_labels, now):
    """{set: {"jobs": n, "oldest": seconds}} for every driven set: how many
    jobs GitHub still lists as queued for it, and how long the oldest has
    waited. No grace and no orphan filter: this is the raw wait a person
    sees, and a job the waker has set aside as an orphan is exactly the one
    that is stuck. A job counts for the first set it matches (first_set), as
    in demand_by_set."""
    out = {s["name"]: {"jobs": 0, "oldest": 0.0} for s in cfg["sets"]}
    for job in jobs:
        name = first_set(cfg, job, set_labels)
        if name is None:
            continue
        age = max(0.0, (now - job["created_at"]).total_seconds())
        out[name]["jobs"] += 1
        out[name]["oldest"] = max(out[name]["oldest"], age)
    return out


def render_queue_metrics(stats, sampled_at):
    """Prometheus text exposition (a node_exporter textfile) for
    oldest_queued_by_set. The sample time is its own gauge: the file is only
    rewritten when GitHub was readable, so a frozen file shows as a stale
    sample time rather than as a calm queue. Set names come from waker.conf
    and are DNS-like; no escaping is needed."""
    lines = [
        "# HELP zagreus_runner_oldest_queued_seconds Age of the oldest job GitHub lists as queued for the scale set (0 when none).",
        "# TYPE zagreus_runner_oldest_queued_seconds gauge",
    ]
    lines += [f'zagreus_runner_oldest_queued_seconds{{set="{n}"}} {int(v["oldest"])}'
              for n, v in sorted(stats.items())]
    lines += [
        "# HELP zagreus_runner_queued_jobs Jobs GitHub lists as queued for the scale set.",
        "# TYPE zagreus_runner_queued_jobs gauge",
    ]
    lines += [f'zagreus_runner_queued_jobs{{set="{n}"}} {v["jobs"]}' for n, v in sorted(stats.items())]
    lines += [
        "# HELP zagreus_runner_queue_sampled_timestamp_seconds When the waker last read GitHub's queue (epoch seconds).",
        "# TYPE zagreus_runner_queue_sampled_timestamp_seconds gauge",
        f"zagreus_runner_queue_sampled_timestamp_seconds {int(sampled_at)}",
    ]
    return "\n".join(lines) + "\n"


SLOT_METRIC_FILE = "slots.prom"
NODE_STATES = ("awake", "asleep", "cordoned", "not_ready", "unknown")


def is_runner_pod(pod):
    """A runner pod, owned by an EphemeralRunner. Not the set's listener,
    which carries the same scale-set label but runs on the control plane
    (owned by an AutoscalingListener) and holds no job."""
    return any(o.get("kind") == "EphemeralRunner"
               for o in (pod.get("metadata", {}).get("ownerReferences") or []))


def node_state_name(st):
    """One word for a listed node's state, for the slot metrics. None (the
    node could not be read this pass) is "unknown", never "awake"."""
    if st is None:
        return "unknown"
    if st["asleep"]:
        return "asleep"
    if not st["ready"]:
        return "not_ready"
    if st["unschedulable"]:
        return "cordoned"
    return "awake"


def _label(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def slot_usage(cfg, states, pods_by_node, offered, runner_jobs):
    """What the slot metrics say, as data (render_slot_metrics writes it).

    used: runner pods (any scale set, light ones included) on each node,
    finished ones left out. Every node that has any pod at all gets a row
    for every set seen anywhere plus the driven sets, so zero is written as
    zero and a missing row means "not known", not "none".
    offered: per listed node and driven set, what the waker would offer now
    (compute_caps per_node, own runners included); 0 on a node that is not
    awake. A listed node that could not be read gets no row.
    jobs: one row per runner pod whose EphemeralRunner names a job."""
    driven = [s["name"] for s in cfg["sets"]]
    listed = [n["name"] for n in cfg["nodes"]]
    used = {}
    for node, pods in pods_by_node.items():
        if not node:
            continue
        counts = used.setdefault(node, {})
        for p in pods:
            set_name = (p.get("metadata", {}).get("labels") or {}).get(SCALE_SET_LABEL)
            if set_name and is_runner_pod(p) and p.get("status", {}).get("phase") not in ("Succeeded", "Failed"):
                counts[set_name] = counts.get(set_name, 0) + 1
    for node in listed:
        if states.get(node) is not None:
            used.setdefault(node, {})
    sets_seen = sorted(set(driven) | {s for c in used.values() for s in c})
    used = {node: {s: c.get(s, 0) for s in sets_seen} for node, c in used.items()}
    state = {node: node_state_name(states.get(node)) for node in listed}
    # A set sharing a pool shows the pool's slots: the same slots as its
    # other sets, not more.
    configured = {n["name"]: {s: node_slots(cfg, n, s) for s in driven} for n in cfg["nodes"]}
    offer = {node: {s: (offered.get(node, {}).get(s, 0) if state[node] == "awake" else 0) for s in driven}
             for node in listed if state[node] != "unknown"}
    jobs = []
    for node, pods in pods_by_node.items():
        for p in pods:
            name = p.get("metadata", {}).get("name")
            job = runner_jobs.get(name)
            if node and job and p.get("status", {}).get("phase") not in ("Succeeded", "Failed"):
                jobs.append(dict(job, node=node, runner=name))
    return {"used": used, "state": state, "configured": configured, "offered": offer,
            "jobs": sorted(jobs, key=lambda j: (j["node"], j["runner"]))}


def render_slot_metrics(usage, sampled_at):
    """Prometheus text for slot_usage. The node is runner_node, because the
    scrape already sets node (the exporter's own host, and every awake node
    exports this same file). The sample time ages if the waker stops."""
    lines = ["# HELP zagreus_runner_node_state The waker's view of a node it manages: 1 for its current state.",
             "# TYPE zagreus_runner_node_state gauge"]
    for node, st in sorted(usage["state"].items()):
        lines += [f'zagreus_runner_node_state{{runner_node="{node}",state="{k}"}} {int(k == st)}'
                  for k in NODE_STATES]
    lines += ["# HELP zagreus_runner_slots_used Runner pods of the scale set on the node (finished ones left out).",
              "# TYPE zagreus_runner_slots_used gauge"]
    for node, c in sorted(usage["used"].items()):
        lines += [f'zagreus_runner_slots_used{{runner_node="{node}",set="{s}"}} {k}' for s, k in sorted(c.items())]
    lines += ["# HELP zagreus_runner_slots_offered Slots the waker would offer the set on the node now, its running runners included (0 unless awake).",
              "# TYPE zagreus_runner_slots_offered gauge"]
    for node, c in sorted(usage["offered"].items()):
        lines += [f'zagreus_runner_slots_offered{{runner_node="{node}",set="{s}"}} {k}' for s, k in sorted(c.items())]
    lines += ["# HELP zagreus_runner_slots_configured The node's slots for the set in waker.conf.",
              "# TYPE zagreus_runner_slots_configured gauge"]
    for node, c in sorted(usage["configured"].items()):
        lines += [f'zagreus_runner_slots_configured{{runner_node="{node}",set="{s}"}} {k}' for s, k in sorted(c.items())]
    lines += ["# HELP zagreus_runner_job_info 1 for each runner pod running a job: which job is in which slot.",
              "# TYPE zagreus_runner_job_info gauge"]
    for j in usage["jobs"]:
        lines.append('zagreus_runner_job_info{runner_node="%s",set="%s",runner="%s",repo="%s",workflow_run="%s",job="%s"} 1'
                     % tuple(_label(j[k]) for k in ("node", "set", "runner", "repo", "workflow_run", "job")))
    lines += ["# HELP zagreus_runner_slots_sampled_timestamp_seconds When the waker last wrote the slot metrics (epoch seconds).",
              "# TYPE zagreus_runner_slots_sampled_timestamp_seconds gauge",
              f"zagreus_runner_slots_sampled_timestamp_seconds {int(sampled_at)}"]
    return "\n".join(lines) + "\n"


def unmet_by_set(demand, live_caps, runners, unstarted):
    """Demand a set cannot absorb with the caps it already has.

    runners: EphemeralRunners the set has (claimed or running).
    unstarted: those not running a job yet. Each stands for a job the set has
    claimed that GitHub still reports as queued, so it is already in demand
    and must not be counted twice.
    spare: claims the set can still make; GitHub fills it within seconds."""
    out = {}
    for name, d in demand.items():
        claimed = min(unstarted.get(name, 0), d)
        cap = live_caps.get(name, 0)
        if cap is None:  # no maxRunners on the set: ARC reads that as unlimited
            out[name] = 0
            continue
        spare = max(0, cap - runners.get(name, 0))
        out[name] = max(0, d - claimed - spare)
    return out


def share_overlap(unmet, demand, alts, live_caps, runners, unstarted):
    """Unmet demand a set with overlapping labels can still absorb. A job
    counted for one set (demand_by_set) may be claimed by, or offered to,
    another set it also matches: that set's claims beyond its own demand,
    and its spare beyond its own unmet, cancel the first set's unmet for
    those jobs (Codex on 346d1b43)."""
    out = dict(unmet)
    left = {}
    for name, d in demand.items():
        cap = live_caps.get(name, 0)
        own_claims = min(unstarted.get(name, 0), d)
        surplus_claims = max(0, unstarted.get(name, 0) - d)
        spare = 0 if cap is None else max(0, cap - runners.get(name, 0))
        spare_left = max(0, spare - max(0, d - own_claims))
        left[name] = surplus_claims + spare_left
    for (counted, other), n in sorted(alts.items()):
        take = min(out.get(counted, 0), n, left.get(other, 0))
        if take:
            out[counted] -= take
            left[other] -= take
    return out


def node_awake(node_state):
    """Counts towards the caps: Ready, schedulable, and not declared asleep."""
    return node_state["ready"] and not node_state["unschedulable"] and not node_state["asleep"]


def parse_qty(value):
    """A Kubernetes quantity to a float (bytes, or cores for cpu)."""
    v = str(value).strip()
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
             "n": 1e-9, "u": 1e-6, "m": 1e-3,
             "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
    for suffix in sorted(units, key=len, reverse=True):
        if v.endswith(suffix):
            return float(v[:-len(suffix)]) * units[suffix]
    return float(v)


def pod_requests(spec):
    """(cpu cores, memory bytes) the scheduler reserves for a pod spec:
    containers plus native sidecars (init containers with restartPolicy
    Always, such as the builder's dind), or the largest plain init
    container if that is bigger."""
    def req(c, key):
        return parse_qty(((c.get("resources") or {}).get("requests") or {}).get(key, "0"))
    out = []
    for key in ("cpu", "memory"):
        sidecars = sum(req(c, key) for c in spec.get("initContainers") or []
                       if c.get("restartPolicy") == "Always")
        main = sum(req(c, key) for c in spec.get("containers") or []) + sidecars
        plain = max([req(c, key) for c in spec.get("initContainers") or []
                     if c.get("restartPolicy") != "Always"] or [0])
        out.append(max(main, plain))
    return tuple(out)


# Taints the node controller puts on a node that is down; the kubelet's
# first Ready lifts them. Not held against a sleeping node's room.
DOWN_TAINTS = ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready")
# Taints a node carries while it is off that its boot lifts as well: Cilium
# marks a node whose agent is not running (seen on every sleeping node,
# 6 Nov 2026). The waker's own cordon (node.kubernetes.io/unschedulable)
# comes off at the wake too, but only that cordon: _room_if_awake.
BOOT_TAINTS = DOWN_TAINTS + ("node.cilium.io/agent-not-ready",)
CORDON_TAINT = "node.kubernetes.io/unschedulable"


def untolerated_taints(node, tolerated_keys=()):
    """Every NoSchedule or NoExecute taint key the runner pods do not
    tolerate."""
    return [t.get("key") for t in (node.get("spec") or {}).get("taints") or []
            if t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") not in tolerated_keys]


def untolerated_taint(node, tolerated_keys=()):
    """The first NoSchedule or NoExecute taint the runner pods do not
    tolerate (they tolerate none), or None. PreferNoSchedule does not stop
    scheduling."""
    for t in (node.get("spec") or {}).get("taints") or []:
        if t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") not in tolerated_keys:
            return t.get("key")
    return None


def compute_caps(cfg, node_states, set_requests=None, unplaced=None, per_node=None,
                 want=None, recent=None, turn=None, ceilings=None):
    """base + what each awake node can still take, per driven set (no
    ceiling, but see ceilings below).

    With set_requests (each set's pod requests) and each node's free room
    (allocatable minus every pod already on it), a node offers a set no more
    than its configured slots, less the set's pods already there, and no
    more than still fits beside whatever else runs there, another set's
    runner included. A set's runners on an awake node count towards its cap:
    they hold claims already.

    unplaced (per set): runners already claimed whose pod is not bound to a
    node yet. Each will need its requests somewhere, so before anything is
    offered it is placed, on paper, on an awake node it fits (one where its
    set has slots first): it then counts as that set's own runner there and
    its room is taken. One that fits nowhere adds nothing: a cap raised for
    it would point at room that does not exist.

    Sets are filled in waker.conf order, so the builder (listed first: it
    preempts CI pods) gets its room before CI does. Without set_requests:
    the configured slots, as written.

    Shared pools (sharesSlotsOf; README, "Sharing"): the sets of a pool draw
    on one set of slots per node, so together they are never offered more
    than its slots (and its room). Where the pool's first set falls in
    waker.conf order, with want given:
      1. want (per pool set: the slots it wants in all, those it holds plus
         its queued jobs not yet claimed) is served one slot at a time, up
         to that total, each to the set holding the fewest slots so far; a
         tie to the set whose oldest queued job is oldest (turn: its age in
         seconds), then to waker.conf order;
      2. recent (per pool set: the most slots it wanted in all lately) the
         same way, after every queued job.
         Both are totals, not increments, so a runner that appears or ends
         between the demand read and this call moves a slot from queued to
         held without changing what the set is offered (5 Nov, live: an
         increment read against a newer runner count flipped both caps);
      3. whatever is left goes to each pool set at its own place in
         waker.conf order, as for any set. So with no other set's demand,
         the pool's first set gets what it would have had alone.
    ceilings (shared pool sets only): a pool set is offered nothing past
    its ceiling, so a slot it could not use goes to the others.

    per_node: a dict to fill with what each awake node offers each set
    ({node: {set: n}}, its own runners included); the caps are its sums
    plus base. For the slot metrics only: the caps never read it."""
    caps = {s["name"]: int(s.get("base", 0)) for s in cfg["sets"]}
    pools = shared_pools(cfg)
    pooled = {name: key for key, names in pools.items() for name in names}
    rank = {s["name"]: i for i, s in enumerate(cfg["sets"])}
    by_name = {s["name"]: s for s in cfg["sets"]}
    # [node config, own counts, [free cpu, free mem] or None (slots alone)]
    entries = []
    for n in cfg["nodes"]:
        st = node_states.get(n["name"])
        if st is None or not node_awake(st):
            # Its runners, if any, are not counted: on a node that is gone
            # they may be ghosts the API has not cleaned up, and ARC never
            # stops a busy runner because its set's cap went down.
            continue
        if set_requests is None or "free" not in st:
            # Slots alone: offered as configured whatever runs there, except
            # in a shared pool, where one set's runners hold the slots the
            # others would be offered.
            entries.append([n, {k: v for k, v in (st.get("own") or {}).items() if k in pooled}, None])
        else:
            entries.append([n, dict(st.get("own") or {}), list(st["free"])])

    def fits(free, req, k=1):
        if free is None:
            return max(0, k)
        cpu, mem = req
        if cpu == float("inf") or mem == float("inf"):
            return 0
        n = k
        if cpu > 0:
            n = min(n, int(max(0.0, free[0]) // cpu))
        if mem > 0:
            n = min(n, int(max(0.0, free[1]) // mem))
        return max(0, n)

    def req_of(name):
        return (set_requests or {}).get(name, (0, 0))

    if set_requests is not None:
        # Claimed, unbound runners first: they hold room already.
        room_entries = [e for e in entries if e[2] is not None]
        for s in cfg["sets"]:
            name = s["name"]
            req = req_of(name)
            for _ in range(int((unplaced or {}).get(name, 0))):
                order = sorted(room_entries, key=lambda x: -node_slots(cfg, x[0], name))
                # A pinned set's pod binds only where it has slots (Codex on
                # #338: placed on 02, it hid the need to wake 01).
                spot = next((x for x in order if fits(x[2], req) and not (
                    pinned_to_slots(s) and node_slots(cfg, x[0], name) <= 0)), None)
                if spot is None:
                    # Fits nowhere: offering it a slot could only strand it.
                    continue
                spot[1][name] = spot[1].get(name, 0) + 1
                spot[2][0] -= req[0]
                spot[2][1] -= req[1]

    given = {}  # (node, set) -> slots offered on top of its own runners
    for n, own, _ in entries:
        for name in caps:
            caps[name] += own.get(name, 0)
            if per_node is not None:
                per_node.setdefault(n["name"], {})[name] = own.get(name, 0)

    def slots_left(e, name):
        n, own = e[0], e[1]
        members = pools.get(pooled.get(name), [name])
        used = sum(own.get(m, 0) + given.get((n["name"], m), 0) for m in members)
        return node_slots(cfg, n, name) - used

    def offer(e, name, k):
        k = min(k, slots_left(e, name))
        if name in capped:
            k = min(k, capped[name] - caps[name])
        if name in pooled and ceilings and ceilings.get(name) is not None:
            k = min(k, ceilings[name] - caps[name])
        if k <= 0:
            return 0
        req = req_of(name)
        fit = fits(e[2], req, k)
        if fit:
            if e[2] is not None:
                e[2][0] -= fit * req[0]
                e[2][1] -= fit * req[1]
            key = (e[0]["name"], name)
            given[key] = given.get(key, 0) + fit
            caps[name] += fit
            if per_node is not None:
                per_node[e[0]["name"]][name] += fit
        return fit

    def share(members, totals):
        """Serve each set up to its total, one slot at a time; return what
        could not be served."""
        want = {m: int(totals.get(m, 0)) - (caps[m] - int(by_name[m].get("base", 0))) for m in members}
        want = {m: n for m, n in want.items() if n > 0}
        short = {}
        while want:
            m = min(want, key=lambda x: (caps[x], -float((turn or {}).get(x, 0)), rank[x]))
            if not any(offer(e, m, 1) for e in entries):
                short[m] = want.pop(m)  # nowhere left for this set; the others may still fit
                continue
            want[m] -= 1
            if want[m] <= 0:
                del want[m]
        return short

    capped = {}  # pool set -> the most it may be offered this pass (fair_cap)

    def fair_cap(members, short):
        """A set whose queued jobs found no free slot, holding less than
        its fair share of the pool, would wait for ever behind a busy
        neighbour: ARC hands the neighbour's freed slot to its next queued
        job within seconds, long before the next pass sees it free. So the
        neighbour's cap drops to its own fair share, below the runners it
        has if need be (ARC never stops a busy runner for that). It claims
        nothing more until it is under its share; the slot its next
        finished job frees is then free at the next pass, and the
        sharing rule hands it to the starved set.

        Fair share: the pool's slots now held, split one at a time to the
        set holding fewest that still wants one (wanting: what it holds
        plus its unserved jobs, up to its ceiling), a tie to the oldest
        queue, then waker.conf order."""
        total = sum(caps[m] - int(by_name[m].get("base", 0)) for m in members)
        wants = {}
        for m in members:
            held = caps[m] - int(by_name[m].get("base", 0))
            w = held + short.get(m, 0)
            if m in pooled and ceilings and ceilings.get(m) is not None:
                w = min(w, ceilings[m] - int(by_name[m].get("base", 0)))
            wants[m] = max(0, w)
        fair = dict.fromkeys(members, 0)
        for _ in range(total):
            open_ = [m for m in members if fair[m] < wants[m]]
            if not open_:
                break
            m = min(open_, key=lambda x: (fair[x], -float((turn or {}).get(x, 0)), rank[x]))
            fair[m] += 1
        starved = [m for m in members if short.get(m) and caps[m] - int(by_name[m].get("base", 0)) < fair[m]]
        if not starved:
            return
        for m in members:
            base = int(by_name[m].get("base", 0))
            excess = caps[m] - base - fair[m]
            if excess <= 0:
                continue
            LOG.info("%s held to %s of the shared slots so %s gets its share", m, fair[m], ", ".join(starved))
            caps[m] -= excess
            capped[m] = caps[m]
            if per_node is not None:
                for e in reversed(entries):
                    cut = min(excess, per_node[e[0]["name"]][m])
                    per_node[e[0]["name"]][m] -= cut
                    excess -= cut

    served = set()
    for s in cfg["sets"]:
        name = s["name"]
        key = pooled.get(name)
        if key is not None and key not in served and (want or recent):
            served.add(key)
            short = share(pools[key], want or {})
            if short:
                fair_cap(pools[key], short)
            share(pools[key], recent or {})
        for e in entries:
            offer(e, name, slots_left(e, name))
    return caps


def bound_caps(wanted, live, ceilings):
    """Apply each set's ceiling (the caller passes the last one it read
    when the Application is unreadable now). With no ceiling ever read,
    only lower: over-offering strands jobs, under-offering only sends them
    to the classic runners, and the cap comes back up once a ceiling can be
    read."""
    out = {}
    for name, want in wanted.items():
        ceiling = ceilings.get(name)
        if ceiling is not None:
            out[name] = min(want, ceiling)
        elif name in live and live[name] is not None:
            out[name] = min(want, live[name])
        elif name in live:
            # The set has no maxRunners at all, which ARC reads as unlimited:
            # the slot sum is the only bound there is, so write it.
            out[name] = want
        else:
            out[name] = None
    return out


def pinned_to_slots(s):
    """The set's pod template keeps its pods on the nodes that have slots
    for it (waker.conf "pinnedToSlots", checked against the template by
    tests/test_waker.py), so an awake node without them is no risk."""
    return bool(s.get("pinnedToSlots"))


def floor_caps(cfg, target, live, ceilings, node_states=None):
    """Lift each set to its floor: the least maxRunners the waker writes,
    offered while its nodes sleep (README, "Floor"). Never past the
    ceiling; and with no ceiling ever read, never past what is live
    (bound_caps' rule: only lower until a ceiling can be read). A set with
    neither (no maxRunners live, ARC's unlimited, and no ceiling read) gets
    at least the floor, beside the slot sum bound_caps writes for it.

    Not while an awake node has no slots for the set: the runner pods'
    nodeSelector takes every amd64 node, so a claim with no room on its own
    nodes would land there (and the builder, at ci-release priority, would
    preempt CI to do it). That is only 01 down with 02 or 03 up.

    The floor is not room: unmet, spare and orphans read spare_caps, which
    takes it back off."""
    out = dict(target)
    awake = [n for n in cfg["nodes"]
             if node_states and n["name"] in node_states and node_awake(node_states[n["name"]])]
    for s in cfg["sets"]:
        name, floor = s["name"], int(s.get("floor", 0))
        want = out.get(name)
        if floor <= 0 or want is None:
            continue
        if not pinned_to_slots(s) and any(node_slots(cfg, n, name) <= 0 for n in awake):
            continue
        bound = ceilings.get(name)
        if bound is None:
            bound = live.get(name)
        out[name] = max(want, floor if bound is None else min(floor, bound))
    return out


def spare_caps(cfg, live, room):
    """The caps unmet, spare and orphans may count on: live, but for a set
    with a floor no more than the room its awake nodes give (room: the caps
    before the floor). The floor slot of a set whose nodes sleep holds a
    claim's pod on no node: counted as spare it would hide that set's
    queued jobs from the wake, and set them aside as orphans."""
    out = dict(live)
    for s in cfg["sets"]:
        name = s["name"]
        if int(s.get("floor", 0)) > 0 and out.get(name) is not None and room.get(name) is not None:
            out[name] = min(out[name], room[name])
    return out


def floor_claims(cfg, runners, unplaced, room):
    """Claims of a floored set beyond the room its awake nodes give, that
    have no node yet: each holds a job that only the floor let the set
    claim, so it needs a node woken now, not after the stuck grace."""
    out = {}
    for s in cfg["sets"]:
        name = s["name"]
        if int(s.get("floor", 0)) <= 0 or room.get(name) is None:
            continue
        n = min(unplaced.get(name, 0), max(0, runners.get(name, 0) - room[name]))
        if n:
            out[name] = n
    return out


def choose_wakes(cfg, unmet, node_states, backoff_until, now_s, room=None):
    """The fewest sleeping nodes whose room covers every set's unmet demand
    (Alex, 6 Nov 2026: "nodes should be only be woken by unmet demand").

    Chosen one at a time: each time, the node that covers the most of the
    demand still unmet, given the ones already chosen; a tie to waker.conf
    order, the order nodes are preferred in (and slept in reverse). So a
    node with more slots is taken first exactly when that saves a node.
    It stops when the demand is covered, or when no node left would add
    any room for it. Only if not one node would add room (a taint the
    waker does not know lifts at boot, say) does it fall back to the first
    eligible node: one, logged, rather than strand the jobs.

    All of them wake together and the caps rise once: every maxRunners
    change restarts the set's listener, and a job turned down for want of
    room is held about ten minutes before it is offered again, so raising
    0 -> 1 -> 3 node by node strands the second job (26 Oct 2026).

    room(nodes): the caps those nodes would add, per set, once awake (the
    caller's compute_caps, so sets sharing a node share its room). Without
    it, each node's configured slots."""
    if room is None:
        def room(nodes):
            out = {}
            for n in cfg["nodes"]:
                if n["name"] in nodes:
                    for s in cfg["sets"]:
                        out[s["name"]] = out.get(s["name"], 0) + node_slots(cfg, n, s["name"])
            return out
    want = {name: u for name, u in unmet.items() if u > 0}
    rank = {n["name"]: i for i, n in enumerate(cfg["nodes"])}
    eligible = []
    for n in cfg["nodes"]:
        st = node_states.get(n["name"])
        if st is None or always_on(cfg, n["name"]):
            continue
        # Listed asleep, or left off the list after a failure and not Ready:
        # retried once its backoff has passed.
        if not (st["asleep"] or (st.get("retry") and not st["ready"])):
            continue
        if backoff_until.get(n["name"], 0) > now_s:
            continue
        if not any(node_slots(cfg, n, s) > 0 for s in want):
            continue
        eligible.append(n)

    def covered(nodes):
        got = room(nodes) if nodes else {}
        return sum(min(u, max(0, got.get(s, 0))) for s, u in want.items())

    out, have = [], 0
    while eligible and have < sum(want.values()):
        best = max(eligible, key=lambda n: (covered(out + [n["name"]]), -rank[n["name"]]))
        gain = covered(out + [best["name"]])
        if gain <= have:
            if not out:
                LOG.warning("no sleeping node shows room for %s on paper; waking %s alone", want, best["name"])
                out.append(best["name"])
            break
        out.append(best["name"])
        have = gain
        eligible = [n for n in eligible if n is not best]
    return out


def choose_wake(cfg, unmet, node_states, backoff_until, now_s):
    """The first node choose_wakes would wake."""
    return next(iter(choose_wakes(cfg, unmet, node_states, backoff_until, now_s)), None)


def pod_holds_node(pod, evictable_namespaces=()):
    """A pod that keeps its node awake: anything not a DaemonSet's, not a
    static (mirror) pod, not finished, and not in a namespace whose pods the
    shutdown's drain may simply move (kube-system: coredns, which the
    scheduler happily packs onto a runner node; its PodDisruptionBudget,
    core/coredns-pdb.yaml, makes the drain move one replica at a time).

    A runner pod always holds its node, busy or idle: an idle warm runner can
    be handed a job at any moment, and the drain would kill that job."""
    meta = pod.get("metadata", {})
    if meta.get("namespace") in evictable_namespaces:
        return False
    if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
        return False
    if "kubernetes.io/config.mirror" in meta.get("annotations", {}):
        return False
    for ref in meta.get("ownerReferences", []) or []:
        if ref.get("kind") == "DaemonSet":
            return False
    return True


def choose_sleep(cfg, node_states, idle_since, unmet, backoff_until, now_s,
                 wakeable=None, unplaced=0):
    """The last awake node, in configured order, idle for idleMinutes, while
    no set it serves has unmet demand. Never a node the waker could not wake
    again (no AMT address or credentials, no Wake-on-LAN), and nothing while
    a driven set has a runner not yet bound to a node: it may be waiting for
    this node's capacity."""
    if unplaced:
        return None
    hungry = {name for name, u in unmet.items() if u > 0}
    for n in reversed(cfg["nodes"]):
        if always_on(cfg, n["name"]) or (wakeable is not None and n["name"] not in wakeable):
            continue
        st = node_states.get(n["name"])
        if st is None or not node_awake(st) or st["busy"]:
            continue
        if backoff_until.get(n["name"], 0) > now_s:
            continue
        since = idle_since.get(n["name"])
        if since is None or now_s - since < cfg["idleMinutes"] * 60:
            continue
        if any(node_slots(cfg, n, s) > 0 for s in hungry):
            continue
        if holds_a_floor(cfg, n, node_states, wakeable):
            continue
        return n["name"]
    return None


def holds_a_floor(cfg, node, node_states, wakeable=None):
    """The node is the last awake one with slots for a floored set, and an
    awake node with none for it would be left: sleeping it would make the
    floor stand down (floor_caps), and the cap change restart the set's
    listener, the very thing the floor is for. It sleeps once those have
    (idleness, not list order, picks the node to sleep: 01 idle beside a
    busy 02, 3 Nov review).

    Only nodes the waker could put to sleep count as the ones left: a node
    it can never sleep (no way to wake it again) would pin this one on for
    good. The floor then stands down while that one stays up."""
    def awake(n):
        st = node_states.get(n["name"])
        return st is not None and node_awake(st)
    for s in cfg["sets"]:
        name = s["name"]
        if int(s.get("floor", 0)) <= 0 or node_slots(cfg, node, name) <= 0 or pinned_to_slots(s):
            continue
        others = [n for n in cfg["nodes"] if n["name"] != node["name"] and awake(n)]
        if any(node_slots(cfg, n, name) > 0 for n in others):
            continue  # another node keeps the set's slots
        if any(node_slots(cfg, n, name) <= 0 and (wakeable is None or n["name"] in wakeable)
               for n in others):
            return True
    return False


def heartbeat_lines(asleep, emitter, node_name, renewed, now_s, ttl):
    """What the heartbeat pod on node_name writes for the waker this minute.

    Mirrors the shell loop in core/kmsg-heartbeat.yaml; tests/test_heartbeat
    runs that loop and compares."""
    if node_name != emitter:
        return []
    if not str(renewed).isdigit() or now_s - int(renewed) > ttl:
        return []
    names = [n for n in asleep.split() if NODE_NAME_RE.match(n)][:8]
    return [f"log-pipeline asleep {n}" for n in names]


# --------------------------------------------------------------------------
# Kubernetes
# --------------------------------------------------------------------------

class Kube:
    """The few API calls the waker makes, over the pod's service account."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        self.base = f"https://{host}:{port}"
        self.ctx = ssl.create_default_context(cafile=f"{self.SA}/ca.crt")

    def _token(self):
        with open(f"{self.SA}/token") as f:  # projected: re-read, it rotates
            return f.read().strip()

    def request(self, method, path, body=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token()}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from e

    def get(self, path):
        return self.request("GET", path)

    def merge_patch(self, path, body):
        return self.request("PATCH", path, body, "application/merge-patch+json")

    def create(self, path, body):
        return self.request("POST", path, body)


ARS = "/apis/actions.github.com/v1alpha1/namespaces/{ns}/autoscalingrunnersets/{name}"
ERS = "/apis/actions.github.com/v1alpha1/namespaces/{ns}/ephemeralrunners"
APP = "/apis/argoproj.io/v1alpha1/namespaces/{ns}/applications/{name}"
CM = "/api/v1/namespaces/{ns}/configmaps/{name}"


def node_ready(node):
    for c in node.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def ready_transition(node):
    """Epoch seconds of the Ready condition's last transition, or None."""
    for c in node.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("lastTransitionTime"):
            return parse_time(c["lastTransitionTime"]).timestamp()
    return None


def node_fresh_ready(node, now_s=None, lease_age_s=None):
    """Ready, with a kubelet alive to say so, and Ready since the waker last
    shut it down.

    - The kubelet's Lease must have been seen to change in the last
      LEASE_FRESH_S (lease_age_s, when now_s is given). This is what catches a node that lost power,
      AMT included (AMT dies with mains power), and a Ready the node
      controller never flips because it is down itself. No Lease read
      (None): not alive. Fail closed: the safe direction is fewer slots.
    - A node that has really come back has a Ready transition newer than the
      waker's shutdown stamp. A live kubelet still Ready STALE_READY_LIMIT_S
      after the stamp means the shutdown never happened (the waker stopped
      before talosctl ran)."""
    if not node_ready(node):
        return False
    if now_s is not None and (lease_age_s is None or lease_age_s > LEASE_FRESH_S):
        return False
    ann = node.get("metadata", {}).get("annotations", {}) or {}
    slept_at = ann.get(ANNOTATION_SLEPT_AT)
    if not slept_at:
        return True
    try:
        slept = float(slept_at)
    except ValueError:
        return True
    since = ready_transition(node)
    if since is not None and since > slept:
        return True
    return now_s is not None and now_s - slept > STALE_READY_LIMIT_S


class LeaseUnreadable(Exception):
    pass


def lease_stamp(lease):
    """Epoch seconds the API server last recorded the kubelet's update to the
    Lease (its managedFields stamp; second resolution), or None. Only the
    kubelet's entry counts: anyone else touching the Lease (a label, an
    annotation) must not make a dead node look alive."""
    times = [parse_time(m["time"]).timestamp()
             for m in (lease or {}).get("metadata", {}).get("managedFields", []) or []
             if m.get("time") and m.get("manager") == "kubelet"]
    return max(times) if times else None


LEASE = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/{name}"


def node_ip(node):
    for a in node.get("status", {}).get("addresses", []):
        if a.get("type") == "InternalIP":
            return a["address"]
    return None


# --------------------------------------------------------------------------
# GitHub (queued jobs; Actions: read)
# --------------------------------------------------------------------------

class GitHub:
    """Conditional requests (ETag): GitHub does not count a 304 against the
    rate limit, and between passes most runs have not changed, so a pass
    every 30 s costs little even with a stack of PRs in flight. A 403/429
    for rate limiting stops calls until GitHub's reset time; the waker holds
    (neither wakes nor sleeps) meanwhile."""

    MAX_PAGES = 10

    def __init__(self, token, api, clock=time.time, auth=None):
        """token: a fixed token (the default credential); or auth, a
        callable returning a current one (GitHubApp.token)."""
        self.token = token
        self.auth = auth
        self.api = api.rstrip("/")
        self.clock = clock
        self.etags = {}  # path -> (etag, data)
        self.used = set()
        self.blocked_until = 0.0

    def _get(self, path):
        if self.clock() < self.blocked_until:
            raise RuntimeError(f"GitHub rate limited for {int(self.blocked_until - self.clock())} s more")
        self.used.add(path)
        req = urllib.request.Request(self.api + path)
        req.add_header("Authorization", f"Bearer {self.auth() if self.auth else self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        cached = self.etags.get(path)
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
                etag = resp.headers.get("ETag")
                if etag:
                    self.etags[path] = (etag, data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                return cached[1]
            if e.code in (403, 429) and (e.headers.get("Retry-After")
                                         or e.headers.get("X-RateLimit-Remaining") == "0"):
                wait = e.headers.get("Retry-After")
                if wait and wait.isdigit():
                    self.blocked_until = self.clock() + int(wait)
                else:
                    reset = e.headers.get("X-RateLimit-Reset", "")
                    self.blocked_until = float(reset) if reset.isdigit() else self.clock() + 300
            raise

    def _get_all(self, path, key):
        items = []
        sep = "&" if "?" in path else "?"
        for page in range(1, self.MAX_PAGES + 1):
            got = self._get(f"{path}{sep}per_page=100&page={page}").get(key, [])
            items.extend(got)
            if len(got) < 100:
                break
        return items

    def queued_jobs(self, repo, lookback_hours, now):
        """Every queued job in the repo's queued or in-progress runs.

        A run is in_progress as soon as one of its jobs starts, so its other
        jobs can be queued inside it: both lists are read."""
        # Whole hours, so the run-list URLs (and their ETags) stay the same
        # from one pass to the next.
        since = (now - dt.timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:00:00Z")
        jobs = []
        prefix = f"/repos/{repo}/"
        self.used = {k for k in self.used if not k.startswith(prefix)}
        for status in ("queued", "in_progress"):
            q = urllib.parse.urlencode({"status": status, "created": f">={since}"})
            runs = self._get_all(f"/repos/{repo}/actions/runs?{q}", "workflow_runs")
            for run in runs:
                for j in self._get_all(f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs"):
                    if j.get("status") != "queued":
                        continue
                    jobs.append({
                        "repo": repo,
                        "id": j["id"],
                        "name": j.get("name", ""),
                        "labels": j.get("labels", []),
                        "created_at": parse_time(j["created_at"]),
                    })
        # Forget runs no longer queued or in progress, so the cache stays small.
        self.etags = {k: v for k, v in self.etags.items() if not k.startswith(prefix) or k in self.used}
        return jobs

    def org_queued_jobs(self, org, lookback_hours, now):
        """Every queued job in the org's private repositories this
        installation can see (an org-level scale set serves them all).
        Archived ones run nothing and are skipped. One repository that
        cannot be read makes the whole org's queue unreadable."""
        repos = self._get_all("/installation/repositories", "repositories")
        jobs = []
        for r in repos:
            if (r.get("owner") or {}).get("login", "").lower() != org.lower() or r.get("archived"):
                continue
            # Public repositories: zagreus never runs their jobs (a fork's PR
            # would run code here), and the Default runner group keeps them
            # off org runners anyway, so their queue is never demand.
            if not r.get("private", False):
                continue
            jobs.extend(self.queued_jobs(r["full_name"], lookback_hours, now))
        return jobs


# A GitHub App's installation token, minted with the standard library alone:
# a JWT signed RS256 (RSASSA-PKCS1-v1_5 over SHA-256) with the App's key,
# exchanged for a one-hour token. The key is PKCS#1, as GitHub issues it and
# as the ExternalSecret's filterPEM writes it.

SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _der(buf, i):
    """(tag, value, next offset) of the DER element at i."""
    tag, ln = buf[i], buf[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[i:i + k], "big")
        i += k
    if i + ln > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[i:i + ln], i + ln


def rsa_private_key(pem):
    """(n, e, d) from an "RSA PRIVATE KEY" (PKCS#1) PEM."""
    m = re.search(r"-----BEGIN RSA PRIVATE KEY-----(.*?)-----END RSA PRIVATE KEY-----", pem, re.S)
    if not m:
        raise ValueError("no RSA PRIVATE KEY block")
    der = base64.b64decode("".join(m.group(1).split()), validate=True)
    tag, body, _ = _der(der, 0)
    if tag != 0x30:
        raise ValueError("RSA key is not a DER sequence")
    ints, i = [], 0
    while len(ints) < 4:
        tag, v, i = _der(body, i)
        if tag != 0x02:
            raise ValueError("RSA key field is not an integer")
        ints.append(int.from_bytes(v, "big"))
    _, n, e, d = ints
    return n, e, d


def rs256(pem, data):
    n, e, d = rsa_private_key(pem)
    k = (n.bit_length() + 7) // 8
    t = SHA256_DIGEST_INFO + hashlib.sha256(data).digest()
    if k < len(t) + 11:
        raise ValueError("RSA key too short")
    m = int.from_bytes(b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t, "big")
    sig = pow(m, d, n)
    if pow(sig, e, n) != m:  # a wrong or damaged key signs garbage: say so here
        raise ValueError("RSA signature does not verify")
    return sig.to_bytes(k, "big")


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class GitHubApp:
    """Installation tokens for one App installation, from its Secret
    mounted as files (ARC's keys: github_app_id, github_app_installation_id,
    github_app_private_key). Read on every mint, so a key rotated in the
    vault (or a Secret that appears after the pod started) is picked up.
    A token is reused until five minutes before it expires."""

    def __init__(self, directory, api, clock=time.time, opener=None):
        self.dir = directory
        self.api = api.rstrip("/")
        self.clock = clock
        self.opener = opener or urllib.request.urlopen
        self._token, self._expires = None, 0.0

    def _read(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()

    def token(self):
        if self._token and self.clock() < self._expires - 300:
            return self._token
        app_id = int(self._read("github_app_id").strip())
        installation = int(self._read("github_app_installation_id").strip())
        now = int(self.clock())
        # iat a minute back for clock drift; GitHub refuses exp more than ten
        # minutes after iat.
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
        sig = _b64url(rs256(self._read("github_app_private_key"), f"{head}.{body}".encode()))
        req = urllib.request.Request(f"{self.api}/app/installations/{installation}/access_tokens",
                                     data=b"", method="POST")
        req.add_header("Authorization", f"Bearer {head}.{body}.{sig}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        with self.opener(req, timeout=30) as resp:
            data = json.load(resp)
        self._token = data["token"]
        self._expires = parse_time(data["expires_at"]).timestamp()
        return self._token


# --------------------------------------------------------------------------
# Intel AMT / Standard Manageability over WS-MAN (HTTP digest, port 16992)
# --------------------------------------------------------------------------

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
NS_WSMAN = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
NS_WSEN = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
CIM = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/"
ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"

# CIM PowerState values AMT uses (DSP1027).
POWER_ON = 2
POWER_OFF_HARD = 8  # RequestPowerStateChange 8: power down now (no OS involvement)
OFF_STATES = {6, 8}  # reported for S5: Off - Soft (8) on AMT; Off - Hard (6) on some firmware


def wsman_envelope(to, action, resource_uri, body, selectors=None):
    sel = ""
    if selectors:
        sel = "<w:SelectorSet>" + "".join(
            f'<w:Selector Name="{k}">{v}</w:Selector>' for k, v in selectors.items()
        ) + "</w:SelectorSet>"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}" xmlns:a="{NS_WSA}" xmlns:w="{NS_WSMAN}" xmlns:n="{NS_WSEN}">'
        f"<s:Header>"
        f'<a:Action s:mustUnderstand="true">{action}</a:Action>'
        f'<a:To s:mustUnderstand="true">{to}</a:To>'
        f'<w:ResourceURI s:mustUnderstand="true">{resource_uri}</w:ResourceURI>'
        f'<a:MessageID s:mustUnderstand="true">uuid:{uuid.uuid4()}</a:MessageID>'
        f"<a:ReplyTo><a:Address>{ANON}</a:Address></a:ReplyTo>"
        f"<w:OperationTimeout>PT60S</w:OperationTimeout>"
        f"{sel}"
        f"</s:Header>"
        f"<s:Body>{body}</s:Body>"
        f"</s:Envelope>"
    )


def xml_find_text(xml_text, local_name):
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == local_name and el.text is not None:
            return el.text.strip()
    return None


def power_change_body(state):
    return (
        f'<p:RequestPowerStateChange_INPUT xmlns:p="{CIM}CIM_PowerManagementService">'
        f"<p:PowerState>{int(state)}</p:PowerState>"
        f"<p:ManagedElement>"
        f"<a:Address>{ANON}</a:Address>"
        f"<a:ReferenceParameters>"
        f"<w:ResourceURI>{CIM}CIM_ComputerSystem</w:ResourceURI>"
        f'<w:SelectorSet><w:Selector Name="CreationClassName">CIM_ComputerSystem</w:Selector>'
        f'<w:Selector Name="Name">ManagedSystem</w:Selector></w:SelectorSet>'
        f"</a:ReferenceParameters>"
        f"</p:ManagedElement>"
        f"</p:RequestPowerStateChange_INPUT>"
    )


class AMT:
    def __init__(self, hosts, user, password, port=16992):
        self.hosts = hosts  # node name -> host
        self.user = user
        self.password = password
        self.port = port

    def available(self, node):
        return bool(self.user and self.password and self.hosts.get(node))

    def _post(self, node, envelope_fn):
        url = f"http://{self.hosts[node]}:{self.port}/wsman"
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, self.user, self.password)
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        body = envelope_fn(url).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/soap+xml;charset=UTF-8")
        with opener.open(req, timeout=30) as resp:
            return resp.read().decode()

    def power_state(self, node):
        uri = CIM + "CIM_AssociatedPowerManagementService"
        enum = self._post(node, lambda to: wsman_envelope(
            to, NS_WSEN + "/Enumerate", uri, "<n:Enumerate/>"))
        ctx = xml_find_text(enum, "EnumerationContext")
        if not ctx:
            raise RuntimeError("AMT: no EnumerationContext")
        pull = self._post(node, lambda to: wsman_envelope(
            to, NS_WSEN + "/Pull", uri,
            f"<n:Pull><n:EnumerationContext>{ctx}</n:EnumerationContext>"
            f"<n:MaxElements>10</n:MaxElements></n:Pull>"))
        state = xml_find_text(pull, "PowerState")
        if state is None:
            raise RuntimeError("AMT: no PowerState in the reply")
        return int(state)

    def request_power(self, node, state):
        uri = CIM + "CIM_PowerManagementService"
        reply = self._post(node, lambda to: wsman_envelope(
            to, uri + "/RequestPowerStateChange", uri, power_change_body(state),
            {
                "CreationClassName": "CIM_PowerManagementService",
                "Name": "Intel(r) AMT Power Management Service",
                "SystemCreationClassName": "CIM_ComputerSystem",
                "SystemName": "Intel(r) AMT",
            }))
        rv = xml_find_text(reply, "ReturnValue")
        if rv != "0":
            raise RuntimeError(f"AMT: RequestPowerStateChange({state}) returned {rv}")


def magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"bad MAC {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_wol(mac, broadcast, port=9):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic_packet(mac), (broadcast, port))
    finally:
        s.close()


# --------------------------------------------------------------------------
# Talos
# --------------------------------------------------------------------------

class Talos:
    def __init__(self, talosctl, talosconfig):
        self.talosctl = talosctl
        self.talosconfig = talosconfig

    def available(self):
        return os.path.exists(self.talosctl) and os.path.exists(self.talosconfig)

    def shutdown(self, ip):
        # Straight to the node (-e and -n alike), so the call does not depend
        # on any other node. --wait=false: the node closes the API as it goes
        # down, and the power state is confirmed through AMT instead.
        subprocess.run(
            [self.talosctl, "--talosconfig", self.talosconfig, "-e", ip, "-n", ip,
             "shutdown", "--wait=false"],
            check=True, timeout=120, capture_output=True,
        )


# --------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------

def env_map(value):
    """'a=x,b=y' -> {'a': 'x', 'b': 'y'} (credentials and private addresses
    come from the ESO-synced Secret, never this public repo)."""
    out = {}
    for part in (value or "").split(","):
        part = part.strip()
        if part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


class Waker:
    def __init__(self, cfg, kube, github, amt, talos, wol=None,
                 clock=time.time, sleep=time.sleep, my_node=None):
        self.cfg = cfg
        self.kube = kube
        # One GitHub client per credential a set's queue is read with
        # ({"default": ..., "simplytics": ...}); a lone client is "default".
        self.githubs = dict(github) if isinstance(github, dict) else {"default": github}
        self.github = self.githubs.get("default")
        self.amt = amt
        self.talos = talos
        self.wol = wol or {}
        self.clock = clock
        self.sleep = sleep
        self.my_node = my_node or os.environ.get("NODE_NAME", "")
        self.idle_since = {}
        self.backoff_until = {}
        self.awake_since = {}
        self.last_wake_at = 0.0
        self.pending_first_job = {}  # node -> wake start (s)
        self.on_since = {}  # listed-asleep node -> when AMT first said it was on
        self.lower_streak = {}  # set -> passes in a row that wanted a lower cap
        self.last_ceiling = {}  # set -> last ceiling read from its Application
        self.first_pass = True
        self.lease_uncertain = set()  # nodes whose Lease this pass could not read, with no history
        self.last_wish = None  # last "would wake/sleep" logged, to log changes only
        self.last_demand_ok = clock()
        self.stuck_woke = set()  # sets that already woke a node for a stuck claim
        self.held = {}  # per set: queued jobs older than heldAfterSeconds
        self.jobs = []  # the last demand read, orphans excluded
        self.queue_stats = {}  # per set: queued jobs and the oldest's age, orphans included
        self.alts = {}  # (counted set, other matching set) -> jobs, from the last read
        self.orphans = {}  # job id -> when it was set aside (see mark_orphans)
        self.floor_warned = set()  # job ids warn_floor_matches has logged
        self.spare_since = {}  # per set: when its spare was first seen, unbroken and unraised
        self.lease_last = {}  # node -> (time of the last good read, age it gave)
        self.asleep_labelled = {}  # node -> whether LABEL_ASLEEP was last written on it
        # Sharing (README, "Sharing"): from the last demand read.
        self.share = {}  # pool set -> slots it wants in all: runners plus queued jobs not yet claimed
        self.share_turn = {}  # pool set -> age of its oldest queued job (s)
        self.share_used = {}  # pool set -> [(time, slots it wanted in all)] over shareHoldSeconds
        self.share_sticky = {}  # pool set -> the most it wanted in all over shareHoldSeconds
        self.blind_sets = set()  # sets whose queue could not be read on the last pass
        self.pass_held = set()  # shared pools lowered this pass: their raises wait for the next
        self.raised_at = {}  # pool set -> when its cap was last raised

    def wakeable(self):
        """Nodes the waker can power on again; it puts no other to sleep. Never
        an always-on node, even one given an AMT address or a MAC by mistake."""
        return {n["name"] for n in self.cfg["nodes"]
                if not n.get("alwaysOn") and
                (self.amt.available(n["name"]) or (self.wol.get(n["name"]) and self.wol.get("_broadcast")))}

    # ----- what the mode allows

    @property
    def may_patch_caps(self):
        return self.cfg["mode"] in ("caps", "full")

    @property
    def may_power(self):
        return self.cfg["mode"] == "full"

    # ----- the asleep list (read by the heartbeat pod on the control plane)

    def asleep_cm_path(self):
        c = self.cfg["asleepConfigMap"]
        return CM.format(ns=c["namespace"], name=c["name"])

    def read_asleep(self):
        cm = self.kube.get(self.asleep_cm_path())
        if not cm:
            return []
        return [n for n in cm.get("data", {}).get("asleep", "").split() if NODE_NAME_RE.match(n)]

    def write_asleep(self, names):
        c = self.cfg["asleepConfigMap"]
        data = {
            "asleep": " ".join(sorted(set(names))),
            "renewed": str(int(self.clock())),
            "emitter": self.my_node,
        }
        if self.kube.get(self.asleep_cm_path()) is None:
            self.kube.create(f"/api/v1/namespaces/{c['namespace']}/configmaps", {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": c["name"], "namespace": c["namespace"]},
                "data": data,
            })
        else:
            self.kube.merge_patch(self.asleep_cm_path(), {"data": data})
        self.sync_asleep_labels(names)

    def sync_asleep_labels(self, names):
        """Label the nodes on the asleep list and unlabel the rest, writing
        only what changed since the last success. Best effort: a failed patch
        is retried on the next call and never stops a sleep or a wake. The
        label goes on with the asleep line, before the shutdown, so the
        DaemonSet controller removes its pod from a node that is still
        alive; it comes off when the node leaves the list, after its
        heartbeat is Ready."""
        if not self.may_power:
            return
        asleep = set(names)
        for n in self.cfg["nodes"]:
            name = n["name"]
            if n.get("alwaysOn"):
                continue  # never asleep, never written (README, "Always-on node")
            want = name in asleep
            if self.asleep_labelled.get(name) == want:
                continue
            try:
                self.kube.merge_patch(f"/api/v1/nodes/{name}",
                                      {"metadata": {"labels": {LABEL_ASLEEP: "true" if want else None}}})
                self.asleep_labelled[name] = want
            except Exception as e:
                LOG.warning("could not %s %s on %s: %s", "set" if want else "clear", LABEL_ASLEEP, name, e)

    def mark_asleep(self, node, asleep):
        names = set(self.read_asleep())
        if asleep:
            names.add(node)
        else:
            names.discard(node)
        self.write_asleep(sorted(names))

    def publish_queue_metrics(self, set_labels):
        """Write the queue-age gauges where the node exporter picks them up.
        Only after a pass that read GitHub, so a blind waker leaves a stale
        sample time behind. Never stops the controller. Not in observe mode,
        which writes nothing."""
        if not self.may_patch_caps:
            return
        # A set whose labels could not be read matches no job (first_set), so
        # its queue would read as empty. Publish nothing instead: the sample
        # time ages and the stale alert says the watchdog is blind.
        unreadable = [s["name"] for s in self.cfg["sets"] if s["name"] not in set_labels]
        if unreadable:
            LOG.warning("not publishing queue metrics: labels of %s unreadable", ", ".join(unreadable))
            return
        # A set whose queue could not be read is left out (absent, never a
        # false 0); the other queues' gauges stay live. The waker logs the
        # unread queue every pass, and keeps every node awake meanwhile.
        stats = {k: v for k, v in self.queue_stats.items() if k not in self.blind_sets}
        self.write_metrics_file(QUEUE_METRIC_FILE, render_queue_metrics(stats, self.clock()))

    def publish_slot_metrics(self, snap):
        """Write the slot gauges (slot_usage) every pass, GitHub readable or
        not: they come from the cluster alone. Not in observe mode. Never
        stops the controller."""
        if not self.may_patch_caps:
            return
        try:
            offered = {}
            compute_caps(self.cfg, snap["states"], self.cap_requests(snap), snap.get("unplaced"), per_node=offered,
                         **self.share_args(snap))
            usage = slot_usage(self.cfg, snap["states"], snap.get("pods_by_node") or {}, offered,
                               snap.get("runner_jobs") or {})
            text = render_slot_metrics(usage, self.clock())
        except Exception as e:  # a gauge must never stop the controller
            LOG.warning("could not compute slot metrics: %s", e)
            return
        self.write_metrics_file(SLOT_METRIC_FILE, text)

    def write_metrics_file(self, key, text):
        """One file in the metrics ConfigMap, the others left as they are."""
        c = self.cfg["metricsConfigMap"]
        path = CM.format(ns=c["namespace"], name=c["name"])
        body = {"data": {key: text}}
        try:
            if self.kube.get(path) is None:
                self.kube.create(f"/api/v1/namespaces/{c['namespace']}/configmaps", {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": c["name"], "namespace": c["namespace"]}, **body})
            else:
                self.kube.merge_patch(path, body)
        except Exception as e:  # a gauge must never stop the controller
            LOG.warning("could not publish %s: %s", key, e)

    def record_event(self, kind, node, **fields):
        fields.update({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "event": kind, "node": node})
        LOG.info("event %s", json.dumps(fields, sort_keys=True))
        if not self.may_patch_caps:
            return
        c = self.cfg["eventsConfigMap"]
        path = CM.format(ns=c["namespace"], name=c["name"])
        try:
            cm = self.kube.get(path)
            events = json.loads((cm or {}).get("data", {}).get("events", "[]"))
            events = (events + [fields])[-50:]
            body = {"data": {"events": json.dumps(events)}}
            if cm is None:
                self.kube.create(f"/api/v1/namespaces/{c['namespace']}/configmaps", {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": c["name"], "namespace": c["namespace"]}, **body})
            else:
                self.kube.merge_patch(path, body)
        except Exception as e:  # measurement must never stop the controller
            LOG.warning("could not record event: %s", e)

    # ----- observation

    def observe(self):
        cfg = self.cfg
        driven = {s["name"] for s in cfg["sets"]}
        asleep = set(self.read_asleep())
        pods = self.kube.get("/api/v1/pods").get("items", [])
        by_node = {}
        for p in pods:
            by_node.setdefault(p.get("spec", {}).get("nodeName"), []).append(p)

        states = {}
        ips = {}
        now = self.clock()
        self.lease_uncertain = set()
        for n in cfg["nodes"]:
            try:
                node = self.kube.get(f"/api/v1/nodes/{n['name']}")
            except Exception as e:
                # That node counts for nothing this pass (under-offer); the
                # others are still judged.
                LOG.error("%s: node unreadable (%s); counting it out this pass", n["name"], e)
                continue
            if node is None:
                continue
            ann = node["metadata"].get("annotations", {}) or {}
            ready = node_fresh_ready(node, now, self._lease_age(n["name"]))
            taint = untolerated_taint(node, cfg["toleratedTaintKeys"])
            node_pods = [p for p in by_node.get(n["name"], [])
                         if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
            alloc = (node.get("status") or {}).get("allocatable") or {}
            try:
                used = [pod_requests(p.get("spec") or {}) for p in node_pods]
                free = (parse_qty(alloc["cpu"]) - sum(u[0] for u in used),
                        parse_qty(alloc["memory"]) - sum(u[1] for u in used))
            except (KeyError, ValueError, TypeError) as e:
                # Under-offer, never crash the pass: this node offers nothing new.
                LOG.error("%s: requests or allocatable unreadable (%s); offering no new slots there",
                          n["name"], e)
                free = (0.0, 0.0)
            own = {}
            for p in node_pods:
                set_name = (p.get("metadata", {}).get("labels") or {}).get(SCALE_SET_LABEL)
                if set_name in driven and is_runner_pod(p):
                    own[set_name] = own.get(set_name, 0) + 1
            states[n["name"]] = {
                "ready": ready,
                "slept_at": ann.get(ANNOTATION_SLEPT_AT),
                "retry": ann.get(ANNOTATION_STATE) in RETRY_STATES,
                "unschedulable": bool(node.get("spec", {}).get("unschedulable")) or taint is not None,
                "taint": taint,
                "taints": untolerated_taints(node, cfg["toleratedTaintKeys"]),
                "waker_cordon": ann.get(ANNOTATION_CORDON) == "true",
                # Only the full mode keeps the list: in observe or caps a
                # leftover list (say, after stepping back from full) must
                # not hold a Ready node out of the caps.
                "asleep": n["name"] in asleep and self.may_power,
                "own": own,
                "busy": any(pod_holds_node(p, cfg["evictableNamespaces"]) for p in by_node.get(n["name"], [])),
            }
            states[n["name"]]["free"] = free
            ips[n["name"]] = node_ip(node)

        live, labels, ceilings, runners, unstarted, unplaced, requests = {}, {}, {}, {}, {}, {}, {}
        runner_jobs = {}  # runner pod name (= its EphemeralRunner's) -> the job it runs
        degraded, blind = set(), set()
        stuck = {}
        for s in cfg["sets"]:
            try:
                ars = self.kube.get(ARS.format(ns=s["namespace"], name=s["name"]))
            except Exception as e:
                LOG.error("%s: scale set unreadable (%s); degraded pass: lowering only, no sleep",
                          s["name"], e)
                degraded.add(s["name"])
                continue
            if ars is None:
                continue
            mr = ars["spec"].get("maxRunners")
            live[s["name"]] = None if mr is None else int(mr)
            if mr is None:
                LOG.warning("%s has no maxRunners, which ARC reads as unlimited", s["name"])
            labels[s["name"]] = ars["spec"].get("runnerScaleSetLabels", []) or []
            try:
                requests[s["name"]] = pod_requests(((ars["spec"].get("template") or {}).get("spec")) or {})
            except (ValueError, TypeError) as e:
                LOG.error("%s: template requests unreadable (%s); offering it no new slots", s["name"], e)
                requests[s["name"]] = (float("inf"), float("inf"))
            try:
                app = self.kube.get(APP.format(ns=cfg["applicationsNamespace"], name=s["application"]))
                ceilings[s["name"]] = int(app["spec"]["source"]["helm"]["valuesObject"]["maxRunners"])
            except Exception:  # 404 (None), 403, 5xx, a reshaped spec: all the same here
                ceilings[s["name"]] = self.last_ceiling.get(s["name"])
                LOG.error("%s: ceiling unreadable from Application %s; using %s",
                          s["name"], s["application"],
                          "the last one read" if ceilings[s["name"]] is not None else "none (lowering only)")
            else:
                self.last_ceiling[s["name"]] = ceilings[s["name"]]
            q = urllib.parse.urlencode({"labelSelector": f"{SCALE_SET_LABEL}={s['name']}"})
            try:
                ers = self.kube.get(ERS.format(ns=s["namespace"]) + "?" + q).get("items", [])
            except Exception as e:
                # Unknown claims: no spare (so its demand can still wake),
                # and an unplaced runner assumed (so nothing sleeps on a guess).
                LOG.error("%s: runners unreadable (%s); its demand wakes nothing this pass", s["name"], e)
                blind.add(s["name"])
                runners[s["name"]] = live[s["name"]] or 0
                unstarted[s["name"]] = 0
                unplaced[s["name"]] = 1
                continue
            # Finished runners (Succeeded/Failed, not yet cleaned up) hold no
            # claim: counting one as unstarted would hide a queued job's
            # demand (athena-gitops#239).
            live_ers = [e for e in ers if (e.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
            runners[s["name"]] = len(live_ers)
            for e in live_ers:
                st = e.get("status") or {}
                er_name = (e.get("metadata") or {}).get("name")
                if er_name and (st.get("jobDisplayName") or st.get("jobRequestId")):
                    runner_jobs[er_name] = {
                        "set": s["name"], "repo": st.get("jobRepositoryName") or "",
                        "workflow_run": st.get("workflowRunId") or "",
                        "job": st.get("jobDisplayName") or ""}
            unstarted[s["name"]] = sum(1 for e in live_ers if not e.get("status", {}).get("jobRequestId"))
            # Runner pods only: the listener has the same label and a node.
            placed = sum(1 for p in pods
                         if (p.get("metadata", {}).get("labels") or {}).get(SCALE_SET_LABEL) == s["name"]
                         and is_runner_pod(p)
                         and p.get("spec", {}).get("nodeName")
                         and p.get("status", {}).get("phase") not in ("Succeeded", "Failed"))
            unplaced[s["name"]] = max(0, len(live_ers) - placed)
            # Claims unbound past the grace: a runner's pod normally binds
            # within seconds, so only these say "no room anywhere awake".
            aged = sum(1 for e in live_ers
                       if (now - parse_time(e["metadata"]["creationTimestamp"]).timestamp())
                       > cfg["saturationGraceSeconds"]) if all(
                (e.get("metadata") or {}).get("creationTimestamp") for e in live_ers) else len(live_ers)
            stuck[s["name"]] = max(0, aged - placed)

        return {"states": states, "ips": ips, "pods_by_node": by_node, "live": live,
                "labels": labels, "ceilings": ceilings, "runners": runners,
                "unstarted": unstarted, "unplaced": unplaced, "stuck": stuck,
                "uncertain": bool(self.lease_uncertain),
                # Sets whose scale set could not be read: they are missing
                # from every dict above, so the room they hold is unknown.
                # The pass may lower caps but never raise one, and nothing
                # sleeps (the sleep could not lower that set's cap first).
                "degraded": degraded,
                # Sets whose runners could not be read: claims unknown.
                "blind": blind,
                "runner_jobs": runner_jobs,
                "requests": requests if len(requests) == len(live) else None}

    def queues(self):
        """{(credential, "repo" or "org", name): [set names]}: each queue
        read once a pass, through its own credential."""
        out = {}
        for s in self.cfg["sets"]:
            kind = "org" if s.get("org") else "repo"
            out.setdefault((s.get("github", "default"), kind, s[kind]), []).append(s["name"])
        return out

    def demand(self, labels):
        """Demand per set, from every queue. A queue that cannot be read
        leaves its sets blind (blind_sets: no demand, and the pass puts no
        node to sleep), and the others still count; with none readable it
        raises, and the pass holds."""
        now = dt.datetime.now(dt.timezone.utc)
        jobs, blind = [], set()
        queues = self.queues()
        for (cred, kind, name), sets in sorted(queues.items()):
            try:
                gh = self.githubs.get(cred)
                if gh is None:
                    raise RuntimeError(f"no GitHub credential {cred!r}")
                read = gh.org_queued_jobs if kind == "org" else gh.queued_jobs
                jobs.extend(read(name, self.cfg["runLookbackHours"], now))
            except Exception as e:
                if len(queues) == 1:
                    raise
                LOG.warning("queue %s %s unreadable (%s): %s wake nothing, and nothing sleeps",
                            kind, name, e, ", ".join(sets))
                blind.update(sets)
        if blind and blind >= {s["name"] for s in self.cfg["sets"]}:
            raise RuntimeError("no queue readable")
        self.blind_sets = blind
        queued, t = {j.get("id") for j in jobs}, self.clock()
        self.queue_stats = oldest_queued_by_set(self.cfg, jobs, labels, now)
        self.orphans = {i: at for i, at in self.orphans.items()
                        if i in queued and t - at < self.cfg["orphanForSeconds"]}
        jobs = [j for j in jobs if j.get("id") not in self.orphans]
        self.jobs = jobs
        self.alts = {}
        # Jobs queued past heldAfterSeconds: where the set shows spare,
        # these were turned down and are waiting to be offered again.
        self.held = demand_by_set(dict(self.cfg, namedGraceSeconds=self.cfg["heldAfterSeconds"],
                                       saturationGraceSeconds=max(self.cfg["heldAfterSeconds"],
                                                                  self.cfg["saturationGraceSeconds"])),
                                  jobs, labels, now,
                                  max_age=self.cfg["heldAfterSeconds"] + self.cfg["heldForSeconds"])
        return demand_by_set(self.cfg, jobs, labels, now, alts=self.alts)

    # ----- sharing

    def update_share(self, demand, snap):
        """From this pass's demand, per pool set: the slots it wants in all
        (its runners plus its queued jobs not yet claimed), the age of its
        oldest queued job (the tie-break), and the most it wanted in all
        over the last shareHoldSeconds (the hold). Totals, so they stay
        right when runners come and go before the next read (compute_caps).
        A blind set asks for nothing."""
        now = self.clock()
        need = share_need(self.cfg, demand, snap.get("unstarted", {}))
        hold = self.cfg["shareHoldSeconds"]
        for name, n in need.items():
            if name in self.blind_sets:
                # Its queue unread: it asks for nothing new and keeps
                # nothing back, so a stale share cannot squeeze the others
                # for the whole outage. Its running runners keep their slots.
                self.share[name] = 0
                self.share_sticky[name] = 0
                self.share_turn[name] = 0
                self.share_used.pop(name, None)
                continue
            total = snap.get("runners", {}).get(name, 0) + n
            self.share[name] = total
            self.share_turn[name] = self.queue_stats.get(name, {}).get("oldest", 0)
            hist = [(t, u) for t, u in self.share_used.get(name, []) if now - t < hold] + [(now, total)]
            self.share_used[name] = hist
            self.share_sticky[name] = max(u for _, u in hist)

    def share_args(self, snap):
        """compute_caps' sharing arguments for this waker's state."""
        if not self.share:
            return {}
        # A pool set whose ceiling was never read is bounded by its live
        # cap (bound_caps' rule): handed more here, the slots would be
        # clamped away afterwards and reserved for no one.
        ceilings = {name: (c if c is not None else snap.get("live", {}).get(name))
                    for name, c in (snap.get("ceilings") or {}).items()}
        return {"want": dict(self.share), "recent": dict(self.share_sticky),
                "turn": dict(self.share_turn), "ceilings": ceilings}

    # ----- caps

    def cap_requests(self, snap):
        """The pod requests compute_caps counts room with, or None for caps
        from the configured slots alone (roomCaps false). The slots are
        sized so they fit beside everything else that may land on a node
        (test_configured_slots_fit_the_nodes), so they need no room check;
        a cordon or an untolerated taint still takes a node out."""
        return snap.get("requests") if self.cfg["roomCaps"] else None

    def apply_caps(self, snap, states=None, force=False, recount=True):
        """Raise at once. Lower at once too, unless this pass could not read
        some node's Lease and had no earlier reading to go on (the first
        pass after a start, say): then only if the next pass still wants
        it, so one failed read does not flap the listeners. A Lease read
        and found stale is not uncertain, and lowers at once. force: lower
        now (the sleep procedure, before a cordon). recount False: a second
        apply in the same pass, which must not count as the next pass."""
        wanted = compute_caps(self.cfg, states or snap["states"], self.cap_requests(snap), snap.get("unplaced"),
                              **self.share_args(snap))
        room = bound_caps(wanted, snap["live"], snap["ceilings"])
        target = floor_caps(self.cfg, room, snap["live"], snap["ceilings"], states or snap["states"])
        if states is None:
            snap["room"] = room
        changed = {}

        def raising(s):
            live, want = snap["live"].get(s["name"]), target.get(s["name"])
            return live is not None and want is not None and want > live
        # Lowers before raises, and never in the same pass within a pool:
        # a lowered set's listener keeps offering its old cap until ARC has
        # restarted it (about 8 s, "Why maxRunners"), and it may claim a
        # job meanwhile. So a pool's raises wait for the next pass, which
        # recounts claims; the slot moves about one pass later, and the two
        # sets are never offered it at once (Codex on #345).
        pool_of = {m: k for k, members in shared_pools(self.cfg).items() for m in members}
        # Pools with a lower this pass (made or deferred): no raise in them
        # for the rest of the pass, across every apply in it (Codex on #345:
        # the second, share-moving apply raised what the first had lowered).
        held_back = self.pass_held
        for s in sorted(self.cfg["sets"], key=raising):
            name = s["name"]
            want = target.get(name)
            if want is None or name not in snap["live"] or snap["live"][name] == want:
                self.lower_streak.pop(name, None)
                continue
            live = snap["live"][name]
            if snap.get("degraded") and live is not None and want > live:
                continue
            if live is not None and want > live and pool_of.get(name) in held_back:
                LOG.info("%s maxRunners %s -> %s wanted; held until its pool's lower goes through",
                         name, live, want)
                continue
            if live is not None and want < live and not force and snap.get("uncertain"):
                if recount:
                    self.lower_streak[name] = self.lower_streak.get(name, 0) + 1
                if self.lower_streak.get(name, 0) < 2:
                    LOG.info("%s maxRunners %s -> %s wanted; lowering on the next pass if still so",
                             name, live, want)
                    if name in pool_of:
                        held_back.add(pool_of[name])
                    continue
            self.lower_streak.pop(name, None)
            if live is not None and want < live and name in pool_of:
                held_back.add(pool_of[name])
            # A changed cap restarts the listener: spare from it is on paper
            # until GitHub has had its ~10 min to offer held jobs again.
            self.spare_since.pop(name, None)
            changed[name] = (snap["live"][name], want)  # old may be None (unlimited)
            if self.may_patch_caps:
                self.kube.merge_patch(ARS.format(ns=s["namespace"], name=name),
                                      {"spec": {"maxRunners": want}})
                snap["live"][name] = want
                if name in pool_of and (live is None or want > live):
                    self.raised_at[name] = self.clock()
        for name, (old, new) in changed.items():
            LOG.info("%s maxRunners %s -> %s%s", name, old, new,
                     "" if self.may_patch_caps else " (observe: not applied)")
        return changed

    # ----- cordon

    def cordon(self, node, on):
        if on:
            body = {"spec": {"unschedulable": True},
                    "metadata": {"annotations": {ANNOTATION_CORDON: "true", ANNOTATION_STATE: "draining"}}}
        else:
            body = {"spec": {"unschedulable": None},
                    "metadata": {"annotations": {ANNOTATION_CORDON: None, ANNOTATION_STATE: "awake",
                                                 ANNOTATION_SLEPT_AT: None}}}
        self.kube.merge_patch(f"/api/v1/nodes/{node}", body)

    def set_state_annotation(self, node, value):
        self.kube.merge_patch(f"/api/v1/nodes/{node}",
                              {"metadata": {"annotations": {ANNOTATION_STATE: value}}})

    # ----- one pass

    def reconcile_ready_nodes(self, snap):
        """Between procedures no node should be both Ready and asleep, or
        Ready and under our cordon. Either means it came back on by hand, or a
        wake or sleep was interrupted: take it back as awake.

        "Ready" here is fresh Ready (node_fresh_ready): the API server keeps a
        node Ready for ~40 s after it goes down, and taking that back would
        count an off node's slots. Where AMT answers, it must also say on."""
        for name, st in snap["states"].items():
            if not st["ready"] or not (st["asleep"] or st["waker_cordon"]):
                continue
            if self.amt.available(name):
                try:
                    if self.amt.power_state(name) != POWER_ON:
                        LOG.info("%s reports Ready but AMT says it is off: not taking it back", name)
                        continue
                except Exception as e:
                    LOG.warning("AMT unreadable for %s (%s); going by its Ready transition", name, e)
            LOG.info("%s is Ready but %s: taking it back as awake", name,
                     "listed asleep" if st["asleep"] else "under the waker's cordon")
            if not self.may_power:
                continue
            if st["asleep"]:
                self.mark_asleep(name, False)
                st["asleep"] = False
            if st.get("slept_at"):
                self.kube.merge_patch(f"/api/v1/nodes/{name}", {"metadata": {"annotations": {
                    ANNOTATION_SLEPT_AT: None}}})
                st["slept_at"] = None
            if st["waker_cordon"]:
                self.cordon(name, False)
                st["waker_cordon"] = False
                st["unschedulable"] = st.get("taint") is not None

    def audit_asleep_list(self, snap, now):
        """The list must name only nodes that are really off. Names no longer
        configured are dropped. A listed node that AMT says is on, but that
        has not come Ready within readyTimeoutSeconds (a power-restore boot
        that hung, say), leaves the list so its alert fires."""
        listed = self.read_asleep()
        configured = {n["name"] for n in self.cfg["nodes"]}
        keep = [n for n in listed if n in configured]
        for name in keep:
            st = snap["states"].get(name)
            if (st is not None and st["ready"]) or not self.amt.available(name):
                self.on_since.pop(name, None)
                continue
            try:
                on = self.amt.power_state(name) == POWER_ON
            except Exception as e:
                LOG.warning("AMT unreadable for listed %s: %s", name, e)
                continue
            if not on:
                self.on_since.pop(name, None)
                continue
            since = self.on_since.setdefault(name, now)
            if now - since > self.cfg["readyTimeoutSeconds"]:
                LOG.error("%s is listed asleep, AMT says on, and it has not come Ready for %ss: "
                          "taking it off the list so its alert fires", name, int(now - since))
                keep.remove(name)
                if st is not None:
                    st["asleep"] = False
                self.on_since.pop(name, None)
                self.set_state_annotation(name, "on-not-ready")
                self.backoff_until[name] = now + self.cfg["failureBackoffSeconds"]
                self.record_event("on-not-ready", name)
        if keep != listed:
            dropped = sorted(set(listed) - set(keep))
            LOG.info("asleep list: dropping %s", dropped)
            self.write_asleep(keep)

    def tick(self):
        now = self.clock()
        self.pass_held = set()
        snap = self.observe()
        self.first_pass = False
        states = snap["states"]
        if self.may_power:
            self.reconcile_ready_nodes(snap)
            self.audit_asleep_list(snap, now)
            # Catches a list that changed outside write_asleep, and the first
            # pass after a restart (nothing remembered yet).
            self.sync_asleep_labels(self.read_asleep())
            if now - self.last_demand_ok <= self.cfg["demandBlindSeconds"]:
                self._renew()  # or the heartbeat pod stops asserting
            elif self.read_asleep():
                LOG.error("GitHub unreadable for %ss: no longer vouching for sleeping nodes, "
                          "so their heartbeat alerts fire", int(now - self.last_demand_ok))

        # Idle and awake clocks.
        for name, st in states.items():
            if node_awake(st):
                self.awake_since.setdefault(name, now)
            if node_awake(st) and not st["busy"]:
                self.idle_since.setdefault(name, now)
            else:
                self.idle_since.pop(name, None)
        self.note_first_jobs(snap)

        # Caps follow the nodes first, before any GitHub call that may be
        # slow, with the shares from the last demand read. Once demand is
        # read, a shared pool's slots move to the sets now asking, in a
        # second apply that writes only what the new shares changed.
        self.apply_caps(snap)
        try:
            demand = self.demand(snap["labels"])
        except Exception as e:
            demand = None
            err = e
        else:
            before = self.share_args(snap)
            self.update_share(demand, snap)
            if self.share_args(snap) != before:
                self.apply_caps(snap, recount=False)
        self.publish_slot_metrics(snap)
        if demand is None:
            # Demand unknown: neither wake nor sleep on a guess.
            LOG.warning("GitHub demand unreadable, holding: %s", err)
            return
        if not self.blind_sets:
            self.last_demand_ok = now
        self.publish_queue_metrics(snap["labels"])
        self.warn_floor_matches(snap)
        spare = spare_caps(self.cfg, snap["live"], snap.get("room", {}))
        # A pool set whose raise waits for the next pass (a lowered pool,
        # apply_caps) gets that room within a pass: counted as short, it
        # would wake a node for slots already on their way.
        # (Caps and full modes only: in observe nothing is ever raised.)
        pooled = {m for members in shared_pools(self.cfg).values() for m in members}
        pending = {}  # pool set -> slots its held raise will add next pass
        for name in pooled if self.may_patch_caps else ():
            want = snap.get("room", {}).get(name)
            if want is not None and spare.get(name) is not None and want > spare[name]:
                pending[name] = want - spare[name]
                spare[name] = want
        unmet = unmet_by_set(demand, spare, snap["runners"], snap["unstarted"])
        if self.mark_orphans(snap):
            self.alts = {}
            demand = demand_by_set(self.cfg, self.jobs, snap["labels"], dt.datetime.now(dt.timezone.utc),
                                   alts=self.alts)
            unmet = unmet_by_set(demand, spare, snap["runners"], snap["unstarted"])
        unmet = share_overlap(unmet, demand, self.alts, spare, snap["runners"], snap["unstarted"])
        for name, n in floor_claims(self.cfg, snap["runners"], snap["unplaced"], snap.get("room", {})).items():
            unmet[name] = unmet.get(name, 0) + n
        for name in snap.get("blind", ()):
            unmet[name] = 0
        # A pool set capped below the pool's slots (its ceiling) can use no
        # more nodes than its ceiling allows: past it, a wake would only
        # give the room to another set.
        for name in pooled:
            ceiling, live = snap["ceilings"].get(name), spare.get(name)
            if ceiling is not None and live is not None:
                unmet[name] = min(unmet.get(name, 0), max(0, ceiling - live))
        if any(demand.values()):
            LOG.info("demand %s unmet %s runners %s caps %s", demand, unmet, snap["runners"], snap["live"])

        # A set with a claimed runner that is not on a node needs room too:
        # its job counts as claimed, so unmet alone would never wake the
        # node that could run it (full-mode review). Not for sets whose
        # runners could not be read (their unplaced count is a guess).
        # One wake per stuck episode: a claim that can never schedule (a bad
        # template, a taint nothing tolerates) would otherwise wake every
        # node in turn and pin them all on. The episode ends when the set
        # has no stuck claim left.
        # Stuck claims and queued jobs never overlap (unmet already
        # subtracts the claims' jobs), so a set with both needs room for
        # the sum. A set with stuck claims only is a guess (a bad template
        # never binds): it may add at most one node per episode (Codex on
        # 27905470).
        stuck = {}
        for name, u in snap.get("stuck", {}).items():
            if not u:
                self.stuck_woke.discard(name)
            elif name not in self.stuck_woke:
                stuck[name] = u
        hungry = dict(unmet)
        for name, u in stuck.items():
            hungry[name] = hungry.get(name, 0) + u
        wakes = []
        if now - self.last_wake_at >= self.cfg["wakeCooldownSeconds"]:
            room = lambda nodes: self._room_if_awake(snap, nodes)  # noqa: E731
            real = {name: n for name, n in hungry.items() if unmet.get(name, 0) > 0}
            guess = {name: n for name, n in hungry.items() if n > 0 and not unmet.get(name, 0)}
            wakes = choose_wakes(self.cfg, real, states, self.backoff_until, now, room=room)
            if guess:
                got = room(wakes) if wakes else {}
                # Only sets the batch gives no room at all; and one node for
                # all of them.
                still = {name: n for name, n in guess.items() if not got.get(name, 0)}
                extra = [n for n in choose_wakes(self.cfg, still, states, self.backoff_until, now, room=room)
                         if n not in wakes][:1] if still else []
                wakes = wakes + extra
        if wakes:
            self._wish("wake", ", ".join(wakes), f"for {dict((k, v) for k, v in hungry.items() if v)}")
            # Room each node gives, read before the wake updates the snapshot.
            gives = {node: room([node]) for node in wakes} if wakes else {}
            woke = self.wake_many(wakes, snap) if self.may_power else []
            # The episode is spent only by a wake that worked, and only for
            # sets the nodes that woke give room (not merely slots: a node
            # woken for other demand may have none left for them).
            for name, u in snap.get("stuck", {}).items():
                if u and not unmet.get(name) and any(gives[n].get(name, 0) > 0 for n in woke):
                    self.stuck_woke.add(name)
            return

        # A held job (turned down, waiting to be offered again) shows as
        # spare, not unmet: it still keeps the nodes that can run it awake.
        held = {name: n for name, n in self.held.items() if n and not unmet.get(name)}
        if held:
            LOG.warning("jobs queued over %ss beside spare slots (held by GitHub?): %s",
                        self.cfg["heldAfterSeconds"], held)
        keep = {name: max(unmet.get(name, 0), held.get(name, 0)) for name in set(unmet) | set(held)}
        # A pool set's raise held for a pass, or made so lately that its
        # listener may still be restarting, is room on its way to queued
        # jobs, not room to spare: its nodes stay awake for them (delta
        # review of d8dcdc6: 01 slept under the job its raise was for).
        settle = self.cfg["listenerSettleSeconds"] + 2 * self.cfg["pollSeconds"]
        for name in pooled:
            recent = now - self.raised_at.get(name, float("-inf")) < settle
            if demand.get(name) and (name in pending or recent):
                keep[name] = max(keep.get(name, 0), demand[name])
        if self.blind_sets:
            # A queue unread: jobs in it might need the node.
            LOG.warning("not sleeping anything: queue of %s unreadable", ", ".join(sorted(self.blind_sets)))
        sleep = None if snap.get("degraded") or self.blind_sets else choose_sleep(
                             self.cfg, states, self.idle_since, keep, self.backoff_until, now,
                             wakeable=self.wakeable(), unplaced=sum(snap["unplaced"].values()))
        if not sleep:
            self.last_wish = None
        if sleep:
            self._wish("sleep", sleep, "(idle since %s)" % dt.datetime.fromtimestamp(
                self.idle_since[sleep], dt.timezone.utc).isoformat())
            if self.may_power:
                self.put_to_sleep(sleep, snap)

    def _wish(self, kind, node, detail):
        """Log a wake or sleep the waker wants, once per change: in observe
        and caps it never acts, so the same wish would repeat every pass."""
        key = (kind, node)
        level = logging.INFO if key != self.last_wish else logging.DEBUG
        LOG.log(level, "would %s %s %s", kind, node, detail)
        self.last_wish = key

    def note_first_jobs(self, snap):
        driven = {s["name"] for s in self.cfg["sets"]}
        for node, started in list(self.pending_first_job.items()):
            for p in snap["pods_by_node"].get(node, []):
                labels = p.get("metadata", {}).get("labels", {}) or {}
                if (labels.get(SCALE_SET_LABEL) in driven and is_runner_pod(p)
                        and p.get("status", {}).get("phase") == "Running"):
                    self.record_event("first-job", node, seconds=round(self.clock() - started))
                    del self.pending_first_job[node]
                    break

    # ----- wake

    def mark_orphans(self, snap):
        """A job still queued past the held window, beside a free slot its
        set has shown unbroken for heldForSeconds (no cap change since), was
        offered room and never took it: it is not coming. Left in demand it
        wakes a node every time the nodes sleep (Codex on dc38d10d). At most
        as many jobs a pass as there are free slots, oldest first: a backlog behind
        full runners is not orphaned by one runner finishing (delta review
        of 4073dead). A slot free for 15 min beside waiting jobs means GitHub
        is not dispatching them to this set, whatever the reason, and more
        room would not change that; so the whole backlog can be set aside,
        a few a pass. Each is set aside for orphanForSeconds (or until the
        waker restarts), then counts again."""
        limit = self.cfg["heldAfterSeconds"] + self.cfg["heldForSeconds"]
        now = dt.datetime.now(dt.timezone.utc)
        t = self.clock()
        new = set()
        caps = spare_caps(self.cfg, snap["live"], snap.get("room", {}))
        for s in self.cfg["sets"]:
            name = s["name"]
            cap = caps.get(name)
            spare = 0 if (cap is None or name in snap.get("blind", ()) or snap.get("degraded")) \
                else cap - snap["runners"].get(name, 0)
            if spare <= 0:
                self.spare_since.pop(name, None)
                continue
            since = self.spare_since.setdefault(name, t)
            if t - since < self.cfg["heldForSeconds"]:
                continue
            old = sorted((j for j in self.jobs
                          if j.get("id") is not None and j["id"] not in new
                          and owns(s, j)
                          and (now - j["created_at"]).total_seconds() >= limit
                          # The set it counts for, not any it matches: x64's
                          # spare must not orphan zagreus-ci's backlog.
                          and first_set(self.cfg, j, snap["labels"]) == name),
                         key=lambda j: j["created_at"])
            # At most as many per pass as there are free slots. Not charged
            # for ever: an orphan takes no slot, and a cumulative charge
            # pinned nodes awake once orphans outnumbered the cap (delta
            # review of 015feee4). The lasting-spare clock is what protects
            # a real backlog.
            new |= {j["id"] for j in old[:spare]}
        if new:
            LOG.warning("ignoring queued jobs %s: past the held window beside spare slots", sorted(new))
            self.orphans.update(dict.fromkeys(new, t))
            self.jobs = [j for j in self.jobs if j.get("id") not in new]
        return bool(new)

    def warn_floor_matches(self, snap):
        """A floored set claims every job it matches, nodes asleep or not. A
        queued job it matches by shared labels alone could have run on an
        idle classic runner; once claimed it waits for a wake instead. No
        spritz job does that today (they name the set): log it, once a job,
        so a new one shows."""
        floored = {s["name"] for s in self.cfg["sets"] if int(s.get("floor", 0)) > 0}
        if not floored:
            return
        self.floor_warned &= {j.get("id") for j in self.jobs}  # only jobs still queued
        for j in self.jobs:
            name = first_set(self.cfg, j, snap["labels"])
            if name in floored and j.get("id") not in self.floor_warned and \
                    not job_match(j["labels"], name, snap["labels"].get(name, []))[1]:
                self.floor_warned.add(j.get("id"))
                LOG.warning("job %s (%s) matches %s by shared labels only: with the set's floor it may "
                            "be claimed while the nodes sleep and wait for a wake", j.get("id"),
                            ",".join(j["labels"]), name)

    def _room_if_awake(self, snap, nodes):
        """Caps the nodes would add once awake, from their room on paper."""
        states = snap["states"]
        # A powered-off node carries the unreachable taint, which lifts once
        # it is Ready: held against it, every node looked roomless and every
        # wake woke them all (live, 26 Oct 2026).
        # The same for Cilium's agent-not-ready, and for the waker's own
        # cordon, which the wake takes off: held against a sleeping node,
        # they made it look roomless too, and every wake from all-asleep
        # woke all three nodes for any demand (4 to 6 Nov 2026). A cordon a
        # person put on still counts: that node offers nothing once awake.
        def hyp_state(v):
            own = v.get("waker_cordon")
            lifted = BOOT_TAINTS + ((CORDON_TAINT,) if own else ())
            taints = [t for t in v.get("taints", [v.get("taint")]) if t is not None]
            # The cordon is judged by its taint alone (the node controller
            # adds it for every cordon): one enforcement, not two.
            return dict(v, ready=True, asleep=False, unschedulable=any(t not in lifted for t in taints))
        hyp = {k: (hyp_state(v) if k in nodes else v) for k, v in states.items()}
        # Unplaced claims are need (the caller adds them), not room: placed
        # on paper only in `after`, each would count as a slot the node may
        # not have (delta review of 723c5352).
        share = self.share_args(snap)
        before = compute_caps(self.cfg, states, self.cap_requests(snap), **share)
        after = compute_caps(self.cfg, hyp, self.cap_requests(snap), **share)
        return {s: after[s] - before.get(s, 0) for s in after}

    def wake(self, node, snap):
        return node in self.wake_many([node], snap)

    def wake_many(self, nodes, snap):
        """Power every node on, wait for each, then raise the caps once for
        all of them (see choose_wakes). Returns the nodes that woke; one
        that fails is backed off and does not hold the others back."""
        start = self.clock()
        self.last_wake_at = start
        powered, woke, ready_s = [], [], {}
        for node in nodes:
            try:
                self.set_state_annotation(node, "waking")
                self._power_on(node)
                powered.append(node)
            except Exception as e:
                self._wake_failed(node, e)
        for node in powered:
            try:
                # The deadline runs from the first power-on: they boot together.
                left = max(0, self.cfg["readyTimeoutSeconds"] - (self.clock() - start))
                self._wait(lambda: node_fresh_ready(self._node(node) or {}, self.clock(),
                                                    self._lease_age(node, strict=True)),
                           left, f"{node} Ready")
                ready_s[node] = self.clock() - start
                self._wait(lambda: self._heartbeat_ready(node),
                           self.cfg["heartbeatPodTimeoutSeconds"], f"{node} heartbeat pod", fatal=False)
                # Back on the expected-node list before it takes work: its
                # heartbeats are flowing, and the assertion lingers in the
                # alert's 150 s lookback while the first ones arrive.
                self.mark_asleep(node, False)
                self.cordon(node, False)
                woke.append(node)
            except Exception as e:
                self._wake_failed(node, e)
        if not woke:
            return []
        # Raise caps from a fresh look, not the snapshot taken while the
        # nodes were asleep: uncordoning does not lift a taint (Codex on
        # #242). One raise for every node that woke.
        try:
            snap.update(self.observe())
        except Exception as e:
            LOG.warning("re-observe after waking %s failed (%s); caps follow next pass", woke, e)
        else:
            self.apply_caps(snap)
        for node in woke:
            self.pending_first_job[node] = start
            self.idle_since[node] = self.clock()
            self.awake_since[node] = start
            self.record_event("woke", node, ready_seconds=round(ready_s[node]))
        return woke

    def _wake_failed(self, node, e):
        LOG.error("wake of %s failed: %s", node, e)
        self.backoff_until[node] = self.clock() + self.cfg["failureBackoffSeconds"]
        # Off the asleep list, so the node's missing heartbeat raises
        # ZagreusNodeKmsgGone: a node that will not wake is a fault.
        try:
            self.mark_asleep(node, False)
            self.set_state_annotation(node, "wake-failed")
        except Exception as e2:
            LOG.error("could not clear %s from the asleep list: %s", node, e2)
        self.record_event("wake-failed", node, error=str(e)[:200])

    def _power_on(self, node):
        if self.amt.available(node):
            try:
                if self.amt.power_state(node) == POWER_ON:
                    LOG.info("%s already on per AMT", node)
                    return
                self.amt.request_power(node, POWER_ON)
                self._wait(lambda: self.amt.power_state(node) == POWER_ON,
                           self.cfg["powerOnTimeoutSeconds"], f"{node} AMT power on")
                return
            except Exception as e:
                if not self.wol.get(node):
                    raise
                LOG.warning("AMT power-on of %s failed (%s); trying Wake-on-LAN", node, e)
        if self.wol.get(node) and self.wol.get("_broadcast"):
            send_wol(self.wol[node], self.wol["_broadcast"])
            return
        raise RuntimeError(f"no way to power {node} on (no AMT credentials or address, no Wake-on-LAN)")

    def _heartbeat_ready(self, node):
        hb = self.cfg["heartbeat"]
        q = urllib.parse.urlencode({"labelSelector": hb["label"], "fieldSelector": f"spec.nodeName={node}"})
        pods = (self.kube.get(f"/api/v1/namespaces/{hb['namespace']}/pods?{q}") or {}).get("items", [])
        for p in pods:
            for c in p.get("status", {}).get("conditions", []):
                if c.get("type") == "Ready" and c.get("status") == "True":
                    return True
        return False

    # ----- sleep

    def put_to_sleep(self, node, snap):
        cfg = self.cfg
        if always_on(cfg, node):
            LOG.error("not sleeping %s: it is always on", node)
            return False
        unknown = sorted(n for n, c in snap["ceilings"].items() if c is None) + sorted(snap.get("degraded", ()))
        if unknown:
            # No ceiling has ever been read for a set: keep things as they
            # are until one can be.
            LOG.error("not sleeping %s: no ceiling known for %s", node, unknown)
            return False
        st = snap["states"][node]
        # 1. Lower the caps as if the node were gone, so no set can claim a
        #    job this node's slots would have run.
        without = {k: dict(v) for k, v in snap["states"].items()}
        without[node]["unschedulable"] = True
        self.apply_caps(snap, without, force=True)
        self._pause(cfg["listenerSettleSeconds"])
        # 2. Cordon, then look again: a pod may have landed between the idle
        #    check and the cordon.
        self.cordon(node, True)
        self._pause(cfg["drainRecheckSeconds"])
        if self._busy(node):
            return self._abort_sleep(node, snap, "a pod landed while draining")
        if self._unplaced():
            return self._abort_sleep(node, snap, "a driven set has a runner not yet on a node")
        # 3. Off the expected-node list before the power goes.
        self.mark_asleep(node, True)
        self._pause(cfg["assertLeadSeconds"])
        if self._busy(node):
            return self._abort_sleep(node, snap, "a pod landed after the cordon", unassert=True)
        # A set with a floor keeps claiming through the lead: a claim now
        # would only wake this node again once it is off.
        if self._unplaced():
            return self._abort_sleep(node, snap, "a driven set has a runner not yet on a node", unassert=True)
        # 4. Shut down, and confirm through AMT. The time goes on the node
        #    first: until it has been Ready since then, its Ready is stale.
        ip = snap["ips"].get(node)
        self.kube.merge_patch(f"/api/v1/nodes/{node}", {"metadata": {"annotations": {
            ANNOTATION_SLEPT_AT: str(int(self.clock()))}}})
        try:
            if not ip:
                raise RuntimeError("no InternalIP")
            self.talos.shutdown(ip)
            off = self._wait_off(node)
        except Exception as e:
            # A failed or timed-out call may still have been accepted: the
            # node may be going down. Only an answer that it is still on
            # makes this an abort.
            LOG.warning("talosctl shutdown of %s: %s; checking whether it is going down anyway", node, e)
            if not self._wait_off(node, 120):
                if not self.amt.available(node) or self._amt_on(node):
                    if not self.amt.available(node):
                        return self._abort_sleep(node, snap, f"talosctl shutdown failed: {e}", unassert=True)
                    # AMT says still on, but an accepted shutdown may yet
                    # finish (Codex on #242): stay cordoned and out of the
                    # caps, keep the slept-at stamp (so it is not taken back
                    # until it has stayed up past STALE_READY_LIMIT_S), and
                    # come off the asleep list so its alert judges it.
                    LOG.warning("talosctl shutdown of %s failed (%s) and it is still on: "
                                "left cordoned and out of the caps", node, e)
                    self.mark_asleep(node, False)
                    self.set_state_annotation(node, "sleep-unconfirmed")
                    self.backoff_until[node] = self.clock() + cfg["failureBackoffSeconds"]
                    self.record_event("sleep-unconfirmed", node, reason=str(e)[:200])
                    return False
                off = False  # AMT unreadable: fall through to the hard-off below
            else:
                off = True
        if not off and self.amt.available(node):
            LOG.warning("%s still on after %ss; AMT power off", node, cfg["offTimeoutSeconds"])
            try:
                self.amt.request_power(node, POWER_OFF_HARD)
                off = self._wait_off(node, 60)
            except Exception as e:
                LOG.error("AMT power off of %s failed: %s", node, e)
        if not off:
            # Not confirmed off: back on the expected list so the alert
            # judges it, and no further power actions on it for a while.
            # It stays cordoned, out of the caps; if it is in fact up, the
            # next pass takes it back (its Ready is no longer judged against
            # a shutdown that did not happen).
            self.mark_asleep(node, False)
            self.kube.merge_patch(f"/api/v1/nodes/{node}", {"metadata": {"annotations": {
                ANNOTATION_SLEPT_AT: None, ANNOTATION_STATE: "sleep-unconfirmed"}}})
            self.backoff_until[node] = self.clock() + cfg["failureBackoffSeconds"]
            self.record_event("sleep-unconfirmed", node)
            return False
        self.set_state_annotation(node, "asleep")
        st.update({"asleep": True, "ready": False})
        self.idle_since.pop(node, None)
        awake_from = self.awake_since.pop(node, None)
        self.record_event("slept", node,
                          awake_seconds=round(self.clock() - awake_from) if awake_from else None)
        return True

    def _abort_sleep(self, node, snap, why, unassert=False):
        LOG.warning("not sleeping %s: %s", node, why)
        if unassert:
            self.mark_asleep(node, False)
        self.cordon(node, False)
        self.idle_since.pop(node, None)
        self.apply_caps(snap)  # back up: the node is still awake
        self.record_event("sleep-aborted", node, reason=why[:200])
        return False

    def _amt_on(self, node):
        try:
            return self.amt.power_state(node) == POWER_ON
        except Exception:
            return False

    def _unplaced(self):
        """Mid-sleep re-check. A degraded re-observe (a scale set unreadable)
        cannot see that set's runners at all, so it counts as unplaced."""
        snap = self.observe()
        return bool(snap.get("degraded")) or sum(snap["unplaced"].values()) > 0

    def _busy(self, node):
        q = urllib.parse.urlencode({"fieldSelector": f"spec.nodeName={node}"})
        pods = self.kube.get(f"/api/v1/pods?{q}").get("items", [])
        return any(pod_holds_node(p, self.cfg["evictableNamespaces"]) for p in pods)

    def _wait_off(self, node, timeout=None):
        timeout = timeout or self.cfg["offTimeoutSeconds"]
        if self.amt.available(node):
            check = lambda: self.amt.power_state(node) in OFF_STATES
        else:
            # Without AMT, the best evidence is the kubelet gone quiet: its
            # Lease stops, well before the node controller flips Ready.
            # Off means the kubelet's Lease has stopped, nothing else: not
            # "Ready since the stamp" (the stamp was just written, so every
            # node fails that), and not the Ready condition (~40 s late, or
            # never if the node controller is down). strict: an unreadable
            # Lease raises and _wait polls again; it is never evidence of off.
            check = lambda: self._lease_age(node, strict=True) > LEASE_FRESH_S
        try:
            self._wait(check, timeout, f"{node} off")
        except TimeoutError:
            return False
        if not self.amt.available(node):
            # Talos stops the kubelet before it unmounts and powers off: give
            # the rest of the shutdown a margin before calling the node off.
            self._pause(30)
        return True

    # ----- helpers

    def _pause(self, seconds):
        """Sleep inside a procedure, renewing the asleep list as it goes: a
        sleep or wake can run for ten minutes, longer than the heartbeat
        pod's renewal window, and a lapse would make every sleeping node
        alert."""
        end = self.clock() + seconds
        while True:
            left = end - self.clock()
            if left <= 0:
                return
            self.sleep(min(left, 30))
            self._renew()

    def _renew(self):
        if not self.may_power:
            return
        try:
            listed = self.read_asleep()
            if listed:
                self.write_asleep(listed)
        except Exception as e:
            LOG.warning("could not renew the asleep list: %s", e)

    def _node(self, node):
        return self.kube.get(f"/api/v1/nodes/{node}")

    def _lease_age(self, node, strict=False):
        """Seconds since the API server recorded the node's last Lease
        renewal, by the control plane's clock.

        Unreadable (API error, no Lease, no stamp): strict raises
        LeaseUnreadable, so a wait for "off" keeps waiting; otherwise the
        age from the last good read is reused for up to LEASE_HOLD_S, then
        None (not alive)."""
        now = self.clock()
        try:
            stamp = lease_stamp(self.kube.get(LEASE.format(name=node)))
            if stamp is None:
                raise LeaseUnreadable(f"no Lease stamp for {node}")
        except Exception as e:
            if strict:
                raise LeaseUnreadable(str(e)) from e
            LOG.warning("lease unreadable for %s: %s", node, e)
            # Held only while the renewal it describes is under LEASE_HOLD_S
            # old: bounded by the kubelet's last renewal, not our last read.
            last = self.lease_last.get(node)
            if last is None and self.first_pass:
                self.lease_uncertain.add(node)
            if last and last[1] + (now - last[0]) <= LEASE_HOLD_S:
                return last[1]
            return None
        age = max(0.0, now - stamp)
        self.lease_last[node] = (now, age)
        return age

    def _wait(self, cond, timeout, what, fatal=True, step=5):
        deadline = self.clock() + timeout
        while True:
            try:
                if cond():
                    return True
            except Exception as e:
                LOG.debug("waiting for %s: %s", what, e)
            if self.clock() >= deadline:
                if fatal:
                    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
                LOG.warning("timed out after %ss waiting for %s; carrying on", timeout, what)
                return False
            self._pause(step)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def build(cfg):
    token = os.environ.get("GITHUB_TOKEN", "")
    githubs = {"default": GitHub(token, cfg["githubApi"])}
    for name, app in (cfg.get("githubApps") or {}).items():
        githubs[name] = GitHub(None, cfg["githubApi"], auth=GitHubApp(app["dir"], cfg["githubApi"]).token)
    amt = AMT(env_map(os.environ.get("AMT_HOSTS")), os.environ.get("AMT_USER", ""),
              os.environ.get("AMT_PASSWORD", ""))
    wol = env_map(os.environ.get("WOL_MACS"))
    if os.environ.get("WOL_BROADCAST"):
        wol["_broadcast"] = os.environ["WOL_BROADCAST"]
    talos = Talos(os.environ.get("TALOSCTL", "/talosctl/talosctl"),
                  os.environ.get("TALOSCONFIG", "/secrets/talosconfig"))
    return Waker(cfg, Kube(), githubs, amt, talos, wol), token


def main(argv):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(os.environ.get("WAKER_CONFIG", "/etc/waker/waker.conf"))
    waker, token = build(cfg)

    # One-shot commands for the go-live proofs (README, "Go-live").
    if len(argv) >= 2 and argv[0] == "amt-status":
        print(waker.amt.power_state(argv[1]))
        return 0
    if len(argv) >= 2 and argv[0] == "amt-on":
        waker.amt.request_power(argv[1], POWER_ON)
        return 0

    if not token:
        LOG.error("GITHUB_TOKEN is not set: demand cannot be read, so nothing will wake")
    if cfg["mode"] == "full" and not waker.talos.available():
        LOG.error("mode full without talosctl or a talosconfig: sleeps will abort")
    unwakeable = unwakeable_nodes(cfg, waker.wakeable())
    if unwakeable:
        LOG.error("no AMT address or credentials (and no Wake-on-LAN) for %s: "
                  "they will never be put to sleep", unwakeable)
    LOG.info("zagreus waker starting: mode=%s nodes=%s sets=%s", cfg["mode"],
             [n["name"] for n in cfg["nodes"]], [s["name"] for s in cfg["sets"]])
    while True:
        try:
            waker.tick()
        except Exception:
            LOG.exception("tick failed")
        time.sleep(cfg["pollSeconds"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
