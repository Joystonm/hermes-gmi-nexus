"""Task understanding: turn a request into a typed task.

Classification is deliberately *deterministic and local*. A cheap, explainable
rule pass decides the obvious cases (this is a coding task, this carries an
image, this asks for a picture). Only when the rules cannot decide does the
classifier fall back to a model, and when it does, the model is asked for a
strict JSON verdict that is validated before it is trusted.

The result always carries its ``evidence``: which rule fired, or which model
decided. A task type that came from a model guess is never presented the same
way as one that came from an explicit instruction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import TASK_LABELS, Capability, TaskType

#: Confidence assigned to each classification route.
CONFIDENCE_RULE = 0.9
CONFIDENCE_MODEL = 0.7
CONFIDENCE_FALLBACK = 0.35


@dataclass(slots=True)
class Classification:
    """The router's understanding of one request."""

    task_type: TaskType
    confidence: float
    evidence: list[str] = field(default_factory=list)
    required_capabilities: tuple[Capability, ...] = ()
    modalities: tuple[str, ...] = ()
    #: True when the classification came from a model rather than a rule.
    model_assisted: bool = False
    #: Set when the request itself names a model that should be used.
    requested_model: str | None = None
    media_count: int = 0
    text_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "task_label": TASK_LABELS.get(self.task_type, self.task_type.value),
            "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
            "required_capabilities": [c.value for c in self.required_capabilities],
            "modalities": list(self.modalities),
            "model_assisted": self.model_assisted,
            "requested_model": self.requested_model,
            "media_count": self.media_count,
            "text_length": self.text_length,
        }


# --------------------------------------------------------------------------- #
# Rule patterns. Each entry is (task type, regex, capability it implies).
# --------------------------------------------------------------------------- #

_RULES: tuple[tuple[TaskType, re.Pattern[str]], ...] = (
    (TaskType.IMAGE_GENERATION, re.compile(
        r"\b(generate|create|make|produce|draw|render)\b[^.?!]{0,40}\b"
        r"(image|picture|illustration|icon|logo|banner|poster|wallpaper|mockup|"
        r"hero\s*image|thumbnail|artwork|visual asset)\b", re.IGNORECASE)),
    (TaskType.IMAGE_EDITING, re.compile(
        r"\b(edit|modify|change|restyle|recolour|recolor|retouch|upscale|"
        r"remove|replace|clean\s*up)\b[^.?!]{0,40}\b(image|picture|photo|"
        r"screenshot|render|artwork)\b", re.IGNORECASE)),
    (TaskType.IMAGE_UNDERSTANDING, re.compile(
        r"\b(describe|analyse|analyze|explain|read|what(?:'s| is)|identify|"
        r"caption|ocr|summarise|summarize)\b[^.?!]{0,40}\b(image|picture|photo|"
        r"screenshot|diagram|chart|ui|interface|design|logo)\b", re.IGNORECASE)),
    (TaskType.DOCUMENT_ANALYSIS, re.compile(
        r"\b(document|pdf|contract|invoice|receipt|resume|cv|report|spreadsheet|"
        r"form|letter|slides?|deck)\b[^.?!]{0,30}\b(summarise|summarize|analyse|"
        r"analyze|extract|review|read|compare)\b", re.IGNORECASE)),
    (TaskType.DEBUGGING, re.compile(
        r"\b(debug|fix|bug|error|exception|traceback|stack\s*trace|failing|"
        r"broken|crash|regression|fails?)\b", re.IGNORECASE)),
    (TaskType.CODE_REVIEW, re.compile(
        r"\b(code\s*review|review\s+(this|the)\s+(code|diff|pr|pull request)|"
        r"audit\s+(the\s+)?code|security\s+review)\b", re.IGNORECASE)),
    (TaskType.CODING, re.compile(
        r"\b(write|implement|create|build|refactor|rewrite|add|port|scaffold|"
        r"develop|code|function|class|module|script|endpoint|component|"
        r"test\s+case|unit\s+test)\b", re.IGNORECASE)),
    (TaskType.PLANNING, re.compile(
        r"\b(plan|roadmap|break\s*(it\s*)?down|decompose|steps?\s+to|"
        r"strategy|architecture\s+for|design\s+(a|the))\b", re.IGNORECASE)),
    (TaskType.COMPLEX_REASONING, re.compile(
        r"\b(prove|derive|optimi[sz]e|algorithm|mathemat\w+|theorem|proof|"
        r"analyse\s+trade-?offs?|step[-\s]by[-\s]step)\b", re.IGNORECASE)),
    (TaskType.VIDEO, re.compile(
        r"\b(video|animation|animate|clip|motion|footage)\b", re.IGNORECASE)),
    (TaskType.AUDIO, re.compile(
        r"\b(audio|voiceover|voice\s*over|speech|narration|music|soundtrack|"
        r"podcast|audiobook)\b", re.IGNORECASE)),
)

