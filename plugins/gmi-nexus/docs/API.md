# API

Two APIs: the eight tools Hermes exposes to the model, and the six routes the
dashboard reads.

Every example below was produced by running the code that serves it, not
written from memory. Where a long repeated element is elided the example
shows `...` in its place, so the shapes are exact even where the arrays are
not.

---

# The tools

Every tool returns a **JSON string**. On failure the string is a JSON object
with an `error` key:

```json
{"error": "'foo' is not in the live GMI Cloud catalogue, so there is nothing to benchmark."}
```

The toolset is `gmi_nexus`. All eight declare `requires_env=["GMI_API_KEY"]`,
and all but `nexus_report` are hidden from the model when no credential is
configured — the model is never offered a tool that cannot work.

---

# Three rules that hold whichever tool is called

These are not parameters. They are properties of the plugin, and they are
the reason a model cannot make it spend money, read a secret or reach the
machine's own network.

## 1. Only a person approves a spend

NEXUS registers two `pre_tool_call` hooks. The first works out an upper
bound on what the call may cost, using the catalogue it has **already read**
— it never makes a network call, because a hook that blocks on the network
blocks the tool behind it. The second hands the call to Hermes' own approval
prompt.

* A call whose estimate is below `approval_threshold_usd` goes straight
  through.
* A call whose estimate reaches the threshold is held, and the person
  answering Hermes' prompt is the only thing that unlocks it. The prompt
  says what is about to be spent and how much at most.
* A call whose cost **cannot be worked out** is also held. That is the case
  for a model the catalogue does not publish a price for, and for a
  catalogue NEXUS has not read yet. An unknown cost is not a zero cost.
* The approval rule is **per tool**: allowing `nexus_run` for the session
  does not allow `nexus_media`.

There is no `approve` argument anywhere in the eight schemas, and there is
no way for the model to mint an approval. The hook issues a single-use token
into a ledger inside the plugin; the token is random, tied to the arguments
it was issued for, and spent by the call it belongs to. The per-run cap and
the rolling daily cap stay hard limits underneath it, and nothing the model
says can lift them.

## 2. A named file is checked, not trusted

`nexus_media` takes an `image` and a `mask`; `nexus_vision` takes images. A
model can put a path in any of them, so the path is checked before the file
is opened:

* the path is resolved, so a symlink is followed to where it really points;
* it must be a regular file;
* its extension must name an image type **and** its first bytes must be
  that image type — an extension alone proves nothing, because the
  extension is the part a caller chooses;
* it must live in the working directory, NEXUS' own plugin-data directory or
  the system temporary directory;
* and it must never live inside Hermes' home directory, which is where the
  profile's `.env` and every other secret lives.

A file that fails any of these checks is refused with a message naming the
file and the rule it broke. Nothing is uploaded.

## 3. A named URL is checked, not trusted

`nexus_vision` downloads a remote image so it can be inlined. The URL came
from a model, so every hop of the redirect chain is resolved and checked
before anything connects to it:

* only `http` and `https` are fetched, and a URL carrying credentials in its
  authority is refused;
* every address the host answers with must be a public one — a name that
  answers with both a public and a private address is refused, because which
  one the socket picks is not ours to decide;
* private, loopback, link-local, reserved and multicast addresses are all
  refused, `169.254.169.254` among them;
* a host that cannot be resolved is refused, because an address that cannot
  be checked is not an address that has been checked;
* the image type comes from the bytes, not from the server's `Content-Type`,
  because a header is a claim and the bytes are the file.

A refused URL is **not** a failed download: NEXUS does not fall back on
sending it along.

---

## `nexus_models`

List what GMI Cloud actually offers right now.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `query` | string | — | Substring filter on the model id or display name. |
| `modality` | string | `llm` | `llm`, `image`, `video` or `audio`. |
| `capability` | string | — | Only models verified to have this capability. |
| `verified_only` | boolean | `false` | Only models whose capability came from a probe. |
| `refresh` | boolean | `false` | Bypass the cache and re-read the catalogue. |
| `limit` | integer | `40` | Maximum models to return (capped at 200). |

