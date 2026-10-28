# lma (llm-model-api) integration

`miniouto` integrates [lma](https://github.com/llaa33219/llm-model-api) — a re-shaped, fuzzy-search view of [`models.dev/api.json`](https://models.dev/api.json) deployed to [`https://lma.blp.sh`](https://lma.blp.sh) via Cloudflare Workers. lma covers **144 providers** and **5,000+ models** with a 10-minute server-side cache.

miniouto uses lma for five things:

1. **Provider discovery** — `miniouto provider providers` lists every known provider and whether miniouto can host it.
2. **Model discovery** — `miniouto provider models <provider>` lists every model lma knows for a provider; the TUI provider-add wizard and the popup's `Model` row fetch the same data.
3. **Per-model context / max-output caps** — `core/context.py` calls lma's `/model` endpoint to look up `context_window` and `max_output_tokens` instead of the older `lcw-api.blp.sh/context-window` endpoint.
4. **Per-model reasoning metadata** — `core/reasoning.py` reads `reasoning_options` from the same `/model` endpoint to resolve reasoning request kwargs (see below).
5. **Provider dialect / base-URL detection** — `core/providers.py` reads `/provider`'s `dialects.supported` (confirmed) and `dialects.likely` (tentative) maps to choose a hostable wire format and its exact endpoint URL.

### Per-provider overrides (custom providers)

For providers that lma has never heard of (anything added via `miniouto provider custom add` or the TUI `+ add custom…` wizard), the `/model` lookup misses and `core/context.py` falls back to the 16K floor. To let these providers declare their real caps, the TUI custom-add wizard (`_open_custom_add_wizard`) collects two extra fields after the model id (and they can be written by hand into `providers.toml`):

- `max_context_window` — written to `providers.toml`, read by `get_context_window`.
- `max_output_tokens` — written to `providers.toml`, read by `get_max_output_tokens`.

Precedence for `max_output_tokens` (highest wins):

1. `chat --max-tokens <n>` (per-call CLI flag)
2. Provider's `max_output_tokens` override (set when the provider is added, or by hand)
3. lma's `max_output_tokens` for the (model, provider) pair
4. lma's `context_window` (most APIs cap output at the context window)
5. `DEFAULT_MAX_OUTPUT_TOKENS = 16384` (the floor — defends against Anthropic's silent 1024 default that truncates long tool calls, e.g. file writes)

There is no upper ceiling. The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed. A wrong value surfaces as a clear provider error at chat time, which is preferable to silent truncation.

## Endpoints used

miniouto only ever issues **read-only GETs** against the four endpoints below. All responses are JSON; all calls are wrapped in `core.lma` with a 10-minute in-process cache mirroring lma's server TTL.

| Endpoint | miniouto call | Used by |
|---|---|---|
| `GET https://lma.blp.sh/provider` | `lma.list_providers()` | `cli/provider.py:providers_cmd`, `cli/tui.py:_catalog_add_flow` |
| `GET https://lma.blp.sh/model-list?provider-name=<name>` | `lma.list_models(name)` | `cli/provider.py:models_cmd`, `cli/tui.py:_catalog_add_flow`, `cli/tui.py:_catalog_model_picker_flow` |
| `GET https://lma.blp.sh/model?model-name=<name>&provider-name=<name>` | `lma.get_model(name, provider_name)` | `core/context.py:get_context_window`, `core/context.py:get_max_output_tokens`, `core/reasoning.py` |
| `GET https://lma.blp.sh/model?model-name=<name>` (no provider filter) | same, with `provider_name=None` | fallback in `core/context.py` when no provider context is available |

Network failures (timeouts, 5xx, DNS) are caught and fail soft: `lma.list_providers` propagates the exception so callers can show an error, but `lma.find_provider` swallows the error and returns `None` (so the TUI can degrade gracefully to "custom provider" mode).

## Caching

lma caches upstream `models.dev` data for 10 minutes per Cloudflare Worker isolate. miniouto mirrors this TTL in `_CACHE` (`core/lma.py`) so repeated lookups in a single TUI session don't re-hit the network:

```python
_CACHE: dict[str, tuple[float, Any]]  # key → (fetched_at, payload)
CACHE_TTL_SECONDS = 600
```

A cached value of `None` is meaningful — it means "lma returned 404 for this key" — and prevents re-querying on every turn. Use `lma.clear_cache()` to drop all entries (used by tests).

Cache keys:

| Key | Endpoint |
|---|---|
| `"providers"` | `/provider` |
| `f"models:{provider.lower()}"` | `/model-list?provider-name=<provider>` |
| `f"model:{provider.lower()}:{model.lower()}"` | `/model?model-name=…&provider-name=…` |

## Per-model reasoning metadata (`reasoning_options`)

The `/model` endpoint also reports per-model reasoning capabilities via a top-level `reasoning: bool` flag and a `reasoning_options` list (may be `[]` or absent). Three verified shapes of an entry:

```json
[{"type": "toggle"}]                                            // on/off only — MiniMax-M3, GLM-4.7, Kimi-K2.5
[{"type": "effort", "values": ["none","low","medium","high","xhigh"]}]  // effort ladder — gpt-5.2 (gemini-3-pro: ["low","high"])
[{"type": "budget_tokens", "min": 1024}]                        // token budget — claude-sonnet-4-5
```

The list can carry **multiple** descriptors for one model — e.g. kimi-k3 returns `[{"type": "toggle"}, {"type": "effort", "values": ["low","high","max"]}]`. miniouto picks the richest entry (**effort > budget_tokens > toggle**); reading only the first entry would hide the effort ladder behind the toggle.

miniouto consumes this in three places:

1. **`core/reasoning.py:resolve_reasoning_passthrough`** — maps the resolved reasoning choice (`chat --pm '…:REASONING'` > the session pin's `reasoning` > `Provider.reasoning_effort` > lma default) into provider-native request kwargs merged into `provider_passthrough` by `build_runtime`. Only effort values and anthropic's `thinking: {"type": "adaptive"}` are emitted — `budget_tokens` models are driven as a plain toggle because many providers reject budget parameters. The mapping table and the google-format limitation are documented in `docs/core.md` (step 11).
2. **`miniouto provider add`** — auto-fills the new provider's `reasoning_effort` field from `default_reasoning_choice(default_model, provider_name)` when `--reasoning` is not given (best-effort, silent on failure).
3. **TUI pickers** — `reasoning_choices(model, provider_name)` supplies the provider settings popup's `Reasoning` row list (`["off", ...]`); `default_reasoning_choice(...)` supplies the pre-selected default, which is also what picking a Provider or a Model resets to (an effort level belongs to a model). Both return `None` when the model has no reasoning options or lma is unreachable.

Always scope the lookup with `provider-name` (`lma.get_model(model, provider_name)`) — an unscoped lookup can return the same model id from a different provider with different reasoning options.

## Provider name → coreouto format mapping

lma passes through `models.dev`'s `sdk` and `api` URL for every provider. miniouto translates those into one of four coreouto `api_format` values via `core/providers.py:sdk_to_format`:

| lma `sdk` | miniouto `api_format` | `base_url` source |
|---|---|---|
| `openai` | `openai` | lma `api` (if set) |
| `openai-responses` | `openai-response` | lma `api` (if set) |
| `openai-compatible` | `openai` | lma `api` (required) |
| `anthropic` | `anthropic` | lma `api` (if set) |
| `google-generative-ai` | `google` | lma `api` (if set) |
| `google-vertex` | `google` | lma `api` (if set) |
| anything else | `openai` (fallback) **iff** lma `api` is non-null | lma `api` |
| anything else with no `api` | `(None, None)` — not addable | — |

### Dialect detection and base URLs

The `/provider` response may also carry `dialects`, with `supported` and `likely` maps from a dialect id to a complete **SDK base URL** (detector 0.2.0+, not a raw POST endpoint). `supported` is a confirmed detection; `likely` is tentative. `catalog_compatibility()` exposes only dialects miniouto can host, confirmed first:

| lma dialect | `api_format` |
|---|---|
| `openai-chat` | `openai` |
| `openai-responses` | `openai-response` |
| `anthropic-messages` | `anthropic` |
| `gemini-generate` | `google` |

`typesafe-systemone` is deliberately ignored. `resolve_catalog_dialect()` prefers `supported` over `likely`, and within either confidence group prefers the dialect matching the native SDK. If no usable dialect is present it returns `None`, preserving the `sdk_to_format` behavior above. If a dialect is available it can also make a provider addable when its native SDK has no mapping. The dialect's URL replaces lma's generic `api` URL rather than being derived by trimming it. Google is the one SDK adaptation: a trailing `v1beta`/`v1` path segment is passed as google-genai's `api_version` so the SDK does not append it twice. OpenAI-compatible URLs remain exactly as detected; version-less gateway routes are valid and must not receive a blanket `/v1` suffix.

`add_provider_from_lma(..., dialects=...)` persists the selected dialect id in `Provider.catalog_dialect` and its format/URL in `api_format` / `base_url`. This keeps later turns on the user's selected endpoint even if lma's detection changes. Existing catalog records without that field retain their saved native configuration. Custom providers are never probed or rewritten.

When neither `resolve_catalog_dialect` nor `sdk_to_format` finds a host, the provider is **not addable** — miniouto's four built-in coreouto providers (`openai`, `openai-response`, `anthropic`, `google`) cannot host it. Such providers appear in `miniouto provider providers` with `Addable? = ✗` and are filtered out of the TUI add list. The CLI catalog list shows each supported/likely dialect and the selected URL; `provider list` shows the saved dialect and effective URL.

## TUI integration

`cli/tui.py` uses lma in three places (the codebase calls these "catalog" providers; the underlying `source` field value is still the literal `"lma"`):

1. **The provider settings popup's `Provider` row** (`ProviderSettingsPopup`, reached from either the `outo` or `subagent` chip) has two sentinels at the bottom of its list (`cli/tui.py`):
   - `+ add from catalog…` → `ChatTUI._catalog_add_flow()` fetches `/provider`, filters to addable entries, shows a `SelectionModal`, then a `TextInputModal` for the API key, then saves with `source="lma"` and the first model from `/model-list` as `default_model`.
   - `+ add custom…` → `ChatTUI._open_custom_add_wizard()` runs a 5-step wizard saving with `source="custom"`.
2. **The popup's `Compatibility` row** appears for catalog providers after the three trio rows. It shows the saved dialect (or `native SDK` for a legacy record), fetches current catalog metadata on click, and offers every hostable supported/likely dialect with its URL. A pick updates the shared provider record's `catalog_dialect`, `api_format`, and `base_url`; it does not alter the model/reasoning session pin. An inheriting subagent popup edits the effective outo provider record. Custom providers get the existing `Caps` row instead.
3. **The popup's `Model` row** (`ProviderSettingsPopup._model_flow`) dispatches on `provider.source`:
   - `source == "lma"` → fetches `/model-list` and shows a `SelectionModal` with `id — name` rows.
   - `source == "custom"` → the same flow's "no models" fallback shows a free-text `TextInputModal` (a hand-added endpoint need not exist in the catalog).
4. **Saving any of the three trio rows** goes through `_persist_trio`, which writes **both** halves of the resolution: the session pin (`session-config/<session>.toml`) and the global default — `settings.provider` plus the provider record's `default_model` / `reasoning_effort` (`dataclasses.replace` + `provider_store.upsert`) for the outo trio, the three `settings.subagent_*` keys for the subagent one. `settings.model` is never written. Picking a Provider or a Model also resets Reasoning to that model's default. See `docs/cli.md` § Clickable chips.

## Provider.source field

`storage/providers.py:Provider` carries a new field:

```python
source: str = "custom"   # one of SOURCE_CUSTOM | SOURCE_LMA
catalog_dialect: str = ""  # selected lma dialect; empty = legacy/native SDK
```

`SOURCE_LMA` is set by `provider add` (catalog) and by the TUI `+ add from catalog…` wizard. `SOURCE_CUSTOM` is set by `provider custom add` and the TUI `+ add custom…` wizard. Legacy TOML files (pre-this-change) load with `source="custom"` because the field defaults when missing — they continue to work; only the model-picker UI differs (text input vs. catalog picker). `catalog_dialect` is optional for backward-compatible catalog records and is omitted when empty.

`SOURCE_CUSTOM`, `SOURCE_LMA`, and the `VALID_SOURCES = (SOURCE_CUSTOM, SOURCE_LMA)` tuple are exported from `storage.providers`.
