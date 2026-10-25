"""Tests for the zagreus waker. Standard library only:

    python3 -m unittest discover -s cluster/zagreus/waker/tests -v

The fakes stand in for the Kubernetes API, GitHub, AMT and talosctl, and
record every write in order, so the tests can assert what happens before the
power goes off as well as that it goes off.
"""

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
WAKER_DIR = os.path.dirname(HERE)
sys.path.insert(0, WAKER_DIR)

import waker as w  # noqa: E402

UTC = dt.timezone.utc
T0 = 1_800_000_000.0  # fake epoch seconds
STALE_READY_SECONDS = 45  # the API server's node-monitor grace: Ready outlives the kubelet


def iso(t):
    return dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def production_config():
    return w.load_config(os.path.join(WAKER_DIR, "waker.conf"))


def cfg_with(**over):
    cfg = production_config()
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeKube:
    """Just enough of the API server for the paths waker.py uses."""

    def __init__(self, cfg, clock):
        self.clock = clock
        self.boots = {}  # node -> time its kubelet reports Ready
        self.going_down = {}  # node -> time the API server stops reporting it Ready
        self.dead = set()  # nodes whose kubelet is gone: their Lease stops renewing
        self.last_renew = {}
        self.lease_skew = {}  # node -> seconds its (worker) clock is off by
        self.lease_fail = set()  # nodes whose Lease GET errors
        self.lease_extra_fields = {}  # node -> other managers' managedFields entries
        self.fail_get = []  # regexes: a GET of a matching path raises (403/5xx)
        self.writes = []  # (verb, path, body) in order
        self.asleep_writes = []  # (time, asleep) for every write of the asleep list
        self.nodes = {}
        self.pods = []
        self.ars = {}
        self.apps = {}
        self.ers = {}
        self.cms = {}
        self.on_write = None
        for s in cfg["sets"]:
            self.ars[(s["namespace"], s["name"])] = {
                "spec": {"maxRunners": 0, "runnerScaleSetLabels": SET_LABELS[s["name"]],
                         "template": {"spec": copy.deepcopy(TEMPLATES[s["name"]])}}}
            self.apps[s["application"]] = {
                "spec": {"source": {"helm": {"valuesObject": {"maxRunners": CEILINGS[s["name"]]}}}}}
            self.ers[s["namespace"]] = []
        for n in cfg["nodes"]:
            self.add_node(n["name"], ready=True)
        # As on the live cluster (23 Oct 2026): both coredns replicas on 01.
        for i in range(2):
            self.pods.append({
                "metadata": {"name": f"coredns-{i}", "namespace": "kube-system", "labels": {"k8s-app": "kube-dns"},
                             "ownerReferences": [{"kind": "ReplicaSet", "name": "coredns-x"}]},
                "spec": {"nodeName": cfg["nodes"][0]["name"],
                         "containers": [{"name": "coredns", "resources": {"requests": {"cpu": "100m", "memory": "70Mi"}}}]},
                "status": {"phase": "Running"}})

    def add_node(self, name, ready):
        self.nodes[name] = {
            "metadata": {"name": name, "annotations": {}},
            "spec": {},
            "status": {"allocatable": dict(ALLOCATABLE.get(name, {"cpu": "8", "memory": "16Gi"})),
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False",
                                       "lastTransitionTime": iso(self.clock() - 86400)}],
                       "addresses": [{"type": "InternalIP", "address": f"192.0.2.{len(self.nodes) + 10}"}]},
        }
        # Each node runs a heartbeat DaemonSet pod.
        self.pods.append({
            "metadata": {"name": f"hb-{name}", "namespace": "log-heartbeat", "labels": {"app": "kmsg-heartbeat"},
                         "ownerReferences": [{"kind": "DaemonSet", "name": "kmsg-heartbeat"}]},
            "spec": {"nodeName": name},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        })

    def set_ready(self, name, ready):
        cond = self.nodes[name]["status"]["conditions"][0]
        status = "True" if ready else "False"
        if cond["status"] != status:
            cond["status"] = status
            cond["lastTransitionTime"] = iso(self.clock())

    def add_runner_pod(self, node, set_name, phase="Running", name=None):
        spec = copy.deepcopy(TEMPLATES.get(set_name, HEAVY))
        spec["nodeName"] = node
        self.pods.append({
            "metadata": {"name": name or f"{set_name}-runner-{len(self.pods)}", "namespace": "arc-runners",
                         "labels": {w.SCALE_SET_LABEL: set_name},
                         "ownerReferences": [{"kind": "EphemeralRunner", "name": "x"}]},
            "spec": spec,
            "status": {"phase": phase},
        })

    def taint(self, node, key, effect="NoSchedule"):
        self.nodes[node]["spec"].setdefault("taints", []).append({"key": key, "effect": effect})

    # --- API

    def get(self, path):
        for pat in self.fail_get:
            if re.search(pat, path):
                raise RuntimeError(f"GET {path}: 403 forbidden")
        u = urllib.parse.urlparse(path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        if p == "/api/v1/pods":
            items = self.pods
            if "fieldSelector" in q:
                node = q["fieldSelector"][0].split("=", 1)[1]
                items = [x for x in items if x["spec"].get("nodeName") == node]
            return {"items": copy.deepcopy(items)}
        m = re.fullmatch(r"/api/v1/namespaces/([^/]+)/pods", p)
        if m:
            node = q["fieldSelector"][0].split("=", 1)[1]
            label = q["labelSelector"][0]
            k, v = label.split("=")
            return {"items": copy.deepcopy([x for x in self.pods if x["metadata"]["namespace"] == m.group(1)
                                            and x["spec"].get("nodeName") == node
                                            and x["metadata"].get("labels", {}).get(k) == v])}
        m = re.fullmatch(r"/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/([^/]+)", p)
        if m:
            name = m.group(1)
            if name not in self.nodes:
                return None
            if name in self.lease_fail:
                raise RuntimeError("GET lease: 500")
            if name not in self.dead:
                self.last_renew[name] = self.clock()
            t = self.last_renew.get(name, self.clock() - 86400)
            # renewTime is the worker's (possibly skewed) clock; the
            # managedFields stamp is the API server's.
            fields = [{"manager": "kubelet", "operation": "Update", "time": iso(t)}]
            fields += self.lease_extra_fields.get(name, [])
            return {"spec": {"renewTime": iso(t + self.lease_skew.get(name, 0))},
                    "metadata": {"managedFields": fields}}
        m = re.fullmatch(r"/api/v1/nodes/([^/]+)", p)
        if m:
            name = m.group(1)
            if name in self.going_down and self.clock() >= self.going_down[name]:
                self.set_ready(name, False)
                del self.going_down[name]
            if name in self.boots and self.clock() >= self.boots[name]:
                self.set_ready(name, True)  # the kubelet comes up on its own
                self.dead.discard(name)
                del self.boots[name]
            return copy.deepcopy(self.nodes.get(name))
        m = re.fullmatch(r"/apis/actions.github.com/v1alpha1/namespaces/([^/]+)/autoscalingrunnersets/([^/]+)", p)
        if m:
            return copy.deepcopy(self.ars.get((m.group(1), m.group(2))))
        m = re.fullmatch(r"/apis/actions.github.com/v1alpha1/namespaces/([^/]+)/ephemeralrunners", p)
        if m:
            k, v = q["labelSelector"][0].split("=")
            return {"items": [e for e in self.ers.get(m.group(1), []) if e["metadata"]["labels"].get(k) == v]}
        m = re.fullmatch(r"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/([^/]+)", p)
        if m:
            return copy.deepcopy(self.apps.get(m.group(1)))
        m = re.fullmatch(r"/api/v1/namespaces/([^/]+)/configmaps/([^/]+)", p)
        if m:
            return copy.deepcopy(self.cms.get((m.group(1), m.group(2))))
        raise AssertionError(f"unexpected GET {path}")

    def _record(self, verb, path, body):
        self.writes.append((verb, path, copy.deepcopy(body)))
        if path.startswith("/api/v1/namespaces/log-heartbeat/configmaps"):
            self.asleep_writes.append((self.clock(), body["data"]["asleep"]))
        if self.on_write:
            self.on_write(verb, path, body)

    def merge_patch(self, path, body):
        self._record("PATCH", path, body)
        m = re.fullmatch(r"/api/v1/nodes/([^/]+)", path)
        if m:
            node = self.nodes[m.group(1)]
            if "spec" in body:
                if body["spec"].get("unschedulable") is None:
                    node["spec"].pop("unschedulable", None)
                else:
                    node["spec"]["unschedulable"] = body["spec"]["unschedulable"]
            for k, v in body.get("metadata", {}).get("annotations", {}).items():
                if v is None:
                    node["metadata"]["annotations"].pop(k, None)
                else:
                    node["metadata"]["annotations"][k] = v
            return node
        m = re.fullmatch(r"/apis/actions.github.com/v1alpha1/namespaces/([^/]+)/autoscalingrunnersets/([^/]+)", path)
        if m:
            self.ars[(m.group(1), m.group(2))]["spec"]["maxRunners"] = body["spec"]["maxRunners"]
            return {}
        m = re.fullmatch(r"/api/v1/namespaces/([^/]+)/configmaps/([^/]+)", path)
        if m:
            self.cms[(m.group(1), m.group(2))]["data"].update(body["data"])
            return {}
        raise AssertionError(f"unexpected PATCH {path}")

    def create(self, path, body):
        self._record("POST", path, body)
        m = re.fullmatch(r"/api/v1/namespaces/([^/]+)/configmaps", path)
        if m:
            self.cms[(m.group(1), body["metadata"]["name"])] = copy.deepcopy(body)
            return {}
        raise AssertionError(f"unexpected POST {path}")

    # --- helpers for assertions

    def caps(self):
        return {name: a["spec"]["maxRunners"] for (_, name), a in self.ars.items()}

    def asleep(self):
        cm = self.cms.get(("log-heartbeat", "waker-asleep"))
        return set(cm["data"]["asleep"].split()) if cm else set()


SET_LABELS = {
    "zagreus-ci": ["self-hosted", "linux", "spritz-ci", "playwright-full"],
    "zagreus-ci-x64": ["self-hosted", "linux", "X64", "spritz-e2e"],
    "zagreus-docker-amd64": ["self-hosted", "linux", "X64", "docker-amd64"],
}
CEILINGS = {"zagreus-ci": 3, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 1}
# As on the live cluster (23 Oct 2026): the runner pod templates' requests
# (the builder's dind is a native sidecar, so it counts in full) and the
# amd64 nodes' allocatable.
HEAVY = {"containers": [{"name": "runner", "resources": {"requests": {"cpu": "1500m", "memory": "6Gi"}}}],
         "initContainers": [{"name": "bundle-seed", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}]}
TEMPLATES = {
    "zagreus-ci": HEAVY,
    "zagreus-ci-x64": HEAVY,
    "zagreus-docker-amd64": {
        "containers": [{"name": "runner", "resources": {"requests": {"cpu": "250m", "memory": "768Mi"}}}],
        "initContainers": [{"name": "init-dind-externals"},
                           {"name": "dind", "restartPolicy": "Always",
                            "resources": {"requests": {"cpu": "1", "memory": "3Gi"}}}]},
}
ALLOCATABLE = {"zag-amd64-01": {"cpu": "7950m", "memory": "23562480Ki"},
               "zag-amd64-02": {"cpu": "7950m", "memory": "15200576Ki"}}


class FakeGitHub:
    def __init__(self):
        self.jobs = []
        self.fail = False

    def queued_jobs(self, repo, lookback_hours, now):
        if self.fail:
            raise RuntimeError("GitHub is down")
        return [j for j in self.jobs if j["repo"] == repo]


class FakeAMT:
    def __init__(self, kube, clock, boot_seconds=60):
        self.kube = kube
        self.clock = clock
        self.state = {}
        self.calls = []
        self.boot_seconds = boot_seconds
        self.refuse_on = set()
        self.missing = set()  # nodes with no AMT address

    def available(self, node):
        return node not in self.missing

    def power_state(self, node):
        return self.state.get(node, w.POWER_ON)

    def request_power(self, node, state):
        self.calls.append((node, state))
        self.kube.writes.append(("AMT", node, state))
        if state == w.POWER_ON and node in self.refuse_on:
            raise RuntimeError("AMT unreachable")
        self.state[node] = state
        if state == w.POWER_ON:
            self.kube.boots[node] = self.clock() + self.boot_seconds


class FakeTalos:
    def __init__(self, kube, amt):
        self.kube = kube
        self.amt = amt
        self.calls = []

    def available(self):
        return True

    def shutdown(self, ip):
        node = next(n for n, o in self.kube.nodes.items()
                    if o["status"]["addresses"][0]["address"] == ip)
        self.calls.append(node)
        self.kube.writes.append(("SHUTDOWN", node, None))
        # Off at once as AMT sees it; still Ready as the API server sees it.
        self.kube.going_down[node] = self.kube.clock() + STALE_READY_SECONDS
        self.kube.last_renew[node] = self.kube.clock()  # its kubelet's last renewal
        self.kube.dead.add(node)
        self.amt.state[node] = 8


class Rig:
    """A waker wired to fakes. Kube's GETs see the fakes' state, and the
    fake clock advances on every sleep the waker takes."""

    def __init__(self, mode="full", **over):
        self.cfg = cfg_with(mode=mode, **over)
        self.clock = Clock()
        self.kube = FakeKube(self.cfg, self.clock)
        self.github = FakeGitHub()
        self.amt = FakeAMT(self.kube, self.clock)
        self.talos = FakeTalos(self.kube, self.amt)
        self.waker = w.Waker(self.cfg, self.kube, self.github, self.amt, self.talos,
                             clock=self.clock, sleep=self.clock.sleep, my_node="cp1")
        # Real wall time is used for job ages; jobs are stamped relative to now.

    def job(self, labels, age_seconds, repo="abradner/spritz"):
        self.github.jobs.append({
            "repo": repo, "id": len(self.github.jobs) + 1, "name": "j", "labels": labels,
            "created_at": dt.datetime.now(UTC) - dt.timedelta(seconds=age_seconds)})

    def tick(self, advance=0):
        self.clock.sleep(advance)
        self.waker.tick()

    def put_all_to_sleep(self):
        """Both amd64 nodes asleep the way the waker leaves them."""
        self.tick()
        self.tick(advance=61 * 60)  # sleeps zag-amd64-02
        self.tick(advance=1)        # sleeps zag-amd64-01 (idle since the first tick)
        assert self.kube.asleep() == {"zag-amd64-01", "zag-amd64-02"}, self.kube.asleep()


# --------------------------------------------------------------------------
# Pure decisions
# --------------------------------------------------------------------------

class JobMatch(unittest.TestCase):
    def test_shared_labels_match_case_insensitively(self):
        self.assertEqual(w.job_match(["self-hosted", "Linux", "spritz-ci"], "zagreus-ci",
                                     SET_LABELS["zagreus-ci"]), (True, False))

    def test_a_label_the_set_lacks_does_not_match(self):
        self.assertEqual(w.job_match(["self-hosted", "linux", "spritz-e2e"], "zagreus-ci",
                                     SET_LABELS["zagreus-ci"]), (False, False))

    def test_the_set_name_matches_and_is_named(self):
        self.assertEqual(w.job_match(["zagreus-ci"], "zagreus-ci", SET_LABELS["zagreus-ci"]), (True, True))

    def test_no_labels_matches_nothing(self):
        self.assertEqual(w.job_match([], "zagreus-ci", SET_LABELS["zagreus-ci"]), (False, False))


class Demand(unittest.TestCase):
    def setUp(self):
        self.cfg = production_config()
        self.now = dt.datetime(2026, 10, 23, 5, 0, tzinfo=UTC)

    def job(self, labels, age, repo="abradner/spritz"):
        return {"repo": repo, "labels": labels, "created_at": self.now - dt.timedelta(seconds=age)}

    def test_shared_label_job_counts_only_after_the_grace(self):
        young = self.job(["self-hosted", "linux", "spritz-ci"], 30)
        old = self.job(["self-hosted", "linux", "spritz-ci"], 61)
        d = w.demand_by_set(self.cfg, [young, old], SET_LABELS, self.now)
        self.assertEqual(d["zagreus-ci"], 1)

    def test_a_job_naming_the_set_counts_at_once(self):
        d = w.demand_by_set(self.cfg, [self.job(["zagreus-docker-amd64"], 0)], SET_LABELS, self.now)
        self.assertEqual(d["zagreus-docker-amd64"], 1)

    def test_another_repository_is_not_demand(self):
        d = w.demand_by_set(self.cfg, [self.job(["zagreus-ci"], 600, repo="abradner/other")],
                            SET_LABELS, self.now)
        self.assertEqual(sum(d.values()), 0)

    def test_light_jobs_wake_nothing(self):
        d = w.demand_by_set(self.cfg, [self.job(["self-hosted", "linux", "spritz-light"], 600)],
                            SET_LABELS, self.now)
        self.assertEqual(sum(d.values()), 0)


class Unmet(unittest.TestCase):
    def test_a_claimed_job_is_not_counted_twice(self):
        # Cap 1, one runner claimed for a job GitHub still lists as queued.
        self.assertEqual(w.unmet_by_set({"s": 1}, {"s": 1}, {"s": 1}, {"s": 1}), {"s": 0})

    def test_spare_capacity_absorbs_demand(self):
        self.assertEqual(w.unmet_by_set({"s": 2}, {"s": 3}, {"s": 1}, {"s": 0}), {"s": 0})

    def test_cap_zero_leaves_all_demand_unmet(self):
        self.assertEqual(w.unmet_by_set({"s": 2}, {"s": 0}, {"s": 0}, {"s": 0}), {"s": 2})


def states(**kw):
    base = {"ready": True, "unschedulable": False, "waker_cordon": False, "asleep": False, "busy": False}
    return {name: dict(base, **v) for name, v in kw.items()}


class Caps(unittest.TestCase):
    """The rule the whole design rests on (README, "Why maxRunners"):
    a scale set is offered jobs up to maxRunners whether or not its pods can
    schedule, so sleeping nodes must contribute no slots at all."""

    def setUp(self):
        self.cfg = production_config()

    def test_every_driven_set_is_zero_while_the_amd64_nodes_sleep(self):
        caps = w.compute_caps(self.cfg, states(**{
            "zag-amd64-01": {"ready": False, "asleep": True},
            "zag-amd64-02": {"ready": False, "asleep": True}}))
        self.assertEqual(caps, {"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})

    def test_a_node_listed_asleep_counts_nothing_even_if_it_still_reports_ready(self):
        # The window between "asleep" being published and the kubelet going
        # quiet: the node is still Ready, and must already count for nothing.
        caps = w.compute_caps(self.cfg, states(**{
            "zag-amd64-01": {"asleep": True, "unschedulable": True},
            "zag-amd64-02": {"asleep": True, "unschedulable": True}}))
        self.assertEqual(sum(caps.values()), 0)

    def test_a_cordoned_node_counts_nothing(self):
        caps = w.compute_caps(self.cfg, states(**{
            "zag-amd64-01": {"unschedulable": True}, "zag-amd64-02": {}}))
        self.assertEqual(caps, {"zagreus-ci": 2, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})

    def test_both_awake_is_the_static_caps_from_athena_gitops_223(self):
        caps = w.compute_caps(self.cfg, states(**{"zag-amd64-01": {}, "zag-amd64-02": {}}))
        self.assertEqual(caps, CEILINGS)

    def test_the_first_node_alone_serves_every_class(self):
        caps = w.compute_caps(self.cfg, states(**{
            "zag-amd64-01": {}, "zag-amd64-02": {"ready": False, "asleep": True}}))
        self.assertTrue(all(v > 0 for v in caps.values()), caps)

    def test_the_ceiling_bounds_and_an_unknown_ceiling_only_lowers(self):
        # Under-offering only sends jobs to classic runners; over-offering
        # strands them (delta review of 22f122a). The caller passes the last
        # ceiling read, so None means none was ever read.
        self.assertEqual(w.bound_caps({"a": 5, "b": 5, "c": 1}, {"a": 2, "b": 2, "c": 2},
                                      {"a": 3, "b": None, "c": None}), {"a": 3, "b": 2, "c": 1})

    def test_configured_slots_fit_the_nodes(self):
        """The arithmetic in waker.conf, checked: each node's slots, plus
        what else may land there, fit its allocatable memory (23 Oct 2026)."""
        alloc_gib = {"zag-amd64-01": 22.47, "zag-amd64-02": 14.49}
        pod_gib = {"zagreus-ci": 6, "zagreus-ci-x64": 6, "zagreus-docker-amd64": 3 + 0.75}
        others_gib = 2 * 0.75 + 0.25 + 0.5  # two light pods, bundle fill, coredns and slack
        for n in self.cfg["nodes"]:
            used = sum(pod_gib[s] * k for s, k in n["slots"].items()) + others_gib
            self.assertLessEqual(used, alloc_gib[n["name"]], n["name"])


class Choose(unittest.TestCase):
    def setUp(self):
        self.cfg = production_config()

    def test_wake_picks_the_first_sleeping_node_with_a_slot_for_the_hungry_set(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True}})
        self.assertEqual(w.choose_wake(self.cfg, {"zagreus-ci": 1}, st, {}, T0), "zag-amd64-01")

    def test_wake_skips_a_node_in_backoff(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True}})
        self.assertEqual(w.choose_wake(self.cfg, {"zagreus-ci": 1}, st, {"zag-amd64-01": T0 + 1}, T0),
                         "zag-amd64-02")

    def test_wake_never_picks_a_node_without_a_slot_for_the_set(self):
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {"ready": False, "asleep": True}})
        self.assertIsNone(w.choose_wake(self.cfg, {"zagreus-docker-amd64": 1}, st, {}, T0))

    def test_sleep_waits_for_the_idle_timeout_and_takes_the_last_node_first(self):
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}})
        idle = {"zag-amd64-01": T0, "zag-amd64-02": T0}
        self.assertIsNone(w.choose_sleep(self.cfg, st, idle, {}, {}, T0 + 59 * 60))
        self.assertEqual(w.choose_sleep(self.cfg, st, idle, {}, {}, T0 + 60 * 60), "zag-amd64-02")

    def test_no_sleep_while_a_set_the_node_serves_is_hungry(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True}, "zag-amd64-02": {}})
        self.assertIsNone(w.choose_sleep(self.cfg, st, {"zag-amd64-02": T0}, {"zagreus-ci": 1}, {},
                                         T0 + 7200))

    def test_daemonset_and_finished_pods_do_not_hold_a_node(self):
        ds = {"metadata": {"ownerReferences": [{"kind": "DaemonSet"}]}, "status": {"phase": "Running"}}
        done = {"metadata": {}, "status": {"phase": "Succeeded"}}
        mirror = {"metadata": {"annotations": {"kubernetes.io/config.mirror": "x"}}, "status": {}}
        runner = {"metadata": {"ownerReferences": [{"kind": "EphemeralRunner"}]}, "status": {"phase": "Pending"}}
        self.assertEqual([w.pod_holds_node(p) for p in (ds, done, mirror, runner)],
                         [False, False, False, True])

    def test_coredns_does_not_hold_a_node_but_an_idle_warm_runner_does(self):
        coredns = {"metadata": {"namespace": "kube-system", "ownerReferences": [{"kind": "ReplicaSet"}]},
                   "status": {"phase": "Running"}}
        warm = {"metadata": {"namespace": "arc-runners", "ownerReferences": [{"kind": "EphemeralRunner"}]},
                "status": {"phase": "Running"}}
        self.assertFalse(w.pod_holds_node(coredns, ["kube-system"]))
        self.assertTrue(w.pod_holds_node(warm, ["kube-system"]))


