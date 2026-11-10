# Build status

**Project:** GMI NEXUS — an adaptive intelligence and orchestration layer for
Hermes Agent over the GMI Cloud model catalogue.
**Current phase:** Feature-complete, verified in a real browser inside a real
Hermes install, and **exercised live against GMI Cloud** — real inference, real
vision, real image generation, and real code written and tested through
Hermes' own tools.
**Remaining work:** demo recording, repository publication and the catalog PR.

---

## Completed

### Research and design

- Hermes' plugin extension points were read from the source, not from memory:
  `plugin.yaml` manifest, the `register(ctx)` entrypoint, `PluginContext`'s
  `register_tool` / `register_command` / `dispatch_tool` / `get_config`,
  the tool-registry contract, the plugin-data directory, and the secret
  scope.
- GMI Cloud's two endpoints were read from their own documentation: the
  OpenAI-compatible inference API at `api.gmi-serving.com/v1`, and the
  request-queue generation API at `console.gmicloud.ai`.
- The dashboard extension points were read from the Hermes web dashboard's
  source: `dashboard/manifest.json`, `dashboard/dist/index.js`,
  `dashboard/dist/style.css` and `dashboard/plugin_api.py`, plus the
  `window.__HERMES_PLUGIN_SDK__` surface and the `__HERMES_PLUGINS__.register`
  contract.
- The plugin catalog's admission rules were read from
  `plugin-catalog/README.md` and the catalog-submission guide. The plugin's
  layout was changed to satisfy them — see *Repository layout* below.

### The eight-step pipeline

| Step | Module | Done |
| --- | --- | --- |
| UNDERSTAND | `nexus/classifier.py` | Classifies the goal into a task type and works out the capabilities the work needs. |
| PLAN | `nexus/planner.py` | Breaks the goal into steps, each with its own task type and requirements. |
| ROUTE | `nexus/router.py`, `nexus/registry.py` | Scores the live catalogue; rules out models that lack a required capability and keeps the reason. |
| EXECUTE | `nexus/executor.py` | Runs the steps, in parallel where they are independent, accounting for every call. |
| EVALUATE | `nexus/evaluator.py` | Checks each result against the step's own criteria, not merely "did it return something". |
| RECOVER | `nexus/recovery.py` | Bounded retry on a *different* model, with the failure attached to the prompt. |
| LEARN | `nexus/storage.py` | The verified outcome updates the per-model statistics, which change the routing. |
| REPORT | `nexus/orchestrator.py`, `nexus/tools.py` | One structured report: the plan, the routing decisions, the cost, the failures. |

### The five modules

- **GMI Smart Router** — live catalogue discovery with a TTL cache; capability
  extraction from the catalogue's own metadata; capability *probing* for models
  that do not declare theirs; essential capabilities (`TEXT`, `VISION`,
  `IMAGE_OUT`, `AUDIO`, `VIDEO`) as a hard filter and specialisations
  (`CODE`, `REASONING`, `TOOLS`, `LONG_CONTEXT`) as a scoring preference;
  ranked candidates with the reason each rejected model was ruled out.
- **GMI Vision** — screenshot capture, image analysis, structured visual
  feedback, a concrete defect list for a screenshot or mockup, and a
  before/after comparison. Vision capability is **verified, not assumed**: a
  model is shown a randomly drawn three-digit number and has to read it back,
  because a probe that can be answered by luck — or by a model that simply
  cannot see — proves nothing.
- **GMI Arena** — identical test cases across several models, scored on
  quality, correctness, latency, reliability, token consumption, cost,
  failure rate, tool-calling and multimodal; historical records; comparison
  reports; the results feed the router's statistics.
- **GMI Dev Agent** — analyse → generate → review → diagnose → fix → test,
  driving Hermes' own `write_file` and `terminal` tools through
  `ctx.dispatch_tool`, so Hermes' approval policy, sandboxing and audit trail
  apply. No second execution environment.
- **GMI Creative Engine** — text-to-image, image-to-image, image editing,
  masked editing, reference images, audio and video. Each generation model's
  own published schema decides its request body; no parameter is reused across
  models.

### The dashboard

Six sections — Overview, Model Intelligence, Arena, Execution, Multimodal,
Settings — in a plain IIFE against the documented dashboard SDK, with no
bundler and no build step. Every payload carries a `basis`
(`measured` / `estimated` / `none`), every empty section says why it is empty,
spend is labelled an estimate, and the credential is exposed only as a
boolean.

### The dashboard, rendered and verified

The tab has been **opened in a real browser, inside a real Hermes install**,
and photographed. Not a mock page: Hermes' own web server, serving Hermes' own
built SPA, with the plugin discovered from its `dashboard/` folder exactly the
way a user gets it.

| What | Result |
| --- | --- |
| The six plugin routes | All returned `200`, with the documented key sets. |
| The plugin assets | Served at `/dashboard-plugins/gmi-nexus/dist/{index.js,style.css}`, `200`, correct content types. |
| The tab | Reachable at `/nexus`; the DOM carries all six section labels. |
| The page | Driven through all six sections: **zero console errors, zero uncaught exceptions.** |
| The data | Live — 81 language models, 378 generation models, 24 vision-capable, 65 tool-capable, 1 free — read from GMI Cloud during the session. |
| The honesty | `MEASURED` shown where something is measured, `not measured` where it is not, and empty states that give the reason. |
| The credential | Reported as `configured`. Its value appears nowhere. |

