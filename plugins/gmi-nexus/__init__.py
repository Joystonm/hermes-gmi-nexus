"""GMI NEXUS — the adaptive intelligence layer for Hermes Agent.

One agent, many models, one intelligent system.

NEXUS registers eight tools that turn a single goal into a coordinated
multi-model workflow: it understands what was asked, plans the work, routes
each step to the GMI Cloud model that suits it, runs the steps, checks the
results, recovers from failures, learns from the measured outcome and reports
what happened.

What NEXUS deliberately does **not** do:

* It does not ship a list of model ids. The catalogue is read live from GMI
  Cloud every time, so a model that is retired stops being routable and a
  model that is added becomes routable without a code change.
* It does not assume a language model can read an image. Vision capability
  comes from the catalogue's own metadata, or from a probe that demonstrates
  the model can actually read one.
* It does not reuse one model's parameters for another. Each generation
  model's published schema decides the request that gets built.
* It does not claim a generation succeeded when the API failed, and it does
  not report an estimate as a bill.
* It does not shell out on its own. The Dev Agent writes files and runs
  tests through Hermes' own ``write_file`` and ``terminal`` tools, so
  Hermes' approval policy, sandboxing and audit trail apply to it as they
  apply to the agent.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # Hermes loads this file as a package (``hermes_plugins.<slug>``).
    from .nexus.config import read_api_key
    from .nexus.schemas import SCHEMAS
    from .nexus.tools import TOOLS, NexusToolBox
except ImportError:  # pragma: no cover - loaded as a plain module by tooling
    from nexus.config import read_api_key
    from nexus.schemas import SCHEMAS
    from nexus.tools import TOOLS, NexusToolBox

logger = logging.getLogger(__name__)

#: The toolset the NEXUS tools are grouped under.
TOOLSET = "gmi_nexus"

#: The environment variable holding the GMI Cloud API key. Read through
#: Hermes' secret scope, never from a file inside the plugin.
API_KEY_ENV = "GMI_API_KEY"


def _gmi_configured() -> bool:
    """True when a GMI Cloud credential is resolvable.

    Used as the tools' ``check_fn`` so the model is not offered a tool that
    cannot work. The credential itself is never read into a log or a
    response.
    """
    try:
        return bool(read_api_key())
    except Exception:
        return False


def _bind(box: NexusToolBox, handler: Any) -> Any:
    """Bind a handler to the plugin's engine instance."""

    def bound(args: dict[str, Any]) -> str:
        return handler(box, args or {})

    bound.__name__ = getattr(handler, "__name__", "nexus_tool")
    bound.__doc__ = getattr(handler, "__doc__", None)
    return bound


def register(ctx: Any) -> None:
    """Register the NEXUS tools and the ``/nexus`` command with Hermes."""
    box = NexusToolBox(ctx)

    for name, schema in SCHEMAS.items():
        handler = TOOLS.get(name)
        if handler is None:  # pragma: no cover - the two dicts are kept in step
            logger.error("NEXUS has a schema for %s but no handler", name)
            continue
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=_bind(box, handler),
            requires_env=[API_KEY_ENV],
            description=schema.get("description", ""),
            emoji="◈",
            # The report works from recorded history alone, so it stays
            # available even when no credential is configured.
            check_fn=None if name == "nexus_report" else _gmi_configured,
        )

    ctx.register_command(
        name="nexus",
        handler=_make_command(box),
        description="Show GMI NEXUS status: catalogue, budget and recent runs.",
        args_hint="[status|models|report]",
    )

    logger.info("GMI NEXUS registered %d tools", len(SCHEMAS))


def _make_command(box: NexusToolBox) -> Any:
    """Build the ``/nexus`` slash-command handler."""

    def command(raw_args: str) -> str:
        import json

        argument = (raw_args or "").strip().lower()
        try:
            if argument == "models":
                payload = box.nexus_models({})
            elif argument in ("", "status"):
                payload = box.nexus_report({"days": 7})
            else:
                payload = box.nexus_report({"days": 30})
        except Exception as exc:  # pragma: no cover - defensive
            return f"NEXUS could not produce a report: {exc}"
        try:
            return json.dumps(json.loads(payload), ensure_ascii=False, indent=2)[:6000]
        except (TypeError, ValueError):
            return payload[:6000]

    return command


__all__ = ["TOOLSET", "register"]
