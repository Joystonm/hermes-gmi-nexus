# Hermes validation

How GMI NEXUS was verified, and what each kind of verification actually
proves. The four levels are kept separate on purpose: a mocked test that
passes says nothing about the network, and a live request that succeeds says
nothing about how Hermes loads the plugin.

| Level | What it proves | Reaches the network | Costs money | How to run |
| --- | --- | --- | --- | --- |
| **1. Unit** | The logic is right, given its inputs. | No | No | `python -m pytest tests/` |
| **2. Mock integration** | The components fit together: the pipeline, the storage, the router, the dashboard's API. | No | No | `python -m pytest tests/` |
| **3. Live API** | The integration with GMI Cloud is real. | Yes | The catalogue reads are free; one inference call is not | `GMI_LIVE_TESTS=1 GMI_API_KEY=… pytest tests/test_live.py` |
| **4. Hermes plugin** | Hermes discovers the plugin, loads it, and gets the eight tools and the command. | No | No | `python scripts/validate_installation.py` and `hermes plugins validate` |

None of these results are simulated. Where something was not tested, this
document says so.

---

## Level 1 — unit tests

**357 tests collected, 8 skipped, 349 passed. All passing.**

```
$ python -m pytest tests/
349 passed, 8 skipped in 14.58s
```

On an interpreter carrying Hermes' own `web` pins the same suite was last
recorded at **350 passed, 7 skipped** — the difference is one test that only
skips on a system interpreter whose `fastapi` and `starlette` disagree. See
*The environment the tests run in* below.

Each test file covers one module:

| File | Tests | What it covers |
| --- | --- | --- |
| `test_classifier.py` | 11 | Goal classification, and the capabilities each task type requires. |
| `test_registry.py` | 15 | Catalogue reading, the TTL cache, capability extraction, the catalogue ordering, probe results, and the refusal to restore a verdict from an older probe protocol. |
| `test_router.py` | 26 | Scoring, the essential-capability filter, the rejected list and its reasons. |
| `test_pipeline.py` | 25 | The eight-step pipeline end to end, with the provider mocked — including that a generation step is routed against the generation catalogue, that the published route names the model that ran, and that a step is given the token budget its answer needs. |
| `test_arena.py` | 15 | Identical test cases across models, the dimensions, the reports. |
| `test_storage.py` | 22 | The SQLite schema, the aggregation queries, the history's ordering. |
| `test_vision.py` | 32 | Screenshot analysis, defect lists, before/after comparison, the drawn-number probe, refusals, and the inlining of remote images. |
| `test_creative.py` | 21 | Generation, editing, masked editing, and the per-model parameter schemas. |
| `test_dev.py` | 22 | The Dev Agent's analyse → generate → write → test → fix loop, through Hermes' own tools — including that the fix prompt carries the original task, that a call is given room to write a whole file, and that the budget gate is consulted before the money is spent. |
| `test_plugin.py` | 24 | The registration contract: the eight tools, their schemas, the command, the credential handling. |
| `test_approval.py` | 36 | The spend rules: that no schema offers an `approve` argument, that a call over the threshold is held for a person through Hermes' own prompt, that the rule is per tool, that a call whose cost cannot be worked out is held, that the single-use token is bound to its tool and its arguments and cannot be minted by the model, and that a model with no published price is never estimated at $0.00. |
| `test_safety.py` | 41 | The file and URL rules: that the profile's own files are never read, that a file is checked by its bytes and not only by its name, that a symlink is followed to where it points, that an internal address — `169.254.169.254` among them — is refused on every redirect hop, and that an unresolvable host is refused. |
| `test_dashboard.py` | 24 | The dashboard's six routes, their payloads and their empty states, and the bundle the manifest points at: that the files it names exist, that `dist/` is what `src/` builds, and that nothing ignores them out of the repository. |
| `test_resilience.py` | 14 | What the live runs taught the plugin to survive: the provider's real reason read out of a nested error, a retired model remembered and excluded from the candidates, a code reply whose newlines were written literally, and the retry that shows the model the reply which failed. |
| `test_video_claims.py` | 24 | The demo video's narration, and the check that keeps it true: the numbers a person says, reading a value out of a run's JSON, and the refusal to synthesise or cut a video whose voice describes a run that did not happen. |
| `test_live.py` | 5 | **Level 3.** Skipped unless explicitly opted in. |