The photographs are in [`docs/screenshots/`](docs/screenshots/).

**This pass found three real defects, and all three are fixed.** They were
only findable by looking at the rendered page:

1. **Unreadable headings.** The stylesheet read the host's theme variables
   (`var(--color-foreground)` and friends). Hermes declares those inside a
   Tailwind `@theme inline` block, and Tailwind only emits a theme variable to
   `:root` when a generated utility references it — so at runtime they were
   absent, the plugin silently fell back to its light-theme ink, and dark navy
   text landed on the host's dark background. The fix is a palette the plugin
   owns outright: a clean light canvas with dark ink, defined once at the root
   of the plugin's own tree. It renders identically whatever the host theme is
   doing, and it is the light default this surface was designed around.
2. **An awkward tile hint.** "no measured steps step success" — the string
   concatenated two phrases. It now reads "no measured steps yet".
3. **A wall of UUIDs.** The Multimodal catalogue listed every
   account-private workflow ahead of every named product model, because a
   digit sorts before a letter. The models NEXUS would actually route to were
   buried behind the ones it never selects. The listing now puts product
   models first — `Chatterbox-tts`, `Christmas Postcard`, `Flux2-Dev`,
   `Flux2-Klein`, `GLM-Image` — with the private workflows after them, and a
   test holds the order in place.

### The demo, and what it found

`scripts/demo_run.py` runs the scenario in the brief as one continuous
scripted run:

```
analyse → break down → select models → generate visual assets →
implement through Hermes' tools → inspect with a vision model →
detect issues → correct → verify → display the report
```

It has two modes. `--dry-run` (the default) spends nothing and reaches no
network: GMI Cloud is replaced by a scripted stand-in that answers the same
endpoints with the same shapes, so the *orchestration* is real — the router
really scores the catalogue, the budget really refuses an over-limit
request, the Dev Agent really writes a file to disk and really runs its test
against it — while the *outputs* are simulated and labelled as such.
`--live` makes the real calls and refuses to run without both `GMI_API_KEY`
and `--approve-spend`; it prints what it is about to spend before it spends
it, and it does not raise the plugin's own limits.

Every phase feeds the next one, so nothing in the run is theatre: the
generated asset is the asset the vision model reads, the issues the vision
model finds go into the regeneration prompt, the corrected asset is what the
page embeds, and the verification compares the two *different* assets.

**Running the dry demo found a real defect, and the fix is in.**

1. **The report contradicted itself about generation steps.** The ROUTE phase
   routed every planned step through the *language-model* router, so an
   `image_generation` step published *"Step 3 → no eligible model: missing
   required capabilities: image_out"* — directly above a step whose own
   result read *"ok — gpt-image-2"*. Two routers exist because the
   catalogues are different, and the pipeline was using the wrong one.
   `RouteDecision` now carries a `kind` (`"llm"` or `"media"`), and the
   **executor** owns the route preview — it is the component that carries
   the route out, and it knows which catalogue a step belongs to. Five
   regression tests hold it in place.

2. **The same defect in the analysis.** `nexus_analyze` had its own copy of
   the preview, and it published "no eligible model" for the image step too
   — so the plan an agent was shown before it committed to anything
   contradicted the run that followed it. The preview now comes from the
   same place the route does.

3. **A recovery that forgot what it was routing.** A retried step rebuilt
   its decision without the `kind`, so a retried generation step would have
   fallen back to the chat catalogue. The kind travels with the decision now.

**Running it live found six more, and they are the ones that mattered.**

A mocked test cannot find a wrong spelling that the provider happens to
accept, and it cannot find a request that is too small. All six of these
were invisible to the suite and obvious the moment a real model was asked to
do the real thing.

4. **The multimodal message part was spelled the wrong way.** The vision
   path sent `{"type": "image"}`. That is the *Anthropic* spelling; GMI's
   endpoint is OpenAI-compatible and wants `image_url`. Some models reject
   the wrong spelling outright — *"Invalid value: 'image'. Supported values
   are: 'text', 'image_url', 'input_audio', 'refusal'"* — and the rest
   accept it silently, receive no image at all, and answer *"I can't see an
   image attached"*. The second failure is the dangerous one, because it
   looks like the model being incapable rather than the request being
   malformed. The part type is now `image_url`, and the module's constant
   says why in a comment.

5. **The capability probe could be passed by accident.** The first version
   drew two coloured squares and asked the model to name their colours.
   "red" and "blue" are the two most guessable colour words there are, and
   **13 of the 24** catalogue-declared vision models "passed" — including
   models that are plainly blind. The probe now draws a **random three-digit
   number** and asks the model to read it back. Only **5 of the 24** pass,
   and one of the failures is instructive: `openai/gpt-6-luna` answered
   `"456"` for a number it had never seen. A probe that cannot be guessed
   proves something; the old one proved nothing.

6. **A failed verdict from an older probe protocol was still counting.** The
   probe protocol is now versioned (`v2`), and a verdict recorded under
   `v1` no longer counts in either direction — the model keeps whatever the
   catalogue claims and is probed again. Without this, the thirteen
   accidental passes from defect 5 would have been inherited as
   established fact.

