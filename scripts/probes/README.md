# The probes

These are the small scripts that were used to find the defects the mocked
tests could not find. Each one asks GMI Cloud a single, specific question and
prints the answer.

They are development tools. They are **not** part of the plugin payload —
`plugins/gmi-nexus/` contains none of them — and the plugin itself imports
nothing but the Python standard library.

Every probe needs a credential:

```bash
export GMI_API_KEY="your-key"
python scripts/probes/probe_vision_live.py
```

Each script prints what it spent as an **estimate at list price**. GMI's usage
block reports token counts and no dollar figure, so no probe can quote a
billed amount, and none of them pretends to.

---

## What each one does

### Vision

| Script | The question it asks |
| --- | --- |
| `probe_vision_models.py` | Sends every catalogue model that *declares* vision the plugin's own probe and reports which ones can actually see — so the gap between the catalogue's claim and the reality is measured rather than assumed. |
| `probe_real_image.py` | Sends the live demo's real generated asset to every model that passed the probe. The probe shows a small synthetic drawing; a real asset is a photograph-like image with gradients and fine detail. The answers are printed so they can be compared against the asset that was actually produced. |
| `probe_real_image_variants.py` | Varies one thing at a time — the encoding, the size, and whether the image travels as a data URI or as a URL — and reports the **prompt-token count** alongside each answer, because the token count shows whether the image reached the model at all. |
| `probe_url_vs_datauri.py` | Sends the same real asset both ways, to every model that passed the probe and a couple that failed it, because a data URI and a URL are very different in size and the models behave differently for each. |
| `probe_vision_live.py` | Runs the plugin's *own* vision path against the real generated asset: the plugin picks a model, probes it if it has not been probed, inlines the image and asks the question. The answer is printed with the model that produced it. Cost: **$0.000341**. |
| `probe_image_limit.py` | Finds the largest image the models will actually look at. A full-size photograph comes back as *"I can't see an image attached"*, with a prompt-token count showing the image was dropped before it was ever tokenised. |
| `probe_image_delivery.py` | Varies the image, its encoding and its size against one model, to find why a model can see the probe image but not the real one. |
| `probe_probe_size.py` | Shows the same drawn number at several sizes and asks for the number each time, so the only thing that changes is the size of the image the model is shown. |
| `probe_image_repeat.py` | Repeats each delivery variant several times and reports the tally, to answer the question the size sweep could not: is *"I can't see an image"* deterministic, or intermittent? |

### The Dev Agent

| Script | The question it asks |
| --- | --- |
| `probe_dev_agent_400.py` | Prints what the catalogue publishes about the code model the live demo failed on, then tries the same prompt with different parameter sets — to find which one the endpoint rejects. |
| `probe_dev_reply.py` | Sends the real implement prompt to the real model and prints the raw reply, so the failure can be *seen* rather than guessed at. |
| `probe_dev_raw.py` | Prints the raw response payload — its keys, its finish reason, and every string field it contains — so an empty reply can be explained rather than guessed at. |
| `probe_code_speed.py` | Sends every code-capable model the same writing task and reports how many **characters a second** each one returns. The catalogue publishes prices and context lengths; it does not publish speed, and speed is what decides whether a page can be written inside a video's time budget. |

### The Creative Engine

| Script | The question it asks |
| --- | --- |
| `probe_image_speed.py` | Sends one real generation to each configuration the plugin's own options can express and reports the wall-clock seconds. The catalogue publishes a media model's price and its parameter schema but not how long it takes, and the generation is the phase a ninety-second video cannot do without. |

---

## What the probes found

Seven defects. Every one of them is fixed, and every one of them has a
regression test. An eighth was found afterwards by a live run with a
rejected credential — it was not a probe's doing, so it is not on this page.

**Vision.**

1. The multimodal message part was spelled the Anthropic way (`"image"`)
   instead of the OpenAI-compatible way (`"image_url"`). Some models reject
   it; the rest accept it *silently* and answer without ever receiving the
   picture. `probe_real_image_variants.py` found it by reporting the
   prompt-token count next to each answer — the image was simply not there.
