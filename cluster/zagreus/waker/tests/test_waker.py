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
                "spec": {"maxRunners": 0, "runnerScaleSetLabels": SET_LABELS[s["name"]]}}
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
                "spec": {"nodeName": cfg["nodes"][0]["name"]},
                "status": {"phase": "Running"}})

    def add_node(self, name, ready):
        self.nodes[name] = {
            "metadata": {"name": name, "annotations": {}},
            "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
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
        self.nodes[name]["status"]["conditions"][0]["status"] = "True" if ready else "False"

    def add_runner_pod(self, node, set_name, phase="Running", name=None):
        self.pods.append({
            "metadata": {"name": name or f"{set_name}-runner-{len(self.pods)}", "namespace": "arc-runners",
                         "labels": {w.SCALE_SET_LABEL: set_name},
                         "ownerReferences": [{"kind": "EphemeralRunner", "name": "x"}]},
            "spec": {"nodeName": node},
            "status": {"phase": phase},
        })

    # --- API

    def get(self, path):
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
        m = re.fullmatch(r"/api/v1/nodes/([^/]+)", p)
        if m:
            name = m.group(1)
            if name in self.boots and self.clock() >= self.boots[name]:
                self.set_ready(name, True)  # the kubelet comes up on its own
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

    def available(self, node):
        return True

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
        self.kube.set_ready(node, False)
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
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=300)  # wakes 01 at time t; the wake itself takes ~65 s
        r.amt.calls.clear()
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
        r.kube.set_ready("zag-amd64-02", True)
        r.tick(advance=30)
        self.assertNotIn("zag-amd64-02", r.kube.asleep())
        self.assertNotIn("unschedulable", r.kube.nodes["zag-amd64-02"]["spec"])
        self.assertEqual(r.kube.caps()["zagreus-ci"], 2)


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
            ({}, "cp1"),
        ]
        for files, node in cases:
            got = self.run_once(files, node)
            want = ["log-pipeline heartbeat"] + w.heartbeat_lines(
                files.get("asleep", ""), files.get("emitter", ""), node, files.get("renewed", ""), now, 300)
            self.assertEqual(got, want, files)


if __name__ == "__main__":
    unittest.main()