# --------------------------------------------------------------------------
# Procedures, against the fakes
# --------------------------------------------------------------------------

class SleepProcedure(unittest.TestCase):
    def test_caps_drop_and_the_node_leaves_the_expected_list_before_the_power_goes(self):
        r = Rig()
        r.tick()
        r.kube.writes.clear()
        r.tick(advance=61 * 60)
        verbs = [(v, p) for v, p, _ in r.kube.writes]
        shutdown = verbs.index(("SHUTDOWN", "zag-amd64-02"))
        cap_drop = next(i for i, (v, p) in enumerate(verbs) if v == "PATCH" and "autoscalingrunnersets" in p)
        cordon = next(i for i, (v, p, b) in enumerate(r.kube.writes)
                      if v == "PATCH" and p == "/api/v1/nodes/zag-amd64-02" and b.get("spec", {}).get("unschedulable"))
        asleep = next(i for i, (v, p, b) in enumerate(r.kube.writes)
                      if p.startswith("/api/v1/namespaces/log-heartbeat/configmaps")
                      and "zag-amd64-02" in b["data"]["asleep"].split())
        self.assertLess(cap_drop, cordon)
        self.assertLess(cordon, asleep)
        self.assertLess(asleep, shutdown)
        # And the published line led the shutdown by the configured lead.
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 1, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 1})

    def test_assertion_names_its_emitter_and_is_renewed(self):
        r = Rig()
        r.tick()
        r.tick(advance=61 * 60)
        cm = r.kube.cms[("log-heartbeat", "waker-asleep")]["data"]
        self.assertEqual(cm["emitter"], "cp1")
        first = int(cm["renewed"])
        r.tick(advance=30)
        self.assertGreater(int(r.kube.cms[("log-heartbeat", "waker-asleep")]["data"]["renewed"]), first)

    def test_the_asleep_list_is_renewed_through_a_long_procedure(self):
        # 02's shutdown hangs, so its sleep runs the publish lead, the full
        # off-timeout and the hard power-off: over eight minutes from the
        # publish to the end. The heartbeat pod drops an assertion 300 s
        # after its last renewal: no gap, up to the procedure's end, may come
        # near it.
        r = Rig()
        r.tick()
        r.talos.shutdown = lambda ip: None
        r.tick(advance=61 * 60)
        self.assertIn(("zag-amd64-02", w.POWER_OFF_HARD), r.amt.calls)  # the long path ran
        times = [t for t, listed in r.kube.asleep_writes if "zag-amd64-02" in listed.split()]
        times.append(r.clock())  # the end of the procedure
        self.assertGreater(times[-1] - times[0], 400, "the procedure was not long")
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLessEqual(max(gaps), 60, gaps)

    def test_all_asleep_means_every_driven_set_claims_nothing(self):
        r = Rig()
        r.put_all_to_sleep()
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})
        self.assertEqual(sorted(r.talos.calls), ["zag-amd64-01", "zag-amd64-02"])

    def test_a_pod_landing_after_the_cordon_aborts_the_sleep(self):
        r = Rig()
        r.tick()

        def land(verb, path, body):
            if path == "/api/v1/nodes/zag-amd64-02" and body.get("spec", {}).get("unschedulable"):
                r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci", phase="Pending")
        r.kube.on_write = land
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])
        self.assertEqual(r.kube.asleep(), set())
        self.assertNotIn("unschedulable", r.kube.nodes["zag-amd64-02"]["spec"])
        self.assertEqual(r.kube.caps(), CEILINGS)

    def test_a_busy_node_is_never_put_to_sleep(self):
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        r.tick(advance=5 * 3600)
        self.assertEqual(r.talos.calls, [])

    def test_shutdown_not_confirmed_puts_the_node_back_on_the_expected_list(self):
        r = Rig()
        r.tick()
        r.talos.shutdown = lambda ip: None  # the node ignores it
        r.amt.request_power = lambda node, state: None  # and AMT cannot turn it off
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), set())
        self.assertIn("zag-amd64-02", r.waker.backoff_until)


