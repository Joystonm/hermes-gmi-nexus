"""The model may ask for money. Only a person may approve it.

These tests answer the first two blockers raised on the Hermes catalogue pull
request:

1. *the model approves its own spend* — every spending tool used to take a
   model-set ``approve: true`` that skipped the approval threshold;
2. *a model with no published price is estimated at $0.00* — which passes
   every cap while the real price is unknown.

Every test here is offline: nothing reaches GMI Cloud, and nothing spends.
"""

from __future__ import annotations

import pytest

from nexus.approval import (
    APPROVAL_TOKEN_KEY,
    SPENDING_TOOLS,
    Estimate,
    SpendApprover,
    approver,
    estimate_call,
    fingerprint,
    make_hooks,
    rule_key_for,
)
from nexus.budget import BudgetGuard
from nexus.config import Settings
from nexus.errors import ValidationError
from nexus.media_catalog import MediaCatalog, MediaModelSpec
from nexus.media_router import MediaRouter
from nexus.models import Modality
from nexus.schemas import SCHEMAS
from nexus.tools import NexusToolBox

from conftest import FakeTransport


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def priced_engine(engine):
    """An engine whose catalogue has been read, so a call can be priced."""
    engine.registry.list_models()
    engine.media.catalog.get_spec("gpt-image-2")
    return engine


@pytest.fixture
def ledger():
    """A fresh approval ledger.

    The plugin keeps one ledger for the life of the session, and it remembers
    an estimate for a few seconds so the two hooks agree. A test that uses
    the plugin's own ledger would read another test's answer, so each test
    gets its own.
    """
    return SpendApprover()


@pytest.fixture
def hooks(priced_engine, ledger):
    """The two ``pre_tool_call`` callbacks, built the way the plugin builds them."""
    return make_hooks(ledger, lambda: (priced_engine.settings, priced_engine))


def _spend_args(goal: str = "write a report about the weather") -> dict:
    return {"goal": goal}


# --------------------------------------------------------------------------- #
# Blocker 1: the model cannot approve its own spend
# --------------------------------------------------------------------------- #


def test_no_tool_schema_offers_an_approve_argument():
    """There is no argument the model can set to unlock a spend."""
    for name, schema in SCHEMAS.items():
        properties = schema.get("properties") or {}
        assert "approve" not in properties, (
            f"{name} still lets the model approve its own spend"
        )


def test_the_plugin_registers_the_spend_hooks(plugin_module, ctx):
    """The plugin registers the callbacks that escalate a spend to a person."""
    plugin_module.register(ctx)

    assert len(ctx.hooks.get("pre_tool_call", [])) == 2


