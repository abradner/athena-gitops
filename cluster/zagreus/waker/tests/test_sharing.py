"""Sharing the amd64 CI slots between spritz and Simplytics (README,
"Sharing"). Standard library only, run with the rest:

    python3 -m unittest discover -s cluster/zagreus/waker/tests -v

The rule under test: a pool's free slots go one at a time to the set with
queued jobs that holds the fewest slots now, a tie to the set whose oldest
queued job has waited longest; slots no queue asks for go to the sets in
waker.conf order, so with Simplytics idle spritz keeps what it had.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import waker as w  # noqa: E402
from test_waker import (  # noqa: E402
    ALLOCATABLE, CEILINGS, SET_LABELS, iso, SHARED_CEILINGS, TEMPLATES, UTC, WAKER_DIR,
    Clock, FakeAMT, FakeGitHub, FakeKube, FakeTalos, T0, live_config, production_config,
    shared_config, states)

CI, X64, BUILDER, SIM = "zagreus-ci", "zagreus-ci-x64", "zagreus-docker-amd64", "zagreus-simplytics-ci"
ALL_CEILINGS = {**CEILINGS, **SHARED_CEILINGS}
REQS = {name: w.pod_requests(TEMPLATES[name]) for name in TEMPLATES}


def room_states(**kw):
    """Nodes with their free room as observe() reads it: allocatable less
    the requests of the runners already there (the coredns pods FakeKube
    puts on 01 are left out: these are pure decisions)."""
    st = states(**kw)
    for name, v in st.items():
        v.setdefault("own", {})
        if name in ALLOCATABLE and "free" not in kw[name]:
            a = ALLOCATABLE[name]
            v["free"] = [w.parse_qty(a["cpu"]) - sum(REQS[s][0] * k for s, k in v["own"].items()),
                         w.parse_qty(a["memory"]) - sum(REQS[s][1] * k for s, k in v["own"].items())]
    return st


def both_awake(**own):
    return room_states(**{"zag-amd64-01": {"own": dict(own.get("zag_amd64_01", {}))},
                          "zag-amd64-02": {"own": dict(own.get("zag_amd64_02", {}))}})


class Config(unittest.TestCase):
    def test_live_config_shares_zagreus_cis_slots_with_simplytics(self):
        cfg = live_config()
        sim = next(s for s in cfg["sets"] if s["name"] == SIM)
        self.assertEqual(sim["sharesSlotsOf"], CI)
        self.assertEqual(sim["org"], "Simplytics")
        self.assertEqual(sim["github"], "simplytics")
        self.assertNotIn("repo", sim)
        self.assertEqual(w.shared_pools(cfg), {CI: [CI, SIM]})
        # Spritz's other sets share nothing.
        for name in (X64, BUILDER):
            self.assertEqual(w.slot_key(next(s for s in cfg["sets"] if s["name"] == name)), name)

    def test_a_set_names_exactly_one_queue(self):
        cfg = shared_config()
        sim = next(s for s in cfg["sets"] if s["name"] == SIM)
        sim["repo"] = "Simplytics/simplytics"
        with self.assertRaises(ValueError):
            w.validate_config(cfg)
        del sim["repo"], sim["org"]
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_sharing_an_unknown_or_sharing_set_is_refused(self):
        for target in ("zagreus-nope", SIM):
            cfg = shared_config()
            next(s for s in cfg["sets"] if s["name"] == SIM)["sharesSlotsOf"] = target
            with self.assertRaises(ValueError, msg=target):
                w.validate_config(cfg)

    def test_slots_keyed_by_a_sharing_set_are_refused(self):
        cfg = shared_config()
        cfg["nodes"][0]["slots"][SIM] = 1
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_an_unknown_credential_is_refused(self):
        cfg = shared_config()
        next(s for s in cfg["sets"] if s["name"] == SIM)["github"] = "nobody"
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_a_pin_in_a_shared_pool_is_refused(self):
        # A pin is a promise about where one set's pods land; a pool's
        # slots may be handed to any of its sets.
        cfg = shared_config()
        next(s for s in cfg["sets"] if s["name"] == SIM)["pinnedToSlots"] = True
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_live_config_gives_simplytics_a_floor(self):
        # 5 Nov: a Simplytics job queued while its set was at 0 waited past
        # the wake and two raises, held by GitHub (README, "Floor").
        sim = next(s for s in live_config()["sets"] if s["name"] == SIM)
        self.assertEqual(sim.get("floor"), 1)
        w.validate_config(live_config())


class Owns(unittest.TestCase):
    def test_an_org_set_owns_every_repo_of_its_org_and_no_other(self):
        sim = {"name": SIM, "org": "Simplytics"}
        self.assertTrue(w.owns(sim, {"repo": "Simplytics/simplytics"}))
        self.assertTrue(w.owns(sim, {"repo": "simplytics/other"}))
        self.assertFalse(w.owns(sim, {"repo": "abradner/spritz"}))
        self.assertFalse(w.owns(sim, {"repo": "SimplyticsX/simplytics"}))

    def test_simplytics_jobs_are_never_spritz_demand_and_back(self):
        cfg = shared_config()
        now = dt.datetime(2026, 11, 5, tzinfo=UTC)
        jobs = [{"repo": "Simplytics/simplytics", "labels": [CI], "created_at": now - dt.timedelta(seconds=90)},
                {"repo": "abradner/spritz", "labels": [SIM], "created_at": now - dt.timedelta(seconds=90)},
                {"repo": "Simplytics/simplytics", "labels": [SIM], "created_at": now}]
        d = w.demand_by_set(cfg, jobs, SET_LABELS, now)
        self.assertEqual(d, {BUILDER: 0, CI: 0, X64: 0, SIM: 1})


class ShareRule(unittest.TestCase):
    """compute_caps with each pool set's queued need."""

    def caps(self, st, want, turn=None, recent=None, cfg=None, unplaced=None):
        """want/recent: each pool set's total, its runners included."""
        return w.compute_caps(cfg or shared_config(), st, REQS, unplaced, want=want, turn=turn,
                              recent=recent, ceilings=ALL_CEILINGS)

    def test_simplytics_idle_leaves_spritz_exactly_as_without_it(self):
        spritz = w.compute_caps(production_config(), both_awake(), REQS)
        for need in (None, {}, {CI: 3}, {CI: 1, SIM: 0}):
            got = self.caps(both_awake(), need)
            self.assertEqual({k: v for k, v in got.items() if k != SIM}, spritz, need)
            self.assertEqual(got[SIM], 0, need)

    def test_a_simplytics_job_takes_a_slot_spritz_was_only_holding_spare(self):
        got = self.caps(both_awake(), {SIM: 1})
        self.assertEqual(got[SIM], 1)
        self.assertEqual(got[CI], 2)  # the pool is three slots on the frozen nodes
        self.assertEqual((got[X64], got[BUILDER]), (1, 1))  # outside the pool: untouched

    def test_two_queues_at_once_split_the_free_slots(self):
        # Both queues deep: the three free slots go one each in turn, the
        # older queue first, so neither gets nothing.
        got = self.caps(both_awake(), {CI: 5, SIM: 5}, turn={CI: 100, SIM: 50})
        self.assertEqual((got[CI], got[SIM]), (2, 1))
        got = self.caps(both_awake(), {CI: 5, SIM: 5}, turn={CI: 50, SIM: 100})
        self.assertEqual((got[CI], got[SIM]), (1, 2))

    def test_a_busy_queue_never_starves_the_other_to_zero(self):
        # spritz holds two running slots; one slot frees. Simplytics, at
        # none, gets it, even though spritz's backlog is older.
        st = both_awake(zag_amd64_02={CI: 2})
        got = self.caps(st, {CI: 9, SIM: 1}, turn={CI: 900, SIM: 10})
        self.assertEqual((got[CI], got[SIM]), (2, 1))

    def test_the_fewest_held_goes_first_then_the_oldest_queue(self):
        # Simplytics holds one and wants one more; spritz holds none and
        # wants one: spritz (fewest held) is served first, though
        # Simplytics' queue is older.
        st = both_awake(zag_amd64_02={SIM: 1})
        got = self.caps(st, {CI: 1, SIM: 2}, turn={CI: 1, SIM: 999})
        self.assertEqual((got[CI], got[SIM]), (1, 2))

    def test_simplytics_stops_at_its_ceiling_and_spritz_gets_the_rest(self):
        got = self.caps(both_awake(), {SIM: 9})
        self.assertEqual(got[SIM], SHARED_CEILINGS[SIM])
        self.assertEqual(got[CI], 3 - SHARED_CEILINGS[SIM])

    def test_a_busy_set_is_held_to_its_share_so_a_freed_slot_is_not_reclaimed(self):
        # The refill race (independent review of a2b2e86): spritz runs all
        # three pool slots with a deep backlog. ARC hands each slot a spritz
        # job frees to the next spritz job within seconds, so Simplytics
        # would never see one free. spritz's cap drops to its fair share
        # (2), below the three it runs; Simplytics gets nothing yet (no
        # room), and is not over-offered.
        st = both_awake(zag_amd64_01={CI: 1}, zag_amd64_02={CI: 2})
        got = self.caps(st, {CI: 9, SIM: 1}, turn={CI: 900, SIM: 10})
        self.assertEqual((got[CI], got[SIM]), (2, 0))
        # A spritz job ends: its slot is free at the next pass, and goes to
        # Simplytics, not back to spritz.
        st = both_awake(zag_amd64_01={CI: 1}, zag_amd64_02={CI: 1})
        got = self.caps(st, {CI: 9, SIM: 1}, turn={CI: 900, SIM: 10})
        self.assertEqual((got[CI], got[SIM]), (2, 1))

    def test_the_cap_rule_holds_both_ways(self):
        # Simplytics runs two (its ceiling here); spritz queues five and
        # holds one. The fair share of three is two and one: Simplytics
        # is held to one, its runners keep running.
        st = both_awake(zag_amd64_02={SIM: 2})
        got = self.caps(st, {CI: 5}, turn={CI: 900})
        self.assertEqual((got[CI], got[SIM]), (1, 1))

    def test_no_one_is_held_back_while_nobody_is_short(self):
        st = both_awake(zag_amd64_01={CI: 1}, zag_amd64_02={CI: 2})
        got = self.caps(st, {CI: 9})
        self.assertEqual((got[CI], got[SIM]), (3, 0))

    def test_a_claimed_unbound_runner_holds_its_pool_slot(self):
        # Simplytics claimed a job whose pod has not bound yet: placed on
        # paper, it holds a pool slot, and spritz is offered the rest.
        got = self.caps(both_awake(), {CI: 9}, unplaced={SIM: 1}, turn={CI: 5})
        self.assertEqual((got[CI], got[SIM]), (2, 1))

    def test_recent_use_holds_a_slot_but_never_against_queued_jobs(self):
        got = self.caps(both_awake(), {}, recent={SIM: 1})
        self.assertEqual((got[CI], got[SIM]), (2, 1))
        got = self.caps(both_awake(), {CI: 3}, recent={SIM: 1}, turn={CI: 5})
        self.assertEqual((got[CI], got[SIM]), (3, 0))

    def test_slot_mode_shares_the_pool_too(self):
        # roomCaps false: no room counted, so the pool's slots are the only
        # bound. Its two sets together never exceed them.
        st = states(**{"zag-amd64-01": {"own": {}}, "zag-amd64-02": {"own": {SIM: 1}}})
        got = w.compute_caps(shared_config(), st, None, want={CI: 9, SIM: 1}, ceilings=ALL_CEILINGS)
        self.assertEqual(got[CI] + got[SIM], 3)
        self.assertEqual(got[SIM], 1)
        # Spritz alone in slot mode: the configured slots, as before.
        self.assertEqual(w.compute_caps(production_config(), st, None),
                         {BUILDER: 1, CI: 3, X64: 1})

    def test_never_more_offered_than_the_awake_nodes_run(self):
        # The README's invariant, under the sharing rule: whatever the
        # queues and runners, the two pool sets' caps on the awake nodes
        # never exceed the pool's slots there, and every node's requests fit.
        cfg = shared_config()
        rnd = random.Random(20261105)
        for _ in range(500):
            awake = {n: rnd.random() < 0.7 for n in ("zag-amd64-01", "zag-amd64-02")}
            kw = {}
            for n, up in awake.items():
                own = {s: rnd.randint(0, 2) for s in (CI, SIM)}
                kw[n] = {"own": own} if up else {"ready": False, "asleep": True}
            st = room_states(**kw)
            need = {CI: rnd.randint(0, 4), SIM: rnd.randint(0, 4)}
            per_node = {}
            got = w.compute_caps(cfg, st, REQS, want=need, turn={CI: rnd.random(), SIM: rnd.random()},
                                 recent={CI: rnd.randint(0, 4), SIM: rnd.randint(0, 4)},
                                 ceilings=ALL_CEILINGS, per_node=per_node)
            for node, offered in per_node.items():
                # Never more than the pool's slots there, or than the
                # runners already running there (never taken back).
                slots = next(n for n in cfg["nodes"] if n["name"] == node)["slots"].get(CI, 0)
                held = sum(st[node]["own"].values())
                self.assertLessEqual(offered[CI] + offered[SIM], max(slots, held), (kw, need, offered))
                # What is offered beyond the runners already there fits the
                # room they leave (none, if they already overfill the node).
                own = st[node]["own"]
                extra = {s: k - own.get(s, 0) for s, k in offered.items()}
                self.assertTrue(all(k >= 0 or s in own for s, k in extra.items()), (node, offered))
                cpu = sum(REQS[s][0] * max(0, k) for s, k in extra.items())
                mem = sum(REQS[s][1] * max(0, k) for s, k in extra.items())
                free = st[node]["free"]
                self.assertLessEqual(cpu, max(0.0, free[0]) + 1e-9, (node, offered, own))
                self.assertLessEqual(mem, max(0.0, free[1]), (node, offered, own))