7. **The Dev Agent lost the task when it asked for a fix.** In the live run
   the first implementation wrote `example.txt` instead of the
   `landing/index.html` the task named; the test failed with
   `FileNotFoundError`, and the retry prompt carried only the failure
   output — so the model, no longer knowing what it was building, had no
   reason to write the right path either and repeated the mistake. The fix
   prompt now carries the original task, and both prompts state the rule
   that the paths must be exactly the paths the task asked for.

8. **The token budget was too small for a file.** The inference client's
   default of 2048 tokens is enough for a sentence and nowhere near enough
   for a source file. The live model came back with
   `finish_reason: "length"` — cut off **mid-JSON** — so the file list
   could not be parsed at all, and the error said *"the model did not
   return a usable file list"*, which pointed at the model rather than at
   the request. The Dev Agent now asks for `code_max_tokens` (8192 by
   default, configurable), and it reads the finish reason: a reply that
   was cut off is reported as **truncated**, and the next attempt asks for
   more room rather than repeating the same bounded question. The same call
   that was cut off at 2048 tokens completes at 8192 with
   `finish_reason: "stop"`.

9. **The Dev Agent was spending without asking the budget guard.** The
   `approved` parameter existed on `implement()` and `review()` and was
   never read — and neither method checked the per-run cap, the daily cap
   or the approval threshold before making a call. Every other path in the
   plugin goes through `budget.enforce()` first. The Dev Agent now does
   too, which makes the parameter mean what its name says.

The live run also surfaced something the plugin should *show* rather than
hide: the catalogue's own code model,
`Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8`, answers **every** request with
*"Backend request failed with status 400"*, under every parameter variant.
The Dev Agent now treats the model list as candidates and passes over one
that fails, and the report says which model actually ran — which is not the
one the ROUTE phase published. The demo's RECOVER section reconciles the two
out loud rather than quietly agreeing with itself.

### The demo video, and what measuring it cost

The hackathon asks for a demo **under 90 seconds**. The full nine-phase
scenario takes about four minutes against the real service, so it cannot be
recorded uncut. `scripts/demo_video.py` is a second script that runs the same
plugin on the same real service in five phases — the catalogue, a generation,
a vision inspection, an implementation through Hermes' tools, and the report —
so the video can be one continuous recording with nothing edited out of it.

The first attempt at that script **took 348 seconds**, and the reason was not
the plugin. Measuring where the time went showed two things the mocked tests
could never have shown, because both are about *time*:

| Phase | Time | What it was actually doing |
| --- | --- | --- |
| The catalogue | 13.6 s | Reading the live catalogue. Free, and unavoidable — it is the point of the phase. |
| The generation | 23.4 s | One real image generation. |
| The vision call | 101.1 s | Of which the model's answer was **8.3 s**. The other ~93 s was the **capability probe**: the router's top candidates were each probed, one after another, until one turned out to be able to see. |
| The implementation | 210.0 s | One code model writing an 11,110-byte HTML file. |

Both findings became design decisions rather than workarounds.

**The probe is cached, and that is the plugin's own behaviour.** A probe is a
real request, and its verdict is the only evidence that a declared capability
is true. The plugin records the verdict in its own persistent data directory
and re-applies it to every later catalogue read, precisely so that the same
question is not paid for twice. The video script takes a `--home`, and
running it twice against the same home means the second run starts with the
verdicts already in place. Nothing was cut out of the recording; the probe
was simply not repeated.

**Speed is not in the catalogue.** The catalogue publishes each model's
prices and context length. It does not publish how fast the model writes, and
that turned out to be the single number that decides whether a page fits in
a video's time budget. `scripts/probes/probe_code_speed.py` gives every
code-capable model the same writing task and measures the answer:

| Model | Time | Written | Rate |
| --- | --- | --- | --- |
| `moonshotai/kimi-k2.7-code-highspeed` | 11.9 s | 2,415 chars | **203.8 chars/s** |
| `moonshotai/kimi-k2.7-code` | 41.9 s | 2,000 chars | 47.7 chars/s |
| `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` | — | — | answers every request with a 400 |

The two working models differ by a factor of four, which on the same file is
the difference between a minute and a quarter. The video script expresses
that through the plugin's own documented `blocked_models` setting, so the
operator's choice is what the router honours — and if the blocked model is
ever retired the block stops matching anything and the router falls back to
the catalogue on its own. The script degrades; it does not break.

#### The image generation, measured

The generation is the phase the video cannot do without, and the catalogue
publishes nothing about how long it takes. `scripts/probes/probe_image_speed.py`
sent one real generation to each configuration and timed it:

| Model and options | Time | Result |
| --- | --- | --- |
| `gpt-image-2.5-flare-generate`, quality `medium` | 17.1 s | success |
| `gpt-image-2.5-flare-generate`, quality `low` | 18.2 s | success |
| `Qwen-Image-2512`, 8 steps | — | sat in `queued` for 240 s and never started |
| `Flux2-Klein`, 4 steps | — | sat in `queued` for 240 s and never started |

Two things follow. The cheapest models in the catalogue are not the usable
ones: they waited in the queue and were never dispatched. And the quality
tier makes no measurable difference to the wait, so the reel asks for the
model's own default.

Across seven real generations in this project the time was **14.5 s, 17.1 s,
17.9 s, 19.3 s, 20.3 s, 21.3 s and one 109.7 s outlier** — a single occasion
when the queue held the request for a minute and a half. Six of seven sit
under twenty-two seconds; the outlier is the reason the script prints the
total and says plainly whether it came in under ninety seconds.

