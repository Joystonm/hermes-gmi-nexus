# Add GMI NEXUS to the plugin catalog

GMI NEXUS is an adaptive intelligence and orchestration layer for Hermes Agent
over the GMI Cloud model catalogue. It turns one goal into a coordinated
multi-model workflow through an eight-step pipeline — **UNDERSTAND → PLAN →
ROUTE → EXECUTE → EVALUATE → RECOVER → LEARN → REPORT** — and reports what
actually ran, what it cost and what failed.

- **Repository:** https://github.com/Joystonm/hermes-gmi-nexus
- **Pinned commit:** `829fec1a816a7a7d36bdef8e73987688f5a3397c`
- **Subdirectory:** `plugins/gmi-nexus`
- **Maintainer:** Joystonm
- **Category:** `models` · **Tier:** `community` · **Requires Hermes:** `>=0.21.6`
- **Demo:** [`docs/demo/gmi-nexus-demo.mp4`](https://github.com/Joystonm/hermes-gmi-nexus/blob/829fec1a816a7a7d36bdef8e73987688f5a3397c/docs/demo/gmi-nexus-demo.mp4) — 80 seconds, one continuous live run, with narration. Every number in it came out of that run; nothing in it was drawn by hand.

## What this plugin does

It registers eight tools and one command:

| Tool | Purpose |
| --- | --- |
| `nexus_models` | The live catalogue, with the capabilities NEXUS has verified for each model and their published prices. |
| `nexus_analyze` | Understands a goal, breaks it into steps and shows which model each step would route to. Calls no model and spends nothing. |
| `nexus_run` | Runs a goal through the whole pipeline. |
| `nexus_benchmark` | Identical test cases across several models: quality, correctness, latency, tokens, cost, reliability. The scores come from real requests. |
| `nexus_vision` | Image analysis, a concrete defect list for a screenshot or mockup, before/after comparison. |
| `nexus_media` | Image, video and audio generation; image editing, including masked edits. |
| `nexus_dev` | Implements a change, writes it through Hermes' `write_file`, tests it through Hermes' `terminal`, reads the failure and fixes it. |
| `nexus_report` | Runs, measured costs and latencies, spend by model, benchmark history, routing statistics. |

The `/nexus` command shows the same status without a tool call.

## Which Hermes surfaces it uses

| Surface | Use |
| --- | --- |
| `ctx.register_tool` | The eight tools. `requires_env=["GMI_API_KEY"]`, plus a `check_fn` so a tool that cannot work is never offered. |
| `ctx.register_command` | `/nexus`. |
| `ctx.get_config` | The plugin's settings, from `plugins.entries.gmi-nexus.settings`. |
| `ctx.dispatch_tool` | The Dev Agent's write and test steps — Hermes' own `write_file` and `terminal`, so Hermes' approval policy, sandboxing and audit trail apply to them exactly as they apply to the agent. |
| `agent.secret_scope.get_secret` | The GMI Cloud key. |
| `plugins.plugin_storage` | The persistent data directory. |
| `dashboard/manifest.json`, `dashboard/dist/`, `dashboard/plugin_api.py` | The web dashboard tab. |

No core file is overridden, no core function is rebound, and no dependency is
added: the plugin imports only the Python standard library.

## Disclosure (catalog rule 13)

What a user would want to know before installing. The same text is in the
plugin's own README, which is the page the catalog renders.

* **Network calls to a third-party service.** The plugin calls GMI Cloud:
  the OpenAI-compatible inference endpoint `https://api.gmi-serving.com/v1`
  and the generation request queue
  `https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey`. Model
  catalogues are read from these; inference and generation are sent to them.
* **Stored credential.** It needs one credential, `GMI_API_KEY`, declared
  `secret: true` in the manifest. Hermes stores it in the profile's `.env` and
  the plugin reads it through `agent.secret_scope.get_secret`. The plugin
  never writes it to disk, never logs it and never returns it — the dashboard
  reports only whether one is configured. Inside Hermes the secret scope is
  the *only* source: the plugin never falls back to a bare `os.environ` read,
  which under profile multiplexing could pick up another profile's key.
* **Reads outside the plugin's own data.** The Dev Agent reads and writes
  files **through Hermes' own tools**, under Hermes' approval policy, in the
  working directory the agent is in. It spawns no shell of its own.
* **Long-running background processes.** None. The plugin starts no threads,
  daemons or services.
* **Telemetry and usage reporting.** None. Nothing is sent anywhere except
  GMI Cloud, and only as part of a request the agent asked for.
* **Spending.** A per-run ceiling, a rolling daily ceiling and an approval
  threshold are enforced **before** a request is submitted. A request
  estimated above the threshold is refused unless the caller approves it. The
  figures NEXUS reports are the provider's reported usage priced at the
  catalogue's published rates, and are labelled as estimates — never as a
  bill.
* **Capability probing.** To verify that a model can actually read an image,
  NEXUS may send it a small probe image — a randomly drawn three-digit
  number — and ask it to read the number back. This is a real request and is
  accounted for like any other.

## How it was verified

Four levels, kept separate; see the plugin's
`docs/HERMES_VALIDATION.md` for the full report.

1. **Unit** — 274 tests collected: **268 passed, 6 skipped** (the live ones),
   all passing. No network, no cost. Under Hermes' own `web` pins the same
   suite was recorded at **230 passed, 5 skipped**, with the real `fastapi`
   router loaded. (That pinned figure was recorded before the suite grew by
   the tests covering the generation-latency fix, the retired-model verdict,
   the malformed-JSON repair and the narration's claim check.)
2. **Mock integration** — the real pipeline, router, executor, evaluator,
   storage, budget and dashboard API, with only the HTTP layer stubbed.
   `scripts/demo_run.py --dry-run` runs the whole scenario end to end this
   way: the run's status comes back `completed`, and the page the Dev Agent
   wrote is really on disk and really passes its test.
3. **Live API** — the catalogue reads, a paid chat completion, a multimodal
   completion carrying a real image, image generations through the request
   queue, and the whole demo scenario have all been run against the real
   service with a real credential. The live test suite exists, is
   opt-in behind `GMI_LIVE_TESTS=1`, and every live figure quoted anywhere
   in this repository is an **estimate priced at the catalogue's published
   rates** — GMI's usage block reports token counts and no dollar amount, so
   no billed total is claimed. The whole live exercise cost **just over $1**
   of the project's $10 ceiling.
4. **Hermes plugin** — `hermes plugins validate ./plugins/gmi-nexus
   --install-deps` passes with a `safe` security scan, and
   `scripts/validate_installation.py` passes 44/44 by loading the plugin
   through Hermes' own discovery and loader in a real temporary Hermes home.

### What the live run found that the mocked tests could not

Worth saying plainly, because it is the argument for running a plugin
against the real service rather than only against a mock. Ten defects were
found this way and all ten are fixed:

* the multimodal message part was spelled the Anthropic way (`image`)
  instead of the OpenAI-compatible way (`image_url`), which some models
  reject and the rest accept *silently*, receiving no image at all;
* the capability probe asked for the colours of two squares — "red" and
  "blue" being the two most guessable colour words there are — so **13 of
  the 24** catalogue-declared vision models passed a probe that proved
  nothing. It now draws a random three-digit number, and only **5 of the
  24** pass;
* a verdict recorded under the old probe protocol was still counting;
* the Dev Agent's retry prompt did not carry the original task, so a model
  that had written the wrong file path repeated the mistake;
* the token budget was too small for a source file, so replies came back
  cut off mid-JSON and the error blamed the model rather than the request;
* the Dev Agent accepted an `approved` flag and never read it, spending
  without consulting the budget guard that every other path in the plugin
  goes through;
* the reported generation latency was not the time the caller waited. The
  request queue's create call blocks while the request is admitted and
  processed — a real generation took 24 seconds — and the client was timing
  only the polling loop that followed it, so the latency in the tool reply,
  in the report and on the dashboard read **0.4 s**. The clock now starts
  before the submission.

* a failed catalogue read was reported as an empty one. A rejected API key
  produced "0 language models" and then, for every step of the plan, "no
  model in the live catalogue satisfies coding" — each of which reads like a
  fact about GMI Cloud rather than a fact about the request. The registry
  had recorded the reason; nothing carried it forward. The error now reaches
  every place that reports an empty catalogue, and both demo scripts stop
  rather than run against a catalogue that was never fetched;
* a model the provider had **retired** was still routable, and the provider's
  real reason was swallowed. GMI Cloud publishes the catalogue and the models
  it can actually serve separately, so a deprecated model keeps its place in
  the catalogue. The router chose it, called it, and was refused with a
  generic *"Backend request failed with status 400"* — while the actual
  reason, *"The requested model has been deprecated and is no longer
  available."*, sat one level down in the error body and never reached the
  caller. The plugin now reads the provider's own reason out of the error,
  reports it verbatim, and **remembers the verdict**: a model the provider
  has retired is not routed to again, and the work recovers on a different
  one. The demo video shows this happening;
* a code reply that was *right* but wrote the newlines inside its JSON
  string literally was discarded as unparseable, and the retry prompt did
  not show the model the reply that had failed — so it repeated the same
  malformed answer. The parser now repairs raw newlines inside JSON string
  literals instead of throwing the work away, and the retry carries the
  model's own unusable reply, the exact shape it must use, and the rule
  that a newline inside a string is written as `\n`.

Each has a regression test — fourteen of them for the last two alone, in
`tests/test_resilience.py`.

## The card

The entry carries an `image:` banner (1200x600, 2:1) and the six dashboard
screenshots in `screenshots:` — the maximum the schema allows. Both are
pinned to the entry's commit, so the card can never show a picture of a
different version than the code it describes.

The dashboard tab has six sections — Overview, Model Intelligence, Arena,
Execution, Multimodal and Settings. It has been **rendered in a real
browser, inside a real Hermes install**, and driven through all six
sections with zero console errors; the photographs are in `docs/screenshots/`
in the plugin's repository.

The Overview photograph is the honest state of a fresh install: the live
catalogue read from GMI Cloud, and no runs — so the spend charts are empty
and say why. It never shows a number NEXUS did not measure.

## Reviewer notes

* The plugin lives in a **subdirectory** (`plugins/gmi-nexus`) so that the
  development tooling around it — the tests, the validation script, the
  workspace `pyproject.toml` — is not installed with it. The entry's
  `subdir:` points there.
* The plugin's `pyproject.toml` declares `dependencies = []` with no build
  system, no extras and no dependency groups, so it reads as "installs
  nothing" and never triggers a dependency question.
* `nexus_dev` deliberately has **no** shell of its own. Every file write and
  every test command goes through `ctx.dispatch_tool` to Hermes' own tools.
  It also goes through the same budget gate as every other path, so the
  `approve` flag on the tool means what it says.
* `scripts/demo_run.py` runs the brief's scenario end to end — analyse, route,
  generate, implement through Hermes' tools, inspect, correct, verify, report.
  `--dry-run` spends nothing and labels every simulated output as such;
  `--live` needs both a credential and `--approve-spend`, and prints what it
  is about to spend before it spends it.
* `scripts/demo_video.py` is the same plugin on the same real service in five
  phases — catalogue, generate, see, build, report — arranged so the whole
  thing finishes inside the ninety seconds the demo video is allowed. Three
  measured runs came in at **69.3**, **53.4**, **76.0** and **82.2 seconds** —
  every one of them inside the limit, the last one on a cold probe cache where
  the vision model is probed live, and the spread between them is the request
  queue's, not the plugin's. The time budget was measured
  rather than guessed: `scripts/probes/probe_code_speed.py` measured the code
  models and `scripts/probes/probe_image_speed.py` measured the image models,
  and the video script expresses both results through the plugin's own
  `blocked_models` setting — the slow code model is blocked, and the reel asks
  for the image model's own default rather than pretending a cheaper quality
  tier is a faster one. Across seven real generations the time was 14.5 s,
  17.1 s, 17.9 s, 19.3 s, 20.3 s, 21.3 s and one 109.7 s outlier, when the
  queue held the request for a minute and a half. The two runs that were
  recorded reported their own generation at 19.0 s and 22.3 s — the time the
  caller actually waited, which is the figure the latency fix restored.
* The live run's RECOVER section **reconciles the published route against
  the route that was actually taken**, out loud. On the live catalogue the
  published code model has been **retired by the provider**, so it is refused
  and the model that ran is not the model that was published. That is the
  RECOVER step doing its job, and the report says so rather than quietly
  agreeing with itself. The demo video shows it happening.
* **The demo video is in the repository** — `docs/demo/gmi-nexus-demo.mp4`,
  80.4 seconds, 1920×1080, narrated, cut from a single continuous live run.
  The terminal on the left is that run's own transcript and the numbers on the
  right are read out of its JSON, so nothing in the video was drawn by hand.
  The narration was synthesised with Inworld TTS in **one request** for all
  seven segments, and every claim the voice makes is checked against that
  run's own JSON before the narration is synthesised and again before the
  video is cut — so the voice cannot describe a run that did not happen.
  Nothing in the picture sits still: over the finished file, 2410 of the
  2411 frames are distinct.
* The banner is drawn by `scripts/make_banner.py`, a development tool. It is
  the only thing in the repository that needs a third-party package (Pillow),
  it is not in the plugin payload, and the plugin itself still imports nothing
  but the standard library.
* The one-off scripts that found seven of the ten defects live in `scripts/probes/`,
  outside the plugin, each documented in that directory's README.
