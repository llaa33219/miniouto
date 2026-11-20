"""The listing commands emit YAML — the machine interface for CLI wrappers.

Every list/status command prints one YAML document on stdout and nothing
else (no banner, no rich markup), so a third-party wrapper can rely on
`yaml.safe_load(stdout)`. These tests pin each command's schema and the
stdout/stderr separation: data on stdout, errors as text on stderr with a
non-zero exit.
"""

from __future__ import annotations

import uuid

import yaml
from typer.testing import CliRunner

from miniouto.cli import app as root_app
from miniouto.cli import message as message_mod
from miniouto.cli import provider as provider_mod
from miniouto.cli import skill as skill_mod
from miniouto.cli import style as style_mod
from miniouto.cli import subagent as subagent_mod
from miniouto.core import inbox
from miniouto.storage import providers as provider_store
from miniouto.storage import settings as settings_store
from miniouto.storage import skills as skill_store

runner = CliRunner()


def _session() -> str:
    return f"yaml-cli-{uuid.uuid4().hex[:8]}"


def _load(output: str) -> dict:
    """Parse the command's stdout as exactly one YAML document."""

    docs = list(yaml.safe_load_all(output))
    assert len(docs) == 1, f"expected exactly one YAML document, got {len(docs)}"
    assert isinstance(docs[0], dict)
    return docs[0]


# ── the shared emitter ───────────────────────────────────────────────────────


def test_emit_yaml_key_order_is_preserved_and_multiline_becomes_block_scalar() -> None:
    # Given: a payload with schema-ordered keys and a multiline body
    from miniouto.cli._yaml import emit_yaml

    # When: it is emitted
    text = emit_yaml({"session": "s", "messages": [{"id": "a", "text": "one\ntwo"}]})

    # Then: keys stay in insertion order and the body is a literal block scalar
    assert text.index("session:") < text.index("messages:")
    assert "text: |" in text
    assert yaml.safe_load(text)["messages"][0]["text"] == "one\ntwo"


def test_emit_yaml_outputs_plain_unicode() -> None:
    # Given: non-ASCII text
    from miniouto.cli._yaml import emit_yaml

    # When: it is emitted
    text = emit_yaml({"s": "버전 — ✓"})

    # Then: the characters are not escaped
    assert "버전 — ✓" in text
    assert yaml.safe_load(text) == {"s": "버전 — ✓"}


# ── provider providers / models (catalog, monkeypatched) ─────────────────────

_CATALOG = [
    {
        "name": "Anthropic",
        "slug": "anthropic",
        "formats": [
            {"format": "openai", "baseUrl": "https://api.anthropic.com/v1"},
            {"format": "anthropic", "baseUrl": "https://api.anthropic.com"},
        ],
    },
    {"name": "Unhostable", "slug": "unhostable", "formats": [{"format": "typesafe"}]},
]

_MODELS = [{"id": "claude-a", "name": "Claude A"}, {"id": "claude-b", "name": "Claude B"}]


def test_provider_providers_emits_the_catalog_schema(monkeypatch) -> None:
    # Given: a catalog with one hostable and one unhostable provider
    monkeypatch.setattr(provider_mod.catalog_api, "list_providers", lambda: _CATALOG)

    # When: the catalog index is listed
    result = runner.invoke(provider_mod.app, ["providers"])

    # Then: stdout is one YAML document with per-provider rows
    assert result.exit_code == 0
    doc = _load(result.output)
    assert list(doc) == ["providers"]
    hostable, unhostable = doc["providers"]
    assert list(hostable) == [
        "name", "slug", "format", "base_url", "compatibility", "addable",
    ]
    assert hostable["name"] == "Anthropic"
    assert hostable["slug"] == "anthropic"
    assert hostable["format"] == "openai"
    assert hostable["base_url"] == "https://api.anthropic.com/v1"
    assert hostable["compatibility"] == ["openai", "anthropic"]
    assert hostable["addable"] is True
    assert unhostable["format"] == ""
    assert unhostable["compatibility"] == []
    assert unhostable["addable"] is False