#### A real defect the timing exposed

The reel's own output made the problem visible: it reported the generation
as having taken **0.4 s** when the phase it was in had taken 24 s. The
request queue's create call blocks while the request is admitted and
processed, and the plugin was timing only the polling loop that follows it.
So the latency NEXUS recorded and reported — in the tool reply, in
`nexus_report` and on the dashboard — was a fraction of a second for a
generation that took half a minute.

The clock now starts before the submission rather than after it, so the
number the report gives is the time the caller actually waited. There is a
regression test for it, and the mock transport gained a `submit_delay` so a
test can reproduce the queue's behaviour rather than assume it.

#### The reel as it stands

The measured run, warm probe cache and all:

| Phase | Time |
| --- | --- |
| 1. The catalogue is live | 14.7 s |
| 2. Generate | 24.2 s |
| 3. See | 12.6 s |
| 4. Build | 17.8 s |
| 5. Report | 0.0 s |
| **Total** | **69.3 s** |

Two further live runs of the same script, against the same service, came in
at **53.4 s** and **76.0 s** — both inside the ninety seconds a demo video is
allowed. Their generations took 19.0 s and 22.3 s, which is the time the
caller actually waited and not the queue's own timing; that is the figure the
latency fix above restored. The spread between the three runs is the request
queue's, not the plugin's.

A fourth run, on a **cold** probe cache — no verdicts carried over, so the
vision model was probed live — came in at **82.2 s**. It is the slowest of
the four because the probe is a real request, and it is still inside the
ninety seconds. That run also exercised RECOVER against the live catalogue:
the published code model answered with a 400, the plugin moved to a different
model, wrote the file and passed the test. The vision model it used had been
probed in that run and had read the drawn number correctly.

Two further runs of the same script came after the ninth and tenth defects
were fixed. The first took **91.7 s** and failed in the BUILD phase — that is
the run that exposed them. The second took **68.6 s**, wrote the file, passed
the test and produced a complete report, and it is the run the demo video is
cut from.

Two changes were made because of the measurements rather than because of
guesses. The generation asks for a JPEG instead of a PNG — the model
publishes the option and the plugin passes it through — which is a tenth of
the bytes to move into the vision model. And the reel stops at the vision
phase if the generation produced no image at all, rather than carrying an
empty step forward as though it had worked.

#### The eighth defect, found by a live run with a rejected key

A live run was started with a credential that GMI Cloud rejected. The
catalogue read failed, and the plugin reported it as an **empty catalogue**:

```
  language models            0
  generation models          0
  step 0 -> planning         (none)   score None
  ...
  error                      no media model was available for this request
  error                      no code-capable model is available in the live catalogue
```

Every one of those lines reads like a fact about GMI Cloud — as though the
catalogue were genuinely empty, or as though the task were one no model
could serve. Each was in fact a fact about the request. The registry had
recorded the reason all along:

```
llm catalogue: Invalid API key; media catalogue: Invalid API key
```

and nothing carried it forward to the caller.

This is the same class of error as the other seven, and the brief's own
rule against reporting a failure as a success. An empty catalogue and a
catalogue that could not be read are two different states, and only one of
them is a statement about GMI Cloud.

The fix puts the reason where the emptiness is reported: the executor's
`NoEligibleModel`, the creative module's `no_candidate`, the Dev Agent's
no-code-model, and both demo scripts — which now print the error and stop,
because a reel that records ninety seconds of "no eligible model" is worse
than no reel at all. Two regression tests cover it, and the demo script's
refusal is visible in the transcript the script writes.

#### The ninth and tenth defects, found by the recorded run

The reel that the demo video is cut from failed its first attempt, in the
BUILD phase, at 91.7 seconds — over the limit. The transcript said the model
did not return a usable file list. It was not the model.

**Ninth: a retired model stayed routable, and the provider's real reason was
swallowed.** GMI Cloud publishes the catalogue and the models it can actually
serve separately, so `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` was still
listed long after the provider had deprecated it. The router chose it, called
it, and was refused:

```
HTTP 400
{"error": {"message": "Backend request failed with status 400",
            "details": "{\"error\": {\"code\": \"UnavailableModel\",
                        \"message\": \"The requested model has been deprecated
                                    and is no longer available.\"}}"}}
```

The real reason was there, one level down, and the plugin flattened the whole
thing to *"Backend request failed with status 400"* — a sentence that says
nothing and blames the wrong thing. Three changes followed:

| Change | What it does |
| --- | --- |
| `nexus/errors.py` | Reads the nested detail and reports the provider's own wording, with the wrapper's wording kept only when it adds something. |
| `nexus/storage.py` | A `retired_models` table, with the reason and the time it was recorded. |
| `nexus/providers/gmi_llm.py` | A refusal that reads as a retirement is recorded, so the router never asks that model again. |

`nexus/registry.py` applies the remembered verdicts on every catalogue read,
marking a retired model `is_ready = False`, and the Dev Agent filters retired
models out of its candidates both before it starts and after each failed
attempt. The same run then **recovered on a different model and finished in
68.6 seconds** — and the recovery is on screen, because it is what happened.

