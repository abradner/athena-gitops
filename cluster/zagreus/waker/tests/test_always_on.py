"""An always-on node (README, "Always-on node"): a node the waker counts
but never powers, cordons or sleeps. Its slots are room whenever it is
Ready, so the first jobs of a burst start there with no wake, and the
sleeping Talos nodes wake only for demand beyond it.

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
from test_waker import (T0, TWO_NODES, WAKER_DIR, FakeAMT, FakeGitHub, FakeKube,  # noqa: E402
                        FakeTalos, Clock, live_config, production_config, states)

VM = "zag-amd64vm-01"
TAINT = "zagreus.asn.casa/always-on"


def always_on_config():
    """The frozen two-node world plus one always-on node with one zagreus-ci
    slot, listed last (as in waker.conf)."""
    cfg = production_config()
    cfg["nodes"] = [dict(n, slots=dict(n["slots"])) for n in TWO_NODES] + [
        {"name": VM, "alwaysOn": True, "slots": {"zagreus-ci": 1}}]
    cfg["toleratedTaintKeys"] = [TAINT]
    return cfg


class AlwaysOnRig:
    """The Rig of test_waker, with the always-on node: no AMT address, its
    own taint, and allocatable for one heavy pod."""

    def __init__(self):
        self.cfg = always_on_config()
        self.clock = Clock()
        self.kube = FakeKube(self.cfg, self.clock)
        self.kube.nodes[VM]["status"]["allocatable"] = {"cpu": "3800m", "memory": "7Gi"}
        self.kube.taint(VM, TAINT)
        self.github = FakeGitHub()
        self.amt = FakeAMT(self.kube, self.clock)
        self.amt.missing.add(VM)
        self.talos = FakeTalos(self.kube, self.amt)
        self.waker = w.Waker(self.cfg, self.kube, self.github, self.amt, self.talos,
                             clock=self.clock, sleep=self.clock.sleep, my_node="cp1")

    def job(self, labels, age_seconds, repo="abradner/spritz"):
        import datetime as dt
        self.github.jobs.append({
            "repo": repo, "id": len(self.github.jobs) + 1, "name": "j", "labels": labels,
            "created_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)})

    def tick(self, advance=0):
        self.clock.sleep(advance)
        self.waker.tick()

    def put_talos_to_sleep(self):
        self.tick()
        self.tick(advance=61 * 60)
        self.tick(advance=1)
        self.tick(advance=1)
        assert self.kube.asleep() == {"zag-amd64-01", "zag-amd64-02"}, self.kube.asleep()


class Config(unittest.TestCase):
    def test_always_on_must_be_a_boolean(self):
        cfg = always_on_config()
        cfg["nodes"][-1]["alwaysOn"] = "yes"
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_always_on_is_read_from_the_node(self):
        cfg = always_on_config()
        self.assertTrue(w.always_on(cfg, VM))
        self.assertFalse(w.always_on(cfg, "zag-amd64-01"))
        self.assertFalse(w.always_on(cfg, "no-such-node"))


class Decisions(unittest.TestCase):
    def setUp(self):
        self.cfg = always_on_config()

    def test_never_chosen_to_sleep_even_idle_and_last(self):
        # choose_sleep walks the list from the end: the always-on node comes
        # first and must be passed over, even with no wakeable set given.
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, VM: {}})
        idle = {n: T0 for n in ("zag-amd64-01", "zag-amd64-02", VM)}
        self.assertEqual(w.choose_sleep(self.cfg, st, idle, {}, {}, T0 + 2 * 3600), "zag-amd64-02")

    def test_never_chosen_to_wake_even_if_marked_asleep_or_retry(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True},
                       VM: {"ready": False, "asleep": True, "retry": True}})
        self.assertNotIn(VM, w.choose_wakes(self.cfg, {"zagreus-ci": 9}, st, {}, T0))

    def test_its_slot_is_room_while_the_talos_nodes_sleep(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True},
                       VM: {}})
        caps = w.compute_caps(self.cfg, st)
        self.assertEqual(caps["zagreus-ci"], 1)
        self.assertEqual(caps["zagreus-ci-x64"], 0)
        self.assertEqual(caps["zagreus-docker-amd64"], 0)

    def test_its_slot_counts_nothing_while_it_is_down(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True},
                       VM: {"ready": False}})
        self.assertEqual(w.compute_caps(self.cfg, st)["zagreus-ci"], 0)

    def test_wakes_cover_only_demand_beyond_it(self):
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True},
                       VM: {}})
        # Unmet is what the live caps (the VM's one slot) leave over: one job
        # beyond it wakes the first Talos node only.
        self.assertEqual(w.choose_wakes(self.cfg, {"zagreus-ci": 1}, st, {}, T0), ["zag-amd64-01"])


class Procedures(unittest.TestCase):
    def test_idle_for_hours_it_is_never_cordoned_listed_or_shut_down(self):
        r = AlwaysOnRig()
        r.put_talos_to_sleep()
        r.tick(advance=3 * 3600)
        self.assertNotIn(VM, r.talos.calls)
        self.assertNotIn(VM, r.kube.asleep())
        self.assertFalse(r.kube.nodes[VM]["spec"].get("unschedulable"))
        self.assertFalse(any(len(x) > 1 and x[1] == VM for x in r.amt.calls))

    def test_put_to_sleep_refuses_it(self):
        r = AlwaysOnRig()
        r.tick()
        self.assertFalse(r.waker.put_to_sleep(VM, r.waker.observe()))
        self.assertNotIn(VM, r.talos.calls)
        self.assertFalse(r.kube.nodes[VM]["spec"].get("unschedulable"))

    def test_the_asleep_label_sync_never_writes_it(self):
        # Review of 710b204: the label sync patched every configured node,
        # the always-on one included (and logged a 404 every pass before it
        # joined).
        r = AlwaysOnRig()
        r.put_talos_to_sleep()
        r.tick(advance=3 * 3600)
        self.assertFalse([x for x in r.kube.writes if x[0] != "AMT" and str(x[1]).endswith("/nodes/" + VM)],
                         "the waker wrote the always-on node")
        # Positive control: the Talos nodes' labels were written.
        self.assertTrue([x for x in r.kube.writes if x[0] != "AMT" and str(x[1]).endswith("/nodes/zag-amd64-01")])

    def test_the_startup_check_does_not_call_it_unwakeable(self):
        cfg = always_on_config()
        names = w.unwakeable_nodes(cfg, {"zag-amd64-01", "zag-amd64-02"})
        self.assertEqual(names, [])
        self.assertEqual(w.unwakeable_nodes(cfg, {"zag-amd64-01"}), ["zag-amd64-02"])

    def test_the_waker_never_counts_it_as_wakeable(self):
        r = AlwaysOnRig()
        r.amt.missing.discard(VM)  # even with an AMT address by mistake
        self.assertNotIn(VM, r.waker.wakeable())

    def test_the_first_job_takes_its_slot_with_no_wake(self):
        r = AlwaysOnRig()
        r.put_talos_to_sleep()
        r.amt.calls.clear()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.tick(advance=60)
        self.assertEqual(r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"], 1)
        self.assertEqual(r.amt.calls, [])

    def test_a_second_job_wakes_one_talos_node(self):
        r = AlwaysOnRig()
        r.put_talos_to_sleep()
        r.amt.calls.clear()
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.job(["self-hosted", "linux", "spritz-ci"], age_seconds=90)
        r.kube.add_runner_pod(VM, "zagreus-ci")
        r.tick(advance=60)
        r.tick(advance=60)
        powered = [n for n, s in r.amt.calls if s == w.POWER_ON]
        self.assertEqual(powered, ["zag-amd64-01"])


class FloorWithAlwaysOn(unittest.TestCase):
    """The pilot's point (9 Nov 2026): zagreus-ci's floor equals the always-on
    slot plus the room of 01+02, so a burst that fits there changes no cap,
    and so restarts no listener, across the wake."""

    ASLEEP = {"ready": False, "asleep": True}

    def setUp(self):
        self.cfg = live_config()
        if not any(n.get("alwaysOn") for n in self.cfg["nodes"]):
            self.skipTest("no always-on node configured")
        self.ceilings = {"zagreus-docker-amd64": 1, "zagreus-ci": 8, "zagreus-ci-x64": 2,
                         "zagreus-simplytics-ci": 2}
        self.floor = next(s["floor"] for s in self.cfg["sets"] if s["name"] == "zagreus-ci")

    def caps(self, **st):
        node_states = states(**st)
        room = w.compute_caps(self.cfg, node_states)
        return room, w.floor_caps(self.cfg, room, self.ceilings, self.ceilings, node_states)

    def test_the_floor_is_the_always_on_slot_plus_the_room_of_01_and_02(self):
        vm = next(n for n in self.cfg["nodes"] if n.get("alwaysOn"))
        talos = {n["name"]: n for n in self.cfg["nodes"]}
        want = sum(w.node_slots(self.cfg, n, "zagreus-ci")
                   for n in (vm, talos["zag-amd64-01"], talos["zag-amd64-02"]))
        self.assertEqual(self.floor, want)

    def test_the_cap_does_not_change_from_asleep_to_01_and_02_awake(self):
        vm = next(n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn"))
        _, asleep = self.caps(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                                 "zag-amd64-03": self.ASLEEP, vm: {}})
        _, first = self.caps(**{"zag-amd64-01": {}, "zag-amd64-02": self.ASLEEP,
                                "zag-amd64-03": self.ASLEEP, vm: {}})
        _, both = self.caps(**{"zag-amd64-01": {}, "zag-amd64-02": {},
                               "zag-amd64-03": self.ASLEEP, vm: {}})
        self.assertEqual(asleep["zagreus-ci"], self.floor)
        self.assertEqual(first["zagreus-ci"], self.floor)
        self.assertEqual(both["zagreus-ci"], self.floor)

    def test_claims_beyond_the_always_on_slot_wake_01_and_02_together(self):
        vm = next(n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn"))
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       "zag-amd64-03": self.ASLEEP, vm: {}})
        room = w.compute_caps(self.cfg, st)
        # A full first wave at the floor: one runner on the VM, the rest
        # claimed with no node.
        claims = w.floor_claims(self.cfg, {"zagreus-ci": self.floor}, {"zagreus-ci": self.floor - 1}, room)
        self.assertEqual(claims, {"zagreus-ci": self.floor - 1})
        self.assertEqual(sorted(w.choose_wakes(self.cfg, claims, st, {}, T0)), ["zag-amd64-01", "zag-amd64-02"])

    def test_demand_beyond_the_floor_wakes_03_and_lifts_the_cap_to_the_ceiling(self):
        vm = next(n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn"))
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": self.ASLEEP, vm: {}})
        self.assertEqual(w.choose_wakes(self.cfg, {"zagreus-ci": 1}, st, {}, T0), ["zag-amd64-03"])
        _, caps = self.caps(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": {}, vm: {}})
        self.assertEqual(caps["zagreus-ci"], 8)

    def test_the_floor_wins_over_the_pools_fair_share_squeeze(self):
        # Review of 710b204, kept on purpose: with spritz holding the whole
        # pool and Simplytics queued, fair share would squeeze zagreus-ci to
        # 5. The floor lifts it back to 6, because the squeeze is itself a
        # cap change and a listener restart. Simplytics is not starved: its
        # own floor claims one job, and its Pending pod is older than any
        # spritz pod created after it, so it takes the next free slot
        # (README, "Simplytics' floor").
        vm = next(n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn"))
        node_states = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": self.ASLEEP, vm: {}})
        room = w.compute_caps(self.cfg, node_states, want={"zagreus-ci": 10, "zagreus-simplytics-ci": 1},
                              ceilings=self.ceilings)
        # Positive control: fair share does squeeze spritz below the floor.
        self.assertLess(room["zagreus-ci"], self.floor, room)
        self.assertGreaterEqual(room["zagreus-simplytics-ci"], 1, room)
        caps = w.floor_caps(self.cfg, room, self.ceilings, self.ceilings, node_states)
        self.assertEqual(caps["zagreus-ci"], self.floor)
        self.assertGreaterEqual(caps["zagreus-simplytics-ci"], 1)

    def test_with_the_vm_down_the_floor_still_holds_and_the_talos_nodes_take_it(self):
        vm = next(n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn"))
        room, caps = self.caps(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                                  "zag-amd64-03": self.ASLEEP, vm: {"ready": False}})
        self.assertEqual(room["zagreus-ci"], 0)
        self.assertEqual(caps["zagreus-ci"], self.floor)


class LiveBurst(unittest.TestCase):
    """The deployed waker.conf end to end, with the always-on node Ready and
    the three Talos nodes asleep."""

    def rig(self):
        from test_sharing import LiveRig
        r = LiveRig(always_on=True)
        if not r.always_on:
            self.skipTest("no always-on node configured")
        r.put_all_to_sleep()
        return r

    def ci_cap(self, r):
        return r.kube.ars[("arc-runners", "zagreus-ci")]["spec"]["maxRunners"]

    def ci_cap_writes(self, r, since):
        return [x for x in r.kube.writes[since:]
                if x[0] != "AMT" and "zagreus-ci" in str(x[1]) and "maxRunners" in str(x[2])
                and "x64" not in str(x[1])]

    def test_asleep_the_cap_sits_at_the_floor(self):
        r = self.rig()
        self.assertEqual(self.ci_cap(r), 6)
        self.assertNotIn(r.always_on[0], r.kube.asleep())

    def test_one_job_runs_on_the_vm_with_no_wake(self):
        r = self.rig()
        r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), [])

    def test_a_first_wave_wakes_01_and_02_and_changes_no_cap(self):
        r = self.rig()
        mark = len(r.kube.writes)
        for _ in range(6):
            r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        # Five beyond the VM's one: 02 covers three, then 01 two.
        self.assertEqual(r.woken(), ["zag-amd64-02", "zag-amd64-01"])
        r.tick(advance=120)
        self.assertEqual(self.ci_cap(r), 6)
        self.assertEqual(self.ci_cap_writes(r, mark), [])


class X64Floor(unittest.TestCase):
    """10 Nov 2026, the second proof burst: zagreus-ci's floor took the first
    wave with no cap change, but x64 had none. Its cap went 0 -> 1 after the
    wake and 1 -> 2 a minute later, two listener restarts, and the second
    job waited 549 s (305 s of it for GitHub's re-offer). x64 now holds a
    floor of 2, pinned to 01 and 02, its only slot nodes."""

    def rig(self):
        from test_sharing import LiveRig
        r = LiveRig(always_on=True)
        if not r.always_on:
            self.skipTest("no always-on node configured")
        r.put_all_to_sleep()
        return r

    def cap(self, r, name):
        return r.kube.ars[("arc-runners", name)]["spec"]["maxRunners"]

    def cap_writes(self, r, name, since):
        return [b["spec"]["maxRunners"] for v, p, b in r.kube.writes[since:]
                if v == "PATCH" and p.endswith("/autoscalingrunnersets/" + name)]

    def test_asleep_x64_sits_at_its_floor(self):
        r = self.rig()
        self.assertEqual(self.cap(r, "zagreus-ci-x64"), 2)

    def test_the_proof_wave_changes_neither_cap_across_the_wake(self):
        from test_sharing import X64
        r = self.rig()
        mark = len(r.kube.writes)
        for _ in range(6):
            r.spritz_job(age=5)
        for _ in range(2):
            r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-02", "zag-amd64-01"])
        self.assertEqual(self.cap_writes(r, "zagreus-ci-x64", mark), [])
        self.assertEqual(self.cap_writes(r, "zagreus-ci", mark), [])

    def test_the_x64_floor_holds_with_03_awake(self):
        # 03 has no x64 slot. Unpinned, the floor would stand down whenever 03
        # is up and the cap would follow 01/02's room (the builder's 4 Nov
        # flapping). Pinned, it holds.
        cfg = live_config()
        st = states(**{"zag-amd64-01": {"ready": False, "asleep": True},
                       "zag-amd64-02": {"ready": False, "asleep": True},
                       "zag-amd64-03": {}, VM: {}})
        ceilings = {"zagreus-docker-amd64": 1, "zagreus-ci": 8, "zagreus-ci-x64": 2, "zagreus-simplytics-ci": 3}
        room = w.compute_caps(cfg, st)
        self.assertEqual(room["zagreus-ci-x64"], 0)
        self.assertEqual(w.floor_caps(cfg, room, ceilings, ceilings, st)["zagreus-ci-x64"], 2)


class SharedVmSlot(unittest.TestCase):
    """11 Nov 2026 (Alex: one zagreus VM per pve node replaces the classic
    CTs): x64 jobs may run on the always-on VM too. It has one heavy slot,
    which either set may take, never both: "slotsShared" on the node."""

    def cfg(self):
        cfg = always_on_config()
        vm = cfg["nodes"][-1]
        vm["slots"] = {"zagreus-ci": 1, "zagreus-ci-x64": 1}
        vm["slotsShared"] = True
        return cfg

    ASLEEP = {"ready": False, "asleep": True}

    def test_slots_shared_must_be_a_boolean(self):
        cfg = self.cfg()
        cfg["nodes"][-1]["slotsShared"] = "yes"
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_an_idle_shared_slot_is_offered_once_not_to_each_set(self):
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP, VM: {}})
        caps = w.compute_caps(self.cfg(), st)
        self.assertEqual(caps["zagreus-ci"] + caps["zagreus-ci-x64"], 1, caps)

    def test_an_x64_runner_on_the_vm_leaves_ci_nothing_there(self):
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       VM: {"own": {"zagreus-ci-x64": 1}}})
        caps = w.compute_caps(self.cfg(), st)
        self.assertEqual((caps["zagreus-ci"], caps["zagreus-ci-x64"]), (0, 1), caps)

    def test_a_ci_runner_on_the_vm_leaves_x64_nothing_there(self):
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       VM: {"own": {"zagreus-ci": 1}}})
        caps = w.compute_caps(self.cfg(), st)
        self.assertEqual((caps["zagreus-ci"], caps["zagreus-ci-x64"]), (1, 0), caps)

    def test_an_unshared_node_still_offers_each_set_its_own_slots(self):
        # Positive control: without the flag the same node offers both.
        cfg = self.cfg()
        cfg["nodes"][-1].pop("slotsShared")
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP, VM: {}})
        caps = w.compute_caps(cfg, st)
        self.assertEqual(caps["zagreus-ci"] + caps["zagreus-ci-x64"], 2, caps)

    def test_the_live_vm_shares_its_slot_between_ci_and_x64(self):
        vm = next(n for n in live_config()["nodes"] if n.get("alwaysOn"))
        self.assertTrue(vm.get("slotsShared"))
        self.assertEqual(vm["slots"], {"zagreus-ci": 1, "zagreus-ci-x64": 1})

    def live_rig(self):
        from test_sharing import LiveRig
        r = LiveRig(always_on=True)
        if not r.always_on:
            self.skipTest("no always-on node configured")
        r.put_all_to_sleep()
        return r

    def test_one_x64_job_takes_the_idle_vm_slot_with_no_wake(self):
        # Review of fb8f86c: the idle slot went to zagreus-ci on paper
        # (waker.conf order), so one x64 job counted as unmet and woke 01,
        # though its pod then bound to the VM anyway.
        from test_sharing import X64
        r = self.live_rig()
        r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), [])

    def test_one_ci_job_takes_the_idle_vm_slot_with_no_wake(self):
        r = self.live_rig()
        r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), [])

    def test_relief_never_lends_a_slot_reserved_for_an_unbound_claim(self):
        # Codex on 5bc56d1 (P2), at the function level: in the live rig the
        # ci claim's own floor wake masks it. compute_caps places the
        # claimed, unbound ci runner on the VM on paper; that slot is taken.
        cfg = live_config()
        vm = next(n["name"] for n in cfg["nodes"] if n.get("alwaysOn"))
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       "zag-amd64-03": self.ASLEEP, vm: {"free": (3.6, 6.6)}})
        reqs = {"zagreus-docker-amd64": (1.25, 3.75), "zagreus-ci": (1.5, 6.0),
                "zagreus-ci-x64": (1.5, 6.0), "zagreus-simplytics-ci": (1.5, 6.0)}
        per_node, per_node_own = {}, {}
        room = w.compute_caps(cfg, st, reqs, unplaced={"zagreus-ci": 1},
                              per_node=per_node, per_node_own=per_node_own)
        self.assertEqual(per_node_own[vm].get("zagreus-ci"), 1)  # placed on paper
        demand = {"zagreus-ci": 1, "zagreus-ci-x64": 1}
        runners = {"zagreus-ci": 1, "zagreus-ci-x64": 0}
        unstarted = {"zagreus-ci": 1, "zagreus-ci-x64": 0}
        spare = {"zagreus-ci": room["zagreus-ci"], "zagreus-ci-x64": room["zagreus-ci-x64"]}
        unmet = w.unmet_by_set(demand, spare, runners, unstarted)
        self.assertEqual(unmet["zagreus-ci-x64"], 1)
        out = w.shared_slot_relief(cfg, unmet, demand, spare, {"zagreus-ci": 6, "zagreus-ci-x64": 2},
                                   runners, unstarted, per_node, st, per_node_own=per_node_own)
        self.assertEqual(out["zagreus-ci-x64"], 1)

    def test_a_claimed_unbound_ci_runner_keeps_the_slot_from_an_x64_job(self):
        # Codex on 5bc56d1 (P2): a ci runner that has claimed its job but
        # whose pod is not bound yet holds the VM slot on paper. The relief
        # read only bound pods, saw the slot free, and lent it to an x64 job:
        # no wake, though the VM fits only one of them.
        from test_sharing import X64
        r = self.live_rig()
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-ci"}},
                                          "status": {}})  # claimed, job not handed over yet
        r.spritz_job(age=5)
        r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(len(r.woken()), 1, r.woken())

    def test_a_claimed_unbound_simplytics_runner_keeps_the_slot_too(self):
        from test_sharing import X64
        r = self.live_rig()
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: "zagreus-simplytics-ci"}},
                                          "status": {}})
        r.sim_job(age=5)
        r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(len(r.woken()), 1, r.woken())

    def test_a_third_x64_job_gets_the_idle_vm_slot_when_01_and_02_are_busy(self):
        # Codex on 4c84909 (P2): with x64 running one job on each i7 and the
        # VM idle, compute_caps gave the VM's slot to ci (waker.conf order)
        # though ci wanted nothing, x64's cap stayed at 2, and a third x64
        # job stayed queued beside the empty VM. The slot now goes to the
        # set whose demand is unmet.
        from test_sharing import LiveRig, X64
        r = LiveRig(always_on=True)
        if not r.always_on:
            self.skipTest("no always-on node configured")
        r.tick()
        for i, node in enumerate(("zag-amd64-01", "zag-amd64-02")):
            r.kube.add_runner_pod(node, X64, name=f"x64-busy-{i}")
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: X64}},
                                              "status": {"jobRequestId": 950 + i}})
        r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=30)
        r.tick(advance=30)
        self.assertEqual(r.kube.ars[("arc-runners", X64)]["spec"]["maxRunners"], 3)

    def test_with_no_demand_the_idle_shared_slot_still_goes_to_ci(self):
        # Unchanged when nobody is waiting: waker.conf order, as before.
        cfg = live_config()
        vm = next(n["name"] for n in cfg["nodes"] if n.get("alwaysOn"))
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       "zag-amd64-03": self.ASLEEP, vm: {}})
        caps = w.compute_caps(cfg, st, want={"zagreus-ci": 0, "zagreus-ci-x64": 0, "zagreus-simplytics-ci": 0})
        self.assertEqual((caps["zagreus-ci"], caps["zagreus-ci-x64"]), (1, 0), caps)

    def test_x64_demand_takes_the_idle_shared_slot_from_an_idle_ci(self):
        cfg = live_config()
        vm = next(n["name"] for n in cfg["nodes"] if n.get("alwaysOn"))
        st = states(**{"zag-amd64-01": self.ASLEEP, "zag-amd64-02": self.ASLEEP,
                       "zag-amd64-03": self.ASLEEP, vm: {}})
        caps = w.compute_caps(cfg, st, want={"zagreus-ci": 0, "zagreus-ci-x64": 1, "zagreus-simplytics-ci": 0})
        self.assertEqual((caps["zagreus-ci"], caps["zagreus-ci-x64"]), (0, 1), caps)

    def test_x64_is_in_the_share_figures_because_it_draws_on_a_shared_node(self):
        need = w.share_need(live_config(), {"zagreus-ci-x64": 2, "zagreus-docker-amd64": 1}, {})
        self.assertEqual(need.get("zagreus-ci-x64"), 2)
        self.assertNotIn("zagreus-docker-amd64", need)

    def test_one_job_of_each_still_wakes_a_node_for_the_second(self):
        # Positive control: the slot is lent, not doubled.
        from test_sharing import X64
        r = self.live_rig()
        r.spritz_job(age=5)
        r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(len(r.woken()), 1, r.woken())

    def test_the_live_proof_wave_with_a_shared_slot_changes_no_cap(self):
        from test_sharing import LiveRig, X64
        r = LiveRig(always_on=True)
        if not r.always_on:
            self.skipTest("no always-on node configured")
        r.put_all_to_sleep()
        mark = len(r.kube.writes)
        for _ in range(6):
            r.spritz_job(age=5)
        for _ in range(2):
            r.spritz_job(age=5, labels=(X64,))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-02", "zag-amd64-01"])
        writes = [p for v, p, b in r.kube.writes[mark:] if v == "PATCH" and "/autoscalingrunnersets/" in p]
        self.assertEqual(writes, [])


class LiveFiles(unittest.TestCase):
    """waker.conf, the runner templates and the RBAC have to agree about the
    always-on node."""

    RUNNERS = os.path.join(WAKER_DIR, "..", "runners")

    def always_on_nodes(self):
        return [n["name"] for n in live_config()["nodes"] if n.get("alwaysOn")]

    def test_the_waker_tolerates_the_always_on_taint(self):
        if not self.always_on_nodes():
            self.skipTest("no always-on node configured")
        self.assertIn(TAINT, live_config()["toleratedTaintKeys"])

    def test_every_template_with_its_slots_tolerates_the_taint(self):
        # The waker offers an always-on node's slots to every set that draws
        # on them (a shared pool included): each such set's pods must
        # tolerate the taint, or the offer strands its jobs.
        cfg = live_config()
        files = {"zagreus-ci": "spritz/ci.yaml", "zagreus-ci-x64": "spritz/ci-x64.yaml",
                 "zagreus-docker-amd64": "spritz/docker-amd64.yaml",
                 "zagreus-simplytics-ci": "simplytics/ci.yaml"}
        for n in cfg["nodes"]:
            if not n.get("alwaysOn"):
                continue
            for s in cfg["sets"]:
                if w.node_slots(cfg, n, s["name"]) <= 0:
                    continue
                with open(os.path.join(self.RUNNERS, files[s["name"]])) as f:
                    text = f.read()
                # In the runner pod template, not the listener's: the part
                # after "template:" at the values' indent.
                runner = text.split("\n        template:\n", 1)
                self.assertEqual(len(runner), 2, s["name"])
                self.assertRegex(runner[1], r"- key: " + re.escape(TAINT), s["name"])


if __name__ == "__main__":
    unittest.main()
