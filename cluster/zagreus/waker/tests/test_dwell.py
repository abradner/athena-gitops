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

    def run(self, trace, name=SIM):
        """Each pass's cap writes for a set, as (pass, value)."""
        return self.run_all(trace)[name]

    def run_all(self, trace):
        """Each pass's cap writes for both pool sets, and per pass whether
        a lower was held by the dwell and whether a pool raise was written."""
        out = {CI: [], SIM: []}
        self.passes = []
        for i, step in enumerate(trace):
            self.set_pass(*step)
            mark = len(self.r.kube.writes)
            before = dict(self.r.kube.caps())
            with self.capture() as logs:
                self.r.tick(advance=30)
            raised = []
            for n in (CI, SIM):
                ws = self.writes(n, mark)
                out[n] += [(i, v) for v in ws]
                if ws and max(ws) > before[n]:
                    raised.append(n)
            held_lower = []
            for line in logs:
                if "held by the share dwell" in line:
                    parts = line.split()
                    name, old, new = parts[0], int(parts[2]), int(parts[4].rstrip(";"))
                    if new < old:
                        held_lower.append(name)
            self.passes.append((held_lower, raised))
        return out

    def capture(self):
        import contextlib
        import logging

        @contextlib.contextmanager
        def cm():
            lines = []

            class H(logging.Handler):
                def emit(self, record):
                    lines.append(record.getMessage())
            h = H(level=logging.INFO)
            logger = logging.getLogger("waker")
            old = logger.level
            logger.addHandler(h)
            logger.setLevel(logging.INFO)
            try:
                yield lines
            finally:
                logger.removeHandler(h)
                logger.setLevel(old)
        return cm()


def ping_pongs(writes, window_passes):
    """A -> B -> A within one dwell window: the shape that hurts."""
    return sum(1 for (a, x), (_, _y), (c, z) in zip(writes, writes[1:], writes[2:])
               if x == z and c - a < window_passes)