**Tenth: a good reply was thrown away because the JSON was not quite JSON.**
Asked for a file as a JSON string, a model sometimes writes the file's
newlines literally. The answer is right and the JSON is not, and the reply was
discarded as unparseable. Worse, the retry prompt did not show the model the
reply that had failed, so the model repeated the same malformed answer and the
run ended with nothing. The parser now repairs raw newlines, tabs and control
characters **inside JSON string literals** and retries the parse; the retry
prompt carries the model's own unusable reply, the exact shape it must use,
and the rule that a newline inside a string is written as `\n`. A reply that
needs no repair is untouched.

Fourteen regression tests cover the two defects, in
`tests/test_resilience.py`.

### The demo video, recorded

The reel that the video is cut from is the one the ninth defect spoiled: the
same five phases, live, ending in a RECOVER that a human can see happen. It
took **68.6 seconds** and it is the run in `demo_output/reel2/`.

The video is built by `scripts/video/build_video.py`, and it is built from
that run and nothing else:

| On screen | Where it comes from |
| --- | --- |
| The terminal | The run's own transcript, line for line, revealed on the clock. |
| The catalogue counts | `video_run.json` → `analyse.catalogue`. |
| The routing table | `video_run.json` → `analyse.routing_preview` — the same six rows the transcript shows. |
| The hero image | The asset GMI generated during that run, downloaded from the storage URL the run returned. |
| The vision critique | `video_run.json` → `vision.structured`. The same three issues the transcript shows. |
| The landing page | A headless-Chrome render of `landing/index.html`, the file the Dev Agent wrote and whose test passed. |
| The report | `video_run.json` → `report`. |
| The eight-phase ribbon | The pipeline, highlighted by the phase each beat belongs to. |

The voice is Inworld TTS (`inworld-tts-2`, voice `Ashley`). All seven
segments were synthesised in **one request** — the API's `script.utterances`
with `outputMode: PER_UTTERANCE` — so nothing was paid for twice, and the
whole narration cost 996 characters against one round trip.

```
$ ffprobe demo_output/gmi-nexus-demo.mp4
duration=70.634   1920x1080   h264 + aac   30 fps   4.8 MB
```

**70.6 seconds**, inside the ninety the brief allows, and every frame of it
is either a line the plugin printed or a number the run measured. The
narration's peak is normalised to −1 dBFS so it is audible on a phone.

The video is in the repository at
[`docs/demo/gmi-nexus-demo.mp4`](docs/demo/gmi-nexus-demo.mp4), with a poster
frame beside it.

### Documentation

| Document | Covers |
| --- | --- |
| `plugins/gmi-nexus/README.md` | The plugin's front page — what it does and what it refuses to do. |
| `plugins/gmi-nexus/docs/ARCHITECTURE.md` | Module by module, the storage schema, the dashboard half, and the integration points. |
| `plugins/gmi-nexus/docs/SETUP.md` | Install, configuration, the spend limits, and the endpoint checks. |
| `plugins/gmi-nexus/docs/API.md` | All eight tools, their parameters and example responses, plus the six dashboard routes. |
| `plugins/gmi-nexus/docs/HERMES_VALIDATION.md` | The four levels of verification, the catalog entry, and what is *not* verified. |
| `BUILD_STATUS.md` | This file. |

### The API doc is verified against the running code

Every example in `docs/API.md` was checked against the code that produces it,
not written from memory. The tool handlers and the dashboard routes were
executed against the mock transport, their payloads dumped, and the doc's
examples corrected to the real shapes.

That pass found and fixed real errors: `nexus_analyze` documented a `label`
and a `signals` key where the code produces `task_label` and `evidence`; the
plan was missing `goal` and `notes`; `nexus_benchmark` documented a
`results`/`summary` shape that the Arena does not produce at all; and
`nexus_media` documented a `cost_usd` field where the code produces
`estimated_cost_usd` — a distinction that matters, because one is a billed
amount and the other is an estimate. Six of the eight tool examples and all
six dashboard routes were wrong before that pass and are right after it.

The task-type vocabulary in the doc is the code's: `coding`, `planning`,
`code_review`, `image_generation` and so on — not `code_generation`, which
never existed.

### The catalog admission audit

The catalog's own admission machinery was run against this plugin, rather
than the rules being read from memory.

| Check | Tool | Result |
| --- | --- | --- |
| Entry schema, field by field | `scripts/validate_plugin_catalog.py` (the validator the admission CI runs) | **Passes** — 0 errors, 0 warnings, on the entry with its placeholders substituted for a real SHA and owner. |
| Capability declaration vs reality | `hermes plugins validate` | "declared tools — matches registrations". |
| No core override | `hermes plugins validate` | "no core override". |
| Security scan | `hermes plugins validate` | `safe` — no findings at all. |
| Unscannable payload (rule 17) | `scripts/scan_plugin_payloads.py` | **Passes** — no bytecode, no binaries, no archives. |
| Self-updating code (rule 3) | the CI's own grep | **Clean** — no GitHub-fetch pattern beside a file-write pattern. |
| Compatibility floor (rule 14) | the catalog's own entries | `>=0.21.6` is inside the current release range; 123 catalog entries use exactly this floor. |
| Name collision (rule 16) | the catalog directory | `gmi-nexus` is unused. |

Two things did not conform, and both are fixed.