```json
{
  "catalogue": "gmi-llm",
  "count": 37,
  "source": {
    "endpoint": "https://api.gmi-serving.com/v1/models",
    "media_endpoint": "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/models",
    "fetched_at": 1762300000.0,
    "age_seconds": 12.4,
    "from_cache": false,
    "error": null,
    "note": "The live GMI Cloud catalogue, read at runtime — not a built-in list."
  },
  "models": [
    {
      "id": "gmicloud/Ripple-65B-Preview",
      "name": "ricursiveAI/Ripple-65B-Preview",
      "owned_by": "ricursiveAI",
      "context_length": 1048576,
      "quantization": "bf16",
      "pricing": {
        "prompt": 2e-7, "completion": 6e-7, "request": 0.0, "image": 0.0,
        "input_cache_read": 0.0, "input_cache_write": 0.0,
        "overrides": [], "discount_to_user": 0.4
      },
      "capabilities": ["long_context", "reasoning", "text", "tools"],
      "capability_evidence": {
        "text": "metadata:input_modalities",
        "reasoning": "heuristic:model-id-pattern",
        "tools": "heuristic:model-id-pattern",
        "long_context": "metadata:context_length=1048576"
      },
      "is_ready": true,
      "is_free": false,
      "description": "A reasoning model with native tool calling."
    }
  ],
  "truncated": false
}
```

Prices are **USD per token** at the catalogue's list price, before the model's
own discount. A model with no published capability metadata is listed with
`capabilities: []` — NEXUS does not guess.

`capability_evidence` says where each capability came from. `metadata:…` means
the catalogue published it; `heuristic:…` means NEXUS inferred it from the
model's id or its context length; `probe:…` means NEXUS verified it by
actually sending a request. A capability NEXUS cannot account for is not
claimed.

For the generation catalogues the shape is different — each entry carries its
modality, its description and whether it is a product model:

```json
{
  "catalogue": "gmi-image",
  "count": 12,
  "models": [
    {"id": "gpt-image-2", "modality": "image", "description": "...", "product": true}
  ],
  "source": {
    "endpoint": "https://api.gmi-serving.com/v1/models",
    "media_endpoint": "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/models",
    "fetched_at": 1762300000.0,
    "age_seconds": 12.4,
    "from_cache": false,
    "error": null,
    "note": "The live GMI Cloud catalogue, read at runtime — not a built-in list."
  },
  "truncated": false,
  "note": "Each model's own published description, price and parameter schema are read before a request is built for it."
}
```

`source.error` is `null` on success and carries the provider's message when
the catalogue could not be read. When it is not null, `models` is empty and
the tool says so rather than showing a stale list as though it were current.

---

## `nexus_analyze`

Understand a goal, break it into steps and show where each step would route.
**Calls no model and spends nothing** — the catalogue read is free.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `goal` | string | **yes** | What you want accomplished, in plain language. |
| `strategy` | string | no | `balanced` (default), `quality`, `fast` or `cheap`. |