The **5 skipped** tests are all in `test_live.py` — they are skipped because
`GMI_LIVE_TESTS` is not set, which is the intended behaviour. On a system
interpreter whose `fastapi` and `starlette` disagree, a sixth test skips:
`test_the_environment_can_build_a_fastapi_router`, which reports that
environment fault explicitly rather than hiding it. Two more skip where the
platform does not allow the test to create a symbolic link, which is what
Windows does without the developer mode; the rule those two check is the same
rule the third test covers.

### The environment the tests run in

The dashboard tests substitute a faithful stand-in for `fastapi.APIRouter`
when the installed `fastapi` and `starlette` cannot agree on the `Router`
signature — an environment fault that breaks Hermes' own dashboard too. The
substitute exercises the same code, but the real router is better, so the
suite was re-run in a virtual environment carrying Hermes' declared pins:

```
$ python -m pytest tests/          # fastapi 0.133.1, starlette 1.9.0
350 passed, 7 skipped in 15.85s
```

That run was recorded before the suite grew by the approval and safety tests,
so it is quoted as it was measured rather than restated as a current figure.

Under those pins the plugin's real `dashboard/plugin_api.py` was loaded
against a genuine `fastapi.routing.APIRouter`, and it registered exactly the
six documented routes with every handler `async def` — which is what Hermes'
mounting contract requires.

The pins live in the virtual environment and shadow the system
interpreter's copies, so nothing on the machine is changed. See `SETUP.md`
for how the environment is created.

---

## Level 2 — mock integration

Included in the 349 above; this level describes *which* of them are
integration-shaped rather than unit-shaped.

The provider is stubbed at the HTTP layer (`nexus.http.HttpResponse`), never
above it, so the code under test is the real code: the real planner, the real
router, the real executor, the real evaluator, the real storage and the real
report. Only the socket is absent.

| Area | What the mock integration proves |
| --- | --- |
| **The pipeline** | A goal becomes a plan, the plan becomes routed steps, the steps run, the results are evaluated, a failure is recovered on a different model, the outcome is learned, and the report comes back — with the plan, the routing decisions and the costs in it. |
| **The budget** | A run that would exceed `max_cost_per_run_usd` is refused *before* the request goes out. A request at or above `approval_threshold_usd` is held for a person through Hermes' own approval prompt — the model cannot approve its own spend, and a request whose cost cannot be worked out is always held. The daily ceiling holds across runs. |
| **The recovery policy** | A failing step is retried on a *different* model, the models already tried are excluded, and both bounds hold: at most 4 provider calls per step and at most 2 recoveries per run. |
| **The learning loop** | A verified success updates the model's statistics, and the statistics change the routing of a later step. |
| **Storage** | The history lands in Hermes' plugin-data directory — not in the plugin — and follows `HERMES_HOME` when that changes. |
| **The dashboard API** | All six routes return their documented shape, every payload carries a `basis`, an empty section says why it is empty, and the credential never appears in any response. |

### The demo run, which is this level end to end

`scripts/demo_run.py --dry-run` is the whole scenario as one continuous run:
analyse, break down, route, execute, generate an asset, implement through
Hermes' tools, inspect with a vision model, correct, verify, report. The
provider is stubbed at the HTTP layer exactly as the tests do it, so the
router really scores the catalogue, the budget really enforces its limits,
the Dev Agent really writes a file and really runs its test against it. The
run's status comes back `completed`, and the file on disk is the file the
test passed on.

What it does **not** do is produce a real model's answer. Every simulated
value is labelled `simulated` in the transcript the script writes, so a dry
run can never be mistaken for a live one.

---

## Level 3 — live API tests

**Not run as part of the default suite. They are opt-in and they cost money.**

```
GMI_LIVE_TESTS=1 GMI_API_KEY=<key> python -m pytest tests/test_live.py -v
```

Both conditions must hold: a real credential *and* the explicit
`GMI_LIVE_TESTS=1` flag. Without both, all five tests are skipped and nothing
reaches the network.

| Test | What it does | Cost |
| --- | --- | --- |
| `test_live_the_llm_catalogue_can_be_read` | Reads `GET /v1/models` and asserts real models come back. | Free |
| `test_live_the_media_catalogue_can_be_read` | Reads `GET /apikey/models`. | Free |
| `test_live_every_generation_model_publishes_its_own_parameters` | Fetches each generation model's own schema and asserts the parameters and the price are present and well-formed. | Free |
| `test_live_a_real_model_answers` | Makes one real chat completion and asserts the provider's own token usage came back. | **Costs money** — one short completion |
| `test_live_the_pipeline_routes_and_reports` | Runs the whole pipeline against the real catalogue and asserts the report comes back with real routing decisions in it. | Catalogue reads are free; the analysed plan calls no model |

