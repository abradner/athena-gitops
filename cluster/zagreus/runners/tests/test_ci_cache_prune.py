"""The prune script in ../ci-cache-prune.yaml, run for one pass against
fixture cache trees.

    python3 -m unittest discover -s cluster/zagreus/runners/tests -v

Needs yq (mise install), as the waker's Heartbeat test does, and GNU find,
mv, date and sort: on macOS the Homebrew g-prefixed ones (findutils,
coreutils) are found and put first on PATH. Run as an ordinary user, not
root: the kubelet's root-owned 0755 directories are stood in for by
directories this user owns but cannot write (0555), which refuse it the
same way, and root would ignore both.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "ci-cache-prune.yaml")
DAY = 86400
GNU = ("find", "mv", "date", "sort")


def gnu_path(tmp):
    """A bin directory putting a GNU build of each tool first on PATH, or
    None when one is missing."""
    shim = os.path.join(tmp, "bin")
    os.mkdir(shim)
    for tool in GNU:
        plain = shutil.which(tool)
        out = subprocess.run([plain, "--version"], capture_output=True, text=True).stdout if plain else ""
        if "GNU" in out:
            continue
        g = shutil.which("g" + tool)
        if not g:
            return None
        os.symlink(g, os.path.join(shim, tool))
    return shim + os.pathsep + os.environ["PATH"]


def mk(path, age_days=40, used_days=None):
    """A directory holding a file and a bin/ file, all aged; used_days sets
    the files' access time apart from their age."""
    os.makedirs(os.path.join(path, "bin"), exist_ok=True)
    now = time.time()
    m = now - age_days * DAY
    a = now - (used_days if used_days is not None else age_days) * DAY
    for f in (os.path.join(path, "f"), os.path.join(path, "bin", "tool")):
        with open(f, "w") as fh:
            fh.write("x")
        os.utime(f, (a, m))
    os.utime(os.path.join(path, "bin"), (m, m))
    os.utime(path, (m, m))


def make_writable(root):
    for d, dirs, _ in os.walk(root):
        for x in dirs:
            p = os.path.join(d, x)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
    os.chmod(root, 0o755)