class WakeProcedure(unittest.TestCase):
    def test_a_failed_runner_does_not_hide_demand_issue_239(self):
        # athena-gitops#239: a runner that Failed before taking a job counted
        # as an unstarted claim, so it absorbed a queued job's demand and
        # nothing woke.
        r = Rig()
        r.put_all_to_sleep()
        r.kube.ers["arc-builders"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-docker-amd64"}},
                                           "status": {"phase": "Failed"}})
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_github_blind_for_long_lets_sleeping_nodes_alert(self):
        # Full-mode review, finding 1: with GitHub unreadable the waker held
        # for ever and kept vouching for sleeping nodes, so nothing woke and
        # nothing alerted. Past demandBlindSeconds it stops renewing.
        r = Rig()
        r.put_all_to_sleep()
        r.github.fail = True
        for _ in range(30):
            r.tick(advance=60)
        renewed = int(r.kube.cms[("log-heartbeat", "waker-asleep")]["data"]["renewed"])
        # The relay drops an assertion older than its TTL, so the nodes alert.
        self.assertGreater(r.clock() - renewed, r.cfg["assertTtlSeconds"])

    def test_codex_p1_an_ambiguous_shutdown_stays_cordoned_and_out_of_the_caps(self):
        # talosctl errors after Talos accepted the shutdown; AMT still says on
        # after the extra wait. The node may yet go down: keep it cordoned.
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
        r.tick()

        def lost_reply(ip):
            raise RuntimeError("rpc error: connection reset")  # accepted, reply lost; AMT stays on
        r.talos.shutdown = lost_reply
        r.tick(advance=61 * 60)
        self.assertTrue(r.kube.nodes["zag-amd64-02"]["spec"].get("unschedulable"))
        self.assertNotIn("zag-amd64-02", r.kube.asleep())  # its alert judges it
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        r.tick(advance=60)
        self.assertTrue(r.kube.nodes["zag-amd64-02"]["spec"].get("unschedulable"))

    def test_codex_p2_a_woken_node_with_a_taint_offers_nothing(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.taint("zag-amd64-01", "example.com/maintenance")
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})

    def test_codex_p2_a_listed_node_with_no_node_object_is_still_audited(self):
        r = Rig()
        r.put_all_to_sleep()
        r.tick(advance=120)
        r.amt.state["zag-amd64-02"] = w.POWER_ON  # booting, but never registers
        r.kube.fail_get.append(r"/api/v1/nodes/zag-amd64-02$")
        r.tick(advance=30)
        r.tick(advance=r.cfg["readyTimeoutSeconds"] + 60)
        self.assertNotIn("zag-amd64-02", r.kube.asleep())

    def test_a_claim_that_never_schedules_wakes_one_node_not_all(self):
        # Delta review of e37ba73: a claim that can never bind woke every
        # node in turn and pinned them all on.
        r = Rig()
        r.put_all_to_sleep()
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                                                       "creationTimestamp": iso(r.clock() - 3600)},
                                          "status": {}})
        for _ in range(40):
            r.tick(advance=60)
        self.assertEqual(sum(1 for _, st in r.amt.calls if st == w.POWER_ON), 1)

    def test_a_failed_wake_does_not_spend_the_stuck_episode(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.refuse_on.add("zag-amd64-01")
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                                                       "creationTimestamp": iso(r.clock() - 3600)},
                                          "status": {}})
        r.tick(advance=60)  # 01 refuses
        r.tick(advance=r.cfg["wakeCooldownSeconds"] + 60)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_a_new_stuck_episode_can_wake_again(self):
        r = Rig()
        r.put_all_to_sleep()
        stuck = {"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                              "creationTimestamp": iso(r.clock() - 3600)}, "status": {}}
        r.kube.ers["arc-runners"].append(stuck)
        r.tick(advance=60)  # wakes 01 for it
        r.kube.ers["arc-runners"].clear()
        r.tick(advance=60)  # episode over
        r.kube.ers["arc-runners"].append(dict(stuck))
        for _ in range(2):  # 01 full: two running ci runners (their own ERs), no room left awake
            r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                                                           "creationTimestamp": iso(r.clock() - 3600)},
                                              "status": {"jobRequestId": 1}})
        r.amt.calls.clear()
        r.tick(advance=r.cfg["wakeCooldownSeconds"] + 60)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_reconcile_keeps_a_tainted_node_out_of_the_caps(self):
        r = Rig(mode="full")
        r.put_all_to_sleep()
        r.tick(advance=120)
        r.amt.state["zag-amd64-02"] = w.POWER_ON
        r.kube.set_ready("zag-amd64-02", True)
        r.kube.dead.discard("zag-amd64-02")
        r.kube.taint("zag-amd64-02", "example.com/maintenance")
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 0)

    def test_healthy_github_keeps_the_asleep_list_renewed(self):
        r = Rig()
        r.put_all_to_sleep()
        for _ in range(30):  # 30 min, past demandBlindSeconds, GitHub fine
            r.tick(advance=60)
        renewed = int(r.kube.cms[("log-heartbeat", "waker-asleep")]["data"]["renewed"])
        self.assertLessEqual(r.clock() - renewed, 60)

    def test_a_claimed_runner_with_no_room_wakes_a_node(self):
        # Full-mode review, finding 2: an unbound claim set unmet to 0, so it
        # never woke the node that could run it.
        r = Rig()
        r.put_all_to_sleep()
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                                                       "creationTimestamp": iso(r.clock())},
                                          "status": {}})
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=30)  # just created: a pod normally binds within seconds
        self.assertNotIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)
        r.tick(advance=60)  # unbound past the grace: no room anywhere awake
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_a_young_shared_label_job_wakes_nothing(self):
        r = Rig()
        r.put_all_to_sleep()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=20)
        r.tick(advance=300)
        self.assertNotIn(w.POWER_ON, [s for _, s in r.amt.calls])

    def test_a_saturated_classic_pool_wakes_the_first_node_then_raises_its_caps(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.writes.clear()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)
        writes = r.kube.writes
        on = writes.index(("AMT", "zag-amd64-01", w.POWER_ON))
        unlisted = next(i for i, (v, p, b) in enumerate(writes)
                        if p.startswith("/api/v1/namespaces/log-heartbeat/configmaps")
                        and "zag-amd64-01" not in b["data"]["asleep"].split())
        raised = next(i for i, (v, p, b) in enumerate(writes)
                      if v == "PATCH" and "autoscalingrunnersets" in p and b["spec"]["maxRunners"] > 0)
        self.assertLess(on, unlisted)
        self.assertLess(unlisted, raised)
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 1, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 1})
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})

    def test_a_job_naming_a_set_wakes_at_once(self):
        r = Rig()
        r.put_all_to_sleep()
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_x64_demand_with_the_first_node_up_wakes_nothing_it_cannot_use(self):
        r = Rig()
        r.put_all_to_sleep()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)  # wakes 01
        r.github.jobs.clear()
        # 01's one x64 slot is taken; another e2e job waits.
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci-x64"}},
                                          "status": {"jobRequestId": 7}})
        r.job(["self-hosted", "linux", "X64", "spritz-e2e"], age_seconds=90)
        r.amt.calls.clear()
        r.tick(advance=300)
        self.assertEqual(r.amt.calls, [])  # 02 has no x64 slot

    def test_no_wake_within_the_cooldown(self):
        r = Rig()
        r.put_all_to_sleep()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)  # wakes 01 at time t; the wake itself takes ~65 s
        r.amt.calls.clear()
        # More demand than 01 can take arrives inside the cooldown.
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"}},
                                          "status": {"jobRequestId": 8}})
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=30)
        self.assertEqual(r.amt.calls, [])
        r.tick(advance=200)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_a_node_that_never_gets_ready_goes_back_on_the_expected_list(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.boot_seconds = 10 ** 6
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)
        self.assertNotIn("zag-amd64-01", r.kube.asleep())  # so ZagreusNodeKmsgGone fires
        self.assertEqual(r.kube.caps()["zagreus-ci"], 0)
        self.assertEqual(r.kube.nodes["zag-amd64-01"]["metadata"]["annotations"][w.ANNOTATION_STATE],
                         "wake-failed")

    def test_amt_refusing_without_wol_is_a_failed_wake(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.refuse_on.add("zag-amd64-01")
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)
        self.assertNotIn("zag-amd64-01", r.kube.asleep())
        self.assertIn("zag-amd64-01", r.waker.backoff_until)

    def test_github_down_holds_everything(self):
        r = Rig()
        r.tick()
        r.github.fail = True
        r.tick(advance=5 * 3600)
        self.assertEqual(r.talos.calls, [])

    def test_a_node_powered_on_by_hand_is_taken_back(self):
        r = Rig()
        r.put_all_to_sleep()
        r.tick(advance=120)
        r.amt.state["zag-amd64-02"] = w.POWER_ON
        r.kube.set_ready("zag-amd64-02", True)
        r.kube.dead.discard("zag-amd64-02")
        r.tick(advance=30)
        self.assertNotIn("zag-amd64-02", r.kube.asleep())
        self.assertNotIn("unschedulable", r.kube.nodes["zag-amd64-02"]["spec"])
        self.assertEqual(r.kube.caps()["zagreus-ci"], 2)