class ShareNeed(unittest.TestCase):
    def test_need_is_queued_jobs_not_yet_claimed_for_pool_sets_only(self):
        cfg = shared_config()
        need = w.share_need(cfg, {BUILDER: 2, CI: 3, X64: 1, SIM: 2}, {CI: 1, SIM: 5})
        self.assertEqual(need, {CI: 2, SIM: 0})

    def test_spritz_alone_has_no_pool(self):
        self.assertEqual(w.share_need(production_config(), {CI: 3}, {}), {})


class SharedRig:
    """A waker on the shared config: spritz's queue through the default
    token, Simplytics' org queue through its own credential."""

    def __init__(self, mode="full", floor=True, **over):
        """floor False: Simplytics' set without its floor, for the tests of
        the sharing itself (the floor holds its cap at 1 whatever the
        shares say)."""
        self.cfg = shared_config()
        self.cfg.update(mode=mode, **over)
        if not floor:
            next(s for s in self.cfg["sets"] if s["name"] == SIM).pop("floor", None)
        self.clock = Clock()
        self.kube = FakeKube(self.cfg, self.clock)
        self.spritz = FakeGitHub()
        self.simplytics = FakeGitHub()
        self.amt = FakeAMT(self.kube, self.clock)
        self.talos = FakeTalos(self.kube, self.amt)
        self.waker = w.Waker(self.cfg, self.kube, {"default": self.spritz, "simplytics": self.simplytics},
                             self.amt, self.talos, clock=self.clock, sleep=self.clock.sleep, my_node="cp1")

    def job(self, gh, repo, labels, age_seconds):
        gh.jobs.append({"repo": repo, "id": 1000 + len(self.spritz.jobs) + len(self.simplytics.jobs),
                        "name": "j", "labels": labels,
                        "created_at": dt.datetime.now(UTC) - dt.timedelta(seconds=age_seconds)})

    def spritz_job(self, age=30, labels=(CI,)):
        self.job(self.spritz, "abradner/spritz", list(labels), age)

    def sim_job(self, age=30, repo="Simplytics/simplytics"):
        self.job(self.simplytics, repo, [SIM], age)

    def tick(self, advance=0):
        self.clock.sleep(advance)
        self.waker.tick()

    def put_all_to_sleep(self):
        self.tick()
        self.tick(advance=61 * 60)
        self.tick(advance=1)
        assert self.kube.asleep() == {"zag-amd64-01", "zag-amd64-02"}, self.kube.asleep()


