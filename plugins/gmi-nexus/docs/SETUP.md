# Setup

Installing GMI NEXUS, configuring it, and understanding what it will and will
not spend.

---

## Requirements

| | |
| --- | --- |
| Hermes | `>=0.21.6` |
| Python | `>=3.11` |
| Dependencies | **None.** The plugin imports only the standard library. |
| Credentials | One GMI Cloud API key, in `GMI_API_KEY`. |

There is nothing to `pip install`. Hermes validates a plugin's
`python_dependencies` and surfaces them, but never installs them on your
behalf — so NEXUS declares none.

---

## Install

```bash
cd /path/to/hermes-gmi-nexus
hermes plugins install ./plugins/gmi-nexus
```

The installer prompts for the environment variables the manifest declares, and
saves your answers to the Hermes profile's `.env`:

```
gmi-nexus requires the following environment variables:

  GMI_API_KEY — GMI Cloud API key. Hermes stores it in the profile's .env and
  NEXUS reads it through Hermes' secret scope, so it is never written into the
  plugin directory and never appears in a log line or a dashboard response.
  Get yours at: https://gmicloud.ai
  GMI_API_KEY:
```

`GMI_API_KEY` is declared `secret: true`, so it is read through a masked
prompt and never echoed. It lands in your profile's `.env` — never inside the
plugin directory, never in the plugin's logs, never in a dashboard response.

If you would rather set it yourself:

```bash
# a bare UPPER_SNAKE key is written to the profile's .env, not config.yaml
hermes config set GMI_API_KEY "your-key"
hermes config env-path      # print where that .env lives
```

Or edit it by hand:

```bash
# in the file that `hermes config env-path` prints
GMI_API_KEY=your-key-here
```

NEXUS reads it through Hermes' secret scope (`agent.secret_scope.get_secret`).
Inside Hermes that scope is the **only** source: it follows the active
profile, and NEXUS never falls back to a bare environment read, which under
profile multiplexing could pick up another profile's key. The process
environment is consulted only when Hermes' secret scope is not installed at
all — for example when you run the plugin's own tests or scripts outside
Hermes.

Then check the install:

```bash
hermes plugins validate ./plugins/gmi-nexus --install-deps
hermes plugins doctor ./plugins/gmi-nexus
```

`validate` checks the manifest, the security scan and the registration
contract. `doctor` loads the plugin the way Hermes does and reports what it
registered. Both should report no errors.

---

## The endpoints

Two GMI Cloud endpoints are used, and both are read live at runtime:

| Purpose | Default | Where it comes from |
| --- | --- | --- |
| Language-model inference | `https://api.gmi-serving.com/v1` | `llm_base_url` |
| Image, video and audio generation | `https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey` | `media_base_url` |

Check that the inference endpoint answers before anything else — it is a free
read:

```bash
python - <<'PY'
import os, urllib.request
request = urllib.request.Request(
    "https://api.gmi-serving.com/v1/models",
    headers={"Authorization": "Bearer " + os.environ["GMI_API_KEY"]},
)
print(urllib.request.urlopen(request, timeout=30).status)
PY
```

A `200` means the key is valid and the catalogue is reachable. Anything else
means the problem is the key or the endpoint, not the plugin.

---

## Configuration

Every setting lives under the plugin's own entry in `config.yaml`:

```yaml
plugins:
  entries:
    gmi-nexus:
      settings:
        llm_base_url: "https://api.gmi-serving.com/v1"
        media_base_url: "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"
        request_timeout_s: 120
        max_retries: 2
        max_parallel: 3
        max_cost_per_run_usd: 1.0
        daily_budget_usd: 5.0
        approval_threshold_usd: 0.25
        code_max_tokens: 8192
        preferred_models: []
        blocked_models: []
        verify_vision_capability: true
        enable_learning: true
        retention_days: 30
```

| Setting | Default | What it does |
| --- | --- | --- |
| `llm_base_url` | `https://api.gmi-serving.com/v1` | The OpenAI-compatible inference endpoint. |
| `media_base_url` | `https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey` | The request-queue endpoint for generation. |
| `mcp_server` | `gmi` | The name of a configured Hermes MCP server for GMI Cloud. |
| `use_mcp` | `false` | Prefer the MCP path for discovery and generation when it is connected and allowlisted. |
| `request_timeout_s` | `120` | Per-request timeout. |
| `max_retries` | `2` | Retry attempts per step before the recovery policy switches model. |
| `max_parallel` | `3` | Independent steps run concurrently. |
| `max_cost_per_run_usd` | `1.0` | The ceiling for one run. |
| `daily_budget_usd` | `5.0` | The rolling ceiling for the day. |
| `approval_threshold_usd` | `0.25` | Estimated cost at or above which a call is held for a person to approve through Hermes' own approval prompt. A call whose cost cannot be estimated — because the catalogue publishes no price for the model — is always held. NEXUS has no argument the model can set to approve its own spend. |
| `code_max_tokens` | `8192` | The token budget for a call that has to return whole files. The inference client's default of 2048 is enough for a sentence and not enough for a source file: a reply that runs out of budget is cut off mid-JSON. |
| `preferred_models` | `[]` | Model ids to prefer; earlier entries score higher. |
| `blocked_models` | `[]` | Model ids NEXUS must never route to. |
| `verify_vision_capability` | `true` | Probe a model with a tiny image — a randomly drawn three-digit number — before trusting it for vision tasks. |
| `enable_learning` | `true` | Fold verified outcomes into the routing statistics. |
| `retention_days` | `30` | How long execution history is kept before pruning. |

