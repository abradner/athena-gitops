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
        spare = max(0, live_caps.get(name, 0) - runners.get(name, 0))
        out[name] = max(0, d - claimed - spare)
    return out


def node_awake(node_state):
    """Counts towards the caps: Ready, schedulable, and not declared asleep."""
    return node_state["ready"] and not node_state["unschedulable"] and not node_state["asleep"]


def compute_caps(cfg, node_states):
    """base + the slots of every awake node, per driven set (no ceiling)."""
    caps = {s["name"]: int(s.get("base", 0)) for s in cfg["sets"]}
    for n in cfg["nodes"]:
        st = node_states.get(n["name"])
        if st is None or not node_awake(st):
            continue
        for set_name, slots in n.get("slots", {}).items():
            caps[set_name] += int(slots)
    return caps


def bound_caps(wanted, live, ceilings):
    """Apply each set's ceiling. With no ceiling known, only lowering is safe."""
    out = {}
    for name, want in wanted.items():
        ceiling = ceilings.get(name)
        if ceiling is not None:
            out[name] = min(want, ceiling)
        elif name in live:
            out[name] = min(want, live[name])
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
        if st is None or not st["asleep"]:
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


def choose_sleep(cfg, node_states, idle_since, unmet, backoff_until, now_s):
    """The last awake node, in configured order, idle for idleMinutes, while
    no set it serves has unmet demand."""
    hungry = {name for name, u in unmet.items() if u > 0}
    for n in reversed(cfg["nodes"]):
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
    return [f"log-pipeline asleep {n}" for n in asleep.split() if NODE_NAME_RE.match(n)]


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


def node_ip(node):
    for a in node.get("status", {}).get("addresses", []):
        if a.get("type") == "InternalIP":
            return a["address"]
    return None


# --------------------------------------------------------------------------
# GitHub (queued jobs; Actions: read)
# --------------------------------------------------------------------------

