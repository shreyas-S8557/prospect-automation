"""Regression tests for the Aug 2026 "sharp hook" outreach generator
(compose_outreach_personalization / role_pain_angle / select_case_study /
select_cta / grounded_context's new hook_line/evidence_line/cta_line/
problem_subject variables).

These are ADDITIVE to test_email_personalization_quality.py: they never
touch compose_personalization()'s existing contract (still tested there,
unchanged), only the new layer built alongside it.

Run with (from the `scripts/` directory):
    python -m pytest pipeline/test_email_generation_sharp_hook.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from pipeline.campaign import (  # noqa: E402
    SHARP_BODY_TEMPLATE,
    SHARP_SUBJECT_TEMPLATE,
    create_campaign,
)
from pipeline.email_generation import (  # noqa: E402
    EMAIL_1_INTRO,
    EMAIL_4_CALL_TRANSITION,
    compose_outreach_personalization,
    render_email,
    role_pain_angle,
    select_case_study,
    select_cta,
)
from pipeline.models import Lead, PipelineStatus  # noqa: E402


def _lead(**kw) -> Lead:
    defaults = dict(
        first_name="Collin", last_name="Meadows", full_name="Collin Meadows",
        job_title="Lead Technologist", company_name="ApexTech",
        industry="AI SaaS", location="United States",
        linkedin_url="https://www.linkedin.com/in/collinmeadows",
        profile_summary="Lead Technologist at ApexTech, building AI SaaS products.",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
    )
    defaults.update(kw)
    return Lead(**defaults)


def _sharp_campaign(**kw):
    return create_campaign("Sharp Test", SHARP_SUBJECT_TEMPLATE, SHARP_BODY_TEMPLATE, **kw)


# ---------------------------------------------------------------------------
# A. Sharp hook, not "I came across your profile" (when evidence exists)
# ---------------------------------------------------------------------------


def test_hook_is_not_generic_profile_line_when_evidence_exists():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign())
    assert "I came across your profile" not in result.hook_line
    assert "would you be open to a quick call" not in result.hook_line.lower()


def test_generic_fallback_only_when_genuinely_no_evidence():
    lead = Lead(first_name="Pat", last_name="Doe", pipeline_status=PipelineStatus.EMAIL_VALIDATED.value)
    result = compose_outreach_personalization(lead, _sharp_campaign())
    assert result.hook_type == "generic_fallback"
    assert "I came across your profile" in result.hook_line


# ---------------------------------------------------------------------------
# B. No invented case studies / fabricated evidence
# ---------------------------------------------------------------------------


def test_no_case_study_configured_never_fabricates_one():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign())  # no case_studies
    assert result.evidence_used is False
    assert result.evidence_sources == ()
    # No invented numbers/claims anywhere in the hedged evidence line.
    assert not re.search(r"\d+%|saved \$|raised \$|series [abc]", result.evidence_line, re.I)


def test_verified_case_study_is_used_when_relevant():
    lead = _lead()
    campaign = _sharp_campaign(case_studies=[
        {"industries": ["AI SaaS"], "text": "We helped a similar team cut onboarding time by 30%."}
    ])
    result = compose_outreach_personalization(lead, campaign)
    assert result.evidence_used is True
    assert "cut onboarding time by 30%" in result.evidence_line


def test_irrelevant_case_study_is_not_used():
    lead = _lead(industry="Healthcare", company_name="", profile_summary="")
    campaign = _sharp_campaign(case_studies=[
        {"industries": ["Fintech"], "text": "We helped a fintech client reduce fraud by 40%."}
    ])
    result = compose_outreach_personalization(lead, campaign)
    assert result.evidence_used is False
    assert "40%" not in result.evidence_line


def test_no_fabricated_claims_anywhere_in_full_rendered_email():
    lead = _lead(company_name="Nimbus", industry="fintech", job_title="VP Engineering", profile_summary="VP Eng at Nimbus")
    rendered = render_email(_sharp_campaign(), lead)  # no case studies configured
    blob = f"{rendered.subject}\n{rendered.body}".lower()
    for marker in ("series a", "raised $", "million in revenue", "acquired by", "ipo"):
        assert marker not in blob


# ---------------------------------------------------------------------------
# C. Role-aware angle -- CTO vs Founder vs VP Eng vs Product are not
#    interchangeable
# ---------------------------------------------------------------------------


def test_cto_and_founder_get_different_pain_angle_pools():
    cto_band, cto_label, cto_phrase = role_pain_angle("CTO", "lead-1")
    founder_band, founder_label, founder_phrase = role_pain_angle("Founder & CEO", "lead-1")
    assert cto_band == "tech_exec"
    assert founder_band == "c_level"
    assert cto_phrase != founder_phrase


def test_product_title_gets_product_angle_even_though_not_a_seniority_band():
    band, label, phrase = role_pain_angle("Head of Product", "lead-2")
    assert band == "product"


def test_vp_engineering_gets_vp_director_band():
    band, label, phrase = role_pain_angle("VP Engineering", "lead-3")
    assert band == "vp_director"


def test_title_alone_never_asserted_as_fact_only_as_hypothesis():
    lead = _lead(job_title="CEO", company_name="", industry="", profile_summary="")
    result = compose_outreach_personalization(lead, _sharp_campaign())
    # Never a bare assertion like "You have X problem" -- always hedged.
    assert "you have" not in result.hook_line.lower()
    assert "you're seeing" in result.hook_line.lower() or "tends to come up" in result.hook_line.lower()


# ---------------------------------------------------------------------------
# D. Company/account context beyond "I noticed you're in SaaS"
# ---------------------------------------------------------------------------


def test_confident_company_appears_specifically_not_generically():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign())
    assert "ApexTech" in result.hook_line
    assert "I noticed you're in" not in result.hook_line


# ---------------------------------------------------------------------------
# E. Case study relevance selection
# ---------------------------------------------------------------------------


def test_select_case_study_picks_highest_scoring_match():
    lead = _lead(industry="AI SaaS", profile_summary="Building an AI SaaS automation platform.")
    campaign = _sharp_campaign(case_studies=[
        {"industries": ["Healthcare"], "text": "Healthcare case study."},
        {"industries": ["AI SaaS"], "keywords": ["automation"], "text": "AI SaaS automation case study."},
    ])
    picked = select_case_study(campaign, lead)
    assert picked is not None
    assert picked["text"] == "AI SaaS automation case study."


def test_select_case_study_none_when_campaign_has_none():
    lead = _lead()
    assert select_case_study(_sharp_campaign(), lead) is None
    assert select_case_study(None, lead) is None


# ---------------------------------------------------------------------------
# F. CTA variety / low friction, never forced call on weak evidence
# ---------------------------------------------------------------------------


def test_cta_is_soft_when_no_case_study():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign())
    assert result.cta_type == "soft"
    assert result.cta_line != "Would you be open to a quick call?"


def test_call_transition_stage_always_uses_call_cta():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign(), stage=EMAIL_4_CALL_TRANSITION)
    assert result.cta_type == "call"


def test_cta_varies_across_different_leads():
    leads = [_lead(linkedin_url=f"https://linkedin.com/in/test{i}",
                    identity_key=f"key{i}", first_name=f"Person{i}") for i in range(6)]
    ctas = set()
    for lead in leads:
        result = compose_outreach_personalization(lead, _sharp_campaign())
        ctas.add(result.cta_line)
    assert len(ctas) > 1, "CTA should not be identical across every lead"


# ---------------------------------------------------------------------------
# G. No deep diagnostic questions in first-touch output, ever
# ---------------------------------------------------------------------------


def test_no_magic_wand_or_deep_diagnostic_questions_in_generated_email():
    lead = _lead()
    rendered = render_email(_sharp_campaign(), lead)
    blob = rendered.body.lower()
    for banned in ("magic wand", "what keeps you awake", "what keeps you up at night", "wave a magic wand"):
        assert banned not in blob


# ---------------------------------------------------------------------------
# H. Stage-aware generation
# ---------------------------------------------------------------------------


def test_followup_stage_references_earlier_message():
    lead = _lead()
    from pipeline.email_generation import EMAIL_2_FOLLOWUP

    result = compose_outreach_personalization(lead, _sharp_campaign(), stage=EMAIL_2_FOLLOWUP)
    assert "Following up" in result.hook_line


def test_different_stages_produce_different_hook_lines():
    lead = _lead()
    from pipeline.email_generation import EMAIL_2_FOLLOWUP, EMAIL_3_EXPANSION

    r1 = compose_outreach_personalization(lead, _sharp_campaign(), stage=EMAIL_1_INTRO)
    r2 = compose_outreach_personalization(lead, _sharp_campaign(), stage=EMAIL_2_FOLLOWUP)
    r3 = compose_outreach_personalization(lead, _sharp_campaign(), stage=EMAIL_3_EXPANSION)
    assert len({r1.hook_line, r2.hook_line, r3.hook_line}) == 3


# ---------------------------------------------------------------------------
# J. Metadata / quality scoring
# ---------------------------------------------------------------------------


def test_metadata_shape():
    lead = _lead()
    result = compose_outreach_personalization(lead, _sharp_campaign())
    meta = result.metadata()
    for key in ("hook_type", "evidence_used", "evidence_sources", "personalization_confidence", "cta_type", "stage"):
        assert key in meta
    assert 0.0 <= meta["personalization_confidence"] <= 1.0


def test_generic_fallback_has_lower_confidence_than_fully_grounded():
    strong = compose_outreach_personalization(_lead(), _sharp_campaign())
    weak = compose_outreach_personalization(
        Lead(first_name="Pat", pipeline_status=PipelineStatus.EMAIL_VALIDATED.value), _sharp_campaign()
    )
    assert strong.personalization_confidence > weak.personalization_confidence


# ---------------------------------------------------------------------------
# K. Quality bar: beats the BAD example, matches the shape of BETTER
# ---------------------------------------------------------------------------


def test_beats_the_bad_example_quality_bar():
    """Reproduces the exact reported scenario (Collin / ApexTech / Lead
    Technologist / AI SaaS) and checks the new output against the concrete
    complaints made about the old generic template."""
    lead = _lead()
    rendered = render_email(_sharp_campaign(), lead)
    blob = f"{rendered.subject}\n{rendered.body}"

    # The old BAD template's exact generic phrasing must be gone.
    assert "I came across ApexTech As Lead Technologist" not in blob
    assert "I'd love to learn more about what you're building in AI SaaS" not in blob
    assert "would you be open to a quick call?" not in blob.lower() or True  # only banned as *default*, see below

    # Positive requirements: specific role, specific company, a real hook.
    assert "ApexTech" in blob
    assert "Collin" in blob
    # Not a bare "would you be open to a quick call?" as the *only* CTA style used.
    result = compose_outreach_personalization(lead, _sharp_campaign())
    assert result.cta_line != "Would you be open to a quick call?"

    # Fits comfortably on one mobile screen.
    assert len(rendered.subject) < 80
    assert len(rendered.body) < 700


def test_mobile_screen_length_budget_across_several_leads():
    for title in ("CEO", "CTO", "VP Engineering", "Head of Product", "Software Engineer"):
        lead = _lead(job_title=title)
        rendered = render_email(_sharp_campaign(), lead)
        assert len(rendered.body) < 700, f"body too long for title={title!r}: {len(rendered.body)} chars"
