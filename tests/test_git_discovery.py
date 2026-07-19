"""Real-Git tests for the constitutional multi-repository discovery rules."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import constitution as c


def git(repo: Path | None, *args: str, env: dict | None = None) -> str:
    command = ["git"]
    if repo is not None:
        command += ["-C", str(repo)]
    command += list(args)
    merged_env = os.environ.copy()
    merged_env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.test",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "author@example.test",
    })
    if env:
        merged_env.update(env)
    result = subprocess.run(
        command,
        env=merged_env,
        text=True,
        input="",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_remote(tmp_path: Path, name: str) -> tuple[Path, Path]:
    remote = tmp_path / f"{name}.git"
    work = tmp_path / f"{name}-work"
    git(None, "init", "--bare", str(remote))
    git(None, "init", "-b", "main", str(work))
    git(work, "config", "user.name", "Test Author")
    git(work, "config", "user.email", "author@example.test")
    git(work, "remote", "add", "origin", str(remote))
    return remote, work


def commit_file(
    work: Path,
    name: str,
    content: str | bytes,
    message: str,
    timestamp: int,
    *,
    email: str = "author@example.test",
) -> str:
    path = work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    git(work, "add", "--", name)
    date = f"@{timestamp} +0000"
    return git(
        work,
        "commit",
        "-m",
        message,
        env={
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        },
    ) and git(work, "rev-parse", "HEAD")


def push(work: Path, *refs: str) -> None:
    git(work, "push", "--force", "origin", *refs)


@pytest.fixture
def discovery_config(tmp_path, monkeypatch):
    db = c.RocksDb.open(tmp_path / "ledger.rocks")
    monkeypatch.setattr(c, "state_db", db)
    monkeypatch.setattr(c, "GIT_MIRROR_DIR", tmp_path / "mirrors")
    monkeypatch.setattr(c, "GENESIS_MS", 1_000_000)
    monkeypatch.setattr(c, "CONTRIBUTORS", {
        "alice": ["author@example.test"],
        "bob": ["bob@example.test"],
    })
    yield tmp_path
    db.close()


def configure(monkeypatch, repositories):
    monkeypatch.setattr(c, "REPOSITORIES", repositories)


def test_ref_patterns_distinguish_nested_branches():
    shallow = c._ref_pattern_regex("refs/heads/*")
    recursive = c._ref_pattern_regex("refs/heads/**")
    assert shallow.fullmatch("refs/heads/main")
    assert not shallow.fullmatch("refs/heads/team/topic")
    assert recursive.fullmatch("refs/heads/team/topic")


def test_manifest_order_does_not_change_digest(monkeypatch):
    repos = [
        {"id": "b", "url": "/b", "refs": ["refs/heads/z", "refs/heads/a"]},
        {"id": "a", "url": "/a", "refs": ["refs/heads/**"]},
    ]
    configure(monkeypatch, repos)
    monkeypatch.setattr(c, "CONTRIBUTORS", {"alice": ["A@EXAMPLE.TEST"]})
    first = c._config_digest(c._normalized_discovery_config())
    configure(monkeypatch, list(reversed(repos)))
    second = c._config_digest(c._normalized_discovery_config())
    assert first == second


@pytest.mark.parametrize(
    "repositories",
    [
        [{"id": "../escape", "url": "/x", "refs": ["refs/heads/main"]}],
        [{"id": "x", "url": "", "refs": ["refs/heads/main"]}],
        [{"id": "x", "url": "/x", "refs": []}],
        [
            {"id": "x", "url": "/x", "refs": ["refs/heads/main"]},
            {"id": "x", "url": "/y", "refs": ["refs/heads/main"]},
        ],
    ],
)
def test_invalid_manifests_fail(repositories, monkeypatch):
    configure(monkeypatch, repositories)
    monkeypatch.setattr(c, "CONTRIBUTORS", {"alice": ["a@example.test"]})
    with pytest.raises(ValueError):
        c._normalized_discovery_config()


def test_bootstrap_genesis_nested_refs_and_exclusions(discovery_config, monkeypatch):
    remote, work = make_remote(discovery_config, "one")
    old_oid = commit_file(work, "old.txt", "old", "before genesis", 999)
    new_oid = commit_file(work, "new.txt", "new", "after genesis", 1001)
    git(work, "branch", "team/topic")
    push(work, "main", "team/topic")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/**"],
    }])

    event = c._build_discovery(0, c.GENESIS_MS, [])
    observations = {row["oid"].split(":", 1)[1]: row for row in event.observations}
    assert observations[old_oid]["exclusion_reason"] == "before_genesis"
    assert observations[new_oid]["eligible"]
    assert {source["ref_name"] for source in observations[new_oid]["first_sources"]} == {
        "refs/heads/main", "refs/heads/team/topic",
    }
    assert event.commits[0]["contributor"] == "alice"


def test_first_reachable_ignores_old_timestamp_after_bootstrap(
    discovery_config, monkeypatch
):
    remote, work = make_remote(discovery_config, "one")
    commit_file(work, "base.txt", "base", "base", 1001)
    push(work, "main")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])
    first = c._build_discovery(0, c.GENESIS_MS, [])

    git(work, "checkout", "-b", "hidden")
    old_dated_oid = commit_file(work, "late.txt", "late", "old dated", 500)
    git(work, "checkout", "main")
    git(work, "merge", "--ff-only", "hidden")
    push(work, "main")

    second = c._build_discovery(1, c.GENESIS_MS + 1, [first])
    row = next(
        row for row in second.observations
        if row["oid"].endswith(old_dated_oid)
    )
    assert row["eligible"]


def test_force_push_removal_and_reintroduction_never_reattributes(
    discovery_config, monkeypatch
):
    remote, work = make_remote(discovery_config, "one")
    base = commit_file(work, "base.txt", "base", "base", 1001)
    extra = commit_file(work, "extra.txt", "extra", "extra", 1002)
    push(work, "main")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])
    first = c._build_discovery(0, c.GENESIS_MS, [])

    git(work, "reset", "--hard", base)
    push(work, "main")
    second = c._build_discovery(1, c.GENESIS_MS + 1, [first])
    assert second.observations == []

    git(work, "reset", "--hard", extra)
    push(work, "main")
    third = c._build_discovery(2, c.GENESIS_MS + 2, [first, second])
    assert third.observations == []


def test_global_oid_and_cherry_pick_patch_dedup(discovery_config, monkeypatch):
    remote_a, work_a = make_remote(discovery_config, "a")
    commit_file(work_a, "base.txt", "base", "base", 1001)
    shared = commit_file(work_a, "feature.txt", "feature\n", "feature", 1002)
    push(work_a, "main")

    remote_b = discovery_config / "b.git"
    git(None, "clone", "--bare", str(remote_a), str(remote_b))
    work_b = discovery_config / "b-work"
    git(None, "clone", str(remote_b), str(work_b))
    git(work_b, "config", "user.name", "Bob")
    git(work_b, "config", "user.email", "bob@example.test")

    configure(monkeypatch, [
        {"id": "a", "url": str(remote_a), "refs": ["refs/heads/main"]},
        {"id": "b", "url": str(remote_b), "refs": ["refs/heads/main"]},
    ])
    first = c._build_discovery(0, c.GENESIS_MS, [])
    shared_rows = [r for r in first.observations if r["oid"].endswith(shared)]
    assert len(shared_rows) == 1
    assert len(shared_rows[0]["first_sources"]) == 2

    git(work_b, "checkout", "-b", "copy", f"{shared}^")
    git(
        work_b,
        "cherry-pick",
        shared,
        env={
            "GIT_COMMITTER_NAME": "Bob",
            "GIT_COMMITTER_EMAIL": "bob@example.test",
            "GIT_COMMITTER_DATE": "@1003 +0000",
        },
    )
    copied = git(work_b, "rev-parse", "HEAD")
    git(work_b, "branch", "-f", "main", copied)
    push(work_b, "main")

    second = c._build_discovery(1, c.GENESIS_MS + 1, [first])
    copied_row = next(r for r in second.observations if r["oid"].endswith(copied))
    assert copied_row["exclusion_reason"] == "duplicate_patch"
    assert copied_row["canonical_patch_oid"].endswith(shared)
    assert second.commits == []


def test_merge_empty_unknown_and_binary_are_explicit(discovery_config, monkeypatch):
    remote, work = make_remote(discovery_config, "one")
    commit_file(work, "base.txt", "base", "base", 1001)
    git(work, "checkout", "-b", "feature")
    commit_file(work, "binary.bin", b"\x00\x01\xff", "binary", 1002)
    git(work, "checkout", "main")
    commit_file(work, "main.txt", "main", "main", 1003)
    git(
        work,
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "merge",
        env={"GIT_AUTHOR_DATE": "@1004 +0000", "GIT_COMMITTER_DATE": "@1004 +0000"},
    )
    git(
        work,
        "commit",
        "--allow-empty",
        "-m",
        "empty",
        env={"GIT_AUTHOR_DATE": "@1005 +0000", "GIT_COMMITTER_DATE": "@1005 +0000"},
    )
    commit_file(
        work, "unknown.txt", "unknown", "unknown", 1006,
        email="unknown@example.test",
    )
    push(work, "main")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])

    event = c._build_discovery(0, c.GENESIS_MS, [])
    reasons = {row["exclusion_reason"] for row in event.observations}
    assert {"merge_commit", "empty_commit", "unknown_contributor"} <= reasons
    binary = next(row for row in event.commits if row["message"] == "binary")
    assert binary["patch_identity"]
    assert binary["patch_sha256"] == hashlib.sha256(
        binary["patch"].encode()
    ).hexdigest()


def test_repository_failure_does_not_return_partial_snapshot(
    discovery_config, monkeypatch
):
    remote, work = make_remote(discovery_config, "good")
    commit_file(work, "file.txt", "ok", "ok", 1001)
    push(work, "main")
    configure(monkeypatch, [
        {"id": "good", "url": str(remote), "refs": ["refs/heads/main"]},
        {
            "id": "missing",
            "url": str(discovery_config / "missing.git"),
            "refs": ["refs/heads/main"],
        },
    ])
    with pytest.raises(RuntimeError):
        c._build_discovery(0, c.GENESIS_MS, [])


def test_git_replace_refs_cannot_falsify_discovered_commit(
    discovery_config, monkeypatch
):
    remote, work = make_remote(discovery_config, "one")
    original = commit_file(work, "file.txt", "original", "original", 1001)
    replacement = commit_file(work, "file.txt", "replacement", "replacement", 1002)
    git(work, "replace", original, replacement)
    git(work, "reset", "--hard", original)
    push(work, "main", f"refs/replace/{original}")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])

    event = c._build_discovery(0, c.GENESIS_MS, [])
    discovered = next(row for row in event.commits if row["oid"].endswith(original))
    assert discovered["message"] == "original"
    assert "original" in discovered["patch"]
    assert "replacement" not in discovered["patch"]


def test_replay_is_idempotent_and_path_independent(discovery_config, monkeypatch):
    remote, work = make_remote(discovery_config, "one")
    commit_file(work, "file.txt", "ok", "ok", 1001)
    push(work, "main")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])
    first = c._build_discovery(0, c.GENESIS_MS, [])
    second = c._build_discovery(1, c.GENESIS_MS + 1, [first])
    assert second.observations == []
    assert second.commits == []
    assert first.repositories[0]["reachable_set_sha256"] == (
        second.repositories[0]["reachable_set_sha256"]
    )


def test_git_timeout_is_reported_without_partial_result(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git", "fetch"], 1)

    monkeypatch.setattr(c.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        c._git(tmp_path, "fetch")


def test_concurrent_same_epoch_discovery_appends_once(
    discovery_config, monkeypatch
):
    remote, work = make_remote(discovery_config, "one")
    commit_file(work, "file.txt", "ok", "ok", 1001)
    push(work, "main")
    configure(monkeypatch, [{
        "id": "one", "url": str(remote), "refs": ["refs/heads/main"],
    }])
    async def run_both():
        return await asyncio.gather(
            c.discover_repositories(0, c.GENESIS_MS),
            c.discover_repositories(0, c.GENESIS_MS),
        )

    left, right = asyncio.run(run_both())
    assert left.snapshot_id == right.snapshot_id
    discoveries = [
        c._typed(row) for _, row in c.ROOT.discoveries_by_epoch.iter(c._db())
    ]
    assert len(discoveries) == 1


def test_empty_epoch_records_zero_emission_without_burning_pool(
    discovery_config, monkeypatch
):
    async def discover(_epoch, _boundary):
        return SimpleNamespace(
            observations=[], commits=[], snapshot_id="empty-snapshot"
        )

    async def rank(_commits, *, epoch=-1):
        return {}, [], {"ranking_run_id": "", "ranking_event_id": ""}

    monkeypatch.setattr(c, "discover_repositories", discover)
    monkeypatch.setattr(c, "rank_commits", rank)
    entry = asyncio.run(c.run_emission(0, c.GENESIS_MS))
    assert c.Decimal(entry.total_emitted) == 0
    assert entry.pool_before == entry.pool_after
    assert entry.distributions == {}


def test_emission_distribution_sums_exactly_to_total(
    discovery_config, monkeypatch
):
    async def discover(_epoch, _boundary):
        return SimpleNamespace(
            observations=[{"x": 1}],
            commits=[{"x": 1}],
            snapshot_id="ranked-snapshot",
        )

    async def rank(_commits, *, epoch=-1):
        return {
            "alice": c.Decimal("0.33333333333333333333333333333333333333333333333333"),
            "bob": c.Decimal("0.66666666666666666666666666666666666666666666666667"),
        }, ["model"], {"ranking_run_id": "r", "ranking_event_id": "e"}

    monkeypatch.setattr(c, "discover_repositories", discover)
    monkeypatch.setattr(c, "rank_commits", rank)
    entry = asyncio.run(c.run_emission(0, c.GENESIS_MS))
    distributed = sum(c.Decimal(x) for x in entry.distributions.values())
    assert distributed == c.Decimal(entry.total_emitted)
    assert entry.discovery_snapshot_id == "ranked-snapshot"


def test_single_commit_ranking_skips_pairwise(
    discovery_config, monkeypatch,
):
    async def models(n=3):
        return []

    monkeypatch.setattr(c, "fetch_top_models", models)
    ranking, used, info = asyncio.run(c.rank_commits([{
        "contributor": "alice",
        "oid": "sha1:" + "a" * 40,
        "message": "one contribution",
        "patch": "patch",
    }], epoch=0))
    assert ranking == {"alice": c.Decimal("1")}
    assert used == []
    assert info["ranking_event_id"]


def test_same_contributor_multiple_commits_runs_pairwise(
    discovery_config, monkeypatch,
):
    monkeypatch.setattr(c, "PREFERRED_COUNCIL_MODELS", [])
    calls = {"n": 0}

    async def models(n=3):
        return ["m1", "m2", "m3"]

    async def compare(model_id, side_a, side_b, **kwargs):
        calls["n"] += 1
        assert "commit_id" in side_a and "commit_id" in side_b
        if kwargs.get("persist"):
            attempt_id = c.attempt_id_for(kwargs["comparison_id"], model_id, 1)
            await c.append_evidence(kwargs["epoch"], "llm.judgment", {
                "judgment_id": c.judgment_id_for({
                    "attempt_id": attempt_id,
                    "comparison_id": kwargs["comparison_id"],
                    "model_id": model_id,
                    "winner": "A",
                    "ratio": "2:1",
                    "explanation": "ok",
                }),
                "attempt_id": attempt_id,
                "comparison_id": kwargs["comparison_id"],
                "model_id": model_id,
                "winner": "A",
                "ratio": "2:1",
                "explanation": "ok",
                "summary": "ok",
            })
        return {"winner": "A", "ratio": "2:1", "explanation": "ok"}

    monkeypatch.setattr(c, "fetch_top_models", models)
    monkeypatch.setattr(c, "llm_pairwise_compare", compare)
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "test-key")
    commits = [
        {
            "contributor": "alice",
            "oid": "sha1:" + char * 40,
            "message": f"msg-{char}",
            "patch": f"patch-{char}",
        }
        for char in ("a", "b", "c")
    ]
    ranking, used, info = asyncio.run(c.rank_commits(commits, epoch=0))
    assert set(ranking) == {"alice"}
    assert ranking["alice"] > 0
    assert used == ["m1", "m2", "m3"]
    assert calls["n"] >= 3
    completed = c._evidence_for_kind("ranking.completed")[0]
    assert len(completed.payload["commit_ranking"]) == 3
    assert "alice" in completed.payload["contributor_ranking"]
    assert info["ranking_event_id"]


def test_all_council_failures_abort_ranking(discovery_config, monkeypatch):
    monkeypatch.setattr(c, "PREFERRED_COUNCIL_MODELS", [])

    async def models(n=3):
        return ["broken"]

    async def compare(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(c, "fetch_top_models", models)
    monkeypatch.setattr(c, "llm_pairwise_compare", compare)
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "test-key")
    commits = [
        {
            "contributor": contributor,
            "oid": "sha1:" + char * 40,
            "message": contributor,
            "patch": "patch",
        }
        for contributor, char in [("alice", "a"), ("bob", "b")]
    ]
    with pytest.raises(RuntimeError, match="council model failed"):
        asyncio.run(c.rank_commits(commits, epoch=0))


def test_one_council_failure_retires_model_and_continues(discovery_config, monkeypatch):
    monkeypatch.setattr(c, "PREFERRED_COUNCIL_MODELS", [])

    async def models(n=3):
        return ["broken", "solid"]

    async def compare(model_id, side_a, side_b, **kwargs):
        if model_id == "broken":
            raise RuntimeError("model unavailable")
        if kwargs.get("persist"):
            attempt_id = c.attempt_id_for(kwargs["comparison_id"], model_id, 1)
            await c.append_evidence(kwargs["epoch"], "llm.judgment", {
                "judgment_id": c.judgment_id_for({
                    "attempt_id": attempt_id,
                    "comparison_id": kwargs["comparison_id"],
                    "model_id": model_id,
                    "winner": "A",
                    "ratio": "2:1",
                    "explanation": "ok",
                }),
                "attempt_id": attempt_id,
                "comparison_id": kwargs["comparison_id"],
                "model_id": model_id,
                "winner": "A",
                "ratio": "2:1",
                "explanation": "ok",
                "summary": "ok",
            })
        return {"winner": "A", "ratio": "2:1", "explanation": "ok"}

    monkeypatch.setattr(c, "fetch_top_models", models)
    monkeypatch.setattr(c, "llm_pairwise_compare", compare)
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "test-key")
    commits = [
        {
            "contributor": contributor,
            "oid": "sha1:" + char * 40,
            "message": contributor,
            "patch": "patch",
        }
        for contributor, char in [("alice", "a"), ("bob", "b")]
    ]
    ranking, used, _info = asyncio.run(c.rank_commits(commits, epoch=0))
    assert set(ranking) == {"alice", "bob"}
    assert used == ["solid"]
    retired = c._evidence_for_kind("llm.council_member_failed")
    assert len(retired) == 1
    assert retired[0].payload["model_id"] == "broken"
    completed = c._evidence_for_kind("ranking.completed")[0]
    assert completed.payload["models_failed"] == ["broken"]


def test_multi_commit_ranking_requires_openrouter_key(monkeypatch):
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "")
    commits = [
        {
            "contributor": "alice",
            "oid": "sha1:" + char * 40,
            "message": char,
            "patch": "patch",
        }
        for char in ("a", "b")
    ]
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        asyncio.run(c.rank_commits(commits))


def test_watch_page_has_live_controls_progress_and_key_warning(
    discovery_config, monkeypatch
):
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(c, "current_epoch", lambda: (3, 0, 1))
    response = asyncio.run(c.watch())
    html = response.body.decode()
    assert 'role="progressbar"' in html
    assert 'id="play"' in html
    assert 'id="pause"' in html
    assert "new EventSource('/sse')" in html
    assert "OpenRouter key missing" in html


def test_audit_events_are_json_sse_and_update_process_state(monkeypatch):
    clients = []
    history = []
    monkeypatch.setattr(c, "SSE_CLIENTS", clients)
    monkeypatch.setattr(c, "AUDIT_HISTORY", history)
    queue = asyncio.Queue()
    clients.append(queue)

    async def emit():
        event = await c.broadcast_audit(
            "progress", "halfway", progress=50, phase="ranking"
        )
        return event, await queue.get()

    event, wire = asyncio.run(emit())
    assert event["progress"] == 50
    assert event["phase"] == "ranking"
    assert wire.startswith("event: audit\ndata: {")
    assert '"message":"halfway"' in wire