class Shared(unittest.TestCase):
    def test_two_repo_contention_on_awake_nodes_gives_each_a_share(self):
        r = SharedRig(mode="caps", floor=False)
        for _ in range(5):
            r.spritz_job(age=300)
        for _ in range(3):
            r.sim_job(age=120)
        pool = []
        r.kube.on_write = lambda v, p, b: pool.append(r.kube.caps()[CI] + r.kube.caps()[SIM]) \
            if "/autoscalingrunnersets/" in p else None
        r.tick()
        r.tick(advance=30)  # a slot moved from spritz is raised for Simplytics a pass later
        self.assertLessEqual(max(pool), 3, pool)  # never over the pool, even between patches
        caps = r.kube.caps()
        self.assertGreaterEqual(caps[SIM], 1, caps)
        self.assertGreaterEqual(caps[CI], 1, caps)
        self.assertLessEqual(caps[CI] + caps[SIM], 3, caps)

    def test_simplytics_idle_drives_spritz_exactly_as_alone(self):
        # The same scenario run twice, once on spritz's sets alone (the
        # deployed behaviour before this change) and once with Simplytics
        # configured but quiet: every spritz cap write, wake and sleep is
        # the same.
        from test_waker import Rig
        alone = Rig()
        shared = SharedRig()
        for rig in (alone, shared):
            rig.tick()
            rig.tick(advance=61 * 60)
            rig.tick(advance=1)
        alone.job([CI], age_seconds=30)
        shared.spritz_job(age=30)
        for rig in (alone, shared):
            for _ in range(4):
                rig.tick(advance=rig.cfg["wakeCooldownSeconds"])
            rig.tick(advance=61 * 60)

        def spritz_writes(kube):
            return [(v, p, json.dumps(b, sort_keys=True)) for v, p, b in kube.writes
                    if SIM not in p and "/namespaces/node-power/" not in p]
        self.assertEqual(spritz_writes(shared.kube), spritz_writes(alone.kube))
        self.assertEqual(shared.amt.calls, alone.amt.calls)
        self.assertEqual(shared.kube.caps()[SIM], 1)  # its floor, on top of the pool

    def test_a_simplytics_job_wakes_a_node_for_its_slot(self):
        r = SharedRig()
        r.put_all_to_sleep()
        r.sim_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)
        self.assertEqual(r.kube.caps()[SIM], 1)

    def test_simplytics_at_its_ceiling_wakes_nothing_more(self):
        r = SharedRig()
        r.tick()
        r.tick(advance=61 * 60)  # 02 sleeps
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})
        for i in range(SHARED_CEILINGS[SIM]):
            r.kube.add_runner_pod("zag-amd64-01", SIM, name=f"sim-{i}")
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: SIM}},
                                              "status": {"jobRequestId": 70 + i}})
        for _ in range(4):
            r.sim_job(age=5)
        r.amt.calls.clear()
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertNotIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)

    def test_reads_each_queue_through_its_own_credential(self):
        r = SharedRig(mode="caps")
        seen = []
        r.spritz.queued_jobs = lambda repo, h, now: seen.append(("default", "repo", repo)) or []
        r.simplytics.org_queued_jobs = lambda org, h, now: seen.append(("simplytics", "org", org)) or []
        r.tick()
        self.assertEqual(sorted(seen), [("default", "repo", "abradner/spritz"),
                                        ("simplytics", "org", "Simplytics")])

    def test_an_unreadable_simplytics_queue_still_lets_spritz_wake_but_nothing_sleeps(self):
        r = SharedRig()
        r.put_all_to_sleep()
        r.simplytics.fail = True
        r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)
        r.spritz.jobs.clear()
        for _ in range(5):
            r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep() & {"zag-amd64-01"}, set())  # never slept blind

    def test_a_share_moved_after_demand_does_not_hasten_a_first_pass_lowering(self):
        # First pass after a start with 02's Lease unreadable: lowering
        # waits a pass (README, "Caps go down"). The second apply that moves
        # a slot to Simplytics must not count as that next pass.
        r = SharedRig(mode="caps")
        r.kube.ars[("arc-runners", CI)]["spec"]["maxRunners"] = 3
        r.kube.lease_fail.add("zag-amd64-02")
        r.sim_job(age=5)
        r.tick()
        self.assertEqual(r.kube.caps()[CI], 3)
        r.tick(advance=30)
        self.assertLess(r.kube.caps()[CI], 3)

    def test_caps_follow_the_nodes_before_github_is_read(self):
        # A slow or failing GitHub must not delay a lower cap for a node
        # that has gone: the caps are applied before the queue is read.
        r = SharedRig(mode="caps")
        r.tick()
        order = []
        r.kube.on_write = lambda v, p, b: order.append("cap") if "/autoscalingrunnersets/" in p else None
        r.spritz.queued_jobs = lambda *a: order.append("github") or []
        r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = True
        r.tick(advance=30)
        self.assertEqual(order[:2], ["cap", "github"], order)

    def test_a_blind_simplytics_queue_gives_its_share_back(self):
        # Independent review of a2b2e86: the share read before the outage
        # stayed in force, squeezing spritz for as long as it lasted.
        r = SharedRig(mode="caps", floor=False)
        for _ in range(3):
            r.sim_job(age=5)
        r.tick()
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()[SIM], SHARED_CEILINGS[SIM])
        r.simplytics.fail = True
        r.tick(advance=30)
        r.tick(advance=30)  # Simplytics lowered, then spritz raised a pass later
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (3, 0))

    def test_a_blind_simplytics_queue_leaves_spritzs_queue_gauges_live(self):
        r = SharedRig(mode="caps")
        r.simplytics.fail = True
        r.spritz_job(age=30)
        r.tick()
        text = r.kube.cms[("node-power", "waker-metrics")]["data"]["waker.prom"]
        self.assertIn('zagreus_runner_queued_jobs{set="zagreus-ci"} 1', text)
        self.assertNotIn(SIM, text)  # absent, never a false 0

    def test_a_slot_moving_between_sets_is_lowered_before_it_is_raised(self):
        # Simplytics' recently used slot goes back to spritz for a queued
        # job: Simplytics is lowered first, so the two never hold it at once.
        r = SharedRig(mode="caps", floor=False)
        r.sim_job(age=5)
        r.tick()
        r.simplytics.jobs.clear()
        r.tick(advance=30)
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (2, 1))
        r.kube.writes.clear()
        r.spritz_job(age=5)
        for _ in range(3):
            r.spritz_job(age=5)
        r.tick(advance=30)
        patches = [p.rsplit("/", 1)[1] for v, p, b in r.kube.writes if "/autoscalingrunnersets/" in p]
        # The donor only, this pass: its listener keeps its old cap until
        # ARC restarts it (Codex on #345). The raise follows a pass later,
        # once claims are recounted.
        self.assertEqual(patches, [SIM])
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (2, 0))
        r.tick(advance=30)
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (3, 0))

    def test_a_pool_lowered_by_the_nodes_is_not_raised_by_the_share_in_the_same_pass(self):
        # Codex on #345 at 16f0f097: both queues busy, caps 2/0; 01 cordoned.
        # The first apply lowered spritz 2 -> 1, the second (shares) raised
        # Simplytics 0 -> 1 in the same pass, before spritz's listener had
        # restarted.
        r = SharedRig(mode="caps")
        for _ in range(5):
            r.spritz_job(age=300)
        for _ in range(3):
            r.sim_job(age=120)
        r.tick()
        before = r.kube.caps()
        r.kube.writes.clear()
        r.kube.nodes["zag-amd64-01"]["spec"]["unschedulable"] = True
        r.tick(advance=30)
        lowered = [p.rsplit("/", 1)[1] for v, p, b in r.kube.writes
                   if "/autoscalingrunnersets/" in p and b["spec"]["maxRunners"] < before[p.rsplit("/", 1)[1]]]
        raised = [p.rsplit("/", 1)[1] for v, p, b in r.kube.writes
                  if "/autoscalingrunnersets/" in p and b["spec"]["maxRunners"] > before[p.rsplit("/", 1)[1]]
                  and p.rsplit("/", 1)[1] in (CI, SIM)]
        self.assertTrue(lowered, before)
        self.assertEqual(raised, [], r.kube.writes)

    def test_a_raise_held_for_a_pass_wakes_no_node(self):
        # 02 asleep; 01's one pool slot is spritz's spare. A Simplytics job
        # takes it: spritz lowered now, Simplytics raised next pass. Read
        # from the held live cap (0), the job looked unmet and woke 02.
        r = SharedRig()
        r.tick()
        r.kube.add_runner_pod("zag-amd64-01", "zagreus-docker-amd64", name="keep-01")  # 01 stays up
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})
        r.amt.calls.clear()
        r.sim_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertNotIn(("zag-amd64-02", w.POWER_ON), r.amt.calls)
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()[SIM], 1)

    def test_a_held_raise_keeps_its_node_awake(self):
        # Delta review of d8dcdc6: 01 idle past idleMinutes, 02 asleep; a
        # Simplytics job takes 01's spare pool slot. Its raise is held a
        # pass, and 01 must not be put to sleep under it meanwhile.
        r = SharedRig(floor=False)
        r.tick()
        r.tick(advance=61 * 60)
        self.assertEqual(r.kube.asleep(), {"zag-amd64-02"})
        r.clock.sleep(r.cfg["idleMinutes"] * 60)  # 01 idle long enough to sleep
        r.sim_job(age=5)
        r.tick()
        self.assertNotIn("zag-amd64-01", r.kube.asleep())
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()[SIM], 1)
        self.assertNotIn("zag-amd64-01", r.kube.asleep())

    def test_observe_mode_still_reports_a_pool_sets_demand(self):
        # In observe nothing is raised, so a held raise never arrives: the
        # waker must not count it as room and hide the would-be wake.
        # Both nodes awake, so the waker would give Simplytics a slot (room
        # 1), but observe never writes it: the live cap stays 0.
        r = SharedRig(mode="observe")
        r.tick()
        r.sim_job(age=5)
        with self.assertLogs("waker", "INFO") as logs:
            r.tick(advance=30)
        self.assertTrue(any(re.search(r"unmet \{[^}]*'zagreus-simplytics-ci': 1", m) for m in logs.output),
                        logs.output)

    def test_a_runner_appearing_and_ending_moves_no_cap(self):
        # 5 Nov 13:23-13:25 (live): a Simplytics job was claimed as its run
        # was cancelled. Each pass's first apply used the last pass's
        # shares, stored relative to the runner count of that pass, so the
        # new runner flipped them: Simplytics 2 -> 1 -> 2 and spritz
        # 5 -> 4 -> 5, a listener restart on both every time.
        r = SharedRig(mode="caps", floor=False)
        for _ in range(2):
            r.sim_job(age=5)
        for _ in range(3):
            r.tick(advance=30)
        settled = r.kube.caps()
        self.assertEqual(settled[SIM], SHARED_CEILINGS[SIM])
        r.kube.writes.clear()
        # One job claimed and running; the run is cancelled, nothing queued.
        r.simplytics.jobs.clear()
        r.kube.add_runner_pod("zag-amd64-02", SIM, name="sim-run")
        r.kube.ers["arc-runners"].append({"metadata": {"name": "sim-run", "labels": {w.SCALE_SET_LABEL: SIM}},
                                          "status": {"jobRequestId": 99}})
        r.tick(advance=30)
        r.tick(advance=30)
        # It ends.
        r.kube.pods = [p for p in r.kube.pods if p["metadata"]["name"] != "sim-run"]
        r.kube.ers["arc-runners"] = [e for e in r.kube.ers["arc-runners"]
                                     if e["metadata"].get("name") != "sim-run"]
        r.tick(advance=30)
        r.tick(advance=30)
        moved = [(p.rsplit("/", 1)[1], b["spec"]["maxRunners"]) for v, p, b in r.kube.writes
                 if "/autoscalingrunnersets/" in p]
        self.assertEqual(moved, [], moved)  # all inside shareHoldSeconds: nothing moves

    def test_a_pool_set_with_no_ceiling_ever_read_reserves_nothing(self):
        r = SharedRig(mode="caps")
        r.kube.fail_get.append(r"/applications/zagreus-simplytics-ci$")
        for _ in range(3):
            r.sim_job(age=5)
        r.tick()
        # bound_caps holds Simplytics at its live 0, so its share goes to spritz.
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (3, 0))

    def test_the_simplytics_floor_holds_while_the_nodes_sleep_and_a_claim_wakes_one(self):
        r = SharedRig()
        sim = next(s for s in r.cfg["sets"] if s["name"] == SIM)
        self.assertEqual(sim.get("floor"), 1)
        r.put_all_to_sleep()
        self.assertEqual(r.kube.caps()[SIM], 1)  # never 0: no restart on the first job
        self.assertEqual(r.kube.caps()[CI], 0)
        # The floor lets GitHub hand over the job at once; its pod waits
        # for a node, and that claim wakes one.
        r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: SIM},
                                                       "creationTimestamp": iso(r.clock())}, "status": {}})
        r.sim_job(age=5)
        r.amt.calls.clear()
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertIn(("zag-amd64-01", w.POWER_ON), r.amt.calls)

    def test_the_floor_does_not_take_an_idle_slot_from_spritz(self):
        # Awake and idle, spritz keeps the whole pool, as before; the floor
        # is a claim Simplytics may make on top (README, "Floor": the floor
        # is not room).
        r = SharedRig(mode="caps")
        r.tick()
        r.tick(advance=30)
        self.assertEqual((r.kube.caps()[CI], r.kube.caps()[SIM]), (3, 1))

    def test_sticky_slot_outlives_the_job_then_returns_to_spritz(self):
        r = SharedRig(mode="caps", floor=False)
        r.sim_job(age=5)
        r.tick()
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()[SIM], 1)
        r.simplytics.jobs.clear()  # the job ran and finished
        r.tick(advance=60)
        self.assertEqual(r.kube.caps()[SIM], 1)  # held for the next job in the run
        r.tick(advance=r.cfg["shareHoldSeconds"])
        r.tick(advance=30)
        self.assertEqual(r.kube.caps()[SIM], 0)
        self.assertEqual(r.kube.caps()[CI], 3)