```json
{
  "goal": "Build a landing page for a new AI startup",
  "understanding": {
    "task_type": "coding",
    "task_label": "Coding",
    "confidence": 0.9,
    "evidence": ["rule:coding:matched 'Build'"],
    "required_capabilities": ["text", "code"],
    "modalities": [],
    "model_assisted": false,
    "requested_model": null,
    "media_count": 0,
    "text_length": 41
  },
  "plan": {
    "goal": "Build a landing page for a new AI startup",
    "steps": [
      {
        "index": 0,
        "description": "Analyse the brief and settle the page structure",
        "task_type": "planning",
        "prompt": "Original goal: Build a landing page for a new AI startup\n\nYour job: Analyse the brief and settle the page structure. State the concrete decisions and structure that the following steps will use.",
        "modalities": [],
        "depends_on": [],
        "media_model": null,
        "metadata": {}
      }
    ],
    "notes": ["Plan derived from the deliverable template."]
  },
  "routing_preview": [
    {
      "step_index": 0,
      "description": "Analyse the brief and settle the page structure",
      "task_type": "planning",
      "decision": {
        "task_type": "planning",
        "task_label": "Planning",
        "required_capabilities": ["text", "reasoning"],
        "strategy": "balanced",
        "kind": "llm",
        "candidates": [
          {
            "model": "gmicloud/Ripple-65B-Preview",
            "kind": "llm",
            "score": 0.6849,
            "reasons": [
              "quality: no benchmark data, using prior",
              "reliability: no recorded runs, using prior",
              "cost: ~$0.000006 estimated at list price"
            ],
            "estimated_cost_usd": 5.76e-6,
            "benchmark_score": null,
            "historical_success": null
          }
        ],
        "rejected": {
          "openai/gpt-4o-mini": "missing required capabilities: reasoning"
        }
      }
    }
  ],
  "catalogue": {
    "llm_models": 37,
    "media_models": 21,
    "media_by_modality": {"image": 14, "video": 5, "audio": 2},
    "vision_capable": 12,
    "tool_capable": 19,
    "fetched_at": 1762300000.0,
    "catalogue_age_seconds": 12.4,
    "error": null
  },
  "budget": {
    "daily_budget_usd": 5.0,
    "daily_spend_usd": 0.42,
    "daily_remaining_usd": 4.58,
    "max_cost_per_run_usd": 1.0,
    "run_spend_usd": 0.0,
    "run_remaining_usd": 1.0,
    "approval_threshold_usd": 0.25,
    "note": "figures are estimates at list price with the model's published discount applied"
  },
  "cost": {
    "spent_usd": 0.0,
    "note": "Analysis reads the free catalogue; it calls no model."
  }
}
```

`understanding.evidence` names the rule that fired, so a classification is
traceable rather than asserted. `task_type` is one of `general_reasoning`,
`complex_reasoning`, `coding`, `debugging`, `code_review`,
`image_understanding`, `document_analysis`, `image_generation`,
`image_editing`, `audio`, `video`, `multimodal` or `planning`.

`routing_preview` has one entry per planned step. `rejected` is a map of model
id to the reason it was ruled out. A model that cannot do the work is never
merely ranked low — it is named and explained. `benchmark_score` and
`historical_success` are `null` until an Arena run or a recorded run gives
NEXUS something measured to say.

The preview is the *same* decision the run will make: it comes from the
component that carries the route out, and a generation step is previewed
against the generation catalogue, not the language-model one. What
`nexus_analyze` shows and what `nexus_run` then does cannot disagree.

---

## `nexus_run`

Run a goal through the whole eight-step pipeline.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `goal` | string | **yes** | What you want accomplished. |
| `strategy` | string | no | `balanced` (default), `quality`, `fast` or `cheap`. |

```json
{
  "run_id": "run_1762300000_ab12cd",
  "goal": "Build a landing page for a new AI startup",
  "status": "completed",
  "task_type": "coding",
  "task_label": "Coding",
  "plan": {"goal": "...", "steps": [...], "notes": [...]},
  "steps": [
    {
      "step_index": 0,
      "description": "Analyse the brief and settle the page structure",
      "task_type": "planning",
      "status": "ok",
      "model_id": "gmicloud/Ripple-65B-Preview",
      "output": "...",
      "media_urls": [],
      "usage": {"prompt_tokens": 812, "completion_tokens": 430, "total_tokens": 1242, "requests": 1, "cost_usd": 0.0021, "measured": true},
      "latency_ms": 1840,
      "attempts": 1,
      "attempted": ["gmicloud/Ripple-65B-Preview"],
      "fallbacks": [],
      "error": null,
      "validation": {"passed": true, "criteria": ["non-empty output", "no unresolved placeholders"]},
      "estimated_cost_usd": 0.0021
    }
  ],
  "total_cost_usd": 0.0064,
  "total_latency_ms": 5210,
  "models_used": ["gmicloud/Ripple-65B-Preview"],
  "routes": [
    {
      "task_type": "planning",
      "task_label": "Planning",
      "required_capabilities": ["text", "reasoning"],
      "kind": "llm",
      "candidates": [
        {
          "model": "gmicloud/Ripple-65B-Preview",
          "kind": "llm",
          "score": 0.6849,
          "reasons": ["..."],
          "estimated_cost_usd": 5.76e-6,
          "benchmark_score": null,
          "historical_success": null
        }
      ],
      "rejected": {...},
      "strategy": "balanced"
    }
  ],
  "messages": [],
  "created_at": 1762300000.0
}
```