class GitHub:
    def __init__(self, token, api):
        self.token = token
        self.api = api.rstrip("/")

    def _get(self, path):
        req = urllib.request.Request(self.api + path)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def queued_jobs(self, repo, lookback_hours, now):
        """Every queued job in the repo's queued or in-progress runs.

        A run is in_progress as soon as one of its jobs starts, so its other
        jobs can be queued inside it: both lists are read."""
        since = (now - dt.timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs = []
        for status in ("queued", "in_progress"):
            q = urllib.parse.urlencode({"status": status, "per_page": 100, "created": f">={since}"})
            runs = self._get(f"/repos/{repo}/actions/runs?{q}").get("workflow_runs", [])
            for run in runs:
                page = self._get(f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
                for j in page.get("jobs", []):
                    if j.get("status") != "queued":
                        continue
                    jobs.append({
                        "repo": repo,
                        "id": j["id"],
                        "name": j.get("name", ""),
                        "labels": j.get("labels", []),
                        "created_at": parse_time(j["created_at"]),
                    })
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
        asleep = set(self.read_asleep())
        pods = self.kube.get("/api/v1/pods").get("items", [])
        by_node = {}
        for p in pods:
            by_node.setdefault(p.get("spec", {}).get("nodeName"), []).append(p)

        states = {}
        ips = {}
        for n in cfg["nodes"]:
            node = self.kube.get(f"/api/v1/nodes/{n['name']}")
            if node is None:
                continue
            ann = node["metadata"].get("annotations", {}) or {}
            states[n["name"]] = {
                "ready": node_ready(node),
                "unschedulable": bool(node.get("spec", {}).get("unschedulable")),
                "waker_cordon": ann.get(ANNOTATION_CORDON) == "true",
                "asleep": n["name"] in asleep,
                "busy": any(pod_holds_node(p, cfg["evictableNamespaces"]) for p in by_node.get(n["name"], [])),
            }
            ips[n["name"]] = node_ip(node)

        live, labels, ceilings, runners, unstarted = {}, {}, {}, {}, {}
        for s in cfg["sets"]:
            ars = self.kube.get(ARS.format(ns=s["namespace"], name=s["name"]))
            if ars is None:
                continue
            live[s["name"]] = int(ars["spec"].get("maxRunners", 0))
            labels[s["name"]] = ars["spec"].get("runnerScaleSetLabels", []) or []
            app = self.kube.get(APP.format(ns=cfg["applicationsNamespace"], name=s["application"]))
            try:
                ceilings[s["name"]] = int(app["spec"]["source"]["helm"]["valuesObject"]["maxRunners"])
            except (TypeError, KeyError, ValueError):
                ceilings[s["name"]] = None
            q = urllib.parse.urlencode({"labelSelector": f"{SCALE_SET_LABEL}={s['name']}"})
            ers = self.kube.get(ERS.format(ns=s["namespace"]) + "?" + q).get("items", [])
            runners[s["name"]] = len(ers)
            unstarted[s["name"]] = sum(1 for e in ers if not e.get("status", {}).get("jobRequestId"))

        return {"states": states, "ips": ips, "pods_by_node": by_node, "live": live,
                "labels": labels, "ceilings": ceilings, "runners": runners,
                "unstarted": unstarted}

    def demand(self, labels):
        now = dt.datetime.now(dt.timezone.utc)
        jobs = []
        for repo in sorted({s["repo"] for s in self.cfg["sets"]}):
            jobs.extend(self.github.queued_jobs(repo, self.cfg["runLookbackHours"], now))
        return demand_by_set(self.cfg, jobs, labels, now)

    # ----- caps

    def apply_caps(self, snap, states=None):
        wanted = compute_caps(self.cfg, states or snap["states"])
        target = bound_caps(wanted, snap["live"], snap["ceilings"])
        changed = {}
        for s in self.cfg["sets"]:
            name = s["name"]
            want = target.get(name)
            if want is None or name not in snap["live"] or snap["live"][name] == want:
                continue
            changed[name] = (snap["live"][name], want)
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
                    "metadata": {"annotations": {ANNOTATION_CORDON: None, ANNOTATION_STATE: "awake"}}}
        self.kube.merge_patch(f"/api/v1/nodes/{node}", body)

    def set_state_annotation(self, node, value):
        self.kube.merge_patch(f"/api/v1/nodes/{node}",
                              {"metadata": {"annotations": {ANNOTATION_STATE: value}}})

    # ----- one pass

    def reconcile_ready_nodes(self, snap):
        """Between procedures no node should be both Ready and asleep, or
        Ready and under our cordon. Either means it came back on by hand, or a
        wake or sleep was interrupted: take it back as awake."""
        for name, st in snap["states"].items():
            if not st["ready"] or not (st["asleep"] or st["waker_cordon"]):
                continue
            LOG.info("%s is Ready but %s: taking it back as awake", name,
                     "listed asleep" if st["asleep"] else "under the waker's cordon")
            if not self.may_power:
                continue
            if st["asleep"]:
                self.mark_asleep(name, False)
                st["asleep"] = False
            if st["waker_cordon"]:
                self.cordon(name, False)
                st["waker_cordon"] = False
                st["unschedulable"] = False

    def tick(self):
        now = self.clock()
        snap = self.observe()
        states = snap["states"]
        if self.may_power:
            self.reconcile_ready_nodes(snap)
            self._renew()  # or the heartbeat pod stops asserting

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
        unmet = unmet_by_set(demand, snap["live"], snap["runners"], snap["unstarted"])
        if any(demand.values()):
            LOG.info("demand %s unmet %s runners %s caps %s", demand, unmet, snap["runners"], snap["live"])

        wake = None
        if now - self.last_wake_at >= self.cfg["wakeCooldownSeconds"]:
            wake = choose_wake(self.cfg, unmet, states, self.backoff_until, now)
        if wake:
            LOG.info("would wake %s for %s", wake, {k: v for k, v in unmet.items() if v})
            if self.may_power:
                self.wake(wake, snap)
            return

        sleep = choose_sleep(self.cfg, states, self.idle_since, unmet, self.backoff_until, now)
        if sleep:
            LOG.info("would sleep %s (idle since %s)", sleep,
                     dt.datetime.fromtimestamp(self.idle_since[sleep], dt.timezone.utc).isoformat())
            if self.may_power:
                self.put_to_sleep(sleep, snap)

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
            self._wait(lambda: self._node(node) and node_ready(self._node(node)),
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
        snap["states"][node].update({"asleep": False, "unschedulable": False, "ready": True, "busy": False})
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
        st = snap["states"][node]
        # 1. Lower the caps as if the node were gone, so no set can claim a
        #    job this node's slots would have run.
        without = {k: dict(v) for k, v in snap["states"].items()}
        without[node]["unschedulable"] = True
        self.apply_caps(snap, without)
        self._pause(cfg["listenerSettleSeconds"])
        # 2. Cordon, then look again: a pod may have landed between the idle
        #    check and the cordon.
        self.cordon(node, True)
        self._pause(cfg["drainRecheckSeconds"])
        if self._busy(node):
            return self._abort_sleep(node, snap, "a pod landed while draining")
        # 3. Off the expected-node list before the power goes.
        self.mark_asleep(node, True)
        self._pause(cfg["assertLeadSeconds"])
        if self._busy(node):
            return self._abort_sleep(node, snap, "a pod landed after the cordon", unassert=True)
        # 4. Shut down, and confirm through AMT.
        ip = snap["ips"].get(node)
        try:
            if not ip:
                raise RuntimeError("no InternalIP")
            self.talos.shutdown(ip)
        except Exception as e:
            return self._abort_sleep(node, snap, f"talosctl shutdown failed: {e}", unassert=True)
        off = self._wait_off(node)
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
            self.mark_asleep(node, False)
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

    def _busy(self, node):
        q = urllib.parse.urlencode({"fieldSelector": f"spec.nodeName={node}"})
        pods = self.kube.get(f"/api/v1/pods?{q}").get("items", [])
        return any(pod_holds_node(p, self.cfg["evictableNamespaces"]) for p in pods)

    def _wait_off(self, node, timeout=None):
        timeout = timeout or self.cfg["offTimeoutSeconds"]
        if self.amt.available(node):
            check = lambda: self.amt.power_state(node) in OFF_STATES
        else:
            # Without AMT, the best evidence is the kubelet gone quiet.
            check = lambda: not node_ready(self._node(node) or {})
        try:
            self._wait(check, timeout, f"{node} off")
            return True
        except TimeoutError:
            return False

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