def test_provider_providers_failure_goes_to_stderr_with_exit_one(monkeypatch) -> None:
    # Given: a catalog that cannot be reached
    def boom() -> list:
        raise RuntimeError("no route to host")

    monkeypatch.setattr(provider_mod.catalog_api, "list_providers", boom)

    # When: the catalog index is listed
    result = runner.invoke(provider_mod.app, ["providers"])

    # Then: stdout stays empty, the ✗ message is stderr text, exit 1
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Failed to reach catalog" in result.output
    assert "no route to host" in result.output


def test_provider_models_emits_id_name_rows(monkeypatch) -> None:
    # Given: a provider with two catalog models
    monkeypatch.setattr(provider_mod.catalog_api, "list_models", lambda name: list(_MODELS))
    monkeypatch.setattr(provider_mod.catalog_api, "find_provider", lambda name: _CATALOG[0])

    # When: its models are listed
    result = runner.invoke(provider_mod.app, ["models", "anthropic"])

    # Then: the resolved provider name heads the document
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["provider"] == "Anthropic"
    assert doc["models"] == [
        {"id": "claude-a", "name": "Claude A"},
        {"id": "claude-b", "name": "Claude B"},
    ]


def test_provider_models_empty_result_is_a_stderr_error(monkeypatch) -> None:
    # Given: a provider the catalog knows no models for
    monkeypatch.setattr(provider_mod.catalog_api, "list_models", lambda name: [])

    # When: its models are listed
    result = runner.invoke(provider_mod.app, ["models", "nobody"])

    # Then: no partial YAML on stdout — a clean exit-1 error
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "No models returned" in result.output


# ── provider list (storage) ──────────────────────────────────────────────────


def test_provider_list_reports_configured_providers() -> None:
    # Given: a custom and a catalog provider, the catalog one default
    provider_store.upsert(
        provider_store.Provider(
            name="local",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            default_model="m-1",
        )
    )
    provider_store.upsert(
        provider_store.Provider(
            name="kimi",
            api_format="openai",
            base_url="https://api.moonshot.cn/v1",
            default_model="kimi-k2",
            source="modelslist",
            catalog_dialect="openai",
            reasoning_effort="high",
        )
    )
    settings_store.save(settings_store.Settings(provider="kimi"))

    # When: the configured providers are listed
    result = runner.invoke(provider_mod.app, ["list"])

    # Then: every row carries the schema fields and the default flag
    assert result.exit_code == 0
    doc = _load(result.output)
    rows = {row["name"]: row for row in doc["providers"]}
    assert list(doc) == ["providers"]
    assert list(rows["kimi"]) == [
        "name", "source", "format", "compatibility",
        "base_url", "default_model", "reasoning", "default",
    ]
    assert rows["kimi"]["source"] == "catalog"
    assert rows["kimi"]["compatibility"] == "openai"
    assert rows["kimi"]["default_model"] == "kimi-k2"
    assert rows["kimi"]["reasoning"] == "high"
    assert rows["kimi"]["default"] is True
    assert rows["local"]["source"] == "custom"
    assert rows["local"]["compatibility"] == "manual"
    assert rows["local"]["default"] is False


def test_provider_list_with_no_providers_is_an_empty_document(monkeypatch, tmp_path) -> None:
    # Given: an empty provider store (the shared test home accumulates rows)
    monkeypatch.setattr(provider_store, "PROVIDERS_FILE", tmp_path / "providers.toml")

    # When: the configured providers are listed
    result = runner.invoke(provider_mod.app, ["list"])

    # Then: an empty list, not an error — YAML is the only shape
    assert result.exit_code == 0
    assert _load(result.output) == {"providers": []}


# ── skill list ───────────────────────────────────────────────────────────────


def test_skill_list_names_descriptions_and_directories(monkeypatch, tmp_path) -> None:
    # Given: one skill in a scratch local root (global root empty), the same
    # redirection test_skills_listing.py uses
    project = tmp_path / "project"
    root = project / "skills"
    skill_dir = root / "alpha"
    skill_dir.mkdir(parents=True)
    global_root = tmp_path / "global-skills"
    global_root.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Does alpha things\n---\n# alpha body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_store, "SKILLS_DIR", global_root)
    monkeypatch.setattr(skill_store, "INVOCATION_CWD", project)

    # When: skills are listed
    result = runner.invoke(skill_mod.app, ["list"])

    # Then: one row per skill with its root
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc == {
        "skills": [
            {"name": "alpha", "description": "Does alpha things", "directory": str(root)}
        ]
    }


