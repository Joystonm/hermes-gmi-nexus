#!/usr/bin/env python3
"""The narration script for the GMI NEXUS demo video.

Seven segments, one per beat of the reel. Every claim in this text is checked
against the live run's own output by ``build_video.py`` — if the run does not
say what a segment claims, the build refuses to cut the video.

The text is deliberately short. The video has ninety seconds and the terminal
has to be readable while the voice is talking over it, so each segment says one
thing and stops.

Usage
-----

    INWORLD_API_KEY=<key> python scripts/video/narration.py --out demo_output/video_build

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

#: The TTS endpoint.
ENDPOINT = "https://api.inworld.ai/tts/v1/voice"

#: The model. Inworld TTS-2 is the current one.
MODEL_ID = "inworld-tts-2"

#: The voice. A narrator voice, not a character voice.
VOICE_ID = "Ashley"

#: The seven segments. ``id`` matches the phase the renderer shows.
SEGMENTS: tuple[dict[str, str], ...] = (
    {
        "id": "intro",
        "text": (
            "This is GMI Nexus, a Hermes Agent plugin. "
            "One goal in, a coordinated multi-model workflow out."
        ),
    },
    {
        "id": "catalogue",
        "text": (
            "First, it reads the live GMI Cloud catalogue: seventy-three "
            "language models, three hundred and seventy-eight generation "
            "models. No model id is hard-coded."
        ),
    },
    {
        "id": "route",
        "text": (
            "Then the router picks the model for each step: a coder for the "
            "build, a vision model for the inspection. Nothing is spent here."
        ),
    },
    {
        "id": "generate",
        "text": (
            "Now a real generation request goes to the request queue and the "
            "model returns an actual image. Then a vision model that was "
            "probed for that ability reads the image back and lists the "
            "defects it finds."
        ),
    },
    {
        "id": "build",
        "text": (
            "The Dev Agent writes a landing page and runs its test through "
            "Hermes' own file and terminal tools. When a model fails, the "
            "plugin recovers on a different one."
        ),
    },
    {
        "id": "report",
        "text": (
            "The report says what ran, what it cost, and what failed. Every "
            "cost is an estimate at the published rate, because GMI reports "
            "tokens, not dollars."
        ),
    },
    {
        "id": "outro",
        "text": (
            "Understand. Plan. Route. Execute. Evaluate. Recover. Learn. "
            "Report. GMI Nexus, for the Hermes Agent plugin hackathon."
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
        "--rate",
        type=float,
        default=1.0,
        help="speaking rate, 0.5 to 1.5 (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("INWORLD_API_KEY", "").strip()
    if not api_key:
        print("Refusing to run without INWORLD_API_KEY in the environment.", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "modelId": args.model,
        "script": {
            "utterances": [
                {
                    "text": segment["text"],
                    "voiceConfig": {"voiceId": args.voice},
                }
                for segment in SEGMENTS
            ]
        },
        "outputMode": "PER_UTTERANCE",
        "audioConfig": {
            "audioEncoding": "WAV",
            "sampleRateHertz": 24000,
        },
        "deliveryMode": "BALANCED",
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
            }
        )
        print(f"  {segment['id']:<10} {len(raw):>8} bytes -> {path}")

    usage = data.get("usage") or {}
    manifest = {
        "model": args.model,
        "voice": args.voice,
        "segments": written,
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
