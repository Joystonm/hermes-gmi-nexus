"""JSON schemas for the tools NEXUS registers with Hermes.

The schemas are written for the *model* that reads them, so each one says
what the tool does, what it costs and what it does not do — a model that
knows a tool spends money will use it differently from one that does not.

Every schema follows the Hermes tool shape: ``name``, ``description`` and a
``parameters`` object.
"""

from __future__ import annotations

_STRATEGY = {
    "type": "string",
    "enum": ["balanced", "quality", "fast", "cheap"],
    "description": (
        "Routing strategy. 'balanced' weights quality, reliability, latency and "
        "cost evenly; 'quality' prefers the strongest models; 'fast' prefers the "
        "lowest-latency models; 'cheap' prefers the cheapest."
    ),
}

_APPROVE = {
    "type": "boolean",
    "description": (
        "Set to true to approve a request whose estimated cost is above the "
        "configured approval threshold. Costs are estimated from the live "
        "catalogue's published prices; the actual charge is reported separately."
    ),
}

_MODES = ["analyze", "issues", "compare", "screenshot"]

NEXUS_MODELS_SCHEMA = {
    "name": "nexus_models",
    "description": (
        "List the models GMI Cloud actually offers right now, with the "
        "capabilities NEXUS has verified for each one, their published prices "
        "and whether they are ready to use. Reads the live catalogue — it "
        "never returns a hard-coded model list. Use it before choosing a "
        "model by hand, because the catalogue changes and not every language "
        "model can read an image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring filter on the model id or display name.",
            },
            "modality": {
                "type": "string",
                "enum": ["llm", "image", "video", "audio"],
                "description": (
                    "Which catalogue to search. 'llm' is the inference catalogue; "
                    "the others are the generation catalogues."
                ),
            },
            "capability": {
                "type": "string",
                "enum": [
                    "text",
                    "vision",
                    "tools",
                    "reasoning",
                    "long_context",
                    "code",
                    "audio",
                    "video",
                    "image_out",
                ],
                "description": "Only return models verified to have this capability.",
            },
            "verified_only": {
                "type": "boolean",
                "description": (
                    "Only return models whose capabilities were confirmed by a "
                    "probe rather than inferred from the model id."
                ),
            },
            "refresh": {
                "type": "boolean",
                "description": "Bypass the cache and re-read the catalogue.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of models to return (default 40).",
            },
        },
        "required": [],
    },
}

NEXUS_ANALYZE_SCHEMA = {
    "name": "nexus_analyze",
    "description": (
        "Understand a goal, break it into steps and show which model NEXUS "
        "would route each step to — without calling any model and without "
        "spending anything. Use it to show the plan and the routing reasoning "
        "before committing to a run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What you want accomplished, in plain language.",
            },
            "strategy": _STRATEGY,
        },
        "required": ["goal"],
    },
}

NEXUS_RUN_SCHEMA = {
    "name": "nexus_run",
    "description": (
        "Run a goal through NEXUS' eight-step adaptive pipeline: understand, "
        "plan, route each step to a suitable GMI model, execute, evaluate, "
        "recover from failures, learn from the measured outcome and report. "
        "Each step is routed to the model that best fits it, with cheaper "
        "models used where they are good enough. The report gives the "
        "estimated cost up front and the measured cost afterwards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What you want accomplished, in plain language.",
            },
            "strategy": _STRATEGY,
            "approve": _APPROVE,
        },
        "required": ["goal"],
    },
}

NEXUS_BENCHMARK_SCHEMA = {
    "name": "nexus_benchmark",
    "description": (
        "Benchmark GMI models against each other on identical test cases and "
        "report quality, latency, token use, cost and reliability. The scores "
        "come from real requests; nothing is simulated. The results feed the "
        "router, so a later nexus_run picks better models. This spends money "
        "on every model it tests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Model ids to test. Omit to let NEXUS pick a diverse set "
                    "from the live catalogue."
                ),
            },
            "suite": {
                "type": "string",
                "description": (
                    "Name of the test suite. 'default' covers instruction "
                    "following, JSON contracts, code generation, arithmetic "
                    "reasoning and summarisation."
                ),
            },
            "approve": _APPROVE,
        },
        "required": [],
    },
}