def test_skill_list_keeps_the_full_description(monkeypatch, tmp_path) -> None:
    # Given: a skill whose description is longer than the old table's 80-char cut
    long_description = "word " * 30
    project = tmp_path / "project"
    root = project / "skills"
    skill_dir = root / "long"
    skill_dir.mkdir(parents=True)
    global_root = tmp_path / "global-skills"
    global_root.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: long\ndescription: {long_description}\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(skill_store, "SKILLS_DIR", global_root)
    monkeypatch.setattr(skill_store, "INVOCATION_CWD", project)

    # When: skills are listed
    result = runner.invoke(skill_mod.app, ["list"])

    # Then: the description arrives whole, not truncated (the parser strips
    # surrounding whitespace, so compare against the stripped expectation)
    assert result.exit_code == 0
    assert _load(result.output)["skills"][0]["description"] == long_description.strip()
    assert len(long_description) > 100  # comfortably past the old 80-char cut


# ── style list ───────────────────────────────────────────────────────────────


def test_style_list_marks_the_active_style() -> None:
    # Given: the bundled styles with 'coding' active
    settings_store.save(settings_store.Settings(style="coding"))

    # When: styles are listed
    result = runner.invoke(style_mod.app, ["list"])

    # Then: every installed style appears once with an active flag
    assert result.exit_code == 0
    doc = _load(result.output)
    assert list(doc) == ["styles"]
    entries = {entry["name"]: entry["active"] for entry in doc["styles"]}
    assert entries["coding"] is True
    assert entries["default"] is False
    assert all(isinstance(active, bool) for active in entries.values())


# ── subagent show ────────────────────────────────────────────────────────────


def test_subagent_show_reports_the_same_as_outo_inheritance() -> None:
    # Given: no subagent override and no provider at all
    settings_store.save(settings_store.Settings())

    # When: the subagent profile is shown
    result = runner.invoke(subagent_mod.app, ["show"])

    # Then: the effective trio is the honest "" (nothing resolves), and the
    # overrides echo the raw settings keys — no display sentences
    assert result.exit_code == 0
    doc = _load(result.output)
    assert list(doc) == ["provider", "model", "reasoning", "overrides"]
    assert doc["provider"] == ""
    assert doc["model"] == ""
    assert doc["reasoning"] == ""
    assert doc["overrides"] == {"provider": "", "model": "", "reasoning": ""}


def test_subagent_show_reports_an_explicit_trio() -> None:
    # Given: an outo provider and a full subagent override
    provider_store.upsert(
        provider_store.Provider(name="prov", api_format="openai", default_model="m-9")
    )
    settings_store.save(
        settings_store.Settings(
            provider="prov",
            subagent_provider="prov",
            subagent_model="m-9",
            subagent_reasoning="high",
        )
    )

    # When: the subagent profile is shown
    result = runner.invoke(subagent_mod.app, ["show"])

    # Then: the effective trio and the overrides both carry the configured trio
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["provider"] == "prov"
    assert doc["model"] == "m-9"
    assert doc["reasoning"] == "high"
    assert doc["overrides"] == {"provider": "prov", "model": "m-9", "reasoning": "high"}


def test_subagent_show_resolves_same_as_outo_to_outos_trio() -> None:
    # Given: an outo provider with a model and reasoning, no subagent override
    provider_store.upsert(
        provider_store.Provider(
            name="prov", api_format="openai", default_model="m-9", reasoning_effort="high"
        )
    )
    settings_store.save(settings_store.Settings(provider="prov"))

    # When: the subagent profile is shown
    result = runner.invoke(subagent_mod.app, ["show"])

    # Then: the effective trio names outo's own provider/model/reasoning —
    # "same as outo" is resolved, not displayed
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["provider"] == "prov"
    assert doc["model"] == "m-9"
    assert doc["reasoning"] == "high"
    assert doc["overrides"] == {"provider": "", "model": "", "reasoning": ""}