class ReviewFindings(unittest.TestCase):
    """One test per finding of the independent review (23 Oct 2026)."""

    def test_stale_ready_after_shutdown_is_not_taken_back(self):
        # The API server keeps a shut-down node Ready for ~45 s. The next pass
        # lands inside that window: the node must stay listed and count nothing.
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")  # 01 stays awake
        r.tick()
        r.tick(advance=61 * 60)  # 02 shut down; Ready is stale until +45 s
        self.assertEqual(r.kube.nodes["zag-amd64-02"]["status"]["conditions"][0]["status"], "True")
        r.tick(advance=5)
        self.assertIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)  # 01's slot only

    def test_stale_ready_is_not_taken_back_without_amt_either(self):
        r = Rig()
        r.tick()
        r.tick(advance=61 * 60)
        r.amt.missing.add("zag-amd64-02")  # AMT unreachable from here on
        r.tick(advance=5)
        self.assertIn("zag-amd64-02", r.kube.asleep())

    def test_a_failed_talosctl_call_on_a_node_going_down_is_a_sleep(self):
        r = Rig()
        r.tick()
        real = r.talos.shutdown

        def accepted_then_error(ip):
            real(ip)
            raise RuntimeError("rpc error: transport is closing")
        r.talos.shutdown = accepted_then_error
        r.tick(advance=61 * 60)
        self.assertIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_a_failed_talosctl_call_on_a_node_still_on_is_held_then_taken_back(self):
        # Codex on #242 (P1): a failed call may still have been accepted, so
        # the node stays cordoned and out of the caps; once it has stayed up
        # past STALE_READY_LIMIT_S the shutdown clearly never happened and it
        # is taken back.
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")  # 01 stays awake throughout
        r.tick()

        def refused(ip):
            raise RuntimeError("permission denied")
        r.talos.shutdown = refused
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), set())
        self.assertTrue(r.kube.nodes["zag-amd64-02"]["spec"].get("unschedulable"))
        r.tick(advance=w.STALE_READY_LIMIT_S + 60)
        self.assertNotIn("unschedulable", r.kube.nodes["zag-amd64-02"]["spec"])
        self.assertEqual(r.kube.caps()["zagreus-ci"], 3)

    def test_a_failed_wake_is_retried_after_the_backoff(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.refuse_on.add("zag-amd64-01")
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertNotIn("zag-amd64-01", r.kube.asleep())
        r.amt.refuse_on.clear()
        r.amt.calls.clear()
        r.tick(advance=600)
        self.assertEqual(r.amt.calls, [])  # still in backoff
        r.tick(advance=r.cfg["failureBackoffSeconds"])
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)
        self.assertEqual(r.kube.caps()["zagreus-docker-amd64"], 1)

    def test_a_node_it_cannot_wake_is_never_put_to_sleep(self):
        r = Rig()
        r.amt.missing.add("zag-amd64-02")
        r.tick()
        r.tick(advance=61 * 60)
        self.assertNotIn("zag-amd64-02", r.talos.calls)
        self.assertIn("zag-amd64-01", r.talos.calls)  # the wakeable one still sleeps

    def test_a_listed_node_on_but_never_ready_leaves_the_list(self):
        r = Rig()
        r.put_all_to_sleep()
        r.tick(advance=120)
        r.amt.state["zag-amd64-02"] = w.POWER_ON  # a power-restore boot that hangs
        r.tick(advance=30)
        self.assertIn("zag-amd64-02", r.kube.asleep())
        r.tick(advance=r.cfg["readyTimeoutSeconds"] + 60)
        self.assertNotIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"][w.ANNOTATION_STATE],
                         "on-not-ready")

    def test_unknown_names_are_dropped_from_the_list(self):
        r = Rig()
        r.kube.cms[("log-heartbeat", "waker-asleep")] = {"data": {
            "asleep": "zag-amd64-99", "renewed": "0", "emitter": "cp1"}}
        r.tick()
        self.assertEqual(r.kube.asleep(), set())

    def test_a_set_with_no_max_runners_is_written(self):
        r = Rig()
        r.put_all_to_sleep()
        del r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"]
        del r.kube.apps["zagreus-spritz-ci"]["spec"]["source"]["helm"]["valuesObject"]["maxRunners"]
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 0)

    def test_a_runner_not_yet_on_a_node_blocks_sleep(self):
        r = Rig()
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"}},
                                          "status": {}})
        r.tick()
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])
        r.kube.ers["arc-runners"].clear()
        r.tick(advance=30)
        self.assertEqual(r.talos.calls, ["zag-amd64-02"])

    def test_observe_with_nodes_listed_asleep_and_demand_powers_nothing(self):
        # The README's rollback state: back to observe with nodes asleep.
        r = Rig()
        r.put_all_to_sleep()
        r.cfg["mode"] = "observe"
        r.kube.writes.clear()
        r.amt.calls.clear()
        r.talos.calls.clear()
        r.job(["zagreus-ci"], age_seconds=600)
        r.tick(advance=300)
        r.amt.state["zag-amd64-02"] = w.POWER_ON
        r.kube.set_ready("zag-amd64-02", True)
        r.tick(advance=300)
        self.assertEqual(r.kube.writes, [])
        self.assertEqual(r.amt.calls, [])
        self.assertEqual(r.talos.calls, [])