`status` is `completed`, `failed` or `partial`. A step's `status` is `ok`,
`failed` or `skipped`. When a request is refused by the budget, the tool
returns an error rather than a report that looks successful.

`kind` says which catalogue a route came from: `"llm"` for a step that is
answered by a language model, `"media"` for a step that is *generated* by an
image, video or audio model. The two are different catalogues with different
schemas, so a generation step is scored against the generation catalogue
rather than the language-model one — the first candidate of a `"media"`
route is a generation model, and `rejected` explains why the others were
passed over. The same `kind` appears on each candidate, so a consumer never
has to guess whether `"gpt-image-2"` is a chat endpoint or a generator.

---

## `nexus_benchmark`

Run identical test cases across several models and report what happened.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `models` | array of string | no | Model ids to test. Omit to let NEXUS pick a diverse set. |
| `suite` | string | no | `default` covers instruction following, JSON contracts, code generation and arithmetic. |

```json
{
  "scores": {
    "gmicloud/Ripple-65B-Preview": 0.75,
    "openai/gpt-4o-mini": 0.5
  },
  "models": [
    {
      "model_id": "gmicloud/Ripple-65B-Preview",
      "cases": 4,
      "completed": 4,
      "failed": 0,
      "passed": 3,
      "pass_rate": 0.75,
      "mean_score": 0.8125,
      "total_latency_ms": 1840,
      "mean_latency_ms": 460,
      "prompt_tokens": 412,
      "completion_tokens": 168,
      "total_cost_usd": 0.00076,
      "errors": []
    }
  ],
  "measured": true,
  "note": "every figure here comes from real API calls: latencies were measured, token counts came from the provider's usage block, and costs use the catalogue's published prices",
  "benchmark_id": "9f2c1ab34d5e4f7a8b0c2d1e3f4a5b6c",
  "suite": "default",
  "cases": 4,
  "duration_seconds": 6.42,
  "cost_note": "Costs below are measured from the provider's own usage blocks, priced at the catalogue's published rates."
}
```

`models` is one row per model, sorted by pass rate and then by latency;
`scores` is the same pass rate keyed by model id. Every score comes from a
real request. Nothing is simulated and nothing is extrapolated — a model that
failed a test is recorded as failed, with the error, and the row's `failed`
count goes up rather than the case being quietly dropped.

The per-case detail is stored, not returned in this payload: read it back
with the dashboard's Arena route or the report's `benchmarks` section.

---

## `nexus_vision`

Look at images.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `mode` | string | no | `analyze` (default), `issues`, `compare` or `screenshot`. |
| `images` | array of string | yes, except for `screenshot` | Image paths, URLs or `data:` URIs. |
| `question` | string | no | The question to answer about the image(s). |
| `before` / `after` | string | yes, for `compare` | The two images to compare. |
| `criteria` | string | no | Extra criteria for `issues` — e.g. `matches the brand palette`. |
| `app` | string | no | Which application to capture, for `screenshot`. |

```json
{
  "mode": "issues",
  "ok": true,
  "text": "The header logo overlaps the navigation at 1280px.",
  "model_id": "gmicloud/Vision-Model",
  "cost_usd": 0.0031,
  "latency_ms": 3120,
  "images": ["C:/tmp/home.png"],
  "structured": {
    "issues": [
      {
        "severity": "high",
        "area": "header",
        "problem": "The logo overlaps the navigation on a 1280px viewport.",
        "fix": "Give the header a minimum height of 64px and stop the logo overlapping."
      }
    ],
    "summary": "The header overlaps and two spacing values are inconsistent."
  },
  "error": null
}
```