### What has actually been run against the live API

| Check | Result |
| --- | --- |
| `GET https://api.gmi-serving.com/v1/models` with a valid key | **Reachable.** The catalogue reads and returns real models with their published prices. |
| `GET .../apikey/models` | **Reachable.** The generation catalogue reads and returns real models. |
| `GET .../apikey/models/{id}` | **Reachable.** Each generation model publishes its own `parameters`, `price_info` and `percentage_discount`. |
| A paid chat completion | **Run.** Real text came back, with the provider's own token usage in the response. |
| A multimodal completion carrying a real image | **Run.** `scripts/probes/probe_vision_live.py` sent a real PNG and got a description that matches what is in the image. Estimated cost: **$0.000341**. |
| Image generation through the request queue | **Run.** Real assets came back and were fetched and inspected. |
| The full demo scenario, live | **Run.** `scripts/demo_run.py --live --approve-spend` completed all nine phases: three real generations, real vision calls, and real code written and tested through Hermes' own tools. |

The free catalogue reads are the ones NEXUS depends on most — the router,
the pricing and the capability detection are all built on them — and they
have always worked. The paid paths are now exercised too.

### What the live exercise cost, and what it found

GMI's usage block reports **token counts and no dollar figure**, so the cost
of every live call is computed from the catalogue's published rates. These
are **estimates at list price**, not bills, and they are labelled as such
everywhere they appear.

The live exercise found ten defects that the mocked suite could not, and
all ten are fixed:

1. **The multimodal part type was spelled the wrong way.** The vision path
   sent `{"type": "image"}` — the Anthropic spelling. GMI's endpoint is
   OpenAI-compatible and wants `image_url`. Some models reject the wrong
   spelling outright; the rest accept it silently, receive no image at all,
   and answer *"I can't see an image attached"*, which looks like the model
   being incapable rather than the request being malformed.

2. **The capability probe could be passed by accident.** It drew two
   coloured squares and asked for their colours — "red" and "blue" being the
   two most guessable colour words there are. **13 of the 24**
   catalogue-declared vision models "passed", including plainly blind ones.
   The probe now draws a **random three-digit number** and asks the model to
   read it back; only **5 of the 24** pass. One of the failures is the
   clearest possible demonstration of why this matters:
   `openai/gpt-6-luna` answered `"456"` for a number it had never seen.

3. **A verdict from the old probe protocol was still counting.** The probe
   protocol is versioned (`v2`), and a verdict recorded under `v1` no longer
   counts in either direction. Without this, the thirteen accidental passes
   above would have been inherited as established fact.

4. **The Dev Agent lost the task when it asked for a fix.** The first
   implementation wrote `example.txt` instead of the `landing/index.html`
   the task named; the test failed with `FileNotFoundError`, and the retry
   prompt carried only the failure output — so the model, no longer knowing
   what it was building, repeated the mistake. The fix prompt now carries
   the original task.

5. **The token budget was too small for a file.** The inference client's
   default of 2048 tokens is enough for a sentence and not enough for a
   source file. The live reply came back with `finish_reason: "length"` —
   cut off **mid-JSON** — so the file list could not be parsed, and the
   error named the model (*"did not return a usable file list"*) rather
   than the request. The Dev Agent now asks for `code_max_tokens` (8192 by
   default, configurable), reads the finish reason, reports a truncated
   reply as **truncated**, and asks for more room on the next attempt
   instead of repeating the same bounded question. The identical call that
   was cut off at 2048 tokens comes back complete at 8192, with
   `finish_reason: "stop"`.

6. **The Dev Agent was spending without asking the budget guard.** The
   `approved` parameter existed on `implement()` and `review()` and was
   never read, and neither method checked the per-run cap, the daily cap or
   the approval threshold before making a call. Every other path in the
   plugin goes through `budget.enforce()` first; the Dev Agent now does too.

7. **The reported generation latency was not the time the caller waited.**
   The request queue's create call blocks while the request is admitted and
   processed — a real generation took 24 seconds — and the client was timing
   only the polling loop that follows it. So the latency in the tool reply,
   in `nexus_report` and on the dashboard read **0.4 s** for a generation
   that took half a minute. The clock now starts before the submission, so
   the number reported is the number the caller experienced.

