"""The share dwell (README, "Sharing", "The dwell"; 14 Nov 2026): once a pool
set's cap changes, a change that comes from sharing alone waits
shareDwellSeconds. Every cap change rebuilds the set's listener, and under
contention the hold yielded and returned within a pass, so Simplytics' cap
went down and back up every 34-72 s. The deployed config, through LiveRig,
with its own sets' labels."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import waker as w  # noqa: E402
from test_sharing import CI, SIM, LiveRig  # noqa: E402

# Where the replayed runners sit: zagreus-ci across 01, 02 and the VM as on
# 14 Nov; Simplytics' on 02.
CI_NODES = ["zag-amd64-01"] * 3 + ["zag-amd64-02"] * 4 + ["zag-amd64vm-01"]

# (spritz queued, Simplytics queued, spritz runners, Simplytics runners), one
# 30 s pass each: the opening of the 14 Nov contention (00:35:48-00:36:58Z,
# from the waker's log), then the shape that ping-ponged: Simplytics holding
# a slot above its one runner, a spritz job queued one pass and claimed the
# next, a Simplytics job the pass after.
OPENING = [(4, 3, 7, 1), (4, 1, 8, 2), (3, 1, 6, 1)]
TOGGLE = [(1, 0, 8, 1), (0, 1, 7, 1)] * 8


class Replay:
    def __init__(self, dwell=None):
        self.r = LiveRig(always_on=True)
        if dwell is not None:
            self.r.cfg["shareDwellSeconds"] = dwell
        self.r.tick()

    def set_pass(self, cq, sq, cr, sr):
        r = self.r
        r.kube.pods = [p for p in r.kube.pods if p["metadata"]["labels"].get(w.SCALE_SET_LABEL) not in (CI, SIM)]
        r.kube.ers["arc-runners"] = []
        for k in range(cr):
            r.kube.add_runner_pod(CI_NODES[k % len(CI_NODES)], CI, name=f"ci-{k}")
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: CI}},
                                              "status": {"jobRequestId": 1000 + k}})
        for k in range(sr):
            r.kube.add_runner_pod("zag-amd64-02", SIM, name=f"sim-{k}")
            r.kube.ers["arc-runners"].append({"metadata": {"labels": {w.SCALE_SET_LABEL: SIM}},
                                              "status": {"jobRequestId": 2000 + k}})
        r.spritz.jobs.clear()
        r.simplytics.jobs.clear()
        for _ in range(cq):
            r.spritz_job(age=90)
        for _ in range(sq):
            r.sim_job(age=90)

    def writes(self, name, since):
        return [b["spec"]["maxRunners"] for v, p, b in self.r.kube.writes[since:]
                if v == "PATCH" and p.endswith("/autoscalingrunnersets/" + name)]

    def run(self, trace):
        """Each pass's cap writes for Simplytics, as (pass, value)."""
        out = []
        for i, step in enumerate(trace):
            self.set_pass(*step)
            mark = len(self.r.kube.writes)
            self.r.tick(advance=30)
            out += [(i, v) for v in self.writes(SIM, mark)]
        return out


def ping_pongs(writes, window_passes):
    """A -> B -> A within one dwell window: the shape that hurts."""
    return sum(1 for (a, x), (_, _y), (c, z) in zip(writes, writes[1:], writes[2:])
               if x == z and c - a < window_passes)


class PingPong(unittest.TestCase):
    WINDOW = 4  # passes: 120 s

    def test_the_deployed_dwell_is_120_s(self):
        self.assertEqual(LiveRig(always_on=True).cfg["shareDwellSeconds"], 120)

    def test_without_the_dwell_the_shape_ping_pongs(self):
        # Positive control: the replay reproduces 14 Nov's ping-pong.
        writes = Replay(dwell=0).run(OPENING + TOGGLE)
        self.assertGreater(ping_pongs(writes, self.WINDOW), 5, writes)

    def test_with_the_dwell_no_cap_goes_back_within_a_window(self):
        writes = Replay().run(OPENING + TOGGLE)
        self.assertEqual(ping_pongs(writes, self.WINDOW), 0, writes)
        # And no two writes closer than the dwell.
        gaps = [b - a for (a, _), (b, _) in zip(writes, writes[1:])]
        self.assertTrue(all(g >= self.WINDOW for g in gaps), writes)


