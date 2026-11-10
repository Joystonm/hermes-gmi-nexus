# Catalog PR — GMI NEXUS

> **Draft.** Every `SHA`, `OWNER` and `REPLACE-ME` below must be filled in once the
> repository is pushed. Rule 2 requires an exact 40-character commit SHA, and
> the PR must add **one** file: `plugin-catalog/gmi-nexus.yaml`.

## What this plugin does

GMI NEXUS is an adaptive intelligence and orchestration layer for Hermes Agent
over the GMI Cloud model catalogue. It turns one goal into a coordinated
multi-model workflow through an eight-step pipeline — **UNDERSTAND → PLAN →
ROUTE → EXECUTE → EVALUATE → RECOVER → LEARN → REPORT** — and reports what
actually ran, what it cost and what failed.

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

1. **Unit** — 227 tests, 6 skipped (the live ones), all passing. No network,
   no cost. Under Hermes' own `web` pins the same suite is **228 passed,
   5 skipped**, with the real `fastapi` router loaded.
2. **Mock integration** — the real pipeline, router, executor, evaluator,
   storage, budget and dashboard API, with only the HTTP layer stubbed.
   `scripts/demo_run.py --dry-run` runs the whole scenario end to end this
   way: the run's status comes back `completed`, and the page the Dev Agent
   wrote is really on disk and really passes its test.
3. **Live API** — the catalogue reads, a paid chat completion, a multimodal
   completion carrying a real image, three image generations through the
   request queue, and the whole demo scenario have all been run against the
   real service with a real credential. The live test suite exists, is
   opt-in behind `GMI_LIVE_TESTS=1`, and every live figure quoted anywhere
   in this repository is an **estimate priced at the catalogue's published
   rates** — GMI's usage block reports token counts and no dollar amount, so
   no billed total is claimed.
4. **Hermes plugin** — `hermes plugins validate ./plugins/gmi-nexus
   --install-deps` passes with a `safe` security scan, and
   `scripts/validate_installation.py` passes 44/44 by loading the plugin
   through Hermes' own discovery and loader in a real temporary Hermes home.

### What the live run found that the mocked tests could not

Worth saying plainly, because it is the argument for running a plugin
against the real service rather than only against a mock. Six defects were
found this way and all six are fixed:

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
  goes through.

Each has a regression test.

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
* The live run's RECOVER section **reconciles the published route against
  the route that was actually taken**, out loud. On the live catalogue the
  published code model answers every request with a 400, so the model that
  ran is not the model that was published. That is the RECOVER step doing
  its job, and the report says so rather than quietly agreeing with
  itself.
* The banner is drawn by `scripts/make_banner.py`, a development tool. It is
  the only thing in the repository that needs a third-party package (Pillow),
  it is not in the plugin payload, and the plugin itself still imports nothing
  but the standard library.

## Checklist

- [ ] The repository is public.
- [ ] No bytecode is staged. Running the tests writes `__pycache__` back into
      the plugin directory; `.gitignore` excludes it, but check the staged
      tree (`git status`) and confirm the payload gate passes:
      `python scripts/scan_plugin_payloads.py plugins/gmi-nexus`.
- [ ] The commit is pushed and its 40-character SHA replaces every `SHA` in
      `catalog-entry/gmi-nexus.yaml` — in `sha:`, in `docs_url`, in `image:`
      and in all six `screenshots:` URLs.
- [ ] `OWNER` is replaced with the repository owner, in the entry's `repo:`,
      in `docs_url`, in `image:`, in the `screenshots:` URLs, and in the
      `homepage:` field of `plugin.yaml`.
- [ ] `maintainer:` names the maintainer.
- [ ] `docs_url` points at the plugin README **at that SHA**.
- [ ] `plugin-catalog/gmi-nexus.yaml` is the only file this PR adds.
- [ ] The `capabilities:` block matches what the plugin registers — it is
      checked against the pinned commit by the admission CI.
- [ ] The PR description carries the disclosure above.