def test_a_call_over_the_threshold_is_held_for_a_person(hooks, priced_engine):
    """A call that reaches the threshold goes to Hermes' approval prompt."""
    priced_engine.settings.approval_threshold_usd = 0.0
    _, approve = hooks

    directive = approve(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    assert directive["action"] == "approve"
    assert "GMI Cloud" in directive["message"]
    # The human is told what they are authorising, not just that something
    # is about to happen.
    assert "$" in directive["message"]


def test_a_call_under_the_threshold_is_not_held(hooks, priced_engine):
    """A cheap call goes through without bothering a person."""
    priced_engine.settings.approval_threshold_usd = 10.0
    _, approve = hooks

    assert approve(tool_name="nexus_run", args=_spend_args()) is None


def test_each_tool_has_its_own_approval_rule(hooks, priced_engine):
    """Allowing one tool must not allow another.

    They spend different amounts and deserve different decisions, so the
    rule key Hermes remembers an "always allow" choice under is per tool.
    """
    priced_engine.settings.approval_threshold_usd = 0.0
    _, approve = hooks

    run = approve(tool_name="nexus_run", args=_spend_args())
    media = approve(tool_name="nexus_media", args={"modality": "image", "prompt": "a cat"})

    assert run["rule_key"] == "gmi-nexus-spend:nexus_run"
    assert media["rule_key"] == "gmi-nexus-spend:nexus_media"
    assert run["rule_key"] != media["rule_key"]


def test_a_tool_that_cannot_spend_is_never_held(hooks):
    """``nexus_models`` and ``nexus_report`` cost nothing, so they are not held."""
    _, approve = hooks

    assert approve(tool_name="nexus_models", args={}) is None
    assert approve(tool_name="nexus_report", args={}) is None


def test_a_call_whose_cost_cannot_be_worked_out_is_held(settings, ledger):
    """An unknown cost is never treated as a free one.

    The catalogue has not been read yet, so the cost is unknown — and an
    unknown cost is exactly what the human has to be asked about.
    """
    _, approve = make_hooks(ledger, lambda: (settings, None))

    directive = approve(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    assert directive["action"] == "approve"
    assert "cannot be worked out" in directive["message"]


def test_a_call_with_an_unpriced_model_is_held(settings, ledger):
    """A model whose price the catalogue does not publish is held.

    Priced at $0.00 it would pass every cap, which is the bug this answers.
    """
    from nexus.http import HttpClient
    from nexus.orchestrator import Nexus

    # A catalogue whose only model publishes no price at all.
    unpriced = {
        "object": "list",
        "data": [
            {
                "id": "acme/unpriced-model",
                "name": "Unpriced Model",
                "owned_by": "acme",
                "context_length": 8000,
                "discount_to_user": 0.0,
            }
        ],
    }
    transport = FakeTransport(catalogue=unpriced)
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    unpriced_engine = Nexus(settings, ctx=None)
    unpriced_engine.http.close()
    unpriced_engine.http = client
    unpriced_engine.registry.client = client
    unpriced_engine.registry.list_models()

    _, approve = make_hooks(ledger, lambda: (settings, unpriced_engine))
    directive = approve(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    assert directive["action"] == "approve"
    assert "no price" in directive["message"]


def test_the_hook_never_raises(settings, ledger):
    """A hook that raises would veto every spending call.

    Hermes treats a raising ``pre_tool_call`` callback as a block, so a bug
    in the estimate would stop the plugin from working at all. The hook
    holds the call instead.
    """

    def broken():
        raise RuntimeError("the settings could not be read")

    modify, approve = make_hooks(ledger, broken)

    directive = approve(tool_name="nexus_run", args=_spend_args())
    assert directive is not None
    assert directive["action"] == "approve"
    # The token is still issued, so a human who says yes is not then
    # refused by the tool for want of a token.
    assert modify(tool_name="nexus_run", args=_spend_args()) is not None


def test_the_hook_makes_no_network_call(hooks, priced_engine, transport):
    """The hook runs on a bounded worker: it may not block on the network."""
    priced_engine.settings.approval_threshold_usd = 0.0
    _, approve = hooks

    before = len(transport.requests)
    approve(tool_name="nexus_run", args=_spend_args())

    assert len(transport.requests) == before


def test_the_hook_does_not_build_the_engine(settings, ledger):
    """The hook reads the catalogue that exists; it never goes and gets one.

    Building the engine may read the catalogue over the network, and a hook
    that blocks on the network blocks the tool call behind it.
    """
    box = NexusToolBox(ctx=None)
    modify, approve = make_hooks(ledger, box.approval_context)

    approve(tool_name="nexus_run", args=_spend_args())
    modify(tool_name="nexus_run", args=_spend_args())

    assert box._engine is None


def test_the_hook_uses_the_engine_own_settings(priced_engine, ledger):
    """The threshold the hook applies is the threshold the guard applies.

    A hook that read a different configuration would hold calls the tool
    would have let through, and let through calls the tool would hold.
    """
    priced_engine.settings.approval_threshold_usd = 0.0
    box = NexusToolBox(ctx=None)
    box._engine = priced_engine
    _, approve = make_hooks(ledger, box.approval_context)

    directive = approve(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    assert directive["action"] == "approve"


# --------------------------------------------------------------------------- #
# The token: the model's way in, and the only way in
# --------------------------------------------------------------------------- #


def test_a_held_call_carries_a_token(hooks, priced_engine):
    """The modify directive carries the token the tool will ask for."""
    priced_engine.settings.approval_threshold_usd = 0.0
    modify, _ = hooks

    directive = modify(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    assert directive["action"] == "modify"
    token = directive["args"][APPROVAL_TOKEN_KEY]
    assert isinstance(token, str) and token


def test_a_call_that_needs_no_approval_carries_no_token(hooks, priced_engine):
    """No token is issued for a call the person is not being asked about."""
    priced_engine.settings.approval_threshold_usd = 10.0
    modify, _ = hooks

    assert modify(tool_name="nexus_run", args=_spend_args()) is None


def test_the_token_is_single_use():
    """A token is spent by the call it was issued for."""
    ledger = SpendApprover()
    args = {"goal": "write a report"}
    token = ledger.issue("nexus_run", args, estimated_usd=1.0, ceiling_usd=5.0)

    assert ledger.consume("nexus_run", {**args, APPROVAL_TOKEN_KEY: token}) is not None
    assert ledger.consume("nexus_run", {**args, APPROVAL_TOKEN_KEY: token}) is None


def test_the_token_is_bound_to_its_tool():
    """A token for one tool does not unlock another."""
    ledger = SpendApprover()
    args = {"goal": "write a report"}
    token = ledger.issue("nexus_run", args, estimated_usd=1.0, ceiling_usd=5.0)

    assert ledger.consume("nexus_media", {**args, APPROVAL_TOKEN_KEY: token}) is None


def test_the_token_is_bound_to_its_arguments():
    """A token issued for one call does not unlock a different one."""
    ledger = SpendApprover()
    token = ledger.issue(
        "nexus_run", {"goal": "write a report"}, estimated_usd=1.0, ceiling_usd=5.0
    )

    other = {"goal": "something else entirely"}
    assert ledger.consume("nexus_run", {**other, APPROVAL_TOKEN_KEY: token}) is None


def test_a_token_the_model_made_up_is_refused():
    """The model cannot mint a token.

    There is no argument for one, but a model that guessed a value would
    try. The ledger only knows tokens it issued itself.
    """
    ledger = SpendApprover()
    args = {"goal": "write a report"}

    assert ledger.consume("nexus_run", {**args, APPROVAL_TOKEN_KEY: "please-approve"}) is None


def test_the_token_expires():
    """An old token does not unlock a call made much later."""
    ledger = SpendApprover(ttl_seconds=-1.0)
    args = {"goal": "write a report"}
    token = ledger.issue("nexus_run", args, estimated_usd=1.0, ceiling_usd=5.0)

    assert ledger.consume("nexus_run", {**args, APPROVAL_TOKEN_KEY: token}) is None


def test_the_token_is_not_part_of_the_fingerprint():
    """Injecting the token does not change what the token is for.

    The fingerprint is taken over the arguments the model chose, so the
    token the hook adds does not invalidate itself.
    """
    args = {"goal": "write a report"}
    assert fingerprint(args) == fingerprint({**args, APPROVAL_TOKEN_KEY: "anything"})


def test_an_approve_argument_does_not_approve_anything():
    """The model's own flag buys nothing.

    This is the bug the maintainers found: the tool used to take
    ``approve: true`` from the model and skip the threshold with it.
    """
    from nexus.approval import approver as ledger

    args = {"goal": "write a report", "approve": True}

    assert NexusToolBox._approved("nexus_run", args) is False
    assert ledger().consume("nexus_run", args) is None


def test_the_tool_accepts_a_granted_token():
    """The end of the road: the tool accepts the approval a person gave."""
    ledger = approver()
    args = {"goal": "write a report"}
    token = ledger.issue("nexus_run", args, estimated_usd=1.0, ceiling_usd=5.0)

    approved = NexusToolBox._approved("nexus_run", {**args, APPROVAL_TOKEN_KEY: token})

    assert approved is True
    # And it is spent: a second call with the same token is not approved.
    assert NexusToolBox._approved("nexus_run", {**args, APPROVAL_TOKEN_KEY: token}) is False


def test_the_two_hooks_agree(hooks, priced_engine):
    """The token and the escalation describe the same call.

    If they disagreed, a person could say yes to a call the tool then
    refuses, or the tool could spend on a call nobody was asked about.
    """
    priced_engine.settings.approval_threshold_usd = 0.0
    modify, approve = hooks
    args = _spend_args()

    token_directive = modify(tool_name="nexus_run", args=args)
    approve_directive = approve(tool_name="nexus_run", args=args)

    assert token_directive is not None and approve_directive is not None
    assert token_directive["action"] == "modify"
    assert approve_directive["action"] == "approve"


# --------------------------------------------------------------------------- #
# The estimate itself
# --------------------------------------------------------------------------- #


def test_an_estimate_names_the_amount_and_the_limit(hooks, priced_engine):
    """The human is told what they are authorising."""
    priced_engine.settings.approval_threshold_usd = 0.0
    _, approve = hooks

    directive = approve(tool_name="nexus_run", args=_spend_args())

    assert directive is not None
    # The limit is the smaller of the per-run cap and what is left today.
    assert "$5.00" in directive["message"]


def test_an_unknown_amount_is_never_free():
    """``None`` is unknown. It is never ``0.00``."""
    estimate = Estimate(
        amount=None,
        bound=5.0,
        label="a run",
        threshold=10.0,
        ceiling=5.0,
        unknown_reason="the catalogue publishes no price",
    )

    assert estimate.known is False
    assert estimate.needs_approval is True
    assert "publishes no price" in estimate.message("nexus_run")


def test_a_known_amount_below_the_threshold_is_not_held():
    estimate = Estimate(
        amount=0.001,
        bound=5.0,
        label="a run",
        threshold=10.0,
        ceiling=5.0,
    )

    assert estimate.known is True
    assert estimate.needs_approval is False


def test_the_estimate_is_remembered_for_a_few_seconds(hooks, priced_engine):
    """The hook asks the same question twice; the answers must agree."""
    priced_engine.settings.approval_threshold_usd = 0.0
    _, approve = hooks

    first = approve(tool_name="nexus_run", args=_spend_args())
    second = approve(tool_name="nexus_run", args=_spend_args())

    assert first == second


def test_the_estimate_of_a_call_that_cannot_be_priced_is_unknown(settings):
    """``estimate_call`` with no engine returns an unknown amount."""
    estimate = estimate_call(settings, None, "nexus_run", _spend_args())

    assert estimate.amount is None
    assert estimate.needs_approval is True


def test_only_the_spending_tools_are_estimated(settings):
    """The other tools cost nothing and are never estimated."""
    estimate = estimate_call(settings, None, "nexus_models", {})

    assert estimate.amount is None
    assert "nexus_models" not in SPENDING_TOOLS


# --------------------------------------------------------------------------- #
# Blocker 2: a model with no published price is not a free model
# --------------------------------------------------------------------------- #


def _unpriced_spec() -> MediaModelSpec:
    """A model the catalogue describes but does not price."""
    return MediaModelSpec(
        id="acme/unpriced-image",
        display_name="Unpriced Image",
        modality=Modality.IMAGE,
        provider="acme",
        status="active",
        parameters=[],
        price_info={},
    )


def test_an_unpriced_model_cannot_be_estimated():
    """``estimate_cost`` refuses rather than returning $0.00."""
    spec = _unpriced_spec()

    assert spec.has_published_price() is False
    with pytest.raises(ValidationError) as raised:
        spec.estimate_cost(count=1)

    assert "no price" in str(raised.value)


def test_an_unpriced_model_reports_no_price_rather_than_zero():
    """The price is reported as unknown, not as $0.00."""
    spec = _unpriced_spec()

    payload = spec.to_dict()

    assert payload["unit_price_usd"] is None
    assert payload["price_published"] is False


def test_a_priced_model_still_reports_its_price():
    """The change did not break the ordinary case."""
    spec = MediaModelSpec(
        id="acme/priced-image",
        display_name="Priced Image",
        modality=Modality.IMAGE,
        provider="acme",
        status="active",
        parameters=[],
        price_info={"price": 42000, "unit": "image"},
    )

    payload = spec.to_dict()

    assert payload["price_published"] is True
    assert payload["unit_price_usd"] == pytest.approx(0.042)
    assert spec.estimate_cost(count=1) == pytest.approx(0.042)


def test_the_media_router_passes_over_an_unpriced_model(settings):
    """An unpriced model is not a cheap model; it is an unusable one."""
    from nexus.http import HttpClient

    spec_payload = {
        "model": "gpt-image-2",
        "display_name": "GPT Image 2",
        "model_type": "image",
        "external_provider": "openai",
        "status": "active",
        "parameters": [{"name": "prompt", "type": "string", "required": True}],
    }
    transport = FakeTransport(media_specs={"gpt-image-2": spec_payload})
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    catalog = MediaCatalog("https://mock.gmi/apikey", client)
    router = MediaRouter(catalog)

    best, considered = router.select(["gpt-image-2"], Modality.IMAGE, "a cat")

    assert best is None
    assert considered
    assert "no price" in considered[0].rejected
    assert considered[0].to_dict()["unit_price_usd"] is None


def test_the_media_client_refuses_to_submit_an_unpriced_request(settings):
    """The backstop for a caller that reaches the queue directly."""
    from nexus.http import HttpClient
    from nexus.providers.gmi_media import GmiMediaClient

    spec_payload = {
        "model": "gpt-image-2",
        "display_name": "GPT Image 2",
        "model_type": "image",
        "external_provider": "openai",
        "status": "active",
        "parameters": [{"name": "prompt", "type": "string", "required": True}],
    }
    transport = FakeTransport(media_specs={"gpt-image-2": spec_payload})
    client = HttpClient(transport=transport, max_retries=0, timeout=5.0)
    media = GmiMediaClient("https://mock.gmi/apikey", client)

    with pytest.raises(ValidationError) as raised:
        media.generate("gpt-image-2", "a cat")

    assert "no price" in str(raised.value)
    # Nothing was submitted, so nothing was billed.
    assert not [entry for entry in transport.requests if entry["url"].endswith("/requests")]


def test_the_budget_guard_holds_an_unknown_price():
    """The guard inside the tool applies the same rule as the hook."""
    guard = BudgetGuard(Settings(approval_threshold_usd=10.0, max_cost_per_run_usd=5.0))

    decision = guard.check(0.0, approved=False, description="a generation", price_known=False)

    assert decision.allowed is False
    assert decision.requires_approval is True
    assert "no price" in decision.reason


def test_a_zero_price_that_is_published_is_allowed():
    """A model the catalogue prices at zero is free, and that is a price."""
    guard = BudgetGuard(Settings(approval_threshold_usd=10.0, max_cost_per_run_usd=5.0))

    decision = guard.check(0.0, approved=False, description="a generation", price_known=True)

    assert decision.allowed is True
