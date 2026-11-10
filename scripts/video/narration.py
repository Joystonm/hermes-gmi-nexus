#!/usr/bin/env python3
"""The narration script for the GMI NEXUS demo video.

Seven segments, one per beat of the reel. The text is written the way a
person explains their own work out loud — short sentences, long sentences,
a breath where a person would take one, and the numbers said the way they
are said rather than the way they are typed.

The in-text directives are Inworld TTS-2's own:

``<break time="0.5s" />``
    a pause, the length a person would pause for.
``[breathe]``
    the breath a person takes before the next thought.
``[say ...]``
    steering: how the next line is delivered. It holds until it is changed
    or reset, so it is used once and reset at once.
``*word*``
    emphasis.

Every factual claim the voice makes is written down in ``claims.py`` and
checked against the run's own JSON before anything is synthesised — so the
narration cannot describe a run that did not happen.

Usage
-----

    INWORLD_API_KEY=<key> python scripts/video/narration.py \
        --out demo_output/video_build --run demo_output/reel2/video_run.json

It writes one WAV per segment and a ``narration.json`` describing them. One
HTTP request covers all seven segments, so there is no per-call overhead and
nothing is synthesised twice.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claims  # noqa: E402  (the path is set above)

#: The TTS endpoint.
ENDPOINT = "https://api.inworld.ai/tts/v1/voice"

#: The model. Inworld TTS-2 is the current one.
MODEL_ID = "inworld-tts-2"

#: The voice. A narrator voice, not a character voice.
VOICE_ID = "Ashley"

#: The seven segments. ``id`` matches the phase the renderer shows.
#:
#: ``rate``  how fast that segment is spoken. The rate moves about, because a
#:           voice that never changes its pace is the thing that makes a
#:           narration sound like a machine reading a list.
#: ``mode``  the delivery. ``CREATIVE`` lets the voice vary its pacing and
#:           emphasis inside the line, which ``BALANCED`` does not.
SEGMENTS: tuple[dict[str, object], ...] = (
    {
        "id": "intro",
        "rate": 0.98,
        "mode": "CREATIVE",
        "text": (
            "So, here's the idea. <break time=\"0.5s\" /> "
            "This is *GMI Nexus*, a plugin for Hermes Agent. "
            "You give it one goal, and it works out which models to use."
        ),
    },
    {
        "id": "catalogue",
        "rate": 1.0,
        "mode": "CREATIVE",
        "text": (
            "First thing it does? Read the catalogue. Live, from GMI Cloud, "
            "just now. Seventy-three language models, three hundred and "
            "seventy-eight generation models. And not one model id is "
            "hard-coded."
        ),
    },
    {
        "id": "route",
        "rate": 1.04,
        "mode": "CREATIVE",
        "text": (
            "Then it plans the work, and routes each step to the model that "
            "suits it. A coder for the build, a vision model for the "
            "inspection. This part's free — no model's called yet."
        ),
    },
    {
        "id": "generate",
        "rate": 1.02,
        "mode": "CREATIVE",
        "text": (
            "Now the real work. A generation request goes into GMI's request "
            "queue, [say excitedly with a high pitch and fast pace] and an "
            "actual image comes back. [reset] Then a vision model — one we've "
            "probed for it — reads it back and lists what's wrong."
        ),
    },
    {
        "id": "build",
        "rate": 1.0,
        "mode": "CREATIVE",
        "text": (
            "Then the Dev Agent. [breathe] It writes a landing page through "
            "Hermes' own file tools and runs its test through Hermes' own "
            "terminal. No side door. And when the model it picked is refused, "
            "the plugin recovers on another."
        ),
    },
    {
        "id": "report",
        "rate": 0.96,
        "mode": "CREATIVE",
        "text": (
            "And finally, the report: what ran, what it cost, what broke. "
            "One thing worth being precise about — every dollar there is an "
            "*estimate* at GMI's published rates. GMI reports tokens, "
            "not dollars."
        ),
    },
    {
        "id": "outro",
        "rate": 0.94,
        "mode": "CREATIVE",
        "text": (
            "Understand. Plan. Route. Execute. Evaluate. Recover. Learn. "
            "Report. That's GMI Nexus — one goal, many models, one "
            "coordinated system."
        ),
    },
)


def _post(payload: dict, api_key: str, timeout: int = 90) -> dict:
    """Send one synthesis request and return the decoded JSON body."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            # The Inworld key *is* the base64 credential; it is used exactly as
            # issued and is not encoded a second time.
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="demo_output/video_build")
    parser.add_argument("--voice", default=VOICE_ID)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument(
        "--run",
        default="",
        help="the run's JSON; the narration is checked against it before "
        "anything is synthesised",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="speaking rate applied to every segment, 0.5 to 1.5 "
        "(default: %(default)s). Each segment also carries its own rate, "
        "which this scales.",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("INWORLD_API_KEY", "").strip()
    if not api_key:
        print("Refusing to run without INWORLD_API_KEY in the environment.", file=sys.stderr)
        return 2

    texts = {str(segment["id"]): str(segment["text"]) for segment in SEGMENTS}

    # The narration must describe the run it is cut against. If the run says
    # something different, nothing is synthesised and nothing is paid for.
    if args.run:
        run = json.loads(Path(args.run).read_text(encoding="utf-8"))
        problems = claims.check(run, texts)
        if problems:
            print(
                "The narration does not match this run:\n  "
                + "\n  ".join(problems)
                + "\nNothing was synthesised.",
                file=sys.stderr,
            )
            return 1
        print(f"Checked {len(claims.CLAIMS)} claims against {args.run}. All true.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "modelId": args.model,
        "script": {
            "utterances": [
                {
                    "text": segment["text"],
                    "voiceConfig": {
                        "voiceId": args.voice,
                        "speakingRate": round(
                            float(segment["rate"]) * args.rate, 3
                        ),
                        "deliveryMode": segment["mode"],
                    },
                }
                for segment in SEGMENTS
            ]
        },
        "outputMode": "PER_UTTERANCE",
        "audioConfig": {
            "audioEncoding": "WAV",
            "sampleRateHertz": 24000,
        },
        "deliveryMode": "CREATIVE",
        "applyTextNormalization": "ON",
    }

    print(f"Synthesising {len(SEGMENTS)} segments with {args.model} / {args.voice}...")
    try:
        data = _post(payload, api_key)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print(f"HTTP {error.code}: {body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"Network error: {error.reason}", file=sys.stderr)
        return 1

    utterances = (data.get("scriptResult") or {}).get("utterances") or []
    if len(utterances) != len(SEGMENTS):
        print(
            f"Expected {len(SEGMENTS)} utterances, got {len(utterances)}.\n"
            f"Response keys: {sorted(data)}",
            file=sys.stderr,
        )
        return 1

    written = []
    for segment, utterance in zip(SEGMENTS, utterances):
        raw = base64.b64decode(utterance["audioContent"])
        path = out / f"{segment['id']}.wav"
        path.write_bytes(raw)
        written.append(
            {
                "id": segment["id"],
                "text": segment["text"],
                "path": str(path),
                "bytes": len(raw),
                "speaking_rate": round(float(segment["rate"]) * args.rate, 3),
                "delivery_mode": segment["mode"],
            }
        )
        print(f"  {segment['id']:<10} {len(raw):>8} bytes -> {path}")

    usage = data.get("usage") or {}
    manifest = {
        "model": args.model,
        "voice": args.voice,
        "segments": written,
        "claims": list(claims.CLAIMS),
        "processed_characters": usage.get("processedCharactersCount"),
    }
    (out / "narration.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(
        f"\n{usage.get('processedCharactersCount', '?')} characters processed. "
        f"Manifest at {out / 'narration.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