#: Requests that name a model explicitly.
_MODEL_REQUEST = re.compile(
    r"\b(?:use|with|via|using|route\s+to|switch\s+to|ask)\s+"
    r"(?:the\s+)?(?P<model>[A-Za-z0-9][A-Za-z0-9._/-]{2,60})\b",
    re.IGNORECASE,
)

#: Image MIME types recognised as visual input.
_IMAGE_MIME_PREFIXES = ("image/",)

#: Video/audio MIME types recognised as non-visual input.
_AUDIO_MIME_PREFIXES = ("audio/",)
_VIDEO_MIME_PREFIXES = ("video/",)

#: Words that follow "use"/"with" often enough that a model id must clear a
#: bar before it is believed: a provider-qualified id, or a name carrying a
#: digit or a version marker.
_MODEL_ID_HINT = re.compile(r"[0-9]|[._]|[A-Z]")


def _requested_model(text: str) -> str | None:
    """Return a model id the request explicitly asks for, if any.

    "Use gpt-4o-mini" and "route to deepseek-ai/DeepSeek-V4-Pro" both count;
    "use Python to write it" does not, because a bare word with no version
    marker is far more likely to be prose than a model id.
    """
    for match in _MODEL_REQUEST.finditer(text):
        candidate = match.group("model").strip().rstrip(".,;:!?")
        if "/" in candidate:
            return candidate
        if len(candidate) > 3 and _MODEL_ID_HINT.search(candidate):
            return candidate
    return None


def classify_text(text: str, *, media_mimes: tuple[str, ...] = ()) -> Classification:
    """Classify a request using only local rules.

    ``media_mimes`` lists the MIME types of any attached media.
    """
    text = text or ""
    lowered = text.lower()
    evidence: list[str] = []
    modalities: list[str] = []

    has_image = any(m.lower().startswith(_IMAGE_MIME_PREFIXES) for m in media_mimes)
    has_audio = any(m.lower().startswith(_AUDIO_MIME_PREFIXES) for m in media_mimes)
    has_video = any(m.lower().startswith(_VIDEO_MIME_PREFIXES) for m in media_mimes)

    if has_image:
        modalities.append("image")
    if has_audio:
        modalities.append("audio")
    if has_video:
        modalities.append("video")

    requested_model = _requested_model(text)

    # Explicit "generate an image" style instructions win over the generic
    # "create" pattern, because a request to "create a landing page" is coding,
    # not image generation.
    task_type: TaskType | None = None
    for candidate, pattern in _RULES:
        match = pattern.search(text)
        if match:
            task_type = candidate
            evidence.append(f"rule:{candidate.value}:matched '{match.group(0)[:60]}'")
            break

    # Media attachment overrides a generic text rule when the attachment
    # contradicts it — but not when the rule is about producing media.
    if has_image and task_type not in (
        TaskType.IMAGE_GENERATION,
        TaskType.IMAGE_EDITING,
    ):
        task_type = TaskType.IMAGE_UNDERSTANDING
        evidence.append("rule:attachment:image")
    if has_video and task_type is None:
        task_type = TaskType.VIDEO
        evidence.append("rule:attachment:video")
    if has_audio and task_type is None:
        task_type = TaskType.AUDIO
        evidence.append("rule:attachment:audio")

    if task_type is None:
        if has_image:
            task_type = TaskType.MULTIMODAL
            evidence.append("rule:attachment:image:fallback")
        elif any(word in lowered for word in ("think", "reason", "why", "explain", "compare")):
            task_type = TaskType.GENERAL_REASONING
            evidence.append("rule:keyword:general")
        else:
            task_type = TaskType.GENERAL_REASONING
            evidence.append("rule:default:general")

    confidence = CONFIDENCE_RULE if evidence and not evidence[0].startswith("rule:default") else CONFIDENCE_FALLBACK

    return Classification(
        task_type=task_type,
        confidence=confidence,
        evidence=evidence,
        required_capabilities=capabilities_for(task_type),
        modalities=tuple(modalities),
        requested_model=requested_model,
        media_count=len(media_mimes),
        text_length=len(text),
    )