NEXUS_VISION_SCHEMA = {
    "name": "nexus_vision",
    "description": (
        "Look at images with a GMI vision model. Can answer a question about "
        "an image, list the concrete visual defects in a screenshot or mockup, "
        "compare a before and an after, or capture the screen through "
        "Hermes' own computer-use tool. NEXUS verifies that a model can "
        "actually read an image before trusting it with one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": _MODES,
                "description": (
                    "'analyze' answers a question about the image, 'issues' "
                    "returns structured feedback about what is wrong with it, "
                    "'compare' contrasts two images, 'screenshot' captures the "
                    "screen first."
                ),
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Image paths, URLs or data: URIs to read. Required for "
                    "'analyze', 'issues' and 'compare'."
                ),
            },
            "question": {
                "type": "string",
                "description": "The question to answer about the image(s).",
            },
            "before": {
                "type": "string",
                "description": "The 'before' image, for mode 'compare'.",
            },
            "after": {
                "type": "string",
                "description": "The 'after' image, for mode 'compare'.",
            },
            "criteria": {
                "type": "string",
                "description": (
                    "Extra criteria to check, for mode 'issues' — for example "
                    "'matches the brand palette'."
                ),
            },
            "app": {
                "type": "string",
                "description": (
                    "Which application to capture, for mode 'screenshot' "
                    "(default: the whole screen)."
                ),
            },
            "approve": _APPROVE,
        },
        "required": [],
    },
}

NEXUS_MEDIA_SCHEMA = {
    "name": "nexus_media",
    "description": (
        "Generate images, video or audio with GMI's generation models, and "
        "edit existing images (including masked edits). NEXUS reads each "
        "model's own published parameter schema before building the request, "
        "so the options are translated into the parameters that model "
        "actually accepts, and anything it cannot accept is reported as "
        "dropped rather than silently ignored. Generation costs real money."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to generate.",
            },
            "modality": {
                "type": "string",
                "enum": ["image", "video", "audio"],
                "description": "What kind of media to produce (default: image).",
            },
            "options": {
                "type": "object",
                "description": (
                    "Generation options. Recognised keys: aspect_ratio, size, "
                    "width, height, duration, resolution, negative_prompt, "
                    "quality, n, seed, voice, text, style, output_format, "
                    "steps, guidance, fps, speed. Each is translated into "
                    "whichever parameter the chosen model declares."
                ),
            },
            "image": {
                "type": "string",
                "description": (
                    "An existing image to edit, for image-to-image and masked "
                    "editing. A path, URL or data: URI."
                ),
            },
            "mask": {
                "type": "string",
                "description": "The mask that selects the region to edit.",
            },
            "approve": _APPROVE,
        },
        "required": ["prompt"],
    },
}

NEXUS_DEV_SCHEMA = {
    "name": "nexus_dev",
    "description": (
        "Implement a code change with a GMI model, write the files through "
        "Hermes' own write_file tool, run the tests through Hermes' terminal "
        "tool, read the failure output, feed it back for a fix, and verify. "
        "NEXUS never runs anything itself: every side effect goes through "
        "Hermes' tools, so Hermes' approval policy and sandboxing apply. "
        "Use mode 'review' to get structured findings on existing files "
        "instead of writing anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["implement", "review"],
                "description": (
                    "'implement' writes and tests a change; 'review' reports "
                    "problems in existing files and writes nothing."
                ),
            },
            "task": {
                "type": "string",
                "description": "The change to make, described precisely.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Paths of the files the task touches. They are read and "
                    "given to the model as context."
                ),
            },
            "test_command": {
                "type": "string",
                "description": (
                    "Command that runs the tests, e.g. 'pytest -q'. The "
                    "failure output is fed back to the model for a fix."
                ),
            },
            "max_attempts": {
                "type": "integer",
                "description": "How many times to attempt the fix (default 2).",
            },
            "approve": _APPROVE,
        },
        "required": ["task"],
    },
}

NEXUS_REPORT_SCHEMA = {
    "name": "nexus_report",
    "description": (
        "Report what NEXUS has actually done: the recent runs and their "
        "measured costs and latencies, spend by model, the benchmark "
        "history and the routing statistics that decide future model "
        "choices. Every figure comes from recorded history; empty sections "
        "say so rather than showing invented numbers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days of history to include (default 30).",
            },
            "run_id": {
                "type": "string",
                "description": "Return the full detail of one run instead.",
            },
        },
        "required": [],
    },
}


#: Every schema, keyed by tool name.
SCHEMAS: dict[str, dict] = {
    "nexus_models": NEXUS_MODELS_SCHEMA,
    "nexus_analyze": NEXUS_ANALYZE_SCHEMA,
    "nexus_run": NEXUS_RUN_SCHEMA,
    "nexus_benchmark": NEXUS_BENCHMARK_SCHEMA,
    "nexus_vision": NEXUS_VISION_SCHEMA,
    "nexus_media": NEXUS_MEDIA_SCHEMA,
    "nexus_dev": NEXUS_DEV_SCHEMA,
    "nexus_report": NEXUS_REPORT_SCHEMA,
}


__all__ = [
    "NEXUS_ANALYZE_SCHEMA",
    "NEXUS_BENCHMARK_SCHEMA",
    "NEXUS_DEV_SCHEMA",
    "NEXUS_MEDIA_SCHEMA",
    "NEXUS_MODELS_SCHEMA",
    "NEXUS_REPORT_SCHEMA",
    "NEXUS_RUN_SCHEMA",
    "NEXUS_VISION_SCHEMA",
    "SCHEMAS",
]