class Exceptions(unittest.TestCase):
    """What still acts at once, inside a dwell."""

    def settled(self):
        """A replay that has just written Simplytics' cap, so a dwell runs."""
        rp = Replay()
        rp.run(OPENING + TOGGLE[:6])
        last = max(rp.r.waker.cap_changed_at.get(SIM, 0), rp.r.waker.cap_changed_at.get(CI, 0))
        self.assertGreater(last, 0)
        return rp

    def in_dwell(self, rp, name):
        return rp.r.clock() - rp.r.waker.cap_changed_at[name] < rp.r.cfg["shareDwellSeconds"]

    def test_a_node_going_away_lowers_at_once(self):
        rp = Replay()
        rp.set_pass(1, 0, 8, 1)
        rp.r.tick(advance=30)
        rp.set_pass(0, 1, 7, 1)
        rp.r.tick(advance=30)
        self.assertTrue(self.in_dwell(rp, CI) or self.in_dwell(rp, SIM))
        before = rp.r.kube.caps()
        mark = len(rp.r.kube.writes)
        rp.r.kube.nodes["zag-amd64-02"]["spec"]["unschedulable"] = True
        rp.set_pass(0, 1, 3, 0)
        rp.r.tick(advance=30)
        lowered = [n for n in (CI, SIM) if rp.writes(n, mark) and rp.writes(n, mark)[-1] < before[n]]
        self.assertTrue(lowered, (before, rp.r.kube.caps()))

    def test_a_set_with_queued_jobs_and_nothing_held_is_served_at_once(self):
        rp = Replay()
        rp.set_pass(1, 0, 8, 1)
        rp.r.tick(advance=30)
        rp.set_pass(0, 1, 7, 1)
        rp.r.tick(advance=30)
        self.assertTrue(self.in_dwell(rp, SIM) or self.in_dwell(rp, CI))
        mark = len(rp.r.kube.writes)
        # Simplytics' runner ends and two jobs queue: it holds nothing.
        rp.set_pass(0, 2, 7, 0)
        rp.r.tick(advance=30)
        rp.set_pass(0, 2, 7, 0)
        rp.r.tick(advance=30)
        self.assertGreaterEqual(rp.r.kube.caps()[SIM], 1, rp.writes(SIM, mark))
        self.assertTrue(rp.writes(SIM, mark) or rp.r.kube.caps()[SIM] >= 1)

    def held_setup(self):
        from test_waker import states
        waker = Replay().r.waker
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": {}, "zag-amd64vm-01": {}})
        pool_of = {CI: CI, SIM: CI}
        now = waker.clock()
        waker.cap_changed_at = {CI: now, SIM: now}
        waker.pool_nodes_at_change = {CI: waker.pool_nodes(st, CI, pool_of)}
        waker.share = {CI: 8, SIM: 1}
        snap = {"live": {CI: 9, SIM: 2}, "runners": {CI: 8, SIM: 1}, "unstarted": {}, "ceilings": {}}
        return waker, st, pool_of, snap, {CI: 8, SIM: 1}

    def test_an_unread_queue_gives_its_share_back_at_once(self):
        waker, st, pool_of, snap, target = self.held_setup()
        self.assertEqual(sorted(waker.dwell_holds(snap, target, st, pool_of, False)), [CI, SIM])  # control
        waker.blind_sets = {SIM}
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, False), {})

    def test_a_node_changing_state_is_room_not_sharing(self):
        waker, st, pool_of, snap, target = self.held_setup()
        st["zag-amd64-02"]["unschedulable"] = True
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, False), {})

    def test_a_starving_set_releases_the_pool(self):
        waker, st, pool_of, snap, target = self.held_setup()
        snap["runners"][SIM] = 0
        waker.share[SIM] = 2  # two queued, none running or unstarted
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, False), {})

    def test_after_the_dwell_nothing_is_held(self):
        waker, st, pool_of, snap, target = self.held_setup()
        waker.clock.sleep(waker.cfg["shareDwellSeconds"])
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, False), {})

    def test_a_cap_outside_its_floor_or_ceiling_is_corrected_at_once(self):
        rp = Replay()
        rp.set_pass(1, 0, 8, 1)
        rp.r.tick(advance=30)
        rp.set_pass(0, 1, 7, 1)
        rp.r.tick(advance=30)
        # Someone sets zagreus-ci below its floor by hand, inside a dwell.
        rp.r.waker.cap_changed_at[CI] = rp.r.clock()
        rp.r.kube.ars[("arc-runners", CI)]["spec"]["maxRunners"] = 3
        rp.set_pass(0, 1, 3, 1)
        rp.r.tick(advance=30)
        floor = next(s["floor"] for s in rp.r.cfg["sets"] if s["name"] == CI)
        self.assertGreaterEqual(rp.r.kube.caps()[CI], floor)

    def test_a_forced_lower_is_not_held(self):
        # The sleep procedure lowers with force before a cordon: never held.
        from test_waker import states
        waker = Replay().r.waker
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": {}, "zag-amd64vm-01": {}})
        pool_of = {CI: CI, SIM: CI}
        now = waker.clock()
        waker.cap_changed_at = {CI: now, SIM: now}
        waker.pool_nodes_at_change = {CI: waker.pool_nodes(st, CI, pool_of)}
        waker.share = {CI: 8, SIM: 1}
        snap = {"live": {CI: 9, SIM: 2}, "runners": {CI: 8, SIM: 1}, "unstarted": {}, "ceilings": {}}
        target = {CI: 8, SIM: 1}
        # Positive control: unforced, the same change waits out the dwell.
        self.assertEqual(sorted(waker.dwell_holds(snap, target, st, pool_of, force=False)), [CI, SIM])
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, force=True), {})


if __name__ == "__main__":
    unittest.main()