LIVE_ALLOCATABLE = {"zag-amd64-01": {"cpu": "7950m", "memory": "31820076Ki"},
                    "zag-amd64-02": {"cpu": "7950m", "memory": "31691056Ki"},
                    "zag-amd64-03": {"cpu": "3950m", "memory": "15193716Ki"}}


class LiveRig(SharedRig):
    """The deployed waker.conf (three nodes, live allocatable), with the
    taints the node controller and Cilium put on a sleeping node."""

    def __init__(self, mode="full", always_on=False):
        self.cfg = live_config()
        self.cfg.update(mode=mode)
        if not always_on:
            # The Talos nodes alone, as before the always-on pilot (9 Nov
            # 2026): these scenarios were written for that world, and the
            # always-on node and zagreus-ci's floor that came with it have
            # their own tests (test_always_on.py, LiveRig(always_on=True)).
            self.cfg["nodes"] = [n for n in self.cfg["nodes"] if not n.get("alwaysOn")]
            self.cfg["sets"] = [{k: v for k, v in s.items() if not (s["name"] == CI and k == "floor")}
                                for s in self.cfg["sets"]]
        self.clock = Clock()
        self.kube = FakeKube(self.cfg, self.clock)
        self.kube.node_controller_taints = True
        for name, a in LIVE_ALLOCATABLE.items():
            self.kube.nodes[name]["status"]["allocatable"] = dict(a)
        self.always_on = [n["name"] for n in self.cfg["nodes"] if n.get("alwaysOn")]
        for name in self.always_on:
            # As built (asn-infra zagreus/vm-runner): 4 vCPU, 8 GiB, its own
            # taint, and no AMT or Wake-on-LAN.
            self.kube.nodes[name]["status"]["allocatable"] = {"cpu": "3800m", "memory": "7300Mi"}
            self.kube.taint(name, "zagreus.asn.casa/always-on")
        # The ceilings as the Applications write them.
        runners = os.path.join(WAKER_DIR, "..", "runners")
        files = {CI: "spritz/ci.yaml", X64: "spritz/ci-x64.yaml", BUILDER: "spritz/docker-amd64.yaml",
                 SIM: "simplytics/ci.yaml"}
        for st in self.cfg["sets"]:
            with open(os.path.join(runners, files[st["name"]])) as f:
                ceiling = int(re.search(r"^\s+maxRunners: (\d+)$", f.read(), re.M).group(1))
            self.kube.apps[st["application"]]["spec"]["source"]["helm"]["valuesObject"]["maxRunners"] = ceiling
        self.spritz = FakeGitHub()
        self.simplytics = FakeGitHub()
        self.amt = FakeAMT(self.kube, self.clock)
        self.amt.missing |= set(self.always_on)
        self.talos = FakeTalos(self.kube, self.amt)
        self.waker = w.Waker(self.cfg, self.kube, {"default": self.spritz, "simplytics": self.simplytics},
                             self.amt, self.talos, clock=self.clock, sleep=self.clock.sleep, my_node="cp1")

    def put_all_to_sleep(self):
        self.tick()
        for _ in range(6):
            if self.kube.asleep() == set(LIVE_ALLOCATABLE):
                break
            self.tick(advance=61 * 60)
        assert self.kube.asleep() == set(LIVE_ALLOCATABLE), self.kube.asleep()
        self.amt.calls.clear()

    def woken(self):
        return [n for n, st in self.amt.calls if st == w.POWER_ON]

    def tick_without_fallback(self, test, advance):
        """One pass, failing the test if choose_wakes fell back to waking a
        node no room was shown for: that path also wakes one node, so a
        one-job test would pass on the old, roomless arithmetic."""
        with test.assertLogs("waker", "INFO") as logs:
            self.tick(advance=advance)
        test.assertFalse([m for m in logs.output if "shows room" in m], logs.output)


