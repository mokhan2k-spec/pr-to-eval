"""A15 fix: Codex runs with its own CODEX_HOME, so the host's ~/.codex/AGENTS.md never loads."""
import json

import pytest

from harness import agent_runs

from conftest import good_probe_record


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "codex-clean"
    h.mkdir()
    (h / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(h))
    return h


def test_unset_home_is_a_problem(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert agent_runs.codex_home_mode() is None
    assert any("not set" in p for p in agent_runs.codex_home_problems())


def test_clean_home_has_no_problems(home):
    assert agent_runs.codex_home_mode() == "isolated"
    assert agent_runs.codex_home_problems() == []


def test_home_must_be_a_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert any("not a directory" in p for p in agent_runs.codex_home_problems())


@pytest.mark.parametrize("name", ["AGENTS.md", "config.toml", "RTK.md", "skills", "Instructions.md"])
def test_home_with_host_rules_is_refused(home, name):
    (home / name).write_text("x", encoding="utf-8")
    assert any(name in p for p in agent_runs.codex_home_problems())


def test_home_without_login_is_refused(home):
    (home / "auth.json").unlink()
    assert any("auth.json" in p for p in agent_runs.codex_home_problems())


def test_host_codex_dir_is_not_isolated(tmp_path, monkeypatch):
    fake_home = tmp_path / "user"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(agent_runs.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    assert any("host's ~/.codex" in p for p in agent_runs.codex_home_problems())


def test_env_passes_home_to_codex_only(home):
    assert agent_runs.agent_env("codex")["CODEX_HOME"] == str(home)
    assert "CODEX_HOME" not in agent_runs.agent_env("claude")


def test_isolated_policy_has_its_own_digest(home):
    default = agent_runs.policy_digest("codex")
    isolated = agent_runs.policy_digest("codex", codex_home="isolated")
    assert isolated != default
    assert agent_runs.policy_digest("claude", codex_home="isolated") == agent_runs.policy_digest("claude")


def test_binding_records_the_home_mode(home, monkeypatch):
    monkeypatch.setattr(agent_runs, "cli_version", lambda agent, exe=None: "fake-cli 1.0")
    b = agent_runs.probe_binding("codex")
    assert b["codex_home"] == "isolated"
    assert b["policy_digest"] == agent_runs.policy_digest("codex", codex_home="isolated")
    assert agent_runs.probe_binding("claude")["codex_home"] is None


def _write_probe(runs, rec):
    out = runs / agent_runs.BOUNDARY_DIR
    out.mkdir(parents=True)
    (out / "probe.json").write_text(json.dumps({"agents": {"codex": rec}}), encoding="utf-8")


def test_gate_refuses_codex_without_an_isolated_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(agent_runs, "cli_version", lambda agent, exe=None: "fake-cli 1.0")
    runs = tmp_path / "runs"
    _write_probe(runs, good_probe_record("codex", "fake-cli 1.0"))
    _, problems = agent_runs.probe_gate(runs, "codex")
    assert any("CODEX_HOME is not set" in p for p in problems)


def test_gate_refuses_a_probe_made_on_the_host_home(home, tmp_path, monkeypatch):
    """A probe recorded without CODEX_HOME cannot certify isolated runs."""
    monkeypatch.setattr(agent_runs, "cli_version", lambda agent, exe=None: "fake-cli 1.0")
    runs = tmp_path / "runs"
    _write_probe(runs, good_probe_record("codex", "fake-cli 1.0"))
    _, problems = agent_runs.probe_gate(runs, "codex")
    assert any("policy_digest is stale" in p for p in problems)


def test_gate_accepts_an_isolated_probe(home, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runs, "cli_version", lambda agent, exe=None: "fake-cli 1.0")
    runs = tmp_path / "runs"
    rec = good_probe_record("codex", "fake-cli 1.0")
    rec["_binding"] = agent_runs.probe_binding("codex")
    _write_probe(runs, rec)
    _, problems = agent_runs.probe_gate(runs, "codex")
    assert problems == []


def _prev(tmp_path, meta_home):
    d = tmp_path / "attempt-1"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"status": "complete", "codex_home": meta_home}),
                                 encoding="utf-8")
    (d / "classification.json").write_text(json.dumps(
        {"confirmed": True, "outcome": "pass", "host_configured": {"reason": "A15"}}), encoding="utf-8")
    return d


