#!/usr/bin/env python3
"""Tests for skydas.

The bug that started this tool never raised an error — it failed open,
silently, for two days. These tests hold the two shapes that matter: a
guard that really is armed says so, and a guard that quietly went inert
(the 2026-08-29 shape, and its cousins) is caught and fails the exit code.

    python3 test_skydas.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skydas as sk

SKYDAS_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skydas.py")


def git_available():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


class HermeticRepoTest(unittest.TestCase):
    """Base class: builds a throwaway repo + bare 'origin', hermetically."""

    def setUp(self):
        if not git_available():
            self.skipTest("git is not available")
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_CONFIG_SYSTEM"] = os.devnull
        self.work = os.path.join(self.tmp, "work")
        self.origin = os.path.join(self.tmp, "origin.git")
        os.makedirs(self.work)
        self.run_git(["-c", "init.defaultBranch=main", "init", "-q", "."],
                    cwd=self.work)
        self.run_git(["config", "user.email", "a@b.test"], cwd=self.work)
        self.run_git(["config", "user.name", "a"], cwd=self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_git(self, args, cwd=None):
        p = subprocess.run(["git"] + args, cwd=cwd or self.work,
                          capture_output=True, text=True, env=self.env)
        if p.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (args, p.stderr))
        return p.stdout.strip()

    def commit(self, path, content, msg="commit"):
        full = os.path.join(self.work, path)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
        with open(full, "w") as f:
            f.write(content)
        self.run_git(["add", "-A"])
        self.run_git(["commit", "-q", "-m", msg])

    def make_origin_and_push(self, branch="main"):
        self.run_git(["init", "-q", "--bare", self.origin])
        self.run_git(["remote", "add", "origin", self.origin])
        self.run_git(["push", "-q", "origin", branch])

    def run_skydas(self, extra_args=()):
        cmd = [sys.executable, SKYDAS_PY, "check", "--repo", self.work] + list(extra_args)
        p = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
        return p.returncode, p.stdout, p.stderr


class TestHookArmed(HermeticRepoTest):
    def test_hook_on_ref_and_worktree_and_executable_is_armed(self):
        os.makedirs(os.path.join(self.work, "githooks"))
        hook = os.path.join(self.work, "githooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(hook, 0o755)
        self.commit("githooks/pre-commit", "#!/bin/sh\nexit 0\n", "add hook")
        self.run_git(["config", "core.hooksPath", "githooks"])
        self.make_origin_and_push("main")

        code, out, err = self.run_skydas()
        self.assertEqual(code, 0, err or out)
        self.assertIn("ARMED", out)

        code, out, err = self.run_skydas(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["armed"])
        self.assertTrue(any(f["verdict"] == "ARMED" for f in data["findings"]))


class TestTwoThousandTwentySixAugustTwentyNinthBug(HermeticRepoTest):
    """The exact bug: hook merged to origin, never checked out locally.
    core.hooksPath then points at a real directory, but the hook file
    inside it does not exist in the working tree. Git fails open. skydas
    must catch this."""

    def test_hook_present_on_ref_deleted_from_worktree_is_inert(self):
        os.makedirs(os.path.join(self.work, "githooks"))
        hook = os.path.join(self.work, "githooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        self.commit("githooks/pre-commit", "#!/bin/sh\nexit 1\n", "add hook")
        self.run_git(["config", "core.hooksPath", "githooks"])
        self.make_origin_and_push("main")

        # simulate: the local branch never checked this file out locally —
        # delete it from the working tree without committing the deletion,
        # the way a stale local branch simply never had it.
        os.remove(hook)

        code, out, err = self.run_skydas(["--ref", "origin/main"])
        self.assertEqual(code, 1, err or out)
        self.assertIn("INERT", out)
        self.assertIn("absent here", out)


class TestHooksPathDirMissing(HermeticRepoTest):
    def test_hooks_path_pointing_at_missing_dir_is_inert(self):
        self.commit("README.md", "hello", "init")
        self.run_git(["config", "core.hooksPath", "nonexistent-hooks-dir"])

        code, out, err = self.run_skydas()
        self.assertEqual(code, 1, err or out)
        self.assertIn("INERT", out)
        self.assertIn("does not exist in this checkout", out)


class TestHookNotExecutable(HermeticRepoTest):
    def test_hook_present_but_chmod_644_is_inert(self):
        os.makedirs(os.path.join(self.work, "githooks"))
        hook = os.path.join(self.work, "githooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(hook, 0o644)
        self.commit("githooks/pre-commit", "#!/bin/sh\nexit 0\n", "add hook")
        self.run_git(["config", "core.hooksPath", "githooks"])

        code, out, err = self.run_skydas()
        self.assertEqual(code, 1, err or out)
        self.assertIn("INERT", out)
        self.assertIn("not executable", out)


class TestNothingConfigured(HermeticRepoTest):
    def test_no_hookspath_no_settings_json_is_clean_exit_0(self):
        self.commit("README.md", "hello", "init")

        code, out, err = self.run_skydas()
        self.assertEqual(code, 0, err or out)
        self.assertNotIn("INERT", out)


class TestSettingsJsonMissingScript(HermeticRepoTest):
    def test_settings_json_referencing_missing_script_is_inert(self):
        os.makedirs(os.path.join(self.work, ".claude"))
        settings = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [
                        {"type": "command",
                         "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/verify.sh"}
                    ]}
                ]
            }
        }
        with open(os.path.join(self.work, ".claude", "settings.json"), "w") as f:
            json.dump(settings, f)
        # skydas checks the working tree, not what's committed — an initial
        # commit just gives the repo a HEAD so other git plumbing behaves.
        self.commit("README.md", "hello", "init")

        code, out, err = self.run_skydas()
        self.assertEqual(code, 1, err or out)
        self.assertIn("INERT", out)
        self.assertIn("does not exist", out)


class TestSettingsJsonMalformed(HermeticRepoTest):
    def test_malformed_settings_json_is_unknown_not_a_crash(self):
        os.makedirs(os.path.join(self.work, ".claude"))
        with open(os.path.join(self.work, ".claude", "settings.json"), "w") as f:
            f.write("{ not valid json ][")

        code, out, err = self.run_skydas()
        self.assertEqual(code, 0, err or out)  # UNKNOWN never fails the run
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)
        self.assertIn("?", out)

        code, out, err = self.run_skydas(["--json"])
        data = json.loads(out)
        self.assertTrue(any(f["verdict"] == "UNKNOWN" for f in data["findings"]))


class TestJsonShape(HermeticRepoTest):
    def test_json_output_parses_and_has_documented_shape(self):
        self.commit("README.md", "hello", "init")

        code, out, err = self.run_skydas(["--json"])
        self.assertEqual(code, 0, err or out)
        data = json.loads(out)
        self.assertIn("armed", data)
        self.assertIn("findings", data)
        self.assertIsInstance(data["armed"], bool)
        self.assertIsInstance(data["findings"], list)
        for f in data["findings"]:
            self.assertIn("guard", f)
            self.assertIn("verdict", f)
            self.assertIn("reason", f)


class TestNonGitRepo(unittest.TestCase):
    def setUp(self):
        if not git_available():
            self.skipTest("git is not available")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_a_git_repo_is_usage_error(self):
        env = dict(os.environ)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        p = subprocess.run([sys.executable, SKYDAS_PY, "check", "--repo", self.tmp],
                          capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)


class TestUnitHelpers(unittest.TestCase):
    """Direct unit tests of the pure helpers, no subprocess needed."""

    def test_expand_path_substitutes_project_dir(self):
        got = sk.expand_path("$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh", "/repo")
        self.assertEqual(got, "/repo/.claude/hooks/x.sh")

    def test_expand_path_relative_join(self):
        got = sk.expand_path(".claude/hooks/x.sh", "/repo")
        self.assertEqual(got, "/repo/.claude/hooks/x.sh")

    def test_path_like(self):
        self.assertTrue(sk.path_like("$CLAUDE_PROJECT_DIR/x.sh"))
        self.assertTrue(sk.path_like("./x.sh"))
        self.assertFalse(sk.path_like("bash"))
        self.assertFalse(sk.path_like("-c"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
