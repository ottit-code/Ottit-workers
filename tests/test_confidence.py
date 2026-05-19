"""Tests for lib/confidence.py — rule gates and composite scoring."""
from lib import confidence
from models.drafts import ClaudeDraft, VoiceExample


def _ok_body() -> str:
    return (
        "Hi James,\n\n"
        "Appreciate you sharing that — month-end close drag is exactly the kind of "
        "operational pain we tend to help with. We focus on tightening the pipeline "
        "between AP, AR, and reporting so close drops from a multi-day grind to a "
        "one-day sweep. Worth a 15-minute call to see if the fit is right? "
        "Here's my calendar: https://cal.com/saman/intro\n\n"
        "Best,\nSaman"
    )


def test_rule_gates_pass_on_clean_body():
    assert confidence.evaluate_rule_gates(_ok_body()) == []


def test_rule_gates_flag_pricing_leak():
    body = _ok_body().replace("a 15-minute call", "we charge $5000 for the pilot")
    failed = confidence.evaluate_rule_gates(body)
    assert "pricing_leak" in failed


def test_rule_gates_flag_banned_phrase():
    body = _ok_body().replace("Appreciate you", "I hope this finds you well — appreciate you")
    failed = confidence.evaluate_rule_gates(body)
    assert any("banned_phrase" in f for f in failed)


def test_rule_gates_flag_missing_greeting():
    body = _ok_body().replace("Hi James,", "Hey there,")
    failed = confidence.evaluate_rule_gates(body)
    assert "missing_greeting" in failed


def test_rule_gates_flag_missing_closer():
    # Replace the whole sign-off block so neither a rotator (Cheers/Thanks/Best)
    # nor the name "Saman" appears in the tail. The skill lists Cheers/Thanks/Best
    # as valid close rotators, so any of those alone is enough to pass.
    body = _ok_body().replace("Best,\nSaman", "Sent from my phone")
    failed = confidence.evaluate_rule_gates(body)
    assert "missing_closer" in failed


def test_rule_gates_pass_on_html_wrapped_body():
    """The skill instructs Claude to wrap paragraphs in <p>...</p>; rule gates
    must look at the plain text so HTML doesn't hide the greeting/closer."""
    html_body = (
        "<p>Hi James,</p>\n\n"
        "<p>Appreciate you sharing that. Month-end close drag is exactly the kind of "
        "operational pain we tend to help with. Worth a 15-minute call to see if the "
        "fit is right? Here is my calendar: https://cal.com/saman/intro</p>\n\n"
        "<p>Cheers,<br>Saman Izadiyar<br>Account Executive @ Ottit</p>"
    )
    failed = confidence.evaluate_rule_gates(html_body)
    assert failed == [], f"expected clean pass, got: {failed}"


def test_rule_gates_accept_any_skill_approved_close_rotator():
    """Cheers / Thanks / Best / Talk soon all explicitly approved by the skill."""
    base = _ok_body()
    for rotator in ("Cheers", "Thanks", "Best", "Talk soon"):
        body = base.replace("Best,\nSaman", f"{rotator},\nSaman")
        failed = confidence.evaluate_rule_gates(body)
        assert "missing_closer" not in failed, (
            f"{rotator} should be accepted as a closer; got failures: {failed}"
        )


def test_rule_gates_flag_emdash_without_spaces():
    body = _ok_body().replace(" — ", "—")
    failed = confidence.evaluate_rule_gates(body)
    assert "emdash_without_spaces" in failed


def test_composite_zero_when_any_gate_fails():
    draft = ClaudeDraft(subject="Re: ...", body="bad", confidence=0.9)
    composite, components = confidence.composite_score(
        draft=draft,
        failed_gates=["body_too_short (1 words)"],
        ensemble_agreement=0.9,
        rag_quality=0.9,
    )
    assert composite == 0.0
    assert components.composite == 0.0
    assert components.rule_gate_pass is False


def test_composite_combines_components_when_gates_pass():
    draft = ClaudeDraft(subject="Re: x", body=_ok_body(), confidence=0.80)
    composite, components = confidence.composite_score(
        draft=draft,
        failed_gates=[],
        ensemble_agreement=0.60,
        rag_quality=0.40,
    )
    # 0.30*0.80 + 0.30*0.60 + 0.25*0.40 + 0.15 = 0.24 + 0.18 + 0.10 + 0.15 = 0.67
    assert composite == 0.67
    assert components.rule_gate_pass is True
    assert components.composite == 0.67


def test_rag_retrieval_quality_clamps_and_averages():
    examples = [
        VoiceExample(id=1, content="x", similarity=0.8),
        VoiceExample(id=2, content="y", similarity=-0.2),  # clamped to 0
        VoiceExample(id=3, content="z", similarity=0.6),
    ]
    quality = confidence.rag_retrieval_quality(examples)
    assert round(quality, 3) == round((0.8 + 0.0 + 0.6) / 3, 3)