8. **A catalogue that could not be read was reported as an empty one.** A
   live run with a rejected API key produced `language models 0`,
   `generation models 0`, and then — for every step of the plan —
   *"no model in the live catalogue satisfies coding"*, *"no media model was
   available for this request"*, *"no code-capable model is available in the
   live catalogue"*. Every one of those reads like a fact about GMI Cloud.
   Each was in fact a fact about the request. The registry had recorded the
   reason all along (`llm catalogue: Invalid API key; media catalogue:
   Invalid API key`) and nothing carried it forward. The error now reaches
   every place that reports an empty catalogue, and both demo scripts stop
   rather than run a pipeline against a catalogue that was never fetched.
   This one was not found by a probe — it was found by a live run with a
   credential that had been rotated out from under the plugin.

9. **A model the provider had retired was still routable, and the
   provider's own reason was swallowed.** GMI Cloud publishes the catalogue
   and the models it can actually serve separately, so a deprecated model
   keeps its place in the catalogue. The router chose
   `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8`, called it, and was refused
   with a generic *"Backend request failed with status 400"* — while the
   actual reason, *"The requested model has been deprecated and is no longer
   available."*, sat one level down in `error.details` and never reached the
   caller. NEXUS now reads the provider's own reason out of the error,
   reports it verbatim instead of a generic failure, and **remembers the
   verdict**: a model the provider has retired is not routed to again, and
   the work recovers on a different one. The demo video shows this
   happening.

10. **A code reply that was right but malformed was thrown away.** The Dev
    Agent asks for a file list as JSON. The model's answer was correct, but
    it wrote the newlines inside its JSON string literally, so the parser
    rejected it as unparseable — and the retry prompt did not show the model
    the reply that had failed, so it repeated the same malformed answer.
    The parser now repairs raw newlines inside JSON string literals instead
    of discarding good work, and the retry carries the model's own unusable
    reply, the exact shape it must use, and the rule that a newline inside a
    string is written as `\n`.

    The last two have **fourteen regression tests** between them, in
    `tests/test_resilience.py`.

The live run also showed that the catalogue's own code model,
`Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8`, answers **every** request with
*"Backend request failed with status 400"* under every parameter variant.
The provider's real reason for that is item 9: the model has been
deprecated. The plugin now says so in those words rather than passing a
generic failure on, and the work moves to a model that is still served. That
is exactly what the RECOVER step exists for, and the Dev Agent reports which
model actually ran. The demo's RECOVER section reconciles the published
route against the executed one out loud.

---

## Level 4 — Hermes plugin validation

This is the level that proves Hermes itself can load the plugin.

### `hermes plugins validate`

```
$ hermes plugins validate ./plugins/gmi-nexus --install-deps
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.21.6' parses
✓ config schema — shape valid
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no collisions
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)
  · dashboard backend API is served through the plugin host (buffered responses)

Validation passed.
```

The **security scan is `safe`** — no findings at all, not `caution`. The
`no core override` check passes, so the plugin never rebinds a Hermes core
function.

### `scripts/validate_installation.py`

**44 checks, all passing.** This is the strongest test in the suite: it builds
a real Hermes home, copies the plugin into `<home>/plugins/gmi-nexus/`,
enables it in `config.yaml`, and lets **Hermes' own discovery and loader** do
the loading. Nothing is stubbed except the HTTP layer.

```
$ python scripts/validate_installation.py
44/44 checks passed
```

| Group | What it proves |
| --- | --- |
| **Discovery** | The plugin is discovered, loads without an error, is enabled, and is loaded from the user plugin directory. |
| **Tools** | All eight tools are registered in Hermes' tool registry under the plugin's own scope, each with a schema and each declaring `requires_env == ["GMI_API_KEY"]`. |
| **Command** | The `/nexus` command is registered. |
| **A tool actually runs** | `nexus_report` is dispatched through the real handler and returns valid JSON carrying the documented keys, with the credential absent from the response. |
| **Storage** | The history lands in `<hermes home>/plugin-data/gmi-nexus`; the plugin directory holds no runtime state; the source tree ships no bytecode outside `__pycache__`; git tracks no bytecode. |
| **Dashboard** | The manifest, the entry script, the stylesheet and the backend are all present where the dashboard looks for them. |
| **Credentials** | The credential does not appear in any plugin file, and the plugin did not write it into its own directory or into the Hermes home. |

### The credential route

