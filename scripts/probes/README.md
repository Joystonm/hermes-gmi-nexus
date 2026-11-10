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

---

## What the probes found

Six defects. Every one of them is fixed, and every one of them has a
regression test.

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
