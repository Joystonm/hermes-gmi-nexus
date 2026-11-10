# Architecture

How GMI NEXUS is put together, and why it is put together that way.

---

## The shape of the thing

```
                      Hermes Agent
                           │
        ┌──────────────────┴──────────────────┐
        │                                     │
   eight tools                         /nexus command
        │                                     │
        └──────────────────┬──────────────────┘
                           │
                    nexus/orchestrator.py            ← the eight-step pipeline
                           │
   ┌───────────┬───────────┼───────────┬────────────┐
   │           │           │           │            │
registry    planner      router     executor      arena
   │           │           │           │            │
   └───── catalogue ───────┘           │            │
             │                          │            │
        storage.py  ←── measured outcomes ─────────┘
             │
   ┌─────────┴─────────┐
   │                   │
vision / creative   dashboard
```

Two halves, one database:

* the **agent half** (`nexus/`) is what Hermes loads as a plugin. It registers
  eight tools and one command.
* the **dashboard half** (`dashboard/`) is what the Hermes Web dashboard loads.
  It reads the same database and the same live catalogue. It never writes.

---

## The package

| Module | Responsibility |
| --- | --- |
| `nexus/orchestrator.py` | Owns the eight-step pipeline and holds the engine (`Nexus`). Constructing it does no network I/O. |
| `nexus/registry.py` | The live catalogue: language models and generation models, with a TTL cache, the source description and the probe results. |
| `nexus/classifier.py` | Turns a goal into a task type and the capabilities the work needs. |
| `nexus/planner.py` | Turns a goal into steps, each with its own task type and requirements. |
| `nexus/router.py` | Scores the catalogue against a step and returns a ranked list plus the reason each rejected model was ruled out. |
| `nexus/executor.py` | Runs the steps, in parallel where they are independent, and accounts for every call. |
| `nexus/evaluator.py` | Checks a result against the step's criteria. |
| `nexus/recovery.py` | The retry policy: bounded, and never the same model twice for the same step. |
| `nexus/budget.py` | The per-run ceiling, the rolling daily ceiling and the approval threshold. |
| `nexus/arena.py` | Identical test cases across several models; quality, correctness, latency, tokens, cost and reliability. |
| `nexus/storage.py` | The SQLite history: runs, steps, routing statistics, benchmarks, capability probes, usage and generation requests. |
| `nexus/media_catalog.py` | Each generation model's own published schema — its parameters, its price, its unit. |
| `nexus/media_router.py` | Chooses a generation model and builds the request body from that model's own schema. |
| `nexus/modules/vision.py` | Screenshot capture, image analysis, visual defect lists, before/after comparison. |
| `nexus/modules/creative.py` | Text-to-image, image-to-image, editing, masked editing, reference images, audio, video. |
| `nexus/modules/coding.py` | The Dev Agent: analyse, generate, review, diagnose, fix, test — through Hermes' own tools. |
| `nexus/providers/gmi_llm.py` | The OpenAI-compatible inference client. |
| `nexus/providers/gmi_media.py` | The request-queue client for image, video and audio. |
| `nexus/http.py` | One HTTP client for both, with the Cloudflare user-agent rule, bounded retries and a 429/5xx backoff. |
| `nexus/config.py` | Settings resolution and credential access. |
| `nexus/tools.py` | The eight tool handlers and their guard. |
| `nexus/schemas.py` | The JSON schemas the tools are registered with. |
| `nexus/models.py` | The dataclasses the whole plugin speaks in. |

---

## The parts that matter most

### Capabilities are verified, not guessed

A model is eligible for a task only if it can actually do it.

* **Essential capabilities** (`TEXT`, `VISION`, `IMAGE_OUT`, `AUDIO`, `VIDEO`)
  are a hard filter. A model that cannot read an image is never routed to an
  image-understanding task, whatever its name suggests.
* **Specialisations** (`CODE`, `REASONING`, `TOOLS`, `LONG_CONTEXT`) are a
  preference. A general model writes worse code than a code model, but it
  writes code, so it is ranked below the specialist rather than excluded.

Evidence is recorded with every capability — `metadata` when the catalogue
declared it, `probe` when NEXUS demonstrated it. A model whose capability has
no evidence is not trusted for it.

**The probe is a challenge, not a formality.** To check that a model can
really read an image, NEXUS draws a *random three-digit number* as pixels and
asks the model to read it back. The first version of this probe drew two
coloured squares and asked for their colours, and **13 of the 24**
catalogue-declared vision models passed it — "red" and "blue" being the two
most guessable colour words there are, so the probe measured the model's
willingness to guess rather than its eyesight. Against a drawn number, only
**5 of the 24** pass, and one of them answered `"456"` for a number it had
never seen.

The probe protocol is **versioned**. A verdict recorded under an older
protocol no longer counts in either direction, because it answered a
different question; the model keeps whatever the catalogue claims and is
probed again.

### The router scores, and explains itself

Each candidate is scored on the dimensions the task type cares about — quality,
reliability, latency, cost — with the strategy (`balanced`, `quality`, `fast`,
`cheap`) setting the weights. Measured history feeds in: the success rate and
the exponentially weighted moving averages for latency and cost.

Every rejected model is kept with the reason it was rejected, and the decision
is stored with the run. The report says not only which model ran, but why the
others did not.

### Two catalogues, and the one component that knows which is which

GMI Cloud exposes **two** catalogues, with different shapes: language models,
scored by `nexus/router.py`, and generation models, scored by
`nexus/media_router.py` against each model's own published parameter schema.
A step that generates an image is not routable to a chat model, and the
request it needs does not look like a chat request.