**The credential fallback could have crossed profiles.** Rule 11 says a
plugin's secrets are read with `agent.secret_scope.get_secret(NAME)`, "which
honours the active profile (a bare `os.environ` read can pick up another
profile's key under multiplexing)". The plugin called `get_secret` first and
fell back to `os.environ` whenever it returned nothing — including *inside*
Hermes. Hermes' own `get_secret` is stricter than that sentence: with
multiplexing on and no profile scope bound it **raises**, precisely so that
nothing downstream can reach for the environment and pick up another
profile's key. The plugin's `except Exception: pass` was swallowing that
refusal and doing the very thing it exists to prevent.

The fix: inside Hermes the secret scope is the only source. If it has no
key, the plugin has no key; if it raises, the plugin fails closed; and
`os.environ` is read only when Hermes' secret scope is not installed at all
— which is the standalone case, the test suite and `scripts/demo_run.py`.
Two regression tests hold it there.

**The plugin tree carried compiled bytecode.** The payload scanner flagged 29
committed `.pyc` files under `nexus/__pycache__/`. `.gitignore` excluded
them, so a clone at the pinned commit would never have carried them — but a
tree that holds bytecode is a tree one careless `git add -f` turns into a
rule-17 failure. They are deleted, and the scanner now passes on the plugin
directory itself.

The card was also completed while the audit was open: the entry now carries
a 2:1 banner and all six dashboard screenshots, both pinned to the entry's
commit.

---

## Test results

```
$ python -m pytest tests/
244 passed, 6 skipped in 9.36s
```

On an interpreter carrying Hermes' own `web` pins the same suite was **230
passed, 5 skipped** — the sixth skip is the environment test that reports the
`fastapi`/`starlette` mismatch, and it passes once the versions agree. The pins
are installed into a virtual environment, so the system interpreter is never
modified. That pinned run was recorded before the suite grew by the one test
that covers the latency fix below, so the pinned figures are one behind.

The 5 skipped tests are the live API tests, skipped because `GMI_LIVE_TESTS`
is not set. That is their intended behaviour.

```
$ hermes plugins validate ./plugins/gmi-nexus --install-deps
Validation passed.
```

`security scan — safe` (no findings, not `caution`), `no core override` and
`declared tools — matches registrations` all pass, and the run reports **no
private-core-import warning** — which it did not before the repository layout
was changed.

The command prints one line above the checks:

```
dependency preparation failed: python: install failed:
TarFile.extractall() got an unexpected keyword argument 'filter'
```

That is a fault in **Hermes' own** dependency installer on this machine, not
in the plugin: `TarFile.extractall` gained the `filter` argument in Python
3.12 and was backported to 3.11.4, and this machine runs **3.11.3**. It is
harmless here because the plugin declares **no dependencies**, so there is
nothing for that step to install — `python dependencies — none declared`
passes on the same run.

```
$ python scripts/validate_installation.py
44/44 checks passed
```

This loads the plugin through Hermes' own discovery and loader in a real
temporary Hermes home. See
[`plugins/gmi-nexus/docs/HERMES_VALIDATION.md`](plugins/gmi-nexus/docs/HERMES_VALIDATION.md)
for what each check proves.

---

## Integration status

| Integration | Status | Evidence |
| --- | --- | --- |
| Hermes plugin loading | **Verified** | `scripts/validate_installation.py` — 44/44, real loader, real home. |
| Eight tools registered | **Verified** | Same run: each tool in the registry, with a schema and `requires_env`. |
| `/nexus` command | **Verified** | Same run. |
| Hermes plugin-data directory | **Verified** | Same run: the history lands in `<hermes home>/plugin-data/gmi-nexus`, and the plugin directory holds no runtime state. |
| Credential handling | **Verified** | Same run: the key appears in no plugin file and in no tool response. |
| Hermes dashboard discovery | **Verified** | Same run: manifest, entry, stylesheet and backend all present where the dashboard looks. |
| `hermes plugins validate` | **Verified** | Passes with a `safe` security scan. |
| Dashboard backend on a real FastAPI router | **Verified** | Loaded against `fastapi.routing.APIRouter` under Hermes' pinned versions: the six documented routes, every handler `async def`. |
| GMI Cloud inference endpoint | **Verified live** | `GET /v1/models` was read live and returns real models with real prices. Paid chat completions have been run — including a multimodal one that carries a real image. |
| GMI Cloud generation endpoint | **Verified live** | `GET /apikey/models` and `GET /apikey/models/{id}` were read live and return real per-model schemas and prices. Image generation has been run against the live request queue. |
| Vision on a real image | **Verified live** | `scripts/probes/probe_vision_live.py` sent a real image to a real model and got a description that matches the image. Cost: **$0.000341**. |
| The demo scenario | **Verified live** | `scripts/demo_run.py --live --approve-spend` runs all nine phases end to end against GMI Cloud: three real image generations, real vision calls, real code written and tested through Hermes' tools. |
| Dashboard rendering in a browser | **Verified** | Opened in a real browser inside a real Hermes install, driven through all six sections: zero console errors, zero uncaught exceptions. Photographs in `docs/screenshots/`. |
| The demo scenario (dry run) | **Verified, simulated** | `scripts/demo_run.py --dry-run` runs all nine phases end to end against a scripted stand-in: the run's status is `completed`, the page is written to disk and its test really passes. The provider's outputs are simulated and labelled as such. |

---

## Known issues

1. **The demo video is recorded but not yet posted.** It is cut, checked and
   in the repository at `docs/demo/gmi-nexus-demo.mp4` — 70.6 seconds,
   1920×1080, with narration — but posting it to X and filing the hackathon
   form are outward-facing actions that need a human.

2. **The repository is committed but not published.** The tree is a git
   repository with the whole plugin on one commit, and the catalog entry has
   been prepared against that SHA. Nothing has been pushed: publishing a
   repository and opening a pull request against someone else's project are
   outward-facing actions, and they need an explicit go-ahead.

3. **`hermes plugins validate --install-deps` prints a dependency-preparation
   error.** It reads:

   ```
   dependency preparation failed: python: install failed:
   TarFile.extractall() got an unexpected keyword argument 'filter'
   ```

   That is a fault in **Hermes' own** dependency installer on this machine,
   not in the plugin. `TarFile.extractall` gained the `filter` argument in
   Python 3.12 and was backported to 3.11.4; this machine runs **3.11.3**,
   which does not have it. It is harmless here — the plugin declares **no
   dependencies**, so there is nothing for that step to install, and
   `python dependencies — none declared` passes on the same run. The
   validation itself reports `Validation passed`.

### A resolved one

The dashboard tab is now rendered and verified, which closed the one issue
that was blocking it. The cause was environmental, not the plugin's: this
machine has `fastapi` 0.104.1 and `starlette` 1.7.0, while Hermes pins
`fastapi==0.133.1` and `starlette==1.9.0` for its `web` extra, so
`fastapi.APIRouter()` raised `TypeError: Router.__init__() got an unexpected
keyword argument 'on_startup'` — and the **Hermes dashboard server itself**
could not start here.

The fix needed no change to the system interpreter. Hermes' four pins
(`fastapi==0.133.1`, `uvicorn==0.54.0`, `starlette==1.9.0`,
`python-multipart==0.0.32`) were installed into a virtual environment; the
environment was created with `include-system-site-packages = true` so it
could still see the rest of Hermes' dependencies, and its own copies of
`fastapi` and `starlette` shadow the system's. Under those pins the plugin's
real `dashboard/plugin_api.py` loaded against a genuine
`fastapi.routing.APIRouter` and registered exactly the six documented routes,
every handler `async def`:

```
router type: fastapi.routing.APIRouter
routes: ['/arena', '/execution', '/models', '/multimodal', '/overview', '/settings']
all handlers async: True
```

and the whole suite runs **228 passed, 5 skipped** there — one more test
passes and one fewer skips than on the system interpreter, because the
environment test no longer has to substitute a stub router.

---

## Remaining work

| # | Task | Blocked on | Priority |
| --- | --- | --- | --- |
| 1 | Run one live inference and one live generation, within the $10 ceiling | — | **Done** — inference, vision and three image generations, all live |
| 2 | Run `scripts/demo_run.py --live` end to end | — | **Done** — the ninth phase completed; the transcript is `demo_output/live_run_3.txt` |
| 3 | Record the demo video, under 90 seconds | — | **Done** — 70.6 s, at `docs/demo/gmi-nexus-demo.mp4`; still to be posted to X |
| 4 | `git init`, commit, and publish the repository | Approval to publish | **High** — the commit exists; publishing does not |
| 5 | Open the catalog PR with the pinned 40-char SHA | 4 | **High** — the entry is prepared and validates against that SHA |
| 6 | Submit the hackathon entry | 3, 4, 5 | **High** |
| 7 | Fill in the `sha:` and `OWNER` fields of `catalog-entry/gmi-nexus.yaml` | 4 | Medium |

### What the live exercise cost

The project's GMI Cloud ceiling is **$10**. The live work so far has used
roughly **$1.10** of it — about a ninth:

| Activity | Estimated cost |
| --- | --- |
| Live demo run 1 | $0.084 |
| Live demo run 2 | $0.126 |
| Live demo run 3 — the run that exposed a model claiming it could not see the image it was sent | $0.0265 |
| Live demo run 4 — the first full demo, with the Dev Agent | $0.10067632 |
| Live demo run 5 — the run that exposed the truncated reply | $0.12976385 |
| Live demo run 6 — after the Dev Agent fix; the one recorded in `demo_output/live_run_3.txt` | $0.12827019 |
| Video run 1 — the five-phase run, which took 348 seconds and showed where the time goes | $0.03269945 |
| Video runs 2, 3 and 4 — the same five phases, after the probe cache and the code-model block | $0.11161785 |
| Video run 5 — the first of the two recorded reels, at 53.4 seconds | $0.0322753 |
| Video run 6 — the second recorded reel, at 76.0 seconds | $0.0327651 |
| Video run 7 — the verification reel, on a cold probe cache, at 82.2 seconds | $0.0430088 |
| Reel run 8 — the run the demo video is cut from, which failed at 91.7 s and exposed the ninth defect | $0.0827956 |
| Reel run 9 — the same reel after the ninth and tenth defects were fixed, at 68.6 s | $0.0353837 |
| The live vision probe on a real image | $0.000341 |
| Probing all 24 catalogue-declared vision models with the number challenge | ≈ $0.01 |
| Assorted one-off probes (parameter variants, URL vs data URI, part type) | ≈ $0.01 |
| The code-speed probe (two chat completions against every code-capable model) | ≈ $0.005 |
| The image-speed probe — four configurations, two of which ran | $0.0265 |
| The generation latency samples — six real generations, timed one by one | $0.0795 |

Each of the six runs is a full nine-phase scenario against the real service:
analyse, route, generate, implement through Hermes' tools, inspect, correct,
verify, report. Every figure is the plugin's **own ledger**, priced from the
tokens the provider actually reported.

Two of the image-speed probe's four configurations cost nothing, because they
never ran: `Qwen-Image-2512` and `Flux2-Klein` sat in the request queue and
were never dispatched. The same is true of `Z-Image-Turbo` and `GLM-Image`
in the latency samples. A request that never produces an asset is not
billed for one, and it is recorded as a failure rather than as a success.

One number inside a run can exceed the run's ledger: the pipeline's
`estimated cost` prices the **token budget** each call was allowed, so it is
an upper bound on the call rather than a prediction of it. The ledger, which
prices what actually came back, is the smaller and truer figure.

Every figure above is an **estimate at list price**, computed from the
catalogue's published rates. GMI's usage block reports token counts and no
dollar figure, so no billed amount is available to quote — and none is
claimed.

### The catalog entry

`catalog-entry/gmi-nexus.yaml` is finalised. Rule 2 of the catalog's
admission policy requires an exact 40-character commit SHA, and the entry
pins the commit that carries the tested plugin payload and the images. It is
a real commit in this repository's history, so the entry resolves once the
repository is pushed. The SHA itself is in the entry file — it is not written
here, because a document that is part of the payload cannot name the SHA of
the commit that contains it. The procedure is documented in
[`plugins/gmi-nexus/docs/HERMES_VALIDATION.md`](plugins/gmi-nexus/docs/HERMES_VALIDATION.md).
The PR description that accompanies it is `catalog-entry/PRDRAFT.md`.

The entry declares:

```yaml
name: gmi-nexus
repo: https://github.com/Joystonm/hermes-gmi-nexus
sha: <the commit that carries the payload>
subdir: plugins/gmi-nexus
requires_hermes: ">=0.21.6"
version: "1.0.0"
image: https://raw.githubusercontent.com/Joystonm/hermes-gmi-nexus/<sha>/docs/banner.png
screenshots:
  - https://raw.githubusercontent.com/Joystonm/hermes-gmi-nexus/<sha>/docs/screenshots/nexus-01-overview.png
  - ... five more, one per dashboard section ...
readme: true
capabilities:
  provides_tools: [nexus_models, nexus_analyze, nexus_run, nexus_benchmark,
                   nexus_vision, nexus_media, nexus_dev, nexus_report]
  provides_hooks: []
  provides_middleware: []
  requires_env: [GMI_API_KEY]
```

`subdir: plugins/gmi-nexus` is what keeps the development tooling — the
tests, the validation script, the workspace `pyproject.toml` — out of the
installed plugin. It is also what the catalog's rule 9 checks read, and it is
why `hermes plugins validate` reports **no** private-core-import warning.

The pin appears in four places — `sha:`, `docs_url`, `image:` and all six
`screenshots:` URLs — and they must name the same commit, or the card would
show a picture of a different version than the code it describes. The pin is
deliberately not the branch head: it freezes one tested version, and the
commit that records it comes after, because a file cannot contain the SHA of
the commit that contains it.

---

## Repository layout

```
hermes-gmi-nexus/
├── README.md                     the repository front door
├── BUILD_STATUS.md               this file
├── LICENSE                       Apache-2.0
├── .gitignore
├── pyproject.toml                development configuration (pytest)
├── catalog-entry/
│   ├── gmi-nexus.yaml            the catalog entry, prepared but not finalised
│   └── PRDRAFT.md                the PR description and the rule-13 disclosure
├── docs/
│   ├── banner.png                the catalogue card's banner, 1200x600
│   ├── demo/                     the demo video and its poster frame
│   └── screenshots/              the dashboard, photographed in a real browser
├── plugins/
│   └── gmi-nexus/                ← the plugin. This is what Hermes installs.
│       ├── plugin.yaml
│       ├── __init__.py           the register(ctx) entrypoint
│       ├── pyproject.toml        declares no dependencies, deliberately
│       ├── README.md
│       ├── LICENSE
│       ├── nexus/                the agent half
│       ├── dashboard/            the dashboard half
│       └── docs/
├── tests/                        the test suite
└── scripts/
    ├── demo_run.py               the demo scenario, end to end
    ├── demo_video.py             the five-phase run the demo video records
    ├── video/                    the narration and the video renderer
    ├── probes/                   the one-off probes that found the defects
    ├── make_banner.py            draws docs/banner.png (development only)
    └── validate_installation.py  loads the plugin the way Hermes does
```

---

## Codebase size

| Part | Lines |
| --- | --- |
| `plugins/gmi-nexus/nexus/` | 9,759 |
| `plugins/gmi-nexus/dashboard/` | 3,397 |
| `tests/` | 4,267 |
| `plugins/gmi-nexus/docs/` | 1,879 |
| `scripts/` | 2,745 |

The plugin payload — `nexus/` plus `dashboard/` plus `docs/` — is **15,035
lines**, with no third-party dependency anywhere in it.

**Third-party dependencies: none.** The plugin imports only the Python
standard library, which is why it declares no dependencies and triggers no
dependency question at install time.
