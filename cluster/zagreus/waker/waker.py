#!/usr/bin/env python3
"""zagreus node waker: power the amd64 runner nodes on and off, and drive the
ARC scale sets' maxRunners from the slots on the nodes that are awake.

README.md beside this file is the design. In short:

- maxRunners is what GitHub offers a scale set, whether or not its pods can
  schedule, so a set must claim nothing while its nodes sleep. The caps are
  always "base + slots of the awake, schedulable nodes", never above the
  ceiling in the set's Argo Application.
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
    "wakeCooldownSeconds": 180,
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
    "heartbeat": {"namespace": "log-heartbeat", "label": "app=kmsg-heartbeat"},
    "applicationsNamespace": "argocd",
    "evictableNamespaces": ["kube-system"],
    # Taint keys the runner pods tolerate (none do today). Any other
    # NoSchedule or NoExecute taint takes a node out of the caps.
    "toleratedTaintKeys": [],
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
    for s in cfg["sets"]:
        for key in ("name", "namespace", "application", "repo"):
            if not s.get(key):
                raise ValueError(f"set {s!r} needs {key}")
        if int(s.get("base", 0)) < 0:
            raise ValueError(f"set {s['name']}: base must be >= 0")
    nodes = [n["name"] for n in cfg["nodes"]]
    if len(set(nodes)) != len(nodes):
        raise ValueError("duplicate node in nodes")
    for n in cfg["nodes"]:
        if not NODE_NAME_RE.match(n["name"]):
            raise ValueError(f"node name {n['name']!r} is not a DNS label")
        for set_name, slots in n.get("slots", {}).items():
            if set_name not in names:
                raise ValueError(f"node {n['name']}: slots for unknown set {set_name}")
            if int(slots) < 0:
                raise ValueError(f"node {n['name']}: negative slots for {set_name}")


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


def demand_by_set(cfg, jobs, set_labels, now):
    """Queued jobs per driven set that have waited past their grace.

    jobs: dicts with repo, labels, created_at (aware datetime)."""
    out = {s["name"]: 0 for s in cfg["sets"]}
    for job in jobs:
        age = (now - job["created_at"]).total_seconds()
        for s in cfg["sets"]:
            if job["repo"].lower() != s["repo"].lower():
                continue
            ok, by_name = job_match(job["labels"], s["name"], set_labels.get(s["name"], []))
            if not ok:
                continue
            grace = cfg["namedGraceSeconds"] if by_name else cfg["saturationGraceSeconds"]
            if age >= grace:
                out[s["name"]] += 1
    return out


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


def untolerated_taint(node, tolerated_keys=()):
    """The first NoSchedule or NoExecute taint the runner pods do not
    tolerate (they tolerate none), or None. PreferNoSchedule does not stop
    scheduling."""
    for t in (node.get("spec") or {}).get("taints") or []:
        if t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") not in tolerated_keys:
            return t.get("key")
    return None


def compute_caps(cfg, node_states, set_requests=None, unplaced=None):
    """base + what each awake node can still take, per driven set (no
    ceiling).

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
    the configured slots, as written."""
    caps = {s["name"]: int(s.get("base", 0)) for s in cfg["sets"]}
    room_nodes = []  # (node config, own counts, [free cpu, free mem]) for room-aware awake nodes
    for n in cfg["nodes"]:
        st = node_states.get(n["name"])
        if st is None or not node_awake(st):
            # Its runners, if any, are not counted: on a node that is gone
            # they may be ghosts the API has not cleaned up, and ARC never
            # stops a busy runner because its set's cap went down.
            continue
        if set_requests is None or "free" not in st:
            for set_name, slots in n.get("slots", {}).items():
                caps[set_name] += int(slots)
            continue
        room_nodes.append((n, dict(st.get("own") or {}), list(st["free"])))

    def fits(free, req, k=1):
        cpu, mem = req
        if cpu == float("inf") or mem == float("inf"):
            return 0
        n = k
        if cpu > 0:
            n = min(n, int(max(0.0, free[0]) // cpu))
        if mem > 0:
            n = min(n, int(max(0.0, free[1]) // mem))
        return max(0, n)

    if set_requests is not None:
        # Claimed, unbound runners first: they hold room already.
        for s in cfg["sets"]:
            name = s["name"]
            req = set_requests.get(name, (0, 0))
            for _ in range(int((unplaced or {}).get(name, 0))):
                order = sorted(room_nodes, key=lambda x: -int(x[0].get("slots", {}).get(name, 0)))
                spot = next((x for x in order if fits(x[2], req)), None)
                if spot is None:
                    # Fits nowhere: offering it a slot could only strand it.
                    continue
                spot[1][name] = spot[1].get(name, 0) + 1
                spot[2][0] -= req[0]
                spot[2][1] -= req[1]

    for n, own, free in room_nodes:
        for set_name in caps:
            caps[set_name] += own.get(set_name, 0)
        for s in cfg["sets"]:
            name = s["name"]
            req = set_requests.get(name, (0, 0))
            room = int(n.get("slots", {}).get(name, 0)) - own.get(name, 0)
            fit = fits(free, req, room) if room > 0 else 0
            if fit:
                free[0] -= fit * req[0]
                free[1] -= fit * req[1]
                caps[name] += fit
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


def choose_wake(cfg, unmet, node_states, backoff_until, now_s):
    """The first sleeping node, in configured order, with a slot for a set
    whose demand is unmet."""
    hungry = {name for name, u in unmet.items() if u > 0}
    if not hungry:
        return None
    for n in cfg["nodes"]:
        st = node_states.get(n["name"])
        if st is None:
            continue
        # Listed asleep, or left off the list after a failure and not Ready:
        # retried once its backoff has passed.
        if not (st["asleep"] or (st.get("retry") and not st["ready"])):
            continue
        if backoff_until.get(n["name"], 0) > now_s:
            continue
        if any(int(n.get("slots", {}).get(s, 0)) > 0 for s in hungry):
            return n["name"]
    return None


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
        if wakeable is not None and n["name"] not in wakeable:
            continue
        st = node_states.get(n["name"])
        if st is None or not node_awake(st) or st["busy"]:
            continue
        if backoff_until.get(n["name"], 0) > now_s:
            continue
        since = idle_since.get(n["name"])
        if since is None or now_s - since < cfg["idleMinutes"] * 60:
            continue
        if any(int(n.get("slots", {}).get(s, 0)) > 0 for s in hungry):
            continue
        return n["name"]
    return None


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

    def __init__(self, token, api, clock=time.time):
        self.token = token
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
        req.add_header("Authorization", f"Bearer {self.token}")
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
        self.github = github
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
        self.lease_last = {}  # node -> (time of the last good read, age it gave)

    def wakeable(self):
        """Nodes the waker can power on again; it puts no other to sleep."""
        return {n["name"] for n in self.cfg["nodes"]
                if self.amt.available(n["name"]) or (self.wol.get(n["name"]) and self.wol.get("_broadcast"))}

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

    def mark_asleep(self, node, asleep):
        names = set(self.read_asleep())
        if asleep:
            names.add(node)
        else:
            names.discard(node)
        self.write_asleep(sorted(names))

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
                if set_name in driven:
                    own[set_name] = own.get(set_name, 0) + 1
            states[n["name"]] = {
                "ready": ready,
                "slept_at": ann.get(ANNOTATION_SLEPT_AT),
                "retry": ann.get(ANNOTATION_STATE) in RETRY_STATES,
                "unschedulable": bool(node.get("spec", {}).get("unschedulable")) or taint is not None,
                "taint": taint,
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
            unstarted[s["name"]] = sum(1 for e in live_ers if not e.get("status", {}).get("jobRequestId"))
            placed = sum(1 for p in pods
                         if (p.get("metadata", {}).get("labels") or {}).get(SCALE_SET_LABEL) == s["name"]
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
                "requests": requests if len(requests) == len(live) else None}

    def demand(self, labels):
        now = dt.datetime.now(dt.timezone.utc)
        jobs = []
        for repo in sorted({s["repo"] for s in self.cfg["sets"]}):
            jobs.extend(self.github.queued_jobs(repo, self.cfg["runLookbackHours"], now))
        return demand_by_set(self.cfg, jobs, labels, now)

    # ----- caps

    def apply_caps(self, snap, states=None, force=False):
        """Raise at once. Lower at once too, unless this pass could not read
        some node's Lease and had no earlier reading to go on (the first
        pass after a start, say): then only if the next pass still wants
        it, so one failed read does not flap the listeners. A Lease read
        and found stale is not uncertain, and lowers at once. force: lower
        now (the sleep procedure, before a cordon)."""
        wanted = compute_caps(self.cfg, states or snap["states"], snap.get("requests"), snap.get("unplaced"))
        target = bound_caps(wanted, snap["live"], snap["ceilings"])
        changed = {}
        for s in self.cfg["sets"]:
            name = s["name"]
            want = target.get(name)
            if want is None or name not in snap["live"] or snap["live"][name] == want:
                self.lower_streak.pop(name, None)
                continue
            live = snap["live"][name]
            if snap.get("degraded") and live is not None and want > live:
                continue
            if live is not None and want < live and not force and snap.get("uncertain"):
                self.lower_streak[name] = self.lower_streak.get(name, 0) + 1
                if self.lower_streak[name] < 2:
                    LOG.info("%s maxRunners %s -> %s wanted; lowering on the next pass if still so",
                             name, live, want)
                    continue
            self.lower_streak.pop(name, None)
            changed[name] = (snap["live"][name], want)  # old may be None (unlimited)
            if self.may_patch_caps:
                self.kube.merge_patch(ARS.format(ns=s["namespace"], name=name),
                                      {"spec": {"maxRunners": want}})
                snap["live"][name] = want
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
        snap = self.observe()
        self.first_pass = False
        states = snap["states"]
        if self.may_power:
            self.reconcile_ready_nodes(snap)
            self.audit_asleep_list(snap, now)
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

        self.apply_caps(snap)

        try:
            demand = self.demand(snap["labels"])
        except Exception as e:
            # Demand unknown: neither wake nor sleep on a guess.
            LOG.warning("GitHub demand unreadable, holding: %s", e)
            return
        self.last_demand_ok = now
        unmet = unmet_by_set(demand, snap["live"], snap["runners"], snap["unstarted"])
        for name in snap.get("blind", ()):
            unmet[name] = 0
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
        hungry = dict(unmet)
        for name, u in snap.get("stuck", {}).items():
            if not u:
                self.stuck_woke.discard(name)
            elif name not in self.stuck_woke:
                hungry[name] = max(hungry.get(name, 0), u)
        wake = None
        if now - self.last_wake_at >= self.cfg["wakeCooldownSeconds"]:
            wake = choose_wake(self.cfg, hungry, states, self.backoff_until, now)
        if wake:
            for name, u in snap.get("stuck", {}).items():
                if u and not unmet.get(name):
                    self.stuck_woke.add(name)  # this wake is for the stuck claim
            self._wish("wake", wake, f"for {dict((k, v) for k, v in hungry.items() if v)}")
            if self.may_power:
                self.wake(wake, snap)
            return

        sleep = None if snap.get("degraded") else choose_sleep(
                             self.cfg, states, self.idle_since, unmet, self.backoff_until, now,
                             wakeable=self.wakeable(), unplaced=sum(snap["unplaced"].values()))
        if not wake and not sleep:
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
                if labels.get(SCALE_SET_LABEL) in driven and p.get("status", {}).get("phase") == "Running":
                    self.record_event("first-job", node, seconds=round(self.clock() - started))
                    del self.pending_first_job[node]
                    break

    # ----- wake

    def wake(self, node, snap):
        start = self.clock()
        self.last_wake_at = start
        self.set_state_annotation(node, "waking")
        try:
            self._power_on(node)
            self._wait(lambda: node_fresh_ready(self._node(node) or {}, self.clock(),
                                                self._lease_age(node, strict=True)),
                       self.cfg["readyTimeoutSeconds"], f"{node} Ready")
            ready_s = self.clock() - start
            self._wait(lambda: self._heartbeat_ready(node),
                       self.cfg["heartbeatPodTimeoutSeconds"], f"{node} heartbeat pod", fatal=False)
            # Back on the expected-node list before it takes work: its
            # heartbeats are flowing, and the assertion lingers in the alert's
            # 150 s lookback while the first ones arrive.
            self.mark_asleep(node, False)
            self.cordon(node, False)
        except Exception as e:
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
            return False
        # Raise caps from a fresh look, not the snapshot taken while the node
        # was asleep: uncordoning does not lift a taint (Codex on #242).
        try:
            snap.update(self.observe())
        except Exception as e:
            LOG.warning("re-observe after waking %s failed (%s); caps follow next pass", node, e)
        else:
            self.apply_caps(snap)
        self.pending_first_job[node] = start
        self.idle_since[node] = self.clock()
        self.awake_since[node] = start
        self.record_event("woke", node, ready_seconds=round(ready_s))
        return True

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
    amt = AMT(env_map(os.environ.get("AMT_HOSTS")), os.environ.get("AMT_USER", ""),
              os.environ.get("AMT_PASSWORD", ""))
    wol = env_map(os.environ.get("WOL_MACS"))
    if os.environ.get("WOL_BROADCAST"):
        wol["_broadcast"] = os.environ["WOL_BROADCAST"]
    talos = Talos(os.environ.get("TALOSCTL", "/talosctl/talosctl"),
                  os.environ.get("TALOSCONFIG", "/secrets/talosconfig"))
    return Waker(cfg, Kube(), GitHub(token, cfg["githubApi"]), amt, talos, wol), token


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
    unwakeable = sorted({n["name"] for n in cfg["nodes"]} - waker.wakeable())
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
