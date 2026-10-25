"""The init container's talosctl copy (waker.yaml), run for real.

The 106 MB binary is copied from an image volume into an emptyDir. A plain
copy charged its page cache to the container's memory limit and was
OOM-killed on cp1 (athena-gitops#235). The script must fsync each chunk and
drop it from the page cache on both sides, so the cache stays bounded at a
chunk or so, and must leave a byte-identical, executable file.

The script is extracted from the manifest itself with yq, its two paths
pointed at temporary files, and run with a recording `os` that delegates
every real call and counts fsync/posix_fadvise (posix_fadvise is faked where
the platform lacks it, as macOS does).
"""
import os, shutil, subprocess, tempfile, types, unittest

WAKER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 8 << 20


def manifest_script(path=os.path.join(WAKER_DIR, "waker.yaml")):
    yq = shutil.which("yq")
    if not yq:
        raise unittest.SkipTest("yq not installed (mise install)")
    return subprocess.run([yq, 'select(.kind == "Deployment") | .spec.template.spec.initContainers[0].command[2]', path],
                          check=True, capture_output=True, text=True).stdout


def run(script, src, dst):
    calls = {"fsync": 0, "fadvise": []}
    fake = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    real_fsync = os.fsync
    fake.fsync = lambda fd: (calls.__setitem__("fsync", calls["fsync"] + 1), real_fsync(fd))[1]
    fake.posix_fadvise = lambda fd, off, n, advice: calls["fadvise"].append((fd, off, n, advice))
    fake.POSIX_FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", 4)
    body = "\n".join(l for l in script.splitlines() if not l.startswith("import "))
    body = body.replace("/talosctl-image/talosctl", src).replace("/tools/talosctl", dst)
    exec(compile(body, "init-copy", "exec"), {"os": fake, "shutil": shutil, "open": open})
    return calls


class InitCopy(unittest.TestCase):
    def check(self, script):
        d = tempfile.mkdtemp()
        try:
            src, dst = os.path.join(d, "src"), os.path.join(d, "talosctl")
            data = os.urandom(1 << 20) * 40  # 40 MiB: five chunks
            with open(src, "wb") as f:
                f.write(data)
            calls = run(script, src, dst)
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), data, "copy not byte-identical")
            self.assertTrue(os.access(dst, os.X_OK), "copy not executable")
            self.assertFalse(os.path.exists(dst + ".tmp"), "temporary file left behind")
            chunks = -(-len(data) // CHUNK)
            self.assertGreaterEqual(calls["fsync"], chunks, "chunks not fsynced: dirty pages unbounded")
            dropped = sorted((off, n) for _, off, n, adv in calls["fadvise"])
            covered = sum(n for off, n in dropped) // 2  # both source and destination
            self.assertGreaterEqual(covered, len(data), "page cache not dropped across the file")
            self.assertTrue(all(n <= CHUNK for _, n in dropped), "a drop larger than a chunk")
        finally:
            shutil.rmtree(d)

    def test_the_manifests_init_copy(self):
        self.check(manifest_script())


if __name__ == "__main__":
    unittest.main()