class WakeByDemand(unittest.TestCase):
    """Alex, 6 Nov 2026: "nodes should be only be woken by unmet demand".
    Every wake from all-asleep woke all three nodes (waker events, 4 and 5
    Nov): a sleeping node carries its cordon's unschedulable taint and
    Cilium's agent-not-ready, which the room on paper did not lift, so no
    node seemed to add room and choose_wakes took them all."""

    def test_the_fake_carries_the_live_taints(self):
        # Positive control: without these the tests below prove nothing.
        r = LiveRig()
        r.put_all_to_sleep()
        r.clock.sleep(60)  # past the API server's grace for the last node slept
        keys = {t["key"] for t in r.kube.get("/api/v1/nodes/zag-amd64-01")["spec"]["taints"]}
        self.assertTrue({"node.kubernetes.io/unschedulable", "node.cilium.io/agent-not-ready",
                         "node.kubernetes.io/unreachable"} <= keys, keys)

    def test_one_simplytics_job_wakes_one_node(self):
        r = LiveRig()
        r.put_all_to_sleep()
        r.sim_job(age=5)
        r.tick_without_fallback(self, r.cfg["wakeCooldownSeconds"])
        # Any of the three covers it; a tie goes to waker.conf order.
        self.assertEqual(r.woken(), ["zag-amd64-01"])

    def test_one_spritz_job_wakes_one_node(self):
        r = LiveRig()
        r.put_all_to_sleep()
        r.spritz_job(age=5)
        r.tick_without_fallback(self, r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-01"])

    def test_a_burst_wakes_the_fewest_nodes_that_cover_it(self):
        r = LiveRig()
        r.put_all_to_sleep()
        for _ in range(4):
            r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        # 02 first (it covers three, more than any other), then one
        # two-slot node: 01 before 03 on a tie.
        self.assertEqual(r.woken(), ["zag-amd64-02", "zag-amd64-01"])

    def test_three_jobs_wake_the_one_node_that_covers_them(self):
        r = LiveRig()
        r.put_all_to_sleep()
        for _ in range(3):
            r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-02"])

    def test_a_burst_beyond_every_node_wakes_them_all(self):
        r = LiveRig()
        r.put_all_to_sleep()
        for _ in range(9):
            r.spritz_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(sorted(r.woken()), ["zag-amd64-01", "zag-amd64-02", "zag-amd64-03"])

    def test_a_builder_job_wakes_only_its_pinned_node(self):
        r = LiveRig()
        r.put_all_to_sleep()
        r.spritz_job(age=5, labels=("zagreus-docker-amd64",))
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-01"])

    def test_a_human_cordon_still_counts_against_a_node(self):
        # Only the waker's own cordon comes off at the wake; a node a person
        # cordoned offers nothing once awake, so it is no use waking it.
        r = LiveRig()
        r.put_all_to_sleep()
        # 01 would win the tie; with a person's cordon it is passed over.
        r.kube.nodes["zag-amd64-01"]["metadata"]["annotations"].pop(w.ANNOTATION_CORDON, None)
        r.sim_job(age=5)
        r.tick(advance=r.cfg["wakeCooldownSeconds"])
        self.assertEqual(r.woken(), ["zag-amd64-02"])


class ChooseWakes(unittest.TestCase):
    def test_a_node_that_adds_no_room_is_not_woken(self):
        cfg = live_config()
        st = states(**{n: {"ready": False, "asleep": True} for n in ("zag-amd64-01", "zag-amd64-02", "zag-amd64-03")})
        room = lambda nodes: {CI: 2} if "zag-amd64-01" in nodes else {CI: 0}  # noqa: E731
        self.assertEqual(w.choose_wakes(cfg, {CI: 5}, st, {}, T0, room=room), ["zag-amd64-01"])

    def test_with_no_room_shown_anywhere_one_node_still_wakes(self):
        # A taint the waker does not know (lifted at boot) must not strand
        # the jobs: one node, the first eligible, never all of them.
        cfg = live_config()
        st = states(**{n: {"ready": False, "asleep": True} for n in ("zag-amd64-01", "zag-amd64-02", "zag-amd64-03")})
        with self.assertLogs("waker", "WARNING"):
            got = w.choose_wakes(cfg, {CI: 5}, st, {}, T0, room=lambda nodes: {})
        self.assertEqual(got, ["zag-amd64-01"])


class AppToken(unittest.TestCase):
    """The installation token the waker reads Simplytics' queue with, minted
    from the App's key with the standard library alone."""

    @staticmethod
    def make_key(bits=1024, seed=7):
        rnd = random.Random(seed)

        def prime(b):
            while True:
                c = rnd.getrandbits(b) | (1 << (b - 1)) | 1
                if all(pow(a, c - 1, c) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)):
                    return c
        e = 65537
        while True:
            p, q = prime(bits // 2), prime(bits // 2)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e:
                break
        n, d = p * q, pow(e, -1, (p - 1) * (q - 1))

        def der_int(x):
            b = x.to_bytes((x.bit_length() + 8) // 8, "big")
            return b"\x02" + der_len(len(b)) + b

        def der_len(k):
            if k < 128:
                return bytes([k])
            kb = k.to_bytes((k.bit_length() + 7) // 8, "big")
            return bytes([0x80 | len(kb)]) + kb
        body = b"".join(der_int(x) for x in (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p)))
        der = b"\x30" + der_len(len(body)) + body
        b64 = base64.b64encode(der).decode()
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(
            b64[i:i + 64] for i in range(0, len(b64), 64)) + "\n-----END RSA PRIVATE KEY-----\n"
        return pem, n, e

    def test_rs256_signature_verifies_with_the_public_key(self):
        pem, n, e = self.make_key()
        sig = w.rs256(pem, b"header.payload")
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
        digest = hashlib.sha256(b"header.payload").digest()
        self.assertTrue(em.startswith(b"\x00\x01\xff"))
        self.assertTrue(em.endswith(bytes.fromhex("3031300d060960864801650304020105000420") + digest))

    def test_text_that_is_not_an_rsa_key_is_refused(self):
        with self.assertRaises(ValueError):
            w.rs256("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", b"x")

    def test_mints_an_installation_token_and_reuses_it_until_near_expiry(self):
        pem, n, e = self.make_key()
        clock = Clock()
        with tempfile.TemporaryDirectory() as d:
            for k, v in (("github_app_id", "5625896\n"), ("github_app_installation_id", "178879694"),
                         ("github_app_private_key", pem)):
                with open(os.path.join(d, k), "w") as f:
                    f.write(v)
            calls = []

            class Resp:
                def __init__(self, data):
                    self.data = data

                def read(self, *a):
                    return json.dumps(self.data).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def opener(req, timeout=None):
                calls.append(req)
                exp = dt.datetime.fromtimestamp(clock() + 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                return Resp({"token": f"ghs_{len(calls)}", "expires_at": exp})

            app = w.GitHubApp(d, "https://api.example", clock=clock, opener=opener)
            self.assertEqual(app.token(), "ghs_1")
            req = calls[0]
            self.assertEqual(req.full_url, "https://api.example/app/installations/178879694/access_tokens")
            self.assertEqual(req.get_method(), "POST")
            jwt = req.get_header("Authorization").split(" ", 1)[1]
            header, payload, sig = jwt.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            claims = json.loads(base64.urlsafe_b64decode(pad(payload)))
            self.assertEqual(claims["iss"], 5625896)
            self.assertLessEqual(claims["exp"] - claims["iat"], 600)  # GitHub's ten-minute limit
            self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(header)))["alg"], "RS256")
            em = pow(int.from_bytes(base64.urlsafe_b64decode(pad(sig)), "big"), e, n)
            digest = hashlib.sha256(f"{header}.{payload}".encode()).digest()
            self.assertEqual(em.to_bytes((n.bit_length() + 7) // 8, "big")[-32:], digest)
            clock.sleep(50 * 60)
            self.assertEqual(app.token(), "ghs_1")  # cached
            clock.sleep(6 * 60)
            self.assertEqual(app.token(), "ghs_2")  # within five minutes of expiry: renewed

    def test_org_queue_lists_the_installations_repos_of_that_org(self):
        gh = w.GitHub(None, "https://api.example", clock=lambda: T0, auth=lambda: "ghs_x")
        listed = []
        gh._get_all = lambda path, key: [
            {"full_name": "Simplytics/simplytics", "owner": {"login": "Simplytics"}, "private": True},
            {"full_name": "Simplytics/old", "owner": {"login": "Simplytics"}, "private": True, "archived": True},
            {"full_name": "Simplytics/site", "owner": {"login": "Simplytics"}, "private": False},
            {"full_name": "someone/else", "owner": {"login": "someone"}, "private": True},
        ] if path == "/installation/repositories" else []
        gh.queued_jobs = lambda repo, h, now: listed.append(repo) or []
        gh.org_queued_jobs("Simplytics", 72, dt.datetime.fromtimestamp(T0, UTC))
        self.assertEqual(listed, ["Simplytics/simplytics"])


class Manifests(unittest.TestCase):
    """The Simplytics manifests, the waker's RBAC and its mounted credential
    agree with waker.conf."""

    ZAGREUS = os.path.join(WAKER_DIR, "..")

    def read(self, *path):
        with open(os.path.join(self.ZAGREUS, *path)) as f:
            return f.read()

    def test_the_rbac_names_every_driven_set_and_application(self):
        text = self.read("waker", "waker.yaml")
        cfg = live_config()
        sets = set(re.findall(r"resources: \[autoscalingrunnersets\]\n\s+resourceNames: \[([^\]]*)\]", text))
        named = {x.strip() for group in sets for x in group.split(",")}
        self.assertEqual(named, {s["name"] for s in cfg["sets"]})
        apps = re.search(r"resources: \[applications\]\n\s+resourceNames: \[([^\]]*)\]", text).group(1)
        self.assertEqual({x.strip() for x in apps.split(",")}, {s["application"] for s in cfg["sets"]})

    def test_the_simplytics_credential_is_mounted_where_waker_conf_looks(self):
        cfg = live_config()
        text = self.read("waker", "waker.yaml")
        d = cfg["githubApps"]["simplytics"]["dir"]
        self.assertIn(f"mountPath: {d}", text)
        self.assertIn("secretName: arc-github-app-simplytics", text)
        self.assertIn("remoteRef: {key: simplytics-arc-github-app/simplytics-zagreus-arc.2026-11-05.private-key.pem}",
                      text)

    def test_the_scale_sets_point_at_the_simplytics_org(self):
        ci = self.read("runners", "simplytics", "ci.yaml")
        light = self.read("runners", "simplytics", "ci-light.yaml")
        for text, name, label in ((ci, SIM, "simplytics-ci"), (light, "zagreus-simplytics-light", "simplytics-light")):
            self.assertIn(f"runnerScaleSetName: {name}\n", text)
            self.assertIn(f"scaleSetLabels: [self-hosted, linux, {label}]", text)
            self.assertIn("githubConfigUrl: https://github.com/Simplytics\n", text)
            self.assertIn("githubConfigSecret: arc-github-app-simplytics\n", text)
        # The waker owns ci's maxRunners, as it does zagreus-ci's.
        self.assertIn("jsonPointers: [/spec/maxRunners]", ci)
        self.assertIn("RespectIgnoreDifferences=true", ci)
        self.assertNotIn("jsonPointers", light)
        app = re.search(r"^  name: (\S+)$", ci, re.M).group(1)
        self.assertEqual(app, next(s for s in live_config()["sets"] if s["name"] == SIM)["application"])

    def test_the_credential_reaches_every_namespace_its_sets_use(self):
        text = self.read("core", "github-app.yaml")
        found = re.findall(r"name: arc-github-app-simplytics\n  namespace: (\S+)", text)
        self.assertEqual(found, ["arc-runners"])


if __name__ == "__main__":
    unittest.main()