Catalog rule 11 says a plugin's declared secrets are read with
`agent.secret_scope.get_secret(NAME)`, "which honours the active profile (a
bare `os.environ` read can pick up another profile's key under
multiplexing)".

Hermes' own `get_secret` is stricter than that sentence suggests. With a
profile scope bound it answers from the scope and returns nothing on a miss;
with multiplexing on and no scope bound it **raises** rather than fall back to
the process environment, precisely so that a plugin cannot be handed another
profile's key by accident.

The plugin's `read_api_key()` honours that guard:

* inside Hermes, the secret scope is the **only** source — if it has no key,
  the plugin has no key, and the environment is not consulted at all;
* if the secret scope raises, the plugin fails closed rather than reaching
  for `os.environ` behind Hermes' back;
* the process environment is read **only** when Hermes' secret scope is not
  installed at all, which is the case when the plugin runs standalone (the
  test suite, `scripts/demo_run.py`).

Two regression tests hold the last two points in place, and they are the
tests that would fail if the fallback were reintroduced.

### The dashboard, rendered in a browser

The backend loading is only half of it. The tab has been **opened in a real
browser, inside a real Hermes install** — Hermes' own web server, serving
Hermes' own built SPA, with the plugin discovered from its `dashboard/`
folder exactly the way a user gets it.

| Check | Result |
| --- | --- |
| The six plugin routes | All `200`, with the documented key sets. |
| The plugin assets | `200` at `/dashboard-plugins/gmi-nexus/dist/{index.js,style.css}`, with the right content types. |
| The tab itself | Reachable at `/nexus`; the DOM carries all six section labels. |
| All six sections | Driven in turn: **zero console errors, zero uncaught exceptions.** |
| The data | Live — 81 language models, 378 generation models, 24 vision-capable, 65 tool-capable, 1 free. |
| The honesty | `MEASURED` where something is measured, `not measured` where it is not, and empty states that give the reason. |
| The credential | Reported as `configured`; the value appears nowhere. |

The photographs are in `docs/screenshots/` at the repository root.

**This pass found three real defects, all fixed.** They were only findable by
looking at the rendered page, which is the argument for doing it:

1. **Unreadable headings.** The stylesheet read the host's theme variables
   (`var(--color-foreground)` and friends). Hermes declares those inside a
   Tailwind `@theme inline` block, and Tailwind only emits a theme variable to
   `:root` when a generated utility references it — so at runtime they were
   absent, the plugin fell back to its light-theme ink, and dark navy text
   landed on the host's dark background. The plugin now owns its palette
   outright: a clean light canvas with dark ink, defined once at the root of
   its own tree.
2. **An awkward tile hint** — "no measured steps step success". It now reads
   "no measured steps yet".
3. **A wall of UUIDs.** The Multimodal catalogue listed every account-private
   workflow ahead of every named product model, because a digit sorts before a
   letter, burying the routable models behind the ones NEXUS never selects.
   Product models now come first, and a test holds the order in place.

### The demo run, and what it found

`scripts/demo_run.py` runs the scenario in the brief end to end. In its free
`--dry-run` mode it completes with the run's status `completed`, and in
`--live` mode it completes against the real GMI Cloud API. Running it is what
found the defects that the mocked suite could not, which is the argument for
running it:

**The report contradicted itself about generation steps.** The ROUTE phase
routed every planned step through the *language-model* router, so a step
that generates an image published *"Step 3 → no eligible model: missing
required capabilities: image_out"* — directly above a step whose own result
read *"ok — gpt-image-2"*. The two catalogues are different and the pipeline
was using the wrong one for generation.

The fix: `RouteDecision` carries a `kind` (`"llm"` or `"media"`), and the
**executor** owns the route preview, because it is the component that
carries the route out and knows which catalogue a step belongs to.
`nexus_analyze` had the same defect in its own copy of the preview — it
published "no eligible model" for the image step too, so the plan an agent
was shown before it committed to anything contradicted the run that followed
it. Five regression tests hold all of it in place.

**And then, live, the route and the run legitimately disagreed.** The
catalogue's own code model answers every request with a 400, so the model
that ROUTE published was not the model that EXECUTE managed to use. That is
not a contradiction — it is the RECOVER step doing its job — and the demo now
prints the reconciliation:

```
3b. RECOVER
  step 4: published Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8,
          ran moonshotai/kimi-k2.7-code after
          Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8:
          Backend request failed with status 400 failed
```

The four other defects the live run found — the wrong multimodal part type,
the probe that could be passed by accident, the stale probe verdict, and the
Dev Agent losing the task on a retry — are set out under *Level 3* above.