Settings can also be set from the Hermes config UI — the manifest's
`config_schema` populates the entries for you.

---

## The spend limits

This is the part worth reading twice.

Three limits apply, and they apply **before** a request goes out, not after it
comes back:

1. **`max_cost_per_run_usd`** — one run may not exceed this estimated cost.
2. **`daily_budget_usd`** — the rolling daily ceiling across everything NEXUS
   does.
3. **`approval_threshold_usd`** — a request estimated at or above this is
   *held for a person*. NEXUS hands it to Hermes' own approval prompt, and
   the person answering that prompt is the only thing that unlocks it.

There is no `approve` argument in any of NEXUS' tool schemas, and there is no
way for the model to mint an approval for itself. The approval rule is per
tool, so allowing `nexus_run` for the session does not allow `nexus_media`.

A request whose cost **cannot be worked out** is held as well. That is the
case for a model the catalogue publishes no price for, and for a catalogue
NEXUS has not read yet. An unknown cost is not a zero cost, and treating it
as one is how an uncapped spend happens.

The estimate is the provider's published list price with the model's own
published discount applied. It is an estimate. NEXUS reports it as one and
never presents it as a bill.

To raise a limit, raise it in the configuration. NEXUS does not have a way to
raise its own limits.

### Choosing a budget

The defaults are deliberately conservative. If you are exploring the
catalogue, keep them. If you are running real work, size them against what the
catalogue actually charges — `nexus_models` prints each model's published price,
and `nexus_analyze` prints the estimated cost of a plan before you commit to
it.

---

## Verifying the install

Nothing here spends money except the last check.

```bash
# 1. The catalogue reads. Free.
hermes chat -q "Use nexus_models to list the GMI Cloud models available right now."

# 2. A plan, with no model called and no money spent.
hermes chat -q "Use nexus_analyze to break down: write a landing page for a new AI startup."

# 3. The status of what has run so far.
hermes chat -q "Use nexus_report to show me the last 7 days of runs."

# Or, from inside Hermes, the slash command:
/nexus status
```

The third one works even with no credential configured, because it reads the
recorded history alone.

---

## The dashboard

The dashboard tab is part of the plugin. It needs the Hermes Web dashboard,
which is an optional extra.

Hermes declares four pins for its `web` extra, and the dashboard server
requires exactly those:

| Package | Pin |
| --- | --- |
| `fastapi` | `0.133.1` |
| `uvicorn` | `0.54.0` |
| `starlette` | `1.9.0` |
| `python-multipart` | `0.0.32` |

They are one extra away:

```bash
pip install "hermes-agent[web]"
hermes dashboard
```

If you would rather not touch the environment Hermes itself runs in, put the
pins in a virtual environment instead and run Hermes from there. The
environment's own copies shadow the ones outside it, so the versions Hermes
sees are the pins:

```bash
python -m venv --system-site-packages .hermes-web
.hermes-web/Scripts/python -m pip install "hermes-agent[web]"
.hermes-web/Scripts/hermes dashboard
```

`--system-site-packages` matters here: the virtual environment has to see the
rest of Hermes' dependencies, which live outside it. Its own `fastapi` and
`starlette` still win, because a virtual environment's packages take
precedence over the system's.

### When the versions disagree

A `fastapi` and a `starlette` that disagree break the dashboard server, and
with it the plugin's tab. The plugin's own code is not the cause. The symptom
is:

```
TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'
```

That happens when `starlette` is older than the `fastapi` that expects it —
`APIRouter` grew `on_startup`/`on_shutdown` as `lifespan` replaced them, and
the two halves have to agree on which the router accepts.

### What you should see

The tab is discovered automatically from the plugin's `dashboard/` folder, so
there is nothing to register. It appears at `/nexus` in the dashboard's tab
bar.

Where there is no data yet, the sections say so and say why. The Overview of
a fresh install shows the live catalogue — the real number of language models
and generation models, read from GMI Cloud — and a spend figure of `$0.00`,
because nothing has been spent. It never invents a number to fill the space.

---

## Where the data is kept

```
<hermes home>/plugin-data/gmi-nexus/data.db
```

The path follows the active Hermes profile. Nothing is written into the plugin
installation directory, so reinstalling or upgrading the plugin does not
disturb the history, and the history never ends up in a git repository.

To clear it:

```bash
hermes plugins remove gmi-nexus   # the history goes with it
```

---

## Uninstalling

```bash
hermes plugins remove gmi-nexus
```

This removes the plugin and its data directory. The GMI Cloud key stays in
your profile's `.env` — remove that line too if you want it gone.
