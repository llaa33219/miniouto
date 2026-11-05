"""User-level settings: active provider, style, session."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from . import toml_io
from .paths import SETTINGS_FILE, ensure_dirs


@dataclass
class Settings:
    provider: str = ""
    # The MODEL outo runs, as the user last chose it — paired with
    # `provider` above as ONE choice.
    #
    # It is stored HERE, not on the provider record, because the record's
    # `default_model` describes the ENDPOINT ("this endpoint's default
    # model") while this describes the INSTALLATION ("what outo runs
    # here"). Keeping them apart is what stops an outo model edit from
    # dragging a subagent along: a subagent on the same provider resolves
    # its model from the record, and rewriting the record would move it.
    #
    # Empty = "follow the provider record's default_model" (the pre-0.11
    # behaviour, and what a fresh install resolves to).
    model: str = ""
    # The REASONING EFFORT outo runs, same pairing as `model` above.
    # Empty = "follow the provider record's reasoning_effort".
    reasoning: str = ""
    style: str = "default"
    session: str = "default"
    # TUI theme name. Defaults to the harness's own `miniouto` theme
    # (`cli/tui.py:MINIOUTO_THEME`); any other registered name — a stock
    # Textual theme, say — overrides it on the next TUI launch.
    theme: str = "miniouto"
    # Subagent provider/model/reasoning overrides. Empty string = inherit
    # outo's provider/model and fall back to the subagent provider's
    # `reasoning_effort`. NOTE: `merge()` and `update()` skip empty-string
    # values, so these cannot be cleared through `update()` — callers must
    # clear them via `save(replace(load(), subagent_provider="", ...))`
    # (see cli/subagent.py's `clear` command and `set --reasoning ""`).
    subagent_provider: str = ""
    subagent_model: str = ""
    subagent_reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    def normalized(self, previous: Settings | None = None) -> Settings:
        """This settings, with the provider trios made WHOLE.

        Every key of a trio is either set or "same as outo" — there is no
        middle state where a provider is named but its model is not. A
        provider whose model is missing is completed from that provider's
        own endpoint default, so the stored trio always names a runnable
        pair and an outo edit can never leave the subagent pointing at a
        model its provider does not serve.

        ONLY A PROVIDER THAT JUST CHANGED is completed: clearing a model
        on its own keeps its documented meaning ("hand that key back to
        the default"), so `previous` — the settings BEFORE this write —
        is what says whether the provider is the one just named.
        """

        before = previous or Settings()
        provider, model, reasoning = self.provider, self.model, self.reasoning
        if provider and before.provider != provider:
            record = _provider_record(provider)
            if record is not None:
                model = model or record.default_model
                reasoning = reasoning or record.reasoning_effort or ""

        sub_provider = self.subagent_provider
        sub_model = self.subagent_model
        sub_reasoning = self.subagent_reasoning
        if sub_provider and before.subagent_provider != sub_provider:
            record = _provider_record(sub_provider)
            if record is not None:
                sub_model = sub_model or record.default_model
                sub_reasoning = sub_reasoning or record.reasoning_effort or ""

        if (provider, model, reasoning, sub_provider, sub_model, sub_reasoning) == (
            self.provider,
            self.model,
            self.reasoning,
            self.subagent_provider,
            self.subagent_model,
            self.subagent_reasoning,
        ):
            return self
        return replace(
            self,
            model=model,
            reasoning=reasoning,
            subagent_model=sub_model,
            subagent_reasoning=sub_reasoning,
        )

    def merge(self, overrides: dict[str, Any]) -> Settings:
        merged = asdict(self)
        for k, v in overrides.items():
            if v not in (None, ""):
                merged[k] = v
        return Settings(**merged)


def load() -> Settings:
    ensure_dirs()
    raw = toml_io.load(SETTINGS_FILE)
    return Settings(**{k: raw.get(k, getattr(Settings(), k)) for k in asdict(Settings())})


def save(settings: Settings) -> None:
    """Write the settings, with the provider trios made whole.

    `normalized` fills a missing model (and reasoning) from the named
    provider's own record, so the file never holds a provider with no
    model — the state every reader would otherwise have to guess about.
    """

    ensure_dirs()
    previous = _read_raw_settings()
    toml_io.save(SETTINGS_FILE, settings.normalized(previous).to_dict())


def _read_raw_settings() -> Settings:
    """The settings as they are on disk, without any normalization."""

    ensure_dirs()
    raw = toml_io.load(SETTINGS_FILE)
    return Settings(**{k: raw.get(k, getattr(Settings(), k)) for k in asdict(Settings())})


def update(**kwargs: Any) -> Settings:
    """Merge `kwargs` into the stored settings and save them.

    A `provider` change is a PAIR change: the provider and the model it
    runs are one choice, so switching the provider without switching the
    model would leave outo pointing at a model the new provider has never
    heard of. When this call names a provider but no model, the stored
    model is replaced by that provider's own default (its record's
    `default_model`), and the stored reasoning with the record's own
    preference.

    The provider store is read here, so `storage.settings` gains a
    dependency on `storage.providers` — both are disk-layer modules, so
    that stays inside the layer rule (storage must not import core).
    """

    current = load()
    merged = current.merge(kwargs)
    if "provider" in kwargs and "model" not in kwargs:
        # A provider switch replaces the model with that provider's own
        # default — the pair must never name two providers at once.
        record = _provider_record(merged.provider)
        merged.model = (record.default_model if record else "")
        merged.reasoning = (record.reasoning_effort or "") if record else ""
    save(merged)
    return merged


def _provider_record(name: str) -> Any:
    """The provider record `name`, or None — a late import, not a cycle."""

    if not name:
        return None
    from . import providers as provider_store

    return provider_store.get(name)