So the routing decision carries a `kind` — `"llm"` or `"media"` — and the
**executor** produces it. The executor is the component that carries the
route out, so it is the one that knows which catalogue a step belongs to.
Three things follow from that, and all three were learned the hard way:

* The ROUTE phase and the EXECUTE phase cannot disagree, because the
  execution honours the published route instead of re-deciding it.
* `nexus_analyze` previews the *same* decision the run will make, so the
  plan an agent is shown before it commits to anything is the plan it gets.
* A recovery keeps the kind of the decision it is recovering from, so a
  retried generation step is retried against the generation catalogue.

Routing everything through the language-model router — which is what the
pipeline did before — publishes *"no eligible model"* for every image step,
directly above a step whose own result reads *"ok — gpt-image-2"*.

### The cost of every call is known before it is made

`nexus/budget.py` prices a request from the catalogue's published rates before
the request goes out. Three limits apply:

* `max_cost_per_run_usd` — the ceiling for one run;
* `daily_budget_usd` — the rolling ceiling for the day;
* `approval_threshold_usd` — above this, the request is refused unless the
  caller explicitly approves it.

The estimate is the provider's list price with the model's published discount
applied. It is an estimate, and it is labelled as one everywhere it appears.

### Recovery is bounded and never repeats itself

A step that fails is retried on a *different* model, with the failure attached
to the prompt so the next model knows what went wrong. Two bounds apply: at
most `MAX_STEP_CALLS = 4` provider calls for one planned step (recovery
included) and at most `MAX_RECOVERIES = 2` recoveries per run. The models a
step has already tried are excluded from its retry candidates.

### Learning changes the routing, not the model

`nexus/storage.py` keeps a per-model, per-task-type row of runs, successes,
failures, and exponentially weighted moving averages for latency and cost. The
evaluator's verdict is what updates it — a step that "returned something" but
failed its own criteria counts as a failure.

No foundation model is modified, retrained or fine-tuned. NEXUS changes which
model it reaches for.

---

## Storage

Everything is kept in one SQLite database, in Hermes' own persistent plugin
data directory:

```
<hermes home>/plugin-data/gmi-nexus/data.db
```

The path is resolved through `plugins.plugin_storage.plugin_data_dir`, so it
follows the active Hermes profile and moves with it. Nothing is ever written
into the plugin installation directory — the plugin folder stays read-only in
practice, which is what makes it safe to reinstall or upgrade.

The schema is created on first use and is deliberately flat:

| Table | Holds |
| --- | --- |
| `runs` | One row per run: the goal, the task type, the status, the totals, the report. |
| `steps` | One row per step: what it did, which model did it, how it went. |
| `model_stats` | The routing statistics — the learning half. |
| `benchmarks`, `benchmark_results` | The arena history. |
| `capability_probes` | What was verified, how, and when. |
| `usage_events` | Every accounted call: the kind, the model, the cost, the tokens. |
| `media_requests` | Every generation that was actually submitted, with its payload hash. |

The `media_requests` table doubles as duplicate suppression: an identical
generation request is not submitted twice.

---

## The dashboard half

The dashboard is a separate surface from the same data, not a second
implementation of it.

* `dashboard/plugin_api.py` exports `router = APIRouter()` and is mounted by
  Hermes at `/api/plugins/gmi-nexus/`. Every handler is `async def` and hands
  its blocking work to `asyncio.to_thread`, so a slow catalogue read never
  stalls the dashboard's event loop.
* `dashboard/dist/index.js` is a plain IIFE. It uses the dashboard's own React
  and utilities from `window.__HERMES_PLUGIN_SDK__` and registers itself with
  `window.__HERMES_PLUGINS__.register("gmi-nexus", …)`. There is no bundler
  step and no build to run.
* `dashboard/dist/style.css` is scoped entirely under `.nx-`, and draws its
  colours from the dashboard's own `--color-*` tokens, so it follows the host
  theme and cannot leak into it.

The dashboard imports the NEXUS package by preferring the copy Hermes already
loaded as `hermes_plugins.<slug>.nexus`, so both halves share one module and
one database connection. If the agent half is not loaded, it loads the copy
beside it under a private module name.

It also resolves its settings through the same `nexus.config.resolve_settings`,
so both halves agree on every value and every validation.

### What the dashboard will not do

* It never spends money. Catalogue reads are free; generation is initiated by
  the agent through the tools, where Hermes' approval policy applies.
* It never returns the credential. It reads it to answer "is one configured?"
  and only the boolean crosses the boundary.
* It never invents a number. Every payload carries a `basis` — `measured`,
  `estimated` or `none` — and an empty section says why it is empty.

---

## Integration points, and only those

| Hermes surface | How NEXUS uses it |
| --- | --- |
| `ctx.register_tool` | The eight tools. `requires_env=["GMI_API_KEY"]`, and a `check_fn` so the model is never offered a tool that cannot work. |
| `ctx.register_command` | `/nexus`. |
| `ctx.get_config` | The plugin's settings, read from `plugins.entries.gmi-nexus.settings`. |
| `ctx.dispatch_tool` | The Dev Agent's write and test steps — Hermes' own `write_file` and `terminal` tools, so its approval policy and audit trail apply. |
| `agent.secret_scope.get_secret` | The GMI Cloud key. |
| `plugins.plugin_storage` | The persistent data directory. |
| `dashboard/manifest.json` + `dashboard/dist/` + `dashboard/plugin_api.py` | The dashboard tab. |

No other Hermes surface is touched, no core file is overridden, and no
dependency is added: the plugin imports only the standard library.