class CodexFindings(unittest.TestCase):
    """Codex on athena-gitops#232 (a8122230)."""

    def test_a_node_that_loses_power_stops_counting_at_once(self):
        # P1: unexpected power loss. Ready is stale for ~45 s; AMT is not.
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        self.assertEqual(r.kube.caps(), CEILINGS)
        # Mains power lost: the kubelet stops and AMT goes with it.
        r.kube.dead.add("zag-amd64-02")
        r.amt.missing.add("zag-amd64-02")
        r.kube.going_down["zag-amd64-02"] = r.clock() + STALE_READY_SECONDS + w.LEASE_FRESH_S
        r.tick(advance=w.LEASE_FRESH_S + 1)  # Lease stale, Ready not yet flipped
        self.assertEqual(r.kube.nodes["zag-amd64-02"]["status"]["conditions"][0]["status"], "True")
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_an_off_node_whose_ready_never_flips_is_not_taken_back(self):
        # Delta review M2: kube-controller-manager down, so a node the waker
        # shut down keeps Ready=True for ever. Past the 600 s rule it must
        # still not count: its kubelet's Lease is dead.
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
        r.tick()
        r.tick(advance=61 * 60)  # 02 asleep
        r.kube.going_down.pop("zag-amd64-02")  # nothing ever flips Ready
        r.amt.missing.add("zag-amd64-02")  # and AMT can't vouch either way
        r.tick(advance=w.STALE_READY_LIMIT_S + 120)
        self.assertIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_a_listed_uncordoned_on_node_loses_its_stamp_when_taken_back(self):
        # The stamp clear in reconcile, on its own (no waker cordon to clear it).
        r = Rig()
        r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"][w.ANNOTATION_SLEPT_AT] = str(int(T0))
        r.kube.cms[("log-heartbeat", "waker-asleep")] = {"data": {
            "asleep": "zag-amd64-02", "renewed": str(int(T0)), "emitter": "cp1"}}
        r.tick(advance=w.STALE_READY_LIMIT_S + 60)
        self.assertNotIn(w.ANNOTATION_SLEPT_AT, r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"])
        self.assertNotIn("zag-amd64-02", r.kube.asleep())

    def test_a_crash_between_stamp_and_shutdown_does_not_strand_the_node(self):
        # P2: the waker stopped after stamping slept-at and cordoning, before
        # talosctl ran. The node is on, Ready since long before the stamp.
        r = Rig()
        r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = True
        r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"].update({
            w.ANNOTATION_CORDON: "true", w.ANNOTATION_SLEPT_AT: str(int(T0))})
        r.kube.cms[("log-heartbeat", "waker-asleep")] = {"data": {
            "asleep": "zag-amd64-02", "renewed": str(int(T0)), "emitter": "cp1"}}
        r.tick(advance=w.STALE_READY_LIMIT_S + 60)
        ann = r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"]
        self.assertNotIn(w.ANNOTATION_SLEPT_AT, ann)
        self.assertNotIn("unschedulable", r.kube.nodes["zag-amd64-02"]["spec"])
        self.assertNotIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.caps()["zagreus-ci"], 3)


class CapsCodexFindings(unittest.TestCase):
    """Codex on #238 (439b185): caps mode must not offer a slot that cannot
    schedule."""

    def test_p2_1_another_sets_runner_on_the_surviving_node_takes_its_room(self):
        # 01 is gone; an x64 job (6 Gi) is running on 02. 02 has room for one
        # more heavy pod, not two: zagreus-ci may be offered one.
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.dead.add("zag-amd64-01")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci-x64")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        self.assertEqual(r.kube.caps()["zagreus-ci-x64"], 1)  # its own running job

    def test_p2_1_a_builder_on_the_surviving_node_takes_its_room(self):
        # The builder (3.75 Gi with dind) running on 02 leaves room for one
        # heavy pod there, not two.
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.dead.add("zag-amd64-01")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-docker-amd64")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_p2_3_a_claimed_runner_not_yet_bound_holds_its_room(self):
        # Codex on e341ef3: 01 is gone and an already-claimed x64 runner has
        # no pod bound yet. It will need 6 Gi somewhere, so 02 can take one
        # zagreus-ci, not two.
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.dead.add("zag-amd64-01")
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci-x64"}},
                                          "status": {}})
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        self.assertEqual(r.kube.caps()["zagreus-ci-x64"], 1)  # its claim

    def test_a_claim_that_fits_nowhere_raises_no_cap(self):
        # Delta review of 1f2fee4: 01 gone, 02 full; an unbound x64 claim (or
        # the artificial one for unreadable runners) must not raise x64.
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.dead.add("zag-amd64-01")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci-x64"}},
                                          "status": {}})
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci-x64"], 0)

    def test_a_finished_runner_is_not_an_unbound_claim(self):
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.dead.add("zag-amd64-01")
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci-x64"}},
                                          "status": {"phase": "Succeeded"}})
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 2)  # no room held for a finished runner

    def test_p2_2_an_untolerated_noschedule_taint_takes_the_node_out(self):
        r = Rig(mode="caps")
        r.kube.taint("zag-amd64-01", "node.kubernetes.io/disk-pressure")
        r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = True
        r.tick(advance=60)
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})

    def test_a_prefer_noschedule_taint_does_not(self):
        r = Rig(mode="caps")
        r.kube.taint("zag-amd64-01", "example.com/soft", effect="PreferNoSchedule")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps(), CEILINGS)


