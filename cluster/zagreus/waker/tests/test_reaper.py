"""The reaper and the oldest-runner gauge (README, "Reaper").

15 Nov 2026: an EphemeralRunner whose pod sat on zag-amd64vm-03, NotReady
since 14:42Z (its pod stuck Terminating), counted as zagreus-ci's one
current runner, so the listener never scaled up and spritz's jobs waited
for hours, until a human deleted the runner.

    python3 -m unittest discover -s cluster/zagreus/waker/tests -v
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import waker as w  # noqa: E402
from test_waker import iso, live_config, production_config, WAKER_DIR  # noqa: E402
from test_always_on import VM, AlwaysOnRig  # noqa: E402

NS = "arc-runners"
CI = "zagreus-ci"


def add_runner(r, name, node, phase="Running", created_ago=600, deleting=False, set_name=CI):
    """An EphemeralRunner of set_name with its pod on node (None: unbound)."""
    md = {"name": name, "uid": f"uid-{name}", "labels": {w.SCALE_SET_LABEL: set_name},
          "creationTimestamp": iso(r.clock() - created_ago)}
    if deleting:
        md["deletionTimestamp"] = iso(r.clock())
    r.kube.ers[NS].append({"metadata": md, "status": {"jobRequestId": 7, "phase": "Running"}})
    spec = {"containers": [{"name": "runner"}]}
    if node:
        spec["nodeName"] = node
    r.kube.pods.append({"metadata": {"name": name, "namespace": NS, "labels": {w.SCALE_SET_LABEL: set_name},
                                     "ownerReferences": [{"kind": "EphemeralRunner", "name": name}]},
                        "spec": spec, "status": {"phase": phase}})


def rig(minutes=10, mode=None):
    r = AlwaysOnRig()
    # None: a waker.conf without the key, which leaves the default.
    r.cfg["reapNotReadyMinutes"] = w.DEFAULTS["reapNotReadyMinutes"] if minutes is None else minutes
    if mode:
        r.cfg["mode"] = mode
    return r


def deletes(r):
    return [x for x in r.kube.writes if x[0] == "DELETE"]


def names(r):
    return [e["metadata"].get("name") for e in r.kube.ers[NS]]


class Reaper(unittest.TestCase):
    def test_reaps_a_runner_on_an_always_on_node_notready_11_minutes(self):
        # The vm-03 case: an always-on node counts.
        r = rig()
        add_runner(r, "ci-stranded", VM)
        r.kube.set_ready(VM, False)
        r.tick(advance=11 * 60)
        self.assertNotIn("ci-stranded", names(r))
        self.assertEqual(deletes(r), [(
            "DELETE", f"/apis/actions.github.com/v1alpha1/namespaces/{NS}/ephemeralrunners/ci-stranded",
            {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": "uid-ci-stranded"}})])
        events = r.kube.cms[("zagreus-waker", "waker-events")]["data"]["events"]
        self.assertIn('"event": "reaped"', events)
        self.assertIn('"runner": "ci-stranded"', events)

    def test_notready_9_minutes_is_left_alone(self):
        r = rig()
        add_runner(r, "ci-stranded", VM)
        r.kube.set_ready(VM, False)
        r.tick(advance=9 * 60)
        self.assertEqual(deletes(r), [])
        self.assertIn("ci-stranded", names(r))

    def test_a_ready_node_is_left_alone(self):
        r = rig()
        add_runner(r, "ci-running", VM, created_ago=5 * 3600)
        r.tick(advance=11 * 60)
        self.assertEqual(deletes(r), [])

    def test_a_node_the_waker_is_waking_is_left_alone(self):
        r = rig()
        add_runner(r, "ci-on-booting", "zag-amd64-01")
        r.kube.set_ready("zag-amd64-01", False)
        r.kube.nodes["zag-amd64-01"]["metadata"]["annotations"][w.ANNOTATION_STATE] = "waking"
        r.clock.sleep(11 * 60)
        r.waker.reap_stale_runners(r.waker.observe())
        self.assertEqual(deletes(r), [])
        # Positive control: the same node, no longer waking, is reaped.
        r.kube.nodes["zag-amd64-01"]["metadata"]["annotations"][w.ANNOTATION_STATE] = "awake"
        r.waker.reap_stale_runners(r.waker.observe())
        self.assertEqual(len(deletes(r)), 1)

    def test_observe_mode_logs_and_deletes_nothing(self):
        r = rig(mode="observe")
        add_runner(r, "ci-stranded", VM)
        r.kube.set_ready(VM, False)
        with self.assertLogs("waker", level="INFO") as logs:
            r.tick(advance=11 * 60)
        self.assertEqual(deletes(r), [])
        self.assertIn("ci-stranded", names(r))
        self.assertTrue(any("would reap" in m and "ci-stranded" in m for m in logs.output), logs.output)

    def test_disabled_when_the_key_is_absent(self):
        r = rig(minutes=None)
        self.assertIsNone(w.DEFAULTS["reapNotReadyMinutes"])
        add_runner(r, "ci-stranded", VM)
        r.kube.set_ready(VM, False)
        r.tick(advance=3 * 3600)
        self.assertEqual(deletes(r), [])

    def test_a_runner_already_being_deleted_is_not_deleted_again(self):
        r = rig()
        add_runner(r, "ci-going", VM, deleting=True)
        r.kube.set_ready(VM, False)
        r.tick(advance=11 * 60)
        self.assertEqual(deletes(r), [])

    def test_an_unbound_runner_is_the_wake_logics_not_the_reapers(self):
        r = rig()
        add_runner(r, "ci-pending", None, phase="Pending")
        r.kube.set_ready(VM, False)
        r.clock.sleep(11 * 60)
        r.waker.reap_stale_runners(r.waker.observe())
        self.assertEqual(deletes(r), [])

    def test_a_failed_delete_is_logged_and_the_pass_goes_on(self):
        r = rig()
        add_runner(r, "ci-stranded", VM)
        r.kube.set_ready(VM, False)

        def boom(path, body=None):
            raise RuntimeError("DELETE: 403")
        r.kube.delete = boom
        with self.assertLogs("waker", level="ERROR") as logs:
            r.tick(advance=11 * 60)
        self.assertTrue(any("could not reap" in m for m in logs.output))


class StaleRunners(unittest.TestCase):
    """The pure choice, on hand-built nodes."""

    NOW = 1_800_000_000.0

    def node(self, ready, since_ago, state=None):
        ann = {w.ANNOTATION_STATE: state} if state else {}
        return {"metadata": {"annotations": ann},
                "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                           "lastTransitionTime": iso(self.NOW - since_ago)}]}}

    def runner(self, node):
        return {"set": CI, "namespace": NS, "name": "r1", "pod": {"spec": {"nodeName": node}}}

    def test_a_node_the_waker_put_to_sleep_is_not_exempt(self):
        # Design decision (README, "Reaper"): a sleeping node was drained and
        # stays cordoned, so a runner pod bound to it can never run.
        nodes = {"n": self.node(False, 3600, state="asleep")}
        self.assertEqual([x[1] for x in w.stale_runners([self.runner("n")], nodes, self.NOW, 10)], ["n"])

    def test_unknown_ready_counts_as_notready(self):
        nodes = {"n": self.node(False, 601)}
        self.assertEqual(len(w.stale_runners([self.runner("n")], nodes, self.NOW, 10)), 1)

    def test_exactly_the_threshold_is_not_past_it(self):
        nodes = {"n": self.node(False, 600)}
        self.assertEqual(w.stale_runners([self.runner("n")], nodes, self.NOW, 10), [])

    def test_a_node_not_read_this_pass_is_left_alone(self):
        self.assertEqual(w.stale_runners([self.runner("elsewhere")], {}, self.NOW, 10), [])

    def test_no_ready_condition_is_not_known_so_left_alone(self):
        nodes = {"n": {"metadata": {}, "status": {"conditions": []}}}
        self.assertEqual(w.stale_runners([self.runner("n")], nodes, self.NOW, 10), [])


class ReapConfig(unittest.TestCase):
    def test_absent_and_positive_are_valid(self):
        cfg = production_config()
        cfg.pop("reapNotReadyMinutes", None)
        w.validate_config(cfg)
        cfg["reapNotReadyMinutes"] = 10
        w.validate_config(cfg)

    def test_zero_strings_and_booleans_are_refused(self):
        for bad in (0, -5, "10", True):
            cfg = production_config()
            cfg["reapNotReadyMinutes"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                w.validate_config(cfg)

    def test_the_deployed_conf_reaps_after_ten_minutes(self):
        self.assertEqual(live_config()["reapNotReadyMinutes"], 10)

    def test_the_rbac_lets_it_delete_runners_in_every_managed_namespace(self):
        with open(os.path.join(WAKER_DIR, "waker.yaml")) as f:
            docs = f.read().split("\n---\n")
        for ns in sorted({s["namespace"] for s in live_config()["sets"]}):
            role = next(d for d in docs if re.search(r"^kind: Role$", d, re.M)
                        and re.search(rf"^  namespace: {ns}$", d, re.M)
                        and "ephemeralrunners" in d)
            rule = re.search(r"resources: \[ephemeralrunners\]\n(\s+resourceNames: .*\n)?\s+verbs: \[([^\]]*)\]", role)
            self.assertIsNotNone(rule, ns)
            self.assertIsNone(rule.group(1), f"{ns}: runner names are random; a resourceNames rule matches none")
            self.assertEqual(sorted(v.strip() for v in rule.group(2).split(",")), ["delete", "list"], ns)


class OldestRunnerGauge(unittest.TestCase):
    NOW = 1_800_000_000.0

    def er(self, set_name, age, phase):
        return {"set": set_name, "name": f"r{age}", "created": self.NOW - age,
                "pod": {"status": {"phase": phase}} if phase else None}

    def test_the_oldest_running_runner_per_set(self):
        ers = [self.er(CI, 100, "Running"), self.er(CI, 9000, "Running"), self.er(CI, 99999, "Pending"),
               self.er("zagreus-docker-amd64", 50, None)]
        out = w.oldest_running_runner(ers, {CI, "zagreus-docker-amd64", "zagreus-ci-x64"}, self.NOW)
        self.assertEqual(out, {CI: 9000, "zagreus-docker-amd64": 0, "zagreus-ci-x64": 0})

    def test_a_set_whose_runners_were_not_read_is_absent_not_zero(self):
        out = w.oldest_running_runner([self.er(CI, 100, "Running")], set(), self.NOW)
        self.assertEqual(out, {})

    def test_rendered_with_the_slot_metrics(self):
        r = rig()
        add_runner(r, "ci-long", VM, created_ago=4 * 3600)
        r.tick()
        text = r.kube.cms[("node-power", "waker-metrics")]["data"]["slots.prom"]
        self.assertIn("# TYPE zagreus_runner_oldest_runner_seconds gauge", text)
        self.assertIn('zagreus_runner_oldest_runner_seconds{set="zagreus-ci"} 14400', text)
        self.assertIn('zagreus_runner_oldest_runner_seconds{set="zagreus-docker-amd64"} 0', text)

    def test_a_set_whose_runners_are_unreadable_writes_no_row(self):
        r = rig()
        r.kube.fail_get.append(r"/namespaces/arc-builders/ephemeralrunners")
        r.tick()
        text = r.kube.cms[("node-power", "waker-metrics")]["data"]["slots.prom"]
        self.assertNotIn('zagreus_runner_oldest_runner_seconds{set="zagreus-docker-amd64"}', text)
        self.assertIn('zagreus_runner_oldest_runner_seconds{set="zagreus-ci"} 0', text)


if __name__ == "__main__":
    unittest.main()
