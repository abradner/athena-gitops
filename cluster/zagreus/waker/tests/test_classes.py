"""Runner classes (../../runners/CLASSES.md): the labels the spritz sets carry,
read from their Application files, route each kind of job where the design
says, by the waker's own matcher (GitHub's rule: a runner must carry every
label a job asks for). The jobs' labels are typed here from the design's job
table; spritz's workflow is not read."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import waker as w  # noqa: E402

RUNNERS = os.path.join(HERE, "..", "..", "runners", "spritz")


def set_labels(filename):
    with open(os.path.join(RUNNERS, filename)) as f:
        text = f.read()
    name = re.search(r"^\s+runnerScaleSetName: (\S+)$", text, re.M).group(1)
    labels = re.search(r"^\s+scaleSetLabels: \[([^\]]*)\]$", text, re.M).group(1)
    return name, [l.strip() for l in labels.split(",")]


SETS = dict(set_labels(f) for f in ("ci.yaml", "ci-light.yaml", "ci-x64.yaml"))


def takers(job_labels):
    return sorted(n for n, labels in SETS.items() if w.job_match(job_labels, n, labels)[0])


class Classes(unittest.TestCase):
    def test_the_sets_carry_their_class(self):
        self.assertIn("heavy", SETS["zagreus-ci"])
        self.assertNotIn("light", SETS["zagreus-ci"])
        self.assertIn("X64", SETS["zagreus-ci"])
        self.assertIn("heavy", SETS["zagreus-ci-x64"])
        self.assertIn("X64", SETS["zagreus-ci-x64"])
        self.assertIn("light", SETS["zagreus-ci-light"])
        self.assertNotIn("heavy", SETS["zagreus-ci-light"])

    def test_a_heavy_job_of_any_arch_goes_to_the_amd64_heavy_sets(self):
        self.assertEqual(takers(["self-hosted", "heavy"]), ["zagreus-ci", "zagreus-ci-x64"])

    def test_an_x64_heavy_job_goes_to_the_amd64_heavy_sets(self):
        # Both carry heavy + X64 until step 3 folds the x64 set in.
        self.assertEqual(takers(["self-hosted", "heavy", "X64"]), ["zagreus-ci", "zagreus-ci-x64"])

    def test_a_light_job_goes_to_the_light_set_only(self):
        self.assertEqual(takers(["self-hosted", "light"]), ["zagreus-ci-light"])

    def test_an_arm64_heavy_job_has_no_zagreus_set(self):
        # Asking for ARM64 explicitly is a Mac-only (or future arm64 node) job.
        self.assertEqual(takers(["self-hosted", "heavy", "ARM64"]), [])

    def test_bots_still_route_by_set_name(self):
        for name in SETS:
            self.assertEqual(takers([name]), [name])

    def test_the_old_class_labels_still_match_until_step_3(self):
        self.assertIn("zagreus-ci", takers(["self-hosted", "linux", "spritz-ci"]))
        # e2e_full_matrix's label: both amd64 heavy sets carry it (step 1's one
        # routing change: same pod, same nodes).
        self.assertEqual(takers(["self-hosted", "linux", "playwright-full"]), ["zagreus-ci", "zagreus-ci-x64"])
        self.assertEqual(takers(["self-hosted", "linux", "spritz-light"]), ["zagreus-ci-light"])


if __name__ == "__main__":
    unittest.main()