class PingPong(unittest.TestCase):
    WINDOW = 4  # passes: 120 s

    def test_the_deployed_dwell_is_120_s(self):
        self.assertEqual(LiveRig(always_on=True).cfg["shareDwellSeconds"], 120)

    def test_without_the_dwell_the_shape_ping_pongs(self):
        # Positive control: the replay reproduces 14 Nov's ping-pong. The
        # figures are the README's ("The dwell").
        out = Replay(dwell=0).run_all(OPENING + TOGGLE)
        self.assertEqual((len(out[SIM]), ping_pongs(out[SIM], self.WINDOW)), (15, 13), out[SIM])
        self.assertEqual((len(out[CI]), ping_pongs(out[CI], self.WINDOW)), (19, 17), out[CI])

    def test_with_the_dwell_neither_pool_set_goes_back_within_a_window(self):
        out = Replay().run_all(OPENING + TOGGLE)
        self.assertEqual((len(out[SIM]), len(out[CI])), (3, 5), out)
        for name in (CI, SIM):
            self.assertEqual(ping_pongs(out[name], self.WINDOW), 0, (name, out[name]))
            # Nor two writes to one set in one pass (its two applies).
            same = [a for a, b in zip(out[name], out[name][1:]) if a[0] == b[0]]
            self.assertEqual(same, [], (name, out[name]))

    def test_a_held_lower_blocks_its_siblings_raise(self):
        # The pool's invariant: a slot is never offered to both sets. While
        # one set's lower waits out the dwell, the other is not raised.
        rp = Replay()
        rp.run_all(OPENING + TOGGLE)
        held_passes = [(h, r) for h, r in rp.passes if h]
        self.assertTrue(held_passes, rp.passes)  # the replay does hold lowers
        for held, raised in held_passes:
            self.assertFalse(set(raised) - set(held), (held, raised))


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

    def held_setup(self):
        from test_waker import states
        waker = Replay().r.waker
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": {}, "zag-amd64vm-01": {}})
        pool_of = {CI: CI, SIM: CI}
        now = waker.clock()
        waker.cap_changed_at = {CI: now, SIM: now}
        waker.raised_at = {}  # no raise since: the lowers here are sharing alone
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

    def test_a_cap_above_its_ceiling_is_not_held(self):
        waker, st, pool_of, snap, target = self.held_setup()
        snap["ceilings"] = {SIM: 1}
        held = waker.dwell_holds(snap, target, st, pool_of, False)
        self.assertNotIn(SIM, held)
        self.assertIn(CI, held)  # control: the other set still waits

    def test_an_unread_node_does_not_release_the_dwell(self):
        waker, st, pool_of, snap, target = self.held_setup()
        st.pop("zag-amd64-02")  # its GET failed this pass
        self.assertEqual(sorted(waker.dwell_holds(snap, target, st, pool_of, False)), [CI, SIM])

    def test_a_degraded_set_is_not_starving(self):
        waker, st, pool_of, snap, target = self.held_setup()
        del snap["runners"][SIM]  # its scale set unreadable this pass
        snap["degraded"] = {SIM}
        waker.share[SIM] = 2
        self.assertEqual(sorted(waker.dwell_holds(snap, target, st, pool_of, False)), [CI, SIM])

    def test_a_lower_after_a_siblings_raise_is_not_held(self):
        # The other set was raised after this set's last write: its slot is
        # already offered there, so this set's lower goes now.
        waker, st, pool_of, snap, target = self.held_setup()
        waker.raised_at = {SIM: waker.cap_changed_at[CI] + 1}
        held = waker.dwell_holds(snap, target, st, pool_of, False)
        self.assertNotIn(CI, held)
        self.assertIn(SIM, held)  # control: SIM's own lower still waits

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
        # Back to its floor at once, and no further while Simplytics' lower
        # is held: past the floor the slot would be offered to both.
        self.assertEqual(rp.r.kube.caps()[CI], floor)

    def test_a_raise_past_the_pools_hold_goes_only_to_the_floor(self):
        # zagreus-ci's lower is held by the dwell (its live cap is above
        # what sharing gives it), so the pool's raises wait. Simplytics, put
        # below its floor (1) by hand, is raised at once, but only to its
        # floor, not to the 2 its share would give.
        rp = Replay()
        rp.run_all((OPENING + TOGGLE)[:3])
        rp.r.waker.cap_changed_at[CI] = rp.r.clock()
        rp.r.waker.raised_at = {}
        rp.r.kube.ars[("arc-runners", CI)]["spec"]["maxRunners"] = 10
        rp.r.kube.ars[("arc-runners", SIM)]["spec"]["maxRunners"] = 0
        rp.set_pass(1, 3, 8, 1)
        mark = len(rp.r.kube.writes)
        with rp.capture() as logs:
            rp.r.tick(advance=30)
        self.assertTrue([m for m in logs if m.startswith(CI) and "held by the share dwell" in m], logs)
        self.assertEqual(rp.writes(SIM, mark), [1])

    def test_a_held_raise_does_not_hold_a_siblings_raise(self):
        # Only a held lower holds the pool's raises: a held raise leaves its
        # set at the lower cap, so the sibling is offered nothing twice.
        rp = Replay()
        rp.run_all((OPENING + TOGGLE)[:3])
        rp.r.waker.cap_changed_at[CI] = rp.r.clock()
        rp.r.waker.raised_at = {}
        rp.r.kube.ars[("arc-runners", SIM)]["spec"]["maxRunners"] = 0
        rp.set_pass(1, 3, 8, 1)
        mark = len(rp.r.kube.writes)
        with rp.capture() as logs:
            rp.r.tick(advance=30)
        held = [m for m in logs if m.startswith(CI) and "held by the share dwell" in m]
        self.assertTrue(held and int(held[0].split()[4].rstrip(";")) > int(held[0].split()[2]), logs)
        self.assertEqual(rp.writes(SIM, mark), [2])

    def test_a_raise_the_floor_lifted_is_never_held(self):
        # Why a held raise may leave the pool's raises free: a raise held by
        # the dwell always targets the set's share. One whose target the
        # floor lifted starts below the floor, and is not held.
        waker, st, pool_of, snap, target = self.held_setup()
        floor = next(s["floor"] for s in waker.cfg["sets"] if s["name"] == CI)
        snap["live"][CI] = floor - 1
        target[CI] = floor
        self.assertNotIn(CI, waker.dwell_holds(snap, target, st, pool_of, False))

    def test_a_forced_lower_is_not_held(self):
        # The sleep procedure lowers with force before a cordon: never held.
        from test_waker import states
        waker = Replay().r.waker
        st = states(**{"zag-amd64-01": {}, "zag-amd64-02": {}, "zag-amd64-03": {}, "zag-amd64vm-01": {}})
        pool_of = {CI: CI, SIM: CI}
        now = waker.clock()
        waker.cap_changed_at = {CI: now, SIM: now}
        waker.raised_at = {}  # no raise since: the lowers here are sharing alone
        waker.pool_nodes_at_change = {CI: waker.pool_nodes(st, CI, pool_of)}
        waker.share = {CI: 8, SIM: 1}
        snap = {"live": {CI: 9, SIM: 2}, "runners": {CI: 8, SIM: 1}, "unstarted": {}, "ceilings": {}}
        target = {CI: 8, SIM: 1}
        # Positive control: unforced, the same change waits out the dwell.
        self.assertEqual(sorted(waker.dwell_holds(snap, target, st, pool_of, force=False)), [CI, SIM])
        self.assertEqual(waker.dwell_holds(snap, target, st, pool_of, force=True), {})


if __name__ == "__main__":
    unittest.main()