---

## The catalog entry

The entry that goes into the Hermes plugin catalog is finalised at
`catalog-entry/gmi-nexus.yaml` in this repository, and the PR description that
accompanies it is at `catalog-entry/PRDRAFT.md`. Neither is inside the
plugin payload — the catalog entry belongs to the *Hermes* repository, not to
this one.

### Why `subdir: plugins/gmi-nexus`

The catalog admission policy's rule 9 requires a plugin to extend Hermes
through public surfaces only. It is checked by scanning **the subdirectory the
entry points at**, so putting the plugin in `plugins/gmi-nexus/` and setting
`subdir: plugins/gmi-nexus` means the scanner sees only the plugin — not the
test suite, not the validation script, and not the development
`pyproject.toml`.

This is not a formality. Before the layout was changed, the private-import
lint reported `hermes_cli.plugins` as a non-public import, because the
development script at the repository root imported Hermes' plugin manager
directly. Moving the script out of the scanned payload removed the warning
without any suppression, and `hermes plugins validate` now reports none.

### How the entry was pinned

Rule 2 of the policy requires an exact 40-character commit SHA. The entry pins
the commit that carries the tested payload and the images. The procedure was:

1. Commit and push the repository.
2. `git rev-parse HEAD` — take the 40-character result.
3. Put that SHA in `sha:`, and the repository URL in `repo:`.
4. Open the PR against the Hermes repository with `catalog-entry/PRDRAFT.md` as
   the description.

The SHA is not written here: this file is inside the plugin payload, and a
file cannot contain the SHA of the commit that contains it. It is in
`catalog-entry/gmi-nexus.yaml`, and in the PR description.

The pin is deliberately not the branch head. It freezes one tested version,
and the commit that records it comes after, because a file cannot contain the
SHA of the commit that contains it. A SHA bump is a new PR, never an edit to a
merged entry — the policy is explicit that the pin never moves under a
reader's feet.

### The capabilities the entry declares

```yaml
capabilities:
  provides_tools: [nexus_models, nexus_analyze, nexus_run, nexus_benchmark,
                   nexus_vision, nexus_media, nexus_dev, nexus_report]
  provides_hooks: []
  provides_middleware: []
  requires_env: [GMI_API_KEY]
```

These match the registration exactly, which `hermes plugins validate` checks
on every run: "declared tools — matches registrations". The hooks and
middleware lists are empty because the plugin registers none.

---

## What is *not* verified

Stated plainly, so nothing here is overclaimed:

* **Video and audio generation have not been run live.** Image generation
  has been — three times, in the demo — and the per-model parameter schemas
  for every generation model in the catalogue have been read live. But the
  video and audio request shapes are exercised only against the mocked
  transport. The generation catalogue is read live for every one of them, so
  the model selection and the pricing are real; the *submission* is not
  proven against the live service.
* **The dashboard has only been seen on a fresh install.** It was opened in a
  real browser inside a real Hermes install, and every section was driven —
  but the install had no run history, so the Arena, Execution and spend charts
  were exercised through their *empty* states. Those states are deliberate and
  they are what the screenshots show, but the populated versions of those
  panels have not been looked at in a browser yet. They will be, as soon as
  there is a real run behind them.
* **No end-to-end demo video has been produced.** The demo scenario in the
  hackathon brief runs end to end, both in `--dry-run` (orchestration real,
  outputs simulated) and in `--live` against the real GMI Cloud API. The
  recording has not been made.
* **No cost figure is a bill.** GMI's usage block reports token counts and no
  dollar amount, so every cost in this repository is an **estimate computed
  from the catalogue's published rates** at list price. The organisation's own
  discount is applied where the catalogue publishes it. No billed total is
  quoted, because none is available.

---

## Reproducing this

```bash
# Levels 1 and 2 — no network, no cost
python -m pytest tests/

# Level 4 — no network, no cost
hermes plugins validate ./plugins/gmi-nexus --install-deps
python scripts/validate_installation.py

# The demo scenario — no network, no cost, simulated outputs
python scripts/demo_run.py --dry-run

# Level 3 — real network, real cost on one test
GMI_LIVE_TESTS=1 GMI_API_KEY=<key> python -m pytest tests/test_live.py -v

# The full demo, live — real calls, real cost, prints what it will spend first
GMI_API_KEY=<key> python scripts/demo_run.py --live --approve-spend
```
