"""State-machine checks for discovery under changing real Git refs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

import constitution as c


def git(repo: Path | None, *args: str, timestamp: int | None = None) -> str:
    command = ["git"]
    if repo is not None:
        command += ["-C", str(repo)]
    command += list(args)
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "State Machine",
        "GIT_AUTHOR_EMAIL": "state@example.test",
        "GIT_COMMITTER_NAME": "State Machine",
        "GIT_COMMITTER_EMAIL": "state@example.test",
    })
    if timestamp is not None:
        env["GIT_AUTHOR_DATE"] = f"@{timestamp} +0000"
        env["GIT_COMMITTER_DATE"] = f"@{timestamp} +0000"
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitDiscoveryMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="constitution-stateful-"))
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        git(None, "init", "--bare", str(self.remote))
        git(None, "init", "-b", "main", str(self.work))
        git(self.work, "config", "user.name", "State Machine")
        git(self.work, "config", "user.email", "state@example.test")
        git(self.work, "remote", "add", "origin", str(self.remote))

        self.original = (
            c.REPOSITORIES, c.CONTRIBUTORS, c.GIT_MIRROR_DIR, c.GENESIS_MS,
        )
        c.REPOSITORIES = [{
            "id": "state",
            "url": str(self.remote),
            "refs": ["refs/heads/main"],
        }]
        c.CONTRIBUTORS = {"state": ["state@example.test"]}
        c.GIT_MIRROR_DIR = self.root / "mirrors"
        c.GENESIS_MS = 1_000_000

        self.counter = 0
        self.events = []
        self.removed_tip: str | None = None
        self._commit_and_push()

    def teardown(self):
        (
            c.REPOSITORIES, c.CONTRIBUTORS, c.GIT_MIRROR_DIR, c.GENESIS_MS,
        ) = self.original
        shutil.rmtree(self.root, ignore_errors=True)

    def _commit_and_push(self):
        self.counter += 1
        path = self.work / f"file-{self.counter}.txt"
        path.write_text(f"value {self.counter}\n")
        git(self.work, "add", "--", path.name)
        git(
            self.work,
            "commit",
            "-m",
            f"commit {self.counter}",
            timestamp=1000 + self.counter,
        )
        git(self.work, "push", "--force", "origin", "main")

    @rule()
    def advance_branch(self):
        if self.removed_tip is None:
            self._commit_and_push()

    @rule()
    def scan(self):
        event = c._build_discovery(
            len(self.events), c.GENESIS_MS + len(self.events), self.events
        )
        self.events.append(event)

    @precondition(lambda self: self.removed_tip is None)
    @rule()
    def force_push_back(self):
        if self.counter < 2:
            return
        self.removed_tip = git(self.work, "rev-parse", "HEAD")
        git(self.work, "reset", "--hard", "HEAD^")
        git(self.work, "push", "--force", "origin", "main")

    @precondition(lambda self: self.removed_tip is not None)
    @rule()
    def restore_force_pushed_tip(self):
        git(self.work, "reset", "--hard", self.removed_tip)
        git(self.work, "push", "--force", "origin", "main")
        self.removed_tip = None

    @invariant()
    def observations_are_monotonic_and_unique(self):
        observed = [
            row["oid"]
            for event in self.events
            for row in event.observations
        ]
        assert len(observed) == len(set(observed))

    @invariant()
    def patch_classes_are_ranked_at_most_once(self):
        patch_ids = [
            commit["patch_identity"]
            for event in self.events
            for commit in event.commits
        ]
        assert len(patch_ids) == len(set(patch_ids))

    @invariant()
    def replay_matches_accumulated_state(self):
        seen_oids, seen_patches = c._replayed_discovery_state(self.events)
        expected_oids = {
            row["oid"] for event in self.events for row in event.observations
        }
        assert seen_oids == expected_oids
        assert all(oid in seen_oids for oid in seen_patches.values())


TestGitDiscoveryStateMachine = GitDiscoveryMachine.TestCase
TestGitDiscoveryStateMachine.settings = settings(
    max_examples=8,
    stateful_step_count=12,
    deadline=None,
)