def test_subagent_show_never_inherits_outos_model_across_providers() -> None:
    # Given: outo on one provider, the subagent pinned to another with a
    # default model of its own
    provider_store.upsert(
        provider_store.Provider(name="outo-prov", api_format="openai", default_model="m-outo")
    )
    provider_store.upsert(
        provider_store.Provider(name="sub-prov", api_format="openai", default_model="m-sub")
    )
    settings_store.save(
        settings_store.Settings(provider="outo-prov", subagent_provider="sub-prov")
    )

    # When: the subagent profile is shown
    result = runner.invoke(subagent_mod.app, ["show"])

    # Then: the subagent takes ITS OWN endpoint's default, never outo's model.
    # `save` normalized the just-named subagent provider with its endpoint
    # default (invariant 12: a named provider always carries a model), so the
    # override carries that pair.
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["provider"] == "sub-prov"
    assert doc["model"] == "m-sub"
    assert doc["overrides"] == {"provider": "sub-prov", "model": "m-sub", "reasoning": ""}


# ── message list ─────────────────────────────────────────────────────────────


def test_message_list_carries_every_peek_field() -> None:
    # Given: a session with one multiline message
    session = _session()
    message_id = inbox.insert(session, "line one\nline two", kind="alarm", source="spawn")

    # When: the inbox is listed
    result = runner.invoke(message_mod.app, ["list", "--session", session])

    # Then: the schema fields match _peek's payload, multiline text intact
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["session"] == session
    (row,) = doc["messages"]
    assert list(row) == ["id", "ts", "kind", "source", "text"]
    assert row["id"] == message_id
    assert row["kind"] == "alarm"
    assert row["source"] == "spawn"
    assert row["text"] == "line one\nline two"


# ── status ───────────────────────────────────────────────────────────────────


def test_status_emits_the_full_snapshot_schema(monkeypatch, tmp_path) -> None:
    # Given: exactly one configured provider and the default settings
    # (the shared test home accumulates providers across tests, so this
    # status test reads its own store)
    monkeypatch.setattr(provider_store, "PROVIDERS_FILE", tmp_path / "providers.toml")
    provider_store.upsert(
        provider_store.Provider(name="prov", api_format="openai", default_model="m-1")
    )
    settings_store.save(settings_store.Settings(provider="prov", session="work"))

    # When: status is printed
    result = runner.invoke(root_app, ["status"])

    # Then: the top-level keys appear in the schema's order
    assert result.exit_code == 0
    doc = _load(result.output)
    assert list(doc) == [
        "provider", "model", "subagent", "style", "session", "storage",
        "providers", "styles", "skills", "sessions",
    ]
    assert doc["provider"] == "prov"
    assert doc["model"] == "m-1"
    assert doc["subagent"]["provider"] == "prov"
    assert doc["subagent"]["model"].startswith("same as outo (")
    assert doc["session"] == "work"
    assert doc["providers"] == ["prov"]
    assert "default" in doc["styles"]
    assert list(doc["sessions"]) == ["this_directory", "other"]


def test_status_lists_sessions_by_directory() -> None:
    # Given: a session file stamped with a foreign launch directory
    from miniouto.storage import paths
    from miniouto.storage import sessions as session_store

    session_store.save(
        "elsewhere", session_store.SessionData(name="elsewhere", cwd="/elsewhere")
    )

    # When: status is printed
    result = runner.invoke(root_app, ["status"])

    # Then: the session lands in `other`, and the storage path is reported
    assert result.exit_code == 0
    doc = _load(result.output)
    assert "elsewhere" in doc["sessions"]["other"]
    assert isinstance(doc["skills"], list)
    assert doc["storage"] == str(paths.ROOT)


# ── no rich markup anywhere in the data lane ─────────────────────────────────


def test_listing_outputs_carry_no_markup_or_tables() -> None:
    # Given: ordinary installed state (bundled styles exist)
    # When: the pure-output commands run
    outputs = [
        runner.invoke(root_app, ["status"]).output,
        runner.invoke(style_mod.app, ["list"]).output,
        runner.invoke(subagent_mod.app, ["show"]).output,
        runner.invoke(provider_mod.app, ["list"]).output,
        runner.invoke(skill_mod.app, ["list"]).output,
    ]

    # Then: each output is one parseable YAML mapping with no rich markup
    for output in outputs:
        doc = yaml.safe_load(output)
        assert isinstance(doc, dict)
        assert "[bold]" not in output and "[green]" not in output and "[red]" not in output
        assert "┏" not in output and "│" not in output  # no table borders