`mode: "analyze"` returns the same shape with `structured` empty and the
model's answer in `text`. `mode: "compare"` returns `structured` with
`changes`, `improvements`, `regressions` and `summary`.

`ok` is `false` and `error` carries the reason when the request failed. A
failed vision call is never reported as an empty list of issues.

Only a model whose vision capability is **verified** is used. If
`verify_vision_capability` is on and the model has not been probed, NEXUS
probes it first — with a tiny image — and refuses to route to it if the probe
fails.

---

## `nexus_media`

Generate or edit images, video and audio.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `prompt` | string | **yes** | What to generate. |
| `modality` | string | no | `image` (default), `video` or `audio`. |
| `options` | object | no | Generation options — `aspect_ratio`, `size`, `width`, `height`, `duration`, `resolution`, `negative_prompt`, `seed`, `reference_image`. |
| `image` | string | no | An existing image to edit. A path, URL or `data:` URI. |
| `mask` | string | no | The mask that selects the region to edit. |

```json
{
  "ok": true,
  "modality": "image",
  "model_id": "gpt-image-2",
  "request_id": "req_1762300000_zz",
  "media_urls": ["https://cdn.gmicloud.ai/out-1.png"],
  "thumbnail_url": "https://cdn.gmicloud.ai/thumb-1.png",
  "status": "success",
  "estimated_cost_usd": 0.042,
  "dropped_options": [],
  "deduplicated": false,
  "latency_ms": 4210,
  "error": null,
  "candidates": [],
  "cost_note": "estimated_cost_usd is priced from the model's published rate before the request; it is not a billed amount."
}
```

**Each model's own published schema decides the request body.** NEXUS reads
`GET /apikey/models/{id}` for the model it picked and builds the payload from
*that* schema — it never reuses one model's parameters for another. If the
model published no schema, NEXUS says so rather than guessing.

`dropped_options` names the options you supplied that *this* model does not
publish, so they were left out of the request rather than sent and rejected.
An option the model requires and you did not supply is an error, not a silent
default.

`estimated_cost_usd` is priced from the model's published rate **before** the
request. It is an estimate and is labelled as one — NEXUS never reports it as
a billed amount.

`status` is `success` only when the provider actually returned an asset. A
failed request is reported as failed, with the provider's error, and
`ok` is `false`.

---

## `nexus_dev`

Implement a code change with a GMI model, using **Hermes' own tools** for the
writing and the testing.

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `mode` | string | no | `implement` (default) writes and tests a change; `review` reports problems and writes nothing. |
| `task` | string | **yes** | The change to make, described precisely. |
| `files` | array of string | no | Paths of the files the task touches. They are read and given to the model as context. |
| `test_command` | string | no | The command that runs the tests, e.g. `pytest -q`. |
| `max_attempts` | integer | no | How many times to attempt the fix (default 2). |

```json
{
  "mode": "implement",
  "ok": true,
  "files": [
    {"path": "src/api/health.py", "bytes": 812, "written": true, "error": null}
  ],
  "notes": "Added a /health route that reports the service and database status.",
  "model_id": "gmicloud/Code-Model",
  "attempts": 1,
  "tests_passed": true,
  "test_output": "12 passed in 1.4s",
  "cost_usd": 0.0062,
  "latency_ms": 18400,
  "error": null,
  "findings": [],
  "recovery": [],
  "execution_note": "Files were written and commands were run through Hermes' own write_file and terminal tools, so Hermes' approval policy applied."
}
```

`mode: "review"` returns the same shape with `ok` reporting whether the review
ran, `files` empty — a review writes nothing — and `findings` holding the
problems that were found.

`recovery` lists what each model that was tried did, in order. A run that
fell back from one model to another says so here, so the model that actually
ran can be compared with the model that was routed to:

```json
"recovery": [
  {"model_id": "acme/qwen3-coder-plus", "outcome": "call failed: Backend request failed with status 400"}
]
```

The write and the test run go through Hermes' `write_file` and `terminal`
tools. That means Hermes' approval policy, its sandbox and its audit trail
apply to NEXUS exactly as they apply to the agent — NEXUS does not spawn a
second execution environment, and it never runs a destructive command on its
own. A failing test is fed back to the model for a fix, and the fix is
verified before it is reported as done. `tests_passed` is `null` when no test
command was given, so "not run" is never shown as "passed".

The model's calls go through the same spend limits as every other path: the
per-run cap, the rolling daily cap and the approval threshold are all checked
**before** the request goes out. A call that reaches the threshold is held
for a person: NEXUS registers a `pre_tool_call` hook that hands the call to
Hermes' own approval prompt, and only the person answering that prompt
unlocks it. There is no `approve` argument — the model cannot approve its own
spend.

The call that asks for files asks for `code_max_tokens` of room — 8192 by
default. The inference client's own default of 2048 is enough for a sentence
and not enough for a source file, and a reply that runs out of budget comes
back cut off mid-JSON. NEXUS reads the finish reason and reports that case
as **truncated** rather than as an unusable reply, because the two need
different fixes.

---

## `nexus_report`

Report what NEXUS has actually done. **Works without a credential**, because it
reads the recorded history alone.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `days` | integer | `30` | How many days of history to include. |
| `run_id` | string | — | Return the full detail of one run instead. |

```json
{
  "catalogue": {"llm_models": 37, "media_models": 21, "media_by_modality": {"image": 14, "video": 5, "audio": 2}, "vision_capable": 12, "tool_capable": 19, "fetched_at": 1762300000.0, "catalogue_age_seconds": 42.1, "error": null},
  "budget": {
    "daily_budget_usd": 5.0,
    "daily_spend_usd": 0.42,
    "daily_remaining_usd": 4.58,
    "max_cost_per_run_usd": 1.0,
    "run_spend_usd": 0.0064,
    "run_remaining_usd": 0.9936,
    "approval_threshold_usd": 0.25,
    "note": "figures are estimates at list price with the model's published discount applied"
  },
  "routing": {
    "strategy_weights": {
      "balanced": {"quality": 0.35, "reliability": 0.3, "latency": 0.15, "cost": 0.2},
      "quality": {"quality": 0.6, "reliability": 0.25, "latency": 0.05, "cost": 0.1},
      "fast": {"quality": 0.2, "reliability": 0.2, "latency": 0.45, "cost": 0.15},
      "cheap": {"quality": 0.2, "reliability": 0.2, "latency": 0.15, "cost": 0.45}
    },
    "preferred_models": [],
    "blocked_models": [],
    "models_with_benchmark_data": 6,
    "models_with_run_history": 11
  },
  "usage": {
    "window_days": 30,
    "total_cost_usd": 0.42,
    "prompt_tokens": 812340,
    "completion_tokens": 210983,
    "requests": 214,
    "events": 190,
    "by_model": [{"model_id": "...", "events": 42, "cost": 0.21, "tokens": 120433}],
    "by_kind": [{"kind": "llm", "events": 180, "cost": 0.31}]
  },
  "recent_runs": [
    {"id": "run_1762300000_ab12cd", "goal": "...", "task_type": "coding", "status": "completed", "created_at": 1762300000.0, "finished_at": 1762300005.2, "total_cost_usd": 0.0064, "total_latency_ms": 5210, "models_used": ["..."]}
  ],
  "benchmarks": [],
  "model_stats": [{"model_id": "...", "task_type": "coding", "runs": 12, "successes": 11, "failures": 1, "ewma_latency_ms": 1780.2, "ewma_cost_usd": 0.0021, "total_cost_usd": 0.0252, "last_used_at": 1762300000.0}],
  "capability_probes": [],
  "days": 30,
  "cost_note": "Costs are estimates priced at the catalogue's published rates; the token counts are the provider's own reported usage.",
  "recent_runs_note": "No run has been recorded yet — run nexus_run to create one."
}
```