class Prune(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() == 0:
            raise unittest.SkipTest("run as an ordinary user: root ignores the 0555 stand-ins")
        yq = shutil.which("yq")
        if not yq:
            raise unittest.SkipTest("yq not installed (mise install)")
        cls.script = subprocess.run([yq, 'select(.kind == "ConfigMap") | .data["prune.sh"]', MANIFEST],
                                    check=True, capture_output=True, text=True).stdout
        assert "check_trash" in cls.script

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (make_writable(self.tmp), shutil.rmtree(self.tmp)))
        self.path = gnu_path(self.tmp)
        if not self.path:
            self.skipTest("GNU find/mv/date/sort not found (brew install findutils coreutils)")
        self.cache = os.path.join(self.tmp, "cache")
        self.trash = os.path.join(self.cache, ".ci-cache-prune-trash")
        c = self.cache
        for r in (c, os.path.join(c, "simplytics")):
            p = os.path.join(r, "ms-playwright")
            for d in ("chromium-1100", "chromium-1150", "chromium-1187", "chromium_headless_shell-1100",
                      "chromium_headless_shell-1187", "firefox-1490"):
                mk(os.path.join(p, d))
            mk(os.path.join(p, "chromium-1200"), age_days=0)
            m = os.path.join(r, "mise", "installs")
            for v in ("3.2.0", "3.3.0", "3.4.6"):
                mk(os.path.join(m, "ruby", v))
            mk(os.path.join(m, "ruby", "3.4.7"), used_days=0)
            mk(os.path.join(m, "ruby", "3.1.0"), used_days=0)  # old, outside the newest three, but in use
            os.symlink("3.4.7", os.path.join(m, "ruby", "3.4"))
            ro = os.path.join(m, "ruby", "3.2.0", "ro")
            os.makedirs(os.path.join(ro, "sub"))
            os.chmod(os.path.join(ro, "sub"), 0o555)
            os.chmod(ro, 0o555)
            mk(os.path.join(r, "mise", "downloads", "ruby", "3.2.0"))
            mk(os.path.join(r, "bun", "oldpkg@1.0.0@@@1"))
            mk(os.path.join(r, "bun", "midpkg@1.0.0@@@1"), age_days=20)
        mk(os.path.join(c, "bundle-trees", "old-tree"))
        # The kubelet's directories: root-owned 0755, so not ours to write.
        os.chmod(os.path.join(c, "simplytics"), 0o555)
        os.chmod(c, 0o555)

    def give_trash(self):
        """What the init container's install -d leaves: a trash we own, 0700."""
        os.chmod(self.cache, 0o755)
        os.mkdir(self.trash, 0o700)
        os.chmod(self.cache, 0o555)

    def run_pass(self, prune_at="0"):
        env = dict(os.environ, PATH=self.path, CACHE=self.cache, PRUNE_AT=prune_at)
        p = subprocess.run(["bash", "-c", self.script, "prune.sh", "--once"], env=env,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def exists(self, rel):
        return os.path.lexists(os.path.join(self.cache, rel))

    UNTOUCHED = ("ms-playwright/chromium-1100", "mise/installs/ruby/3.2.0", "bun/oldpkg@1.0.0@@@1")

    def test_no_trash_fails_loudly_and_touches_nothing(self):
        rc, out = self.run_pass()
        self.assertEqual(rc, 1, out)
        self.assertIn("FATAL: trash", out)
        for rel in self.UNTOUCHED:
            self.assertTrue(self.exists(rel), rel)

    def test_trash_it_cannot_write_fails_loudly(self):
        self.give_trash()
        os.chmod(self.trash, 0o555)  # as a root-owned trash would be
        rc, out = self.run_pass()
        self.assertEqual(rc, 1, out)
        self.assertIn("FATAL: trash", out)
        for rel in self.UNTOUCHED:
            self.assertTrue(self.exists(rel), rel)

    def test_every_removal_failing_fails_loudly(self):
        self.give_trash()
        for d in ("ms-playwright", "mise/installs/ruby", "mise/downloads/ruby", "bun",
                  "simplytics/ms-playwright", "simplytics/mise/installs/ruby",
                  "simplytics/mise/downloads/ruby", "simplytics/bun"):
            os.chmod(os.path.join(self.cache, d), 0o555)
        rc, out = self.run_pass()
        self.assertEqual(rc, 1, out)
        self.assertIn("0 removed", out)
        self.assertIn("FATAL: every removal failed", out)
        for rel in self.UNTOUCHED:
            self.assertTrue(self.exists(rel), rel)

    def test_after_the_init_container_it_prunes_what_it_should_and_nothing_else(self):
        self.give_trash()
        rc, out = self.run_pass()
        self.assertEqual(rc, 0, out)
        self.assertIn("10 removed, 0 failed", out)
        for root in ("", "simplytics/"):
            for rel in ("ms-playwright/chromium-1100", "ms-playwright/chromium-1150", "mise/installs/ruby/3.2.0",
                        "mise/downloads/ruby/3.2.0", "bun/oldpkg@1.0.0@@@1"):
                self.assertFalse(self.exists(root + rel), root + rel)
            # chromium_headless_shell has only two revisions here: both stay.
            for rel in ("ms-playwright/chromium-1200", "ms-playwright/chromium-1187",
                        "ms-playwright/chromium_headless_shell-1100", "ms-playwright/chromium_headless_shell-1187", "ms-playwright/firefox-1490",
                        "mise/installs/ruby/3.4.7", "mise/installs/ruby/3.4.6", "mise/installs/ruby/3.3.0",
                        "mise/installs/ruby/3.1.0", "mise/installs/ruby/3.4", "bun/midpkg@1.0.0@@@1"):
                self.assertTrue(self.exists(root + rel), root + rel)
        self.assertTrue(self.exists("bundle-trees/old-tree"))
        self.assertEqual(os.listdir(self.trash), [])

    def test_under_the_threshold_it_removes_nothing(self):
        self.give_trash()
        rc, out = self.run_pass(prune_at="101")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to do", out)
        for rel in self.UNTOUCHED:
            self.assertTrue(self.exists(rel), rel)


if __name__ == "__main__":
    unittest.main()