def test_host_configured_attempt_may_be_rerun_isolated(tmp_path):
    assert agent_runs.retry_allowed(_prev(tmp_path, None)) == (True, "")


def test_isolated_attempt_is_final(tmp_path):
    ok, why = agent_runs.retry_allowed(_prev(tmp_path, "isolated"))
    assert not ok and "final" in why


def test_stock_folders_codex_writes_itself_are_allowed(home):
    (home / "skills" / ".system" / "imagegen").mkdir(parents=True)
    (home / "plugins" / "cache" / "openai-curated-remote").mkdir(parents=True)
    (home / "plugins" / ".remote-plugin-install-staging").mkdir()
    assert agent_runs.codex_home_problems() == []


@pytest.mark.parametrize("extra", ["skills/my-skill", "plugins/cache/my-plugin", "plugins/mine"])
def test_own_skills_or_plugins_are_refused(home, extra):
    (home / "skills" / ".system").mkdir(parents=True)
    (home / extra).mkdir(parents=True, exist_ok=True)
    assert any("load host rules" in p for p in agent_runs.codex_home_problems())


def _attempt(trial, n, codex_home, cls):
    d = trial / f"attempt-{n}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"status": "complete", "agent": "codex",
                                             "codex_home": codex_home}), encoding="utf-8")
    (d / "classification.json").write_text(json.dumps({"confirmed": True, **cls}), encoding="utf-8")
    return d


HOST = {"outcome": "pass", "host_configured": {"reason": "A15"}}
ENV = {"outcome": "fail", "failure_source": "environment"}


def test_superseded_host_run_does_not_use_up_an_attempt(tmp_path):
    """attempt-1 on the host home, attempt-2 isolated but an environment failure: one more."""
    trial = agent_runs.trial_dir(tmp_path, "t", "codex", 1)
    _attempt(trial, 1, None, HOST)
    _attempt(trial, 2, "isolated", ENV)
    assert agent_runs.counted_attempts(agent_runs.list_attempts(trial)) == 1
    assert agent_runs.plan_attempt(tmp_path, "t", "codex", 1) == (3, "")


def test_isolated_attempts_still_capped(tmp_path):
    trial = agent_runs.trial_dir(tmp_path, "t", "codex", 1)
    _attempt(trial, 1, None, HOST)
    _attempt(trial, 2, "isolated", ENV)
    _attempt(trial, 3, "isolated", ENV)
    n, why = agent_runs.plan_attempt(tmp_path, "t", "codex", 1)
    assert n is None and "max" in why


def test_isolated_host_flag_still_counts(tmp_path):
    """Only a run WITHOUT an isolated home is a superseded host run."""
    trial = agent_runs.trial_dir(tmp_path, "t", "codex", 1)
    _attempt(trial, 1, "isolated", ENV)
    _attempt(trial, 2, "isolated", ENV)
    assert agent_runs.counted_attempts(agent_runs.list_attempts(trial)) == 2
    assert agent_runs.plan_attempt(tmp_path, "t", "codex", 1)[0] is None


def _trust(home, path):
    (home / "config.toml").write_text(f"[projects.'{path}']\ntrust_level = \"trusted\"\n",
                                      encoding="utf-8")


def test_codex_trust_entries_for_our_workspaces_are_allowed(home):
    _trust(home, str(agent_runs.runs_root() / "t" / "codex" / "trial-1" / "attempt-1-ab").lower())
    assert agent_runs.codex_home_problems() == []


@pytest.mark.parametrize("body", [
    "[projects.'c:\\users\\x\\elsewhere']\ntrust_level = \"trusted\"\n",
    "model = \"gpt-4\"\n",
    "developer_instructions = \"be sneaky\"\n",
])
def test_any_other_config_is_refused(home, body):
    (home / "config.toml").write_text(body, encoding="utf-8")
    assert any("config.toml" in p for p in agent_runs.codex_home_problems())