def capabilities_for(task_type: TaskType) -> tuple[Capability, ...]:
    """Return the capabilities a task type requires."""
    from .models import TASK_CAPABILITIES

    return TASK_CAPABILITIES.get(task_type, (Capability.TEXT,))


def classify(
    text: str,
    *,
    media_mimes: tuple[str, ...] = (),
    classifier_llm: Any | None = None,
) -> Classification:
    """Classify a request, optionally consulting a model when rules are unsure.

    ``classifier_llm`` is a callable taking a prompt and returning the model's
    text. It is only used when the rules produced a low-confidence result, so
    the common path costs nothing.
    """
    result = classify_text(text, media_mimes=media_mimes)
    result.required_capabilities = capabilities_for(result.task_type)

    if classifier_llm is None or result.confidence >= CONFIDENCE_RULE:
        return result

    try:
        verdict = _classify_with_model(text, media_mimes, classifier_llm)
    except Exception:
        result.evidence.append("model-classifier:failed")
        return result

    if verdict is None:
        result.evidence.append("model-classifier:unusable")
        return result

    result.task_type = verdict.task_type
    result.required_capabilities = capabilities_for(verdict.task_type)
    result.model_assisted = True
    result.evidence.append(f"model-classifier:{verdict.task_type.value}")
    result.confidence = max(result.confidence, CONFIDENCE_MODEL)
    return result


def _classify_with_model(
    text: str, media_mimes: tuple[str, ...], classifier_llm: Any
) -> Classification | None:
    """Ask a model to classify the request, and validate its answer."""
    prompt = (
        "Classify the following request into exactly one category. "
        "Reply with JSON only: {\"task_type\": \"...\", \"reason\": \"...\"}\n\n"
        "Categories: " + ", ".join(t.value for t in TaskType) + "\n\n"
        f"Attached media types: {list(media_mimes) or 'none'}\n\n"
        f"Request:\n{text[:4000]}"
    )
    raw = classifier_llm(prompt)
    if not raw:
        return None
    payload = _extract_json(raw)
    if not payload:
        return None
    name = str(payload.get("task_type") or "").strip().lower()
    try:
        task_type = TaskType(name)
    except ValueError:
        return None
    evidence = [f"model-classifier:{task_type.value}"]
    reason = payload.get("reason")
    if reason:
        evidence.append(f"model-classifier:reason:{str(reason)[:160]}")
    return Classification(
        task_type=task_type,
        confidence=CONFIDENCE_MODEL,
        evidence=evidence,
        media_count=len(media_mimes),
        text_length=len(text),
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "CONFIDENCE_FALLBACK",
    "CONFIDENCE_MODEL",
    "CONFIDENCE_RULE",
    "Classification",
    "capabilities_for",
    "classify",
    "classify_text",
]