class CapsReviewLows(unittest.TestCase):
    """The three LOWs of the subagent read of #238."""

    def test_low1_an_unreadable_ceiling_uses_the_last_one_read(self):
        # Delta review of 22f122a: holding the live value froze the cap in
        # the unsafe direction. With a ceiling read before, the caps still
        # follow the nodes both ways.
        r = Rig(mode="caps")
        r.tick()
        del r.kube.apps["zagreus-spritz-ci"]  # renamed, say
        r.kube.set_ready("zag-amd64-02", False)
        r.kube.dead.add("zag-amd64-02")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        r.kube.set_ready("zag-amd64-02", True)
        r.kube.dead.discard("zag-amd64-02")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 3)

    def test_low1_with_no_ceiling_ever_read_caps_only_go_down(self):
        r = Rig(mode="caps")
        r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"] = 3
        del r.kube.apps["zagreus-spritz-ci"]
        r.kube.set_ready("zag-amd64-02", False)
        r.kube.dead.add("zag-amd64-02")
        r.tick()
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        r.kube.set_ready("zag-amd64-02", True)
        r.kube.dead.discard("zag-amd64-02")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)  # held down, never raised blind

    def test_low1_no_sleep_while_no_ceiling_was_ever_read(self):
        r = Rig()
        del r.kube.apps["zagreus-spritz-ci"]
        r.tick()
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])

    def test_low2_one_failed_lease_read_at_start_does_not_flap(self):
        r = Rig(mode="caps")
        for (ns, name), live in {("arc-runners", "zagreus-ci"): 3, ("arc-runners", "zagreus-ci-x64"): 1,
                                 ("arc-builders", "zagreus-docker-amd64"): 1}.items():
            r.kube.ars[(ns, name)]["spec"]["maxRunners"] = live
        r.kube.lease_fail.add("zag-amd64-01")  # the first pass after a start
        r.tick()
        r.kube.lease_fail.clear()
        r.tick(advance=30)
        self.assertFalse([x for x in r.kube.writes if "autoscalingrunnersets" in x[1]])

    def test_every_quantity_suffix_parses(self):
        for v, want in (("500n", 5e-7), ("1u", 1e-6), ("100m", 0.1), ("1", 1.0), ("0.5", 0.5),
                        ("1k", 1e3), ("1Ki", 1024.0), ("2Gi", 2 * 2**30), ("1P", 1e15),
                        ("1Pi", 2**50), ("1E", 1e18), ("1Ei", 2**60)):
            self.assertAlmostEqual(w.parse_qty(v), want, msg=v)

    def test_an_unparseable_pod_offers_nothing_new_on_its_node_and_does_not_stop_the_pass(self):
        r = Rig(mode="caps")
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci-x64")
        r.kube.pods[-1]["spec"]["containers"][0]["resources"]["requests"]["memory"] = "6 bogus"
        r.tick()
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)  # 01's slot; 02 offers nothing new

    def test_a_node_without_allocatable_does_not_count_its_own_runner_twice(self):
        r = Rig(mode="caps")
        del r.kube.nodes["zag-amd64-02"]["status"]["allocatable"]
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        self.assertLessEqual(r.kube.caps()["zagreus-ci"], 3)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1 + 0 + 1)  # 01's slot + 02 (full, own 1)

    def test_a_forbidden_application_still_lets_caps_follow_the_nodes(self):
        # Delta review of 0507bac: a 403/5xx on the Application (not only a
        # 404) must fall back to the last ceiling, not stop the pass.
        r = Rig(mode="caps")
        r.tick()
        r.kube.fail_get.append(r"applications/zagreus-spritz-ci$")
        r.kube.set_ready("zag-amd64-02", False)
        r.kube.dead.add("zag-amd64-02")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_one_bad_template_costs_only_its_own_set(self):
        # Delta review of 0507bac: an unparseable builder template (filled
        # first) turned every node's free room into NaN and every set to 0.
        r = Rig(mode="caps")
        r.kube.ars[("arc-builders", "zagreus-docker-amd64")]["spec"]["template"]["spec"]["containers"][0][
            "resources"]["requests"]["memory"] = "768 bogus"
        r.tick()
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 3, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 0})

    def test_an_unreadable_node_costs_only_that_node(self):
        r = Rig(mode="caps")
        r.tick()
        r.kube.fail_get.append(r"/api/v1/nodes/zag-amd64-02$")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 1, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 1})

    def test_an_unreadable_runner_list_costs_only_that_set_and_blocks_sleep(self):
        r = Rig()
        r.tick()
        r.kube.fail_get.append(r"arc-builders/ephemeralrunners")
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])
        self.assertEqual(r.kube.caps(), CEILINGS)

    def test_an_unreadable_scale_set_degrades_the_pass_no_sleep_no_raise(self):
        # Delta review of 5995bf1: a skipped set's room is unknown, and a
        # sleep could not lower its cap first.
        r = Rig()
        r.tick()
        r.kube.fail_get.append(r"autoscalingrunnersets/zagreus-docker-amd64$")
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])

    def test_a_degraded_pass_lowers_but_never_raises(self):
        r = Rig(mode="caps")
        r.kube.set_ready("zag-amd64-02", False)
        r.kube.dead.add("zag-amd64-02")
        r.tick()
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)
        r.kube.fail_get.append(r"autoscalingrunnersets/zagreus-docker-amd64$")
        r.kube.set_ready("zag-amd64-02", True)
        r.kube.dead.discard("zag-amd64-02")
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)  # held: not raised blind
        r.kube.fail_get.clear()
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()["zagreus-ci"], 3)

    def test_a_scale_set_unreadable_mid_sleep_aborts_it(self):
        # Delta review of cf7d2f9: after the cordon, a runner waits unplaced
        # and its set's ARS GET fails; the re-check must not read that as
        # "nothing unplaced".
        r = Rig()
        r.tick()

        def after_cordon(verb, path, body):
            if path == "/api/v1/nodes/zag-amd64-02" and body.get("spec", {}).get("unschedulable"):
                r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"}},
                                                  "status": {}})
                r.kube.fail_get.append(r"autoscalingrunnersets/zagreus-ci$")
        r.kube.on_write = after_cordon
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, [])

    def test_a_degraded_pass_still_bounds_an_unlimited_set(self):
        r = Rig(mode="caps")
        del r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"]
        r.kube.fail_get.append(r"autoscalingrunnersets/zagreus-docker-amd64$")
        r.tick()
        self.assertEqual(r.kube.caps()["zagreus-ci"], 3)

    def test_demand_for_a_set_whose_runners_are_unreadable_wakes_nothing(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.fail_get.append(r"arc-builders/ephemeralrunners")
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertNotIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_a_lease_never_readable_does_not_delay_lowering_for_another_node(self):
        # Delta review of 22f122a, LOW 4: 02's Lease is unreadable from the
        # start. When 01 then dies (its Lease read and found stale), the
        # drop must not wait a pass because of 02.
        r = Rig(mode="caps")
        r.kube.lease_fail.add("zag-amd64-02")
        r.tick()
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 1, "zagreus-ci-x64": 1, "zagreus-docker-amd64": 1})
        r.kube.last_renew["zag-amd64-01"] = r.clock()
        r.kube.dead.add("zag-amd64-01")  # Ready stays True: only the Lease knows
        r.tick(advance=30)
        self.assertEqual(r.kube.caps(), {"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-docker-amd64": 0})

    def test_low3_a_leftover_asleep_list_does_not_hold_a_ready_node_out_in_caps(self):
        r = Rig(mode="caps")
        r.kube.cms[("log-heartbeat", "waker-asleep")] = {"data": {
            "asleep": "zag-amd64-02", "renewed": str(int(T0)), "emitter": "cp1"}}
        r.tick()
        self.assertEqual(r.kube.caps(), CEILINGS)

    def test_a_repeated_wish_is_logged_once(self):
        r = Rig(mode="observe")
        r.tick()
        with self.assertLogs("waker", level="INFO") as logs:
            for i in range(5):
                r.tick(advance=61 * 60 if i == 0 else 30)
        self.assertEqual(sum("would sleep" in m for m in logs.output), 1)


class LeaseFindings(unittest.TestCase):
    """Delta review of c9d2ce0."""

    def no_amt_rig(self):
        r = Rig()
        r.waker.wol = {"zag-amd64-02": "00:00:5e:00:53:01", "_broadcast": "192.0.2.255"}
        r.amt.missing.add("zag-amd64-02")  # wakeable by WoL only: off is judged by the Lease
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-ci")
        return r

    def test_without_amt_a_stale_lease_confirms_off(self):
        r = self.no_amt_rig()
        r.tick()
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, ["zag-amd64-02"])
        self.assertIn("zag-amd64-02", r.kube.asleep())
        self.assertEqual(r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"][w.ANNOTATION_STATE], "asleep")

    def test_without_amt_a_hung_shutdown_with_a_live_kubelet_is_not_off(self):
        r = self.no_amt_rig()
        r.tick()
        r.talos.shutdown = lambda ip: r.talos.calls.append("zag-amd64-02")  # accepted, nothing happens
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, ["zag-amd64-02"])
        self.assertNotIn("zag-amd64-02", r.kube.asleep())

    def test_without_amt_an_unreadable_lease_never_confirms_off(self):
        # The shutdown hangs and the Lease cannot be read: not "off".
        r = self.no_amt_rig()
        r.tick()

        def hangs_and_lease_unreadable(ip):
            r.talos.calls.append("zag-amd64-02")
            r.kube.lease_fail.add("zag-amd64-02")
        r.talos.shutdown = hangs_and_lease_unreadable
        r.tick(advance=61 * 60)
        self.assertEqual(r.talos.calls, ["zag-amd64-02"])  # the sleep did reach the shutdown
        self.assertNotIn("zag-amd64-02", r.kube.asleep())
        self.assertNotEqual(r.kube.nodes["zag-amd64-02"]["metadata"]["annotations"].get(w.ANNOTATION_STATE),
                            "asleep")

    def test_a_worker_clock_far_behind_still_counts_a_live_node(self):
        r = Rig()
        r.kube.lease_skew["zag-amd64-02"] = -3600
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        r.tick(advance=30)
        self.assertEqual(r.kube.caps(), CEILINGS)

    def test_one_failed_lease_read_does_not_drop_the_node(self):
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        r.kube.lease_fail.add("zag-amd64-02")
        r.kube.writes.clear()
        r.tick(advance=30)  # the real cadence
        self.assertEqual(r.kube.caps(), CEILINGS)
        self.assertFalse([x for x in r.kube.writes if "autoscalingrunnersets" in x[1]])

    def test_a_lease_failing_for_long_drops_the_node(self):
        r = Rig()
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")
        r.tick()
        r.kube.lease_fail.add("zag-amd64-02")
        r.tick(advance=30)
        r.tick(advance=30)
        r.tick(advance=30)  # 90 s without a good read
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_another_writer_on_the_lease_does_not_revive_a_dead_node(self):
        r = Rig()
        r.kube.dead.add("zag-amd64-02")
        r.kube.last_renew["zag-amd64-02"] = r.clock() - 86400
        r.kube.lease_extra_fields["zag-amd64-02"] = [
            {"manager": "kubectl-label", "operation": "Update", "time": iso(r.clock())}]
        r.tick()
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_a_restarted_waker_does_not_count_a_dead_node(self):
        # Delta review: first sight must not count as a renewal. The node
        # died a day ago and the node controller is down, so Ready stays True.
        r = Rig()
        r.kube.dead.add("zag-amd64-02")
        r.kube.last_renew["zag-amd64-02"] = r.clock() - 86400
        r.tick()  # a fresh Waker: nothing remembered
        self.assertEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_without_amt_off_waits_a_margin_after_the_lease_stops(self):
        r = self.no_amt_rig()
        r.tick()
        times = []
        real = r.talos.shutdown

        def stamp(ip):
            times.append(r.clock())
            real(ip)
        r.talos.shutdown = stamp
        orig = r.kube.merge_patch

        def watch(path, body):
            if body.get("metadata", {}).get("annotations", {}).get(w.ANNOTATION_STATE) == "asleep":
                times.append(r.clock())
            return orig(path, body)
        r.kube.merge_patch = watch
        r.tick(advance=61 * 60)
        self.assertEqual(len(times), 2, times)
        self.assertGreaterEqual(times[1] - times[0], w.LEASE_FRESH_S + 30)

    def test_a_woken_node_without_a_live_kubelet_is_a_failed_wake(self):
        # Ready but no Lease renewals: the wake must not count it.
        r = Rig()
        r.put_all_to_sleep()
        orig = r.kube.get

        def boot_without_lease(path):
            out = orig(path)
            r.kube.dead.add("zag-amd64-01")
            return out
        r.kube.get = boot_without_lease
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual(r.kube.nodes["zag-amd64-01"]["metadata"]["annotations"][w.ANNOTATION_STATE],
                         "wake-failed")
        self.assertEqual(r.kube.caps()["zagreus-docker-amd64"], 0)