2. The capability probe asked for the colours of two squares. "Red" and
   "blue" being the two most guessable colour words there are, the probe
   measured willingness to guess rather than eyesight: **13 of the 24**
   catalogue-declared vision models passed it. It now draws a random
   three-digit number, and only **5 of the 24** pass.

**The Dev Agent.**

3. The retry prompt did not carry the original task, so a model that had
   written the wrong file path repeated the mistake.
4. The token budget was too small for a source file, so replies came back cut
   off **mid-JSON** — `finish_reason: "length"` with `completion_tokens:
   2048` exactly — and the error blamed the model rather than the request.
   `probe_dev_reply.py` and `probe_dev_raw.py` found this.
5. The Dev Agent accepted an `approved` flag and never read it, spending
   without consulting the budget guard that every other path in the plugin
   goes through.
6. `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` answers **every** parameter
   variant with a 400. It is broken at the provider, not mis-parameterised by
   NEXUS. `probe_dev_agent_400.py` established that, which is why the demo's
   RECOVER step reconciles the published route against the route that was
   actually taken.

**The Creative Engine.**

7. The reported generation latency was not the time the caller waited. The
   request queue's create call blocks while the request is admitted and
   processed — a real generation took 24 seconds — and the client was timing
   only the polling loop that followed it, so the latency in the tool reply,
   in the report and on the dashboard read **0.4 s**. The clock now starts
   before the submission. `probe_image_speed.py` found it by timing the
   request from the outside and comparing it with the number the plugin
   reported.

---

## The two fixes the Dev Agent findings led to

1. **`code_max_tokens`** (default 8192). The inference client's default of 2048
   is enough for a sentence and nowhere near enough for a source file. A reply
   that runs out of budget is cut off in the middle of the JSON, and a
   half-written file list is not a parse error the model can be asked to fix —
   it is a request that was too small.

2. **The budget gate.** The Dev Agent now goes through the same
   `budget.enforce()` call as every other path in the plugin, before every
   model call.

Both have regression tests in `tests/test_dev.py` and `tests/test_pipeline.py`.

---

## What `probe_code_speed.py` found

The catalogue publishes each model's prices and context length. It does not
publish how fast the model is, and speed turned out to be the single thing
that decides whether a page can be written inside a video's time budget.

On the day it was run, every code-capable model in the catalogue was given
the same writing task and measured:

| Model | Time | Written | Rate |
| --- | --- | --- | --- |
| `moonshotai/kimi-k2.7-code-highspeed` | 11.9 s | 2,415 chars | **203.8 chars/s** |
| `moonshotai/kimi-k2.7-code` | 41.9 s | 2,000 chars | 47.7 chars/s |
| `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` | — | — | answers every request with a 400 |

The two working models differ by a factor of **four**. On the same file, that
is the difference between a minute and a quarter, and four minutes and a
quarter. The demo video blocks the slow one through the plugin's own
`blocked_models` setting, and falls back to the catalogue's own choice if
that model is ever retired.

---

## What `probe_image_speed.py` found

The catalogue publishes a media model's price, its parameter schema and
whether it is active. It publishes nothing about how long a generation
takes, and that turned out to be the number that decides whether the video
fits inside ninety seconds.

| Model and options | Time | Result |
| --- | --- | --- |
| `gpt-image-2.5-flare-generate`, quality `medium` | 17.1 s | success |
| `gpt-image-2.5-flare-generate`, quality `low` | 18.2 s | success |
| `Qwen-Image-2512`, 8 steps | — | queued for 240 s, never started |
| `Flux2-Klein`, 4 steps | — | queued for 240 s, never started |

Two findings. The cheapest models in the catalogue are not the usable ones —
they waited in the request queue and were never dispatched. And the quality
tier makes no measurable difference to the wait, so the reel asks for the
model's own default rather than pretending a cheaper tier is a faster one.

Across seven real generations in this project the time was **14.5 s, 17.1 s,
17.9 s, 19.3 s, 20.3 s, 21.3 s** and one **109.7 s** outlier, when the queue
held the request for a minute and a half. Six of seven sit under
twenty-two seconds.

The same probe is what exposed the seventh defect: the request queue's create
call blocks while the request is admitted and processed, so the plugin's own
latency figure — measured only across the polling loop — reported 0.4 s for a
generation that had really taken 24 s.

