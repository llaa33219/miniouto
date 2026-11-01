# modelslist integration

`miniouto` reads its provider/model catalog from [`https://modelslist.blp.sh`](https://modelslist.blp.sh). The client is `core/modelslist.py` (imported as `catalog_api` by `cli/provider.py` and `cli/tui.py`). There is **no** `lma.blp.sh` client and no `models.dev` fetch path: every catalog request goes to modelslist, and the official provider endpoints modelslist advertises (`modelLists[].source == "official"`) are **never fetched** — miniouto only reads the catalog-owned list (`source == "modelslist"`).

The root index is a **curated** index, not the full universe of LLM vendors (it held **17 entries** when this was written — treat the count as observed, not guaranteed). It carries the providers miniouto can host plus their exact wire-protocol base URLs.

miniouto uses modelslist for five things:

1. **Provider discovery** — `miniouto provider providers` lists the root index and whether miniouto can host each entry.
2. **Model discovery** — `miniouto provider models <provider>` lists the models in that provider's catalog-owned list; the TUI add wizard and the popup's `Model` row read the same data.
3. **Per-model context / max-output caps** — `core/context.py` derives `context_window` and `max_output_tokens` from the model entry (see the envelope below).
4. **Per-model reasoning metadata** — `core/reasoning.py` reads `reasoning` / `reasoning_options` from that same entry.
5. **Provider format / base-URL selection** — `core/providers.py` maps the provider's `formats` array onto a hostable coreouto `api_format` and keeps the exact `baseUrl`.

## Root provider index

`GET https://modelslist.blp.sh/` returns a **JSON array** of provider objects, in the curator's order:

```json
[
  {
    "name": "Kimi",
    "slug": "kimi",
    "formats": [
      {"format": "openai", "baseUrl": "https://api.moonshot.ai/v1"},
      {"format": "openai-responses", "baseUrl": "https://api.moonshot.ai/v1/responses"},
      {"format": "anthropic", "baseUrl": "https://api.moonshot.ai/anthropic"}
    ],
    "modelLists": [
      {"url": "https://api.moonshot.ai/v1/models", "format": "openai", "source": "official"},
      {"url": "https://modelslist.blp.sh/kimi/models", "format": "openai", "source": "modelslist"}
    ]
  }
]
```

- `slug` is the **stable public identifier** and is what a saved provider is named after. `name` is a display label (`Z.AI Coding Plan`, `Alibaba Token Plan Personal`).
- `formats[]` lists the wire protocols the provider advertises, **in priority order**. `baseUrl` is the exact endpoint for that protocol — never derived by trimming another URL.
- `modelLists[]` mixes catalog-owned (`source: "modelslist"`) and vendor (`source: "official"`) lists. miniouto selects **only** the first `modelslist` entry with a non-empty `url`; official entries exist for other consumers and are never requested.

`list_providers()` returns the array filtered to dict entries, preserving advertised order; a non-array payload raises `ValueError("modelslist provider index must be a JSON array")`.

## Model lists

`GET <modelLists[source=modelslist].url>` returns an OpenAI-style envelope:

```json
{
  "object": "list",
  "data": [
    {
      "id": "k3",
      "name": "k3",
      "architecture": {"input_modalities": ["text", "image", "video"], "output_modalities": ["text"]},
      "context_length": 1048576,
      "top_provider": {"max_completion_tokens": 1048576},
      "reasoning": {"mandatory": true, "default_enabled": true, "supported_efforts": ["low", "high", "max"], "default_effort": null},
      "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}],
      "object": "model",
      "created": 0,
      "owned_by": "kimi"
    }
  ]
}
```

`list_models(provider_name)` resolves the provider, picks the `modelslist` URL, and returns `data` filtered to dicts **in source order**. Rules:

- A provider with no `modelslist` entry yields `[]` (cached).
- A **404** on the list URL yields `[]` (cached) — the catalog does not know that provider.
- Any other HTTP/transport error **propagates** and is not cached.
- A payload without a `data` **array** raises `ValueError("modelslist model response must contain a data array")`.

`get_model(model_name, provider_name=None)` searches the cached lists — exact `id` match first, then case-insensitive — and returns the entry run through `_model_info`:

| Envelope field | Adapted key | Used by |
|---|---|---|
| `context_length` (int) | `context_window` | `core/context.py:get_context_window`, `get_max_output_tokens` |
| `top_provider.max_completion_tokens` (int) | `max_output_tokens` | `core/context.py:get_max_output_tokens` |
| `reasoning`, `reasoning_options` | passed through | `core/reasoning.py` |

When `reasoning_options` is missing/empty but `reasoning.supported_efforts` is non-empty, the adapter **synthesizes** `[{"type": "effort", "values": supported_efforts}]`. OpenRouter publishes efforts that way while leaving `reasoning_options` empty, and without the synthesis its effort ladder would vanish from the UI. Without a `provider_name`, `get_model` walks every provider slug in index order — always pass the provider to avoid matching the same model id on a different provider with different caps or reasoning options.

## Caching

`core/modelslist.py` keeps a module-level `_CACHE: dict[str, tuple[float, Any]]` with `CACHE_TTL_SECONDS = 300` (**5 minutes**), so a long TUI session does not re-fetch the index on every interaction:

| Key | Payload |
|---|---|
| `"providers"` | the root provider index |
| `f"models:{_normalize(provider)}"` | that provider's catalog-owned model list |

`_normalize` lowercases and strips spaces, `-`, `_` and `.`, so `Z.AI`, `z-ai` and `zai` share a cache key. Expired entries are dropped on read. `clear_cache()` empties the dict (used by tests / manual refresh).

A cached **empty list is meaningful** — it means "modelslist has no list for this provider", and is not re-queried until the TTL expires. A **transport failure is never cached**: the exception escapes before `_cache_set`, so one network blip cannot pin a provider to "no models" for five minutes. Every request uses a `15.0`-second `httpx` timeout.

## Supported formats

`core/providers.py:DIALECT_TO_FORMAT` maps modelslist's `formats[].format` onto coreouto's four built-in providers:

| modelslist `format` | miniouto `api_format` | coreouto class |
|---|---|---|
| `openai` | `openai` | `OpenAIProvider` (Chat Completions) |
| `openai-responses` | `openai-response` | `OpenAIResponseProvider` |
| `anthropic` | `anthropic` | `AnthropicProvider` |
| `google` | `google` | `GoogleProvider` |
| `typesafe` | — **ignored** | not an LLM wire protocol coreouto can host |

`catalog_compatibility(provider)` returns a `CatalogDialect` per hostable `formats` entry **in advertised order**, skipping unmapped formats, non-string/empty `baseUrl` values and duplicate dialects. `resolve_catalog_dialect(provider)` returns the **first** one — modelslist's own priority decides, miniouto does not second-guess it. All advertised hostable formats are verified modelslist entries; the picker has no confidence or `likely` labels.

`baseUrl` is persisted **verbatim**. The one SDK adaptation happens at construction time (`_instantiate`): a trailing `/responses` or `/messages` is trimmed for the OpenAI-Responses and Anthropic SDKs, a trailing `/v1` is trimmed for Anthropic, and for `google` a trailing version segment (`v1beta`, `v1`) is split out into google-genai's `api_version` so the SDK does not append it twice. Version-less gateway routes are valid and never receive a blanket `/v1` suffix.

### Resolving a name to a provider

`find_provider(name)` / `find_provider_by_slug(slug, base_url="")` apply, in order:

1. normalized `slug` equality — the stable identifier, and the reason `Z.AI` (`slug: zai`) resolves correctly;
2. normalized `name` equality;
3. **endpoint match** against `formats[].baseUrl` — the `base_url` argument when given, otherwise the stored record's own `base_url` (looked up lazily in `storage.providers`). Comparison is lowercased host plus a path with `/responses`, `/messages`, `/chat/completions` (and a trailing `/v1` on anthropic paths) stripped;
4. substring match in either direction, in index order — the last resort, and what makes `miniouto provider models "mini max"` and `"deep seek"` work.

Step 3 sits **above** the fuzzy step on purpose. A stored record's endpoint is a precise identity, while a fuzzy substring match is not: a name that happens to resemble one provider must not be redirected onto it when the record itself points at another. `"zen"` for a record saved against `https://api.deepseek.com` resolves to `deepseek`, not to `opencode-zen`; a legacy `kimi`-ish record whose saved endpoint matches nothing still falls through to the fuzzy step. Because step 4 is order-dependent, the bare `"kim"` lands on `Kimi Code Plan`, which precedes `Kimi` in the index — pass the slug when it matters.

`find_provider` swallows every exception and returns `None` so the TUI can degrade gracefully; `list_providers` / `list_models` / `get_model` let transport errors surface so the CLI can print `✗ Failed to reach catalog: …`.

## Adding a provider

`core/providers.py:add_provider_from_catalog(catalog_provider, *, api_key, default_model="", reasoning_effort=None)` builds the persisted record from modelslist metadata alone:

```python
Provider(
    name=catalog_provider["slug"],          # the slug, not the display name
    api_format=selected.api_format,          # from the first hostable format
    base_url=selected.base_url,              # that format's exact baseUrl
    api_key=api_key,
    default_model=default_model,
    source=SOURCE_MODELSLIST,                # "modelslist"
    reasoning_effort=reasoning_effort,
    catalog_dialect=selected.dialect,        # e.g. "openai-responses"
)
```

It raises `ValueError` when the provider has no hostable format or no usable `slug`; `cli/provider.py:add_cmd` turns that into `✗ {exc}` + exit 1.

## Stored `source` values

`storage/providers.py` exports three tiers:

```python
SOURCE_CUSTOM     = "custom"      # provider custom add / TUI "+ add custom…"
SOURCE_LMA        = "lma"         # LEGACY catalog records only — no lma requests are made
SOURCE_MODELSLIST = "modelslist"  # everything added from the catalog now
VALID_SOURCES     = (SOURCE_CUSTOM, SOURCE_LMA, SOURCE_MODELSLIST)
```

`is_catalog_source(source)` is true for **both** `"modelslist"` and `"lma"`, which is what keeps an old `providers.toml` behaving like a catalog provider in the TUI — it keeps the `Compatibility` row (a custom provider gets `Caps` instead) and renders as `catalog` in `provider list` — even though miniouto never contacts `lma.blp.sh`. The model picker itself no longer branches on `source`. `SOURCE_LMA` is only ever read, never written. Invalid values on load are coerced to `SOURCE_CUSTOM`.

`catalog_dialect` stores the selected `formats[].format` id so the provider keeps its chosen endpoint across catalog refreshes; it is empty for custom and for pre-dialect legacy records (the TUI then shows `native SDK`). `max_context_window` / `max_output_tokens` are custom-provider overrides only — catalog caps always come from modelslist.

## Reasoning metadata

Each model may carry a `reasoning` object and a `reasoning_options` list (possibly empty or absent):

```json
"reasoning": {"mandatory": false, "default_enabled": true, "supported_efforts": [], "default_effort": null}
"reasoning_options": [{"type": "toggle"}]
"reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}]
"reasoning_options": [{"type": "budget_tokens", "min": 1024}]
```

The list can hold **multiple** descriptors for one model (e.g. DeepSeek publishes `[{"type": "effort", "values": ["low","high","max"]}, {"type": "toggle"}]`). `core/reasoning.py:_first_reasoning_option` picks the richest — **effort > budget_tokens > toggle** — because reading only the first entry would hide a ladder or pick a lesser descriptor.

Consumers:

1. **`resolve_reasoning_passthrough`** — turns the resolved choice into provider-native kwargs merged into `provider_passthrough`. `reasoning.default_enabled: false` means auto resolves to **no** reasoning. Auto picks `default_effort` when it is a valid non-`none` value of the ladder, else `"medium"`, else the middle non-`none` value; toggle/budget → `"on"`. Only effort values and anthropic's `thinking: {"type": "adaptive"}` are ever emitted — token-budget shapes are deliberately dropped (many providers reject them), so `budget_tokens` models run as a plain toggle and legacy digit choices degrade to `"on"`. Google always returns `{}`. A stored `"on"`/digit against an effort ladder degrades to the ladder default instead of sending `effort="on"`.
2. **`default_reasoning_choice(model, provider_name)`** — the storable auto default, or `None` when the model has no options (or `default_enabled` is `false`). `provider add` and the TUI add wizard use it to fill `Provider.reasoning_effort`, silent on lookup failure.
3. **`reasoning_choices(model, provider_name)`** — the picker list: `["off", *values-without-none]` for effort, `["off", "on"]` for toggle/budget, `None` when there are no options. With an explicit choice but no metadata, `_best_effort` trusts the user (`"on"`/digits → toggle mapping, otherwise the effort mapping).

## CLI surface

`miniouto provider providers` calls `list_providers()` and prints a table titled `Catalog providers (N)` with columns `Name | Slug | miniouto format | Selected base URL | Available compatibility | Addable?`. The format/URL columns come from `resolve_catalog_dialect`; `Addable?` is `✓` for rows that resolve and `✗` otherwise. An empty index prints yellow `No providers returned by the catalog.` (exit 0); a transport failure prints red `✗ Failed to reach catalog: {exc}` and exits 1.

`miniouto provider models <name>` normalizes/fuzzy-matches the name and prints `Catalog models for '<name>' (N)` with columns `ID | Name`. An empty list prints `No models returned for '<name>'.` and exits 1.

`miniouto provider add <slug-or-name> --api-key <key> [--default-model <id>] [--reasoning <v>]` resolves the provider, builds the record via `add_provider_from_catalog`, fills `default_model` from the **first** model in the catalog-owned list when `--default-model` is empty, and auto-fills `reasoning_effort` from `default_reasoning_choice` when `--reasoning` is omitted. An existing name is overwritten with a yellow warning.

`miniouto provider list` renders `Type` as `custom`/`catalog` (via `is_catalog_source`) and `Compatibility` as the saved `catalog_dialect`, `catalog` for a catalog source with no saved dialect (any pre-dialect legacy record), or `manual` for a custom provider.

## TUI integration

`cli/tui.py` uses modelslist in three places (`catalog_api`):

1. **`+ add from catalog…`** (`SENTINEL_CATALOG_ADD` → `ChatTUI._catalog_add_flow`) — fetches the root index, keeps only entries `resolve_catalog_dialect` can host, shows a `SelectionModal` sorted by display name, confirms before overwriting an existing slug, asks for the API key, takes the **first** model of the catalog-owned list as `default_model`, offers a reasoning picker built from `reasoning_choices` (pre-selected on `default_reasoning_choice`), then persists via `add_provider_from_catalog` (`source="modelslist"`) and adopts the new provider.
2. **The popup's `Model` row** (`ProviderSettingsPopup._model_flow`) — `list_models(provider)` → `id — name` rows in catalog order. When the provider has no list (a hand-added endpoint need not exist in the catalog at all), the "no models" case falls back to a free-text `TextInputModal`. There is no `source` dispatch: catalog and custom providers share this flow.
3. **The popup's `Compatibility` row** — present only for `is_catalog_source(record.source)`; shows the saved `catalog_dialect` (or `native SDK`). Picking re-fetches via `find_provider_by_slug(name, base_url)`, lists every hostable format with its exact URL (via `catalog_compatibility`, so priority order is preserved), and keeps the currently saved choice selectable even when the catalog no longer advertises it. A pick persists `catalog_dialect`, `api_format` and `base_url` on the provider record — provider-global, not part of the session trio. Custom providers get the `Caps` row instead.

`+ add custom…` (`SENTINEL_CUSTOM_ADD` → `_open_custom_add_wizard`) never touches modelslist, and the name is still normalized with `core/modelslist.py:slugify`.

## Caps precedence

`core/context.py` resolves the output cap (`get_max_output_tokens`, highest wins):

1. `chat --max-tokens <n>` (per-call CLI flag);
2. the provider record's `max_output_tokens` override (custom providers; read fresh on every call, never cached);
3. modelslist's `top_provider.max_completion_tokens`;
4. modelslist's `context_length` (most APIs cap output at the context window);
5. `DEFAULT_MAX_OUTPUT_TOKENS = 16384` — the floor that stops Anthropic's silent 1024 default from truncating long tool calls.

There is no ceiling. `get_context_window` follows the same shape with the record's `max_context_window` override above modelslist's `context_length`. `_MODEL_CACHE` (no TTL) memoizes successful lookups only: a transport failure returns `{}` **uncached**, so one blip cannot pin a model to "no caps" for the life of the process. `clear_model_cache()` drops it.