class GitHubClient(unittest.TestCase):
    """ETags, paging and the rate limit, against a fake urlopen."""

    def setUp(self):
        self.calls = []
        self.etag_hits = 0
        self.rate_limited = False
        self.orig = w.urllib.request.urlopen
        test = self

        class Resp:
            def __init__(self, data, etag):
                self.data, self.headers = data, {"ETag": etag}

            def read(self, *a):
                return json.dumps(self.data).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            url = req.full_url
            test.calls.append(url)
            if test.rate_limited:
                raise w.urllib.error.HTTPError(url, 403, "rate", {"X-RateLimit-Remaining": "0",
                                                                  "X-RateLimit-Reset": str(int(T0) + 900)}, None)
            if req.get_header("If-none-match") == "E":
                test.etag_hits += 1
                raise w.urllib.error.HTTPError(url, 304, "not modified", {}, None)
            if "/actions/runs?" in url:
                page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["page"][0])
                runs = [{"id": i} for i in range((page - 1) * 100, min(page * 100, 150))]
                return Resp({"workflow_runs": runs}, "E")
            return Resp({"jobs": [{"id": 1, "status": "queued", "labels": ["zagreus-ci"],
                                   "created_at": "2026-10-23T00:00:00Z"}]}, "E")
        w.urllib.request.urlopen = fake

    def tearDown(self):
        w.urllib.request.urlopen = self.orig

    def test_pages_and_then_revalidates(self):
        gh = w.GitHub("t", "https://api.example", clock=lambda: T0)
        now = dt.datetime.fromtimestamp(T0, UTC)
        first = gh.queued_jobs("o/r", 72, now)
        self.assertEqual(len(first), 2 * 150)  # 150 runs over two pages, both statuses
        n, hits = len(self.calls), self.etag_hits
        second = gh.queued_jobs("o/r", 72, now)
        self.assertEqual(len(second), len(first))
        self.assertEqual(self.etag_hits - hits, len(self.calls) - n)  # every repeat was a 304

    def test_rate_limit_stops_calls_until_reset(self):
        clock = Clock()
        gh = w.GitHub("t", "https://api.example", clock=clock)
        now = dt.datetime.fromtimestamp(T0, UTC)
        self.rate_limited = True
        with self.assertRaises(Exception):
            gh.queued_jobs("o/r", 72, now)
        self.rate_limited = False
        n = len(self.calls)
        with self.assertRaises(RuntimeError):
            gh.queued_jobs("o/r", 72, now)
        self.assertEqual(len(self.calls), n)  # no request while blocked
        clock.sleep(901)
        gh.queued_jobs("o/r", 72, now)
        self.assertGreater(len(self.calls), n)


