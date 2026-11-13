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


SETS = dict(set_labels(f) for f in ("ci.yaml", "ci-light.yaml"))


def takers(job_labels):
    return sorted(n for n, labels in SETS.items() if w.job_match(job_labels, n, labels)[0])


class Classes(unittest.TestCase):
    def test_the_sets_carry_their_class_and_nothing_else(self):
        # Since step 3 (13 Nov 2026) the classic labels are gone: a set
        # carries its class, and zagreus-ci its architecture.
        self.assertEqual(sorted(SETS["zagreus-ci"]), sorted(["self-hosted", "linux", "X64", "heavy"]))
        self.assertEqual(sorted(SETS["zagreus-ci-light"]), sorted(["self-hosted", "linux", "light"]))

    def test_the_x64_set_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(RUNNERS, "ci-x64.yaml")))

    def test_a_heavy_job_of_any_arch_goes_to_zagreus_ci(self):
        self.assertEqual(takers(["self-hosted", "heavy"]), ["zagreus-ci"])

    def test_an_x64_heavy_job_goes_to_zagreus_ci(self):
        self.assertEqual(takers(["self-hosted", "heavy", "X64"]), ["zagreus-ci"])

    def test_a_light_job_goes_to_the_light_set_only(self):
        self.assertEqual(takers(["self-hosted", "light"]), ["zagreus-ci-light"])

    def test_an_arm64_heavy_job_has_no_zagreus_set(self):
        # Asking for ARM64 explicitly is a Mac-only (or future arm64 node) job.
        self.assertEqual(takers(["self-hosted", "heavy", "ARM64"]), [])

    def test_bots_still_route_by_set_name(self):
        for name in SETS:
            self.assertEqual(takers([name]), [name])

    def test_the_classic_labels_match_no_set(self):
        # A job still asking for them would sit queued: none of spritz's
        # workflows does since spritz#1651.
        for labels in (["self-hosted", "linux", "spritz-ci"], ["self-hosted", "linux", "spritz-light"],
                       ["self-hosted", "linux", "playwright-full"], ["self-hosted", "linux", "X64", "spritz-ci"]):
            self.assertEqual(takers(labels), [], labels)


if __name__ == "__main__":
    unittest.main()
