# lma (llm-model-api) integration

`miniouto` integrates [lma](https://github.com/llaa33219/llm-model-api) — a re-shaped, fuzzy-search view of [`models.dev/api.json`](https://models.dev/api.json) deployed to [`https://lma.blp.sh`](https://lma.blp.sh) via Cloudflare Workers. lma covers **144 providers** and **5,000+ models** with a 10-minute server-side cache.

miniouto uses lma for three things:

1. **Provider discovery** — `miniouto provider providers` lists every known provider and whether miniouto can host it.
2. **Model discovery** — `miniouto provider models <provider>` lists every model lma knows for a provider; the TUI provider-add and model-edit flows fetch the same data.
3. **Per-model context / max-output caps** — `core/context.py` calls lma's `/model` endpoint to look up `context_window` and `max_output_tokens` instead of the older `lcw-api.blp.sh/context-window` endpoint.

## Endpoints used

miniouto only ever issues **read-only GETs** against the four endpoints below. All responses are JSON; all calls are wrapped in `core.lma` with a 10-minute in-process cache mirroring lma's server TTL.

| Endpoint | miniouto call | Used by |
|---|---|---|
| `GET https://lma.blp.sh/provider` | `lma.list_providers()` | `cli/provider.py:providers_cmd`, `cli/tui.py:_catalog_add_flow` |
| `GET https://lma.blp.sh/model-list?provider-name=<name>` | `lma.list_models(name)` | `cli/provider.py:models_cmd`, `cli/tui.py:_catalog_add_flow`, `cli/tui.py:_catalog_model_picker_flow` |
| `GET https://lma.blp.sh/model?model-name=<name>&provider-name=<name>` | `lma.get_model(name, provider_name)` | `core/context.py:get_context_window`, `core/context.py:get_max_output_tokens` |
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

When `sdk_to_format` returns `(None, None)`, the provider is **not addable** — miniouto's four built-in coreouto providers (`openai`, `openai-response`, `anthropic`, `google`) cannot host it. Such providers appear in `miniouto provider providers` with `Addable? = ✗` and are filtered out of the TUI add list.

## TUI integration

`cli/tui.py` uses lma in three places (the codebase calls these "catalog" providers; the underlying `source` field value is still the literal `"lma"`):

1. **Provider picker modal** has two sentinels at the bottom of the list (`cli/tui.py`):
   - `+ add from catalog…` → `ChatTUI._catalog_add_flow()` fetches `/provider`, filters to addable entries, shows a `SelectionModal`, then a `TextInputModal` for the API key, then saves with `source="lma"` and the first model from `/model-list` as `default_model`.
   - `+ add custom…` → `ChatTUI._open_custom_add_wizard()` runs a 5-step wizard saving with `source="custom"`.
2. **Model picker** (`_open_model_editor`) dispatches on `provider.source`:
   - `source == "lma"` → `ChatTUI._catalog_model_picker_flow()` fetches `/model-list` and shows a `SelectionModal` with `id — name` rows.
   - `source == "custom"` → `ChatTUI._open_custom_model_editor()` shows a free-text `TextInputModal`.
3. **Saving a model** always writes to `provider.default_model` (via `dataclasses.replace(p, default_model=new_value)` + `provider_store.upsert`) and clears any prior `settings.model` override, so the chip reflects the new provider default immediately.

## Provider.source field

`storage/providers.py:Provider` carries a new field:

```python
source: str = "custom"   # one of SOURCE_CUSTOM | SOURCE_LMA
```

`SOURCE_LMA` is set by `provider add` (catalog) and by the TUI `+ add from catalog…` wizard. `SOURCE_CUSTOM` is set by `provider custom add` and the TUI `+ add custom…` wizard. Legacy TOML files (pre-this-change) load with `source="custom"` because the field defaults when missing — they continue to work; only the model-picker UI differs (text input vs. catalog picker).

`SOURCE_CUSTOM`, `SOURCE_LMA`, and the `VALID_SOURCES = (SOURCE_CUSTOM, SOURCE_LMA)` tuple are exported from `storage.providers`.