class ReofferFindings(unittest.TestCase):
    """26 Oct 2026 live finding: a listener restarts whenever maxRunners
    changes, and a job its set turned down for want of room is held about
    ten minutes before GitHub offers it again. Waking 01 for zagreus-ci: 2
    raised the cap to 1, the second job was turned down, and raising it to
    3 when 02 woke three minutes later left it queued 14 minutes beside two
    idle slots."""

    def ci_patches(self, r):
        return [b["spec"]["maxRunners"] for v, p, b in r.kube.writes
                if v == "PATCH" and p.endswith("/autoscalingrunnersets/zagreus-ci")]

    def test_demand_beyond_one_node_wakes_every_node_it_needs_and_raises_caps_once(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.writes.clear()
        r.amt.calls.clear()
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)
        self.assertEqual(self.ci_patches(r), [3])  # one listener restart, never 0 -> 1 -> 3
        self.assertEqual(r.kube.asleep(), set())

    def test_demand_one_node_covers_wakes_only_that_node(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.calls.clear()
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual([c for c in r.amt.calls if c[1] == w.POWER_ON], [("zag-amd64-01", w.POWER_ON)])
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})

    def test_a_node_that_fails_to_wake_does_not_stop_the_others_raising_caps(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.writes.clear()
        r.amt.refuse_on.add("zag-amd64-01")
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual(self.ci_patches(r), [2])  # 02's two slots, raised once
        self.assertGreater(r.waker.backoff_until.get("zag-amd64-01", 0), r.clock())

    def test_a_held_job_keeps_its_nodes_awake(self):
        # Turned down and held: the set shows spare, so unmet is 0, but the
        # node about to sleep is the room the job will be offered.
        r = Rig()
        r.tick()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + 1)
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), set())
        r.github.jobs.clear()
        r.tick(advance=1)
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})

    def test_sets_sharing_a_node_share_its_room_when_choosing_wakes(self):
        # Review of 27905470, P2: slots were counted per set, but the
        # builder and a CI runner share 01's memory. With room for only the
        # builder there, 02 must wake in the same pass, not a pass later.
        r = Rig()
        r.put_all_to_sleep()
        r.kube.nodes["zag-amd64-01"]["status"]["allocatable"]["memory"] = "8Gi"
        r.kube.writes.clear()
        r.amt.calls.clear()
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)
        self.assertEqual(len(self.ci_patches(r)), 1)
        self.assertGreaterEqual(r.kube.caps()["zagreus-ci"], 1)

    def test_a_job_queued_for_ever_stops_holding_nodes_awake(self):
        # Review of 27905470, P2: an orphaned queued job the lookback still
        # reads must not pin nodes awake for 72 h.
        r = Rig()
        r.tick()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + r.cfg["heldForSeconds"] + 1)
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})

    def test_a_node_never_ready_does_not_stop_the_other_raising_caps(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.writes.clear()
        real = r.amt.request_power

        def never_boots(node, state):
            real(node, state)
            if node == "zag-amd64-02":
                r.kube.boots[node] = r.clock() + 10 ** 6
        r.amt.request_power = never_boots
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual(self.ci_patches(r), [1])  # 01's slot, raised once
        self.assertGreater(r.waker.backoff_until.get("zag-amd64-02", 0), r.clock())
        self.assertNotIn("zag-amd64-02", r.kube.asleep())  # so its alert fires

    def test_the_ready_deadline_runs_from_the_first_power_on(self):
        r = Rig()
        r.put_all_to_sleep()
        r.amt.boot_seconds = 10 ** 6  # neither ever Ready
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        start = r.clock()
        r.tick(advance=300)
        spent = r.clock() - start - 300
        self.assertLess(spent, r.cfg["readyTimeoutSeconds"] + r.cfg["powerOnTimeoutSeconds"] + 120)

    def test_codex_two_stuck_claims_still_wake_one_node_per_episode(self):
        # Codex on 27905470: stuck claims with no unmet demand are a guess
        # (three here: with room on paper, 01 alone seems to cover two)
        # (a bad template never schedules), so batching must not wake both.
        r = Rig()
        r.put_all_to_sleep()
        for _ in range(3):
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"},
                                                           "creationTimestamp": iso(r.clock() - 3600)},
                                              "status": {}})
        for _ in range(40):
            r.tick(advance=60)
        self.assertEqual(sum(1 for _, st in r.amt.calls if st == w.POWER_ON), 1)

    def stuck(self, r, n, set_name="zagreus-ci"):
        ns = "arc-builders" if set_name == "zagreus-docker-amd64" else "arc-runners"
        for _ in range(n):
            r.kube.ers[ns].append({"metadata": {"labels": {w.SCALE_SET_LABEL: set_name},
                                                "creationTimestamp": iso(r.clock() - 3600)},
                                   "status": {}})

    def test_stuck_claims_and_queued_jobs_add_up_when_choosing_wakes(self):
        # Delta review of 723c5352, P2: stuck claims and queued jobs never
        # overlap (unmet already subtracts the claims), and claims placed
        # on paper counted as room: two claims plus four jobs woke 01 alone,
        # and 02 a pass later, a second raise.
        r = Rig()
        r.put_all_to_sleep()
        r.amt.calls.clear()
        self.stuck(r, 2)
        for _ in range(4):
            r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_one_stuck_claim_and_one_unmet_job_need_two_ci_slots(self):
        # The tight case for the sum: max(unmet, stuck) leaves 01 looking
        # enough. (Leaving claims out of room is not pinned by a test: with
        # two nodes, the over-count only ever mattered on the guess path.)
        r = Rig()
        r.put_all_to_sleep()
        r.amt.calls.clear()
        self.stuck(r, 1)
        for _ in range(2):
            r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_a_stuck_only_set_beside_real_demand_adds_at_most_one_node(self):
        # Delta review of 723c5352, P3: demand for the builder plus stuck CI
        # claims (a bad template, say) must not wake 02 for the claims.
        r = Rig()
        r.put_all_to_sleep()
        r.amt.calls.clear()
        self.stuck(r, 3)
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual([c for c in r.amt.calls if c[1] == w.POWER_ON], [("zag-amd64-01", w.POWER_ON)])

    def test_a_stuck_episode_is_not_spent_by_a_node_with_no_room_for_it(self):
        # Delta review of dc38d10d, P3: 02 was chosen for the stuck CI claim
        # and failed; 01 woke for the builder with no room left for CI. The
        # CI episode must stay open, not be spent by 01's mere slot.
        r = Rig()
        r.put_all_to_sleep()
        r.kube.nodes["zag-amd64-01"]["status"]["allocatable"]["memory"] = "8Gi"
        r.amt.refuse_on.add("zag-amd64-02")
        self.stuck(r, 1)
        r.job(["zagreus-docker-amd64"], age_seconds=1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)  # tried for the claim
        self.assertNotIn("zagreus-ci", r.waker.stuck_woke)

    def test_codex_a_failed_annotation_for_one_node_does_not_abandon_the_other(self):
        r = Rig()
        r.put_all_to_sleep()
        r.kube.writes.clear()
        orig = r.kube.merge_patch

        def fail_02_waking(path, body):
            if path == "/api/v1/nodes/zag-amd64-02" and body.get("metadata", {}).get(
                    "annotations", {}).get(w.ANNOTATION_STATE) == "waking":
                raise RuntimeError("apiserver hiccup")
            return orig(path, body)
        r.kube.merge_patch = fail_02_waking
        r.job(["zagreus-ci"], age_seconds=1)
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=300)
        self.assertEqual(self.ci_patches(r), [1])
        self.assertNotIn("zag-amd64-01", r.kube.asleep())
        self.assertGreater(r.waker.backoff_until.get("zag-amd64-02", 0), r.clock())

    def test_codex_an_orphan_job_seen_beside_spare_stops_counting_as_demand(self):
        # Codex on dc38d10d: a queued job that never dispatches, once past
        # the held window, still counted as demand: the nodes slept, their
        # caps went to 0, the orphan became unmet and woke one, and so on.
        r = Rig()
        r.tick()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + r.cfg["heldForSeconds"] + 1)
        for _ in range(8 * 60):
            r.tick(advance=60)
        self.assertEqual([c for c in r.amt.calls if c[1] == w.POWER_ON], [])
        self.assertEqual(r.kube.asleep(), {"zag-amd64-01", "zag-amd64-02"})

    def test_one_free_slot_does_not_orphan_a_backlog(self):
        # Delta review of 4073dead, P2: a runner finished and, for a poll,
        # its set showed one free slot; every old job behind it was
        # orphaned for good. Spare must last heldForSeconds first, and then
        # only as many jobs as there are free slots.
        r = Rig()
        r.tick()
        for i in range(2):
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"}},
                                              "status": {"jobRequestId": 100 + i}})
        for _ in range(4):
            r.job(["zagreus-ci"], age_seconds=28 * 60)
        r.tick(advance=30)
        self.assertEqual(r.waker.orphans, set())
        for _ in range(r.cfg["heldForSeconds"] // 30 + 2):
            r.tick(advance=30)
        self.assertLessEqual(len(r.waker.orphans), 1)

    def test_spare_just_raised_is_not_spare_yet(self):
        # Delta review of 4073dead, P3: a raise restarts the listener and
        # GitHub holds turned-down jobs ~10 min; spare that new is on paper.
        r = Rig()
        r.put_all_to_sleep()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + r.cfg["heldForSeconds"] + 1)
        r.tick(advance=300)  # wakes 01, raises zagreus-ci
        r.tick(advance=30)
        self.assertEqual(r.waker.orphans, set())

    def test_a_raise_restarts_the_spare_clock(self):
        # Spare seen for ten minutes, then a raise (02 uncordoned by hand):
        # the listener restarted, so the old spare no longer counts.
        r = Rig()
        r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = True
        r.kube.add_runner_pod("zag-amd64-02", "zagreus-ci")  # keeps 02 out of the sleep choice
        r.tick()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + r.cfg["heldForSeconds"] + 1)
        for _ in range(20):
            r.tick(advance=30)  # ten minutes of spare on 01
        r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = False
        for _ in range(12):
            r.tick(advance=30)  # six minutes after the raise
        self.assertEqual(r.waker.orphans, set())

    def test_a_job_queued_long_while_its_set_was_full_is_not_an_orphan(self):
        # Waiting behind busy runners is not orphaned: no spare was seen.
        r = Rig()
        r.put_all_to_sleep()
        r.amt.calls.clear()
        r.job(["zagreus-ci"], age_seconds=r.cfg["heldAfterSeconds"] + r.cfg["heldForSeconds"] + 1)
        r.tick(advance=300)
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_a_young_job_with_spare_does_not_hold_nodes_awake(self):
        r = Rig()
        r.tick()
        r.job(["zagreus-ci"], age_seconds=1)
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})


class Modes(unittest.TestCase):
    def test_observe_writes_nothing_at_all(self):
        r = Rig(mode="observe")
        r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"] = 3
        r.tick()
        r.tick(advance=5 * 3600)
        r.job(["zagreus-ci"], age_seconds=600)
        r.tick(advance=300)
        self.assertEqual(r.kube.writes, [])
        self.assertEqual(r.talos.calls, [])

    def test_caps_mode_drives_caps_but_never_power(self):
        r = Rig(mode="caps")
        r.tick()
        self.assertEqual(r.kube.caps(), CEILINGS)
        r.tick(advance=5 * 3600)
        self.assertEqual(r.talos.calls, [])
        self.assertEqual(r.amt.calls, [])
        self.assertEqual(r.kube.asleep(), set())


class Heartbeat(unittest.TestCase):
    """The shell loop in core/kmsg-heartbeat.yaml, run for one pass."""

    def script(self):
        yq = shutil.which("yq")
        if not yq:
            self.skipTest("yq not installed (mise install)")
        path = os.path.join(WAKER_DIR, "..", "core", "kmsg-heartbeat.yaml")
        out = subprocess.run([yq, 'select(.kind == "DaemonSet") | .spec.template.spec.containers[0].command[2]', path],
                             check=True, capture_output=True, text=True).stdout
        self.assertIn("sleep 60 || exit 1", out)
        return out

    def run_once(self, files, node):
        d = tempfile.mkdtemp()
        try:
            kmsg = os.path.join(d, "kmsg")
            wdir = os.path.join(d, "waker")
            os.mkdir(wdir)
            for k, v in files.items():
                with open(os.path.join(wdir, k), "w") as f:
                    f.write(v)
            # /dev/kmsg takes each write as one record; a plain file needs >>.
            s = (self.script().replace("> /dev/kmsg", ">> " + kmsg).replace("/waker/", wdir + "/")
                 .replace("sleep 60 || exit 1", "exit 0"))
            subprocess.run(["sh", "-c", s], check=True, env={"NODE_NAME": node, "PATH": os.environ["PATH"]},
                           timeout=10)
            with open(kmsg) as f:
                return [l[3:].rstrip("\n") for l in f]
        finally:
            shutil.rmtree(d)

    def test_matches_the_python_model(self):
        import time as _t
        now = int(_t.time())
        cases = [
            ({"asleep": "zag-amd64-01 zag-amd64-02", "emitter": "cp1", "renewed": str(now)}, "cp1"),
            ({"asleep": "zag-amd64-01", "emitter": "cp1", "renewed": str(now)}, "zag-amd64-01"),
            ({"asleep": "zag-amd64-01", "emitter": "cp1", "renewed": str(now - 400)}, "cp1"),
            ({"asleep": "zag-amd64-01 BAD;rm x", "emitter": "cp1", "renewed": str(now)}, "cp1"),
            ({"asleep": "zag-amd64-01", "emitter": "cp1", "renewed": "nonsense"}, "cp1"),
            # A glob must not expand; a 0-led time with an 8 or 9 must not
            # end the loop (octal); at most eight names.
            ({"asleep": "* zag-amd64-01 ?", "emitter": "cp1", "renewed": str(now)}, "cp1"),
            ({"asleep": "zag-amd64-01", "emitter": "cp1", "renewed": "0" + str(now)}, "cp1"),
            ({"asleep": " ".join(f"n{i}" for i in range(12)), "emitter": "cp1", "renewed": str(now)}, "cp1"),
            ({}, "cp1"),
        ]
        for files, node in cases:
            got = self.run_once(files, node)
            want = ["log-pipeline heartbeat"] + w.heartbeat_lines(
                files.get("asleep", ""), files.get("emitter", ""), node, files.get("renewed", ""), now, 300)
            self.assertEqual(got, want, files)


if __name__ == "__main__":
    unittest.main()