`recent_runs_note` appears only when nothing has been recorded yet. `usage` is
grouped over the retention window; `by_model` and `by_kind` are inside it.
The `budget` figures are estimates, as its own `note` says.

With a `run_id`, the tool returns that one run in full instead — the same
shape as the dashboard's `GET /execution?run_id=`:

---

# The slash command

```
/nexus status    # the last 7 days
/nexus models    # the live catalogue
/nexus report    # the last 30 days
```

---

# The dashboard API

Mounted by Hermes at `/api/plugins/gmi-nexus/`. Every route is `async def` and
hands its blocking work to a worker thread, so a slow catalogue read never
stalls the dashboard's event loop.

Every response carries a `basis`:

| `basis` | Meaning |
| --- | --- |
| `measured` | The figures came from the recorded history or a live read. |
| `estimated` | The figures are priced from the catalogue's published rates. |
| `none` | There is nothing to report, and `empty_reason` says why. |

No route returns the credential. The settings route reports whether one is
configured, and nothing else.

---

## `GET /overview?days=30`

The landing view.

```json
{
  "catalogue": {"llm_models": 37, "media_models": 21, "media_by_modality": {"image": 14, "video": 5, "audio": 2}, "vision_capable": 12, "tool_capable": 19, "fetched_at": 1762300000.0, "catalogue_age_seconds": 42.1, "error": null},
  "catalogue_source": {"endpoint": "...", "fetched_at": 1762300000.0, "age_seconds": 12.4, "from_cache": false},
  "catalogue_error": null,
  "budget": {"daily_budget_usd": 5.0, "daily_spend_usd": 0.42, "daily_remaining_usd": 4.58, "max_cost_per_run_usd": 1.0, "run_spend_usd": 0.0, "run_remaining_usd": 1.0, "approval_threshold_usd": 0.25},
  "usage": {"total_cost_usd": 0.42, "requests": 214, "by_model": [...]},
  "series": [{"day": "2026-11-05", "ts": 1762300800.0, "cost_usd": 0.12, "tokens": 40210, "requests": 48}],
  "recent_runs": [...],
  "model_stats": [...],
  "credential_configured": true,
  "spend_note": "Spend is the provider's reported usage priced at the catalogue's published rates — an estimate, not a bill.",
  "empty": false,
  "empty_reason": "",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

`series` is the spend grouped by UTC day — a chart needs a series, and a total
cannot draw one.

`empty` is `true` when nothing has been recorded, and `empty_reason` then says
so in words. `catalogue_error` is `null` unless the live catalogue could not be
read, in which case the catalogue figures are absent rather than stale.

---

## `GET /models?limit=200`

The live language-model catalogue, with the measured performance of each model
attached.

```json
{
  "count": 37,
  "models": [
    {
      "id": "gmicloud/Ripple-65B-Preview",
      "capabilities": ["text", "tools"],
      "capability_evidence": {"text": "metadata:input_modalities", "tools": "heuristic:model-id-pattern"},
      "pricing": {"prompt": 2e-7, "completion": 6e-7},
      "measured": {"model_id": "...", "task_type": "coding", "runs": 12, "successes": 11, "failures": 1, "ewma_latency_ms": 1780.2, "ewma_cost_usd": 0.0021, "total_cost_usd": 0.0252, "last_used_at": 1762300000.0}
    }
  ],
  "truncated": false,
  "source": {"endpoint": "...", "fetched_at": 1762300000.0, "age_seconds": 12.4, "from_cache": false},
  "catalogue_error": null,
  "empty": false,
  "empty_reason": "",
  "note": "Capabilities come from the catalogue's own metadata or from a probe NEXUS ran; a capability without evidence is unverified.",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

`measured` is `null` for a model that has never been routed to. The UI shows
"not measured" rather than a zero.

---

## `GET /arena?limit=20`

The recorded benchmark runs and their per-model results.

```json
{
  "benchmarks": [{"id": "bench_1762300000_x9", "suite": "default", "models": ["..."], "status": "completed", "created_at": 1762300000.0}],
  "results": {"bench_1762300000_x9": [{"model_id": "...", "task": "...", "passed": 1, "score": 1.0, "latency_ms": 412, "cost_usd": 0.00019}]},
  "empty": false,
  "empty_reason": "",
  "note": "Every score here comes from a real request. Nothing is simulated and nothing is extrapolated.",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

---

## `GET /execution?days=30`

The recorded runs, the routing statistics and the capability probes.

```json
{
  "runs": [...],
  "model_stats": [...],
  "capability_probes": [{"model_id": "...", "capability": "vision", "verified": 1, "evidence": "probe: the model described the probe image", "probed_at": 1762300000.0}],
  "usage": {...},
  "empty": false,
  "empty_reason": "",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

---

## `GET /execution?run_id=run_1762300000_ab12cd`

One run in full: its steps and its routing decisions.

```json
{
  "run": {
    "id": "run_1762300000_ab12cd",
    "goal": "...",
    "task_type": "coding",
    "status": "completed",
    "created_at": 1762300000.0,
    "finished_at": 1762300005.2,
    "total_cost_usd": 0.0064,
    "total_latency_ms": 5210,
    "models_used": ["..."],
    "report": "{...}",
    "steps": [
      {
        "id": 1, "run_id": "run_1762300000_ab12cd", "seq": 0,
        "description": "...", "task_type": "planning", "status": "ok",
        "model_id": "...", "output": "...", "media_urls": [],
        "latency_ms": 1840, "attempts": 1,
        "prompt_tokens": 812, "completion_tokens": 430,
        "cost_usd": 0.0021, "error": null, "validation": {"passed": true}
      }
    ]
  },
  "runs": [...],
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

An unknown `run_id` returns **404** with the detail
`No run with id '<id>' is recorded.` — not a 200 with an empty body, and never
an invented run.

---

## `GET /multimodal?limit=12`

The generation catalogue, with each model's own published schema.

```json
{
  "count": 21,
  "by_modality": {"image": 12, "video": 6, "audio": 3},
  "models": [
    {
      "id": "gpt-image-2",
      "modality": "image",
      "spec": {
        "unit": "image",
        "unit_price_usd": 0.042,
        "parameters": [{"name": "aspect_ratio", "type": "str", "required": false, "options": ["1:1", "16:9"]}]
      },
      "spec_available": true
    }
  ],
  "described": 12,
  "truncated": true,
  "requests": [{"request_id": "req_...", "model_id": "gpt-image-2", "status": "success", "media_urls": ["..."], "cost_usd": 0.042}],
  "source": {"endpoint": "...", "fetched_at": 1762300000.0, "age_seconds": 12.4, "from_cache": false},
  "catalogue_error": null,
  "empty": false,
  "empty_reason": "",
  "note": "Each model's own published description decides the request that is built for it — NEXUS never reuses one model's parameters for another.",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

`spec_available: false` means the model published no schema — and NEXUS will
not build a request for it.

---

## `GET /settings`

The settings in force, and the limits they impose.

```json
{
  "settings": {"llm_base_url": "...", "max_cost_per_run_usd": 1.0, "daily_budget_usd": 5.0, "enable_learning": true, "...": "..."},
  "budget": {"daily_budget_usd": 5.0, "daily_spend_usd": 0.42, "daily_remaining_usd": 4.58},
  "credential_configured": true,
  "credential_note": "The credential lives in Hermes' secret scope. NEXUS reads it from there and never writes it into the plugin, the logs or this response.",
  "note": "These are the settings in force right now, resolved from config.yaml, the environment and the plugin manifest.",
  "basis": "measured",
  "generated_at": 1762300812.4
}
```

`settings` is `nexus.config.Settings.to_dict()`, which by construction does
not contain the credential. The dashboard resolves its settings through the
same function the agent half uses, so the two can never disagree.
