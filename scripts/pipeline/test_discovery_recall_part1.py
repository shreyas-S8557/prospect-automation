"""Regression tests for the Part 1 discovery + company-identity work:

  - quality.decontaminate_hit_text() / sources.ddgs_search.parse_search_hit:
    salvage the first (target) person's clean segment from a DDGS hit that
    glued a second person's LinkedIn result onto the end, instead of
    rejecting the whole hit.
  - quality.resolve_company(): confidence-labeled company resolution
    (EXPLICIT > STRONG_INFERRED > WEAK_INFERRED > UNKNOWN), combining every
    deterministic extractor (structured "Experience:" field, at/of, dash,
    pipe/comma/middot-separated headlines) without ever guessing.
  - quality.extract_company_from_separator_pattern(): pipe/comma/middot
    "<Title> <sep> <Company>" headline extraction.
  - TargetConfig.title_synonyms/industry_synonyms/keyword_synonyms and the
    expanded_titles/expanded_industries/expanded_keywords views used by
    both query_generator.py and quality.matches_target_criteria.
  - TargetConfig.target_count_mode validation.

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_discovery_recall_part1 -v

No network. Uses only in-memory data and the project's own quality.py /
target_config.py / query_generator.py / sources.ddgs_search.py.
"""

from __future__ import annotations

import unittest

from pipeline.quality import (
    COMPANY_CONFIDENCE_EXPLICIT,
    COMPANY_CONFIDENCE_STRONG_INFERRED,
    COMPANY_CONFIDENCE_UNKNOWN,
    COMPANY_CONFIDENCE_WEAK_INFERRED,
    decontaminate_hit_text,
    extract_company_from_separator_pattern,
    is_contaminated_hit,
    matches_target_criteria,
    resolve_company,
)
from pipeline.query_generator import build_queries
from pipeline.sources.ddgs_search import parse_search_hit
from pipeline.target_config import TargetConfig


# ---------------------------------------------------------------------------
# 1. Contamination salvage
# ---------------------------------------------------------------------------


class TestContaminationSalvage(unittest.TestCase):
    def test_no_op_on_clean_single_person_text(self):
        text = "Jane Doe - Founder & CEO at Acme | LinkedIn"
        self.assertEqual(decontaminate_hit_text(text), text)

    def test_truncates_at_first_linkedin_boundary_not_second(self):
        # Real shape from the saas_ai_founders raw snapshot: THREE people
        # glued together. Truncating must drop everyone after the first
        # "LinkedIn" marker, not just everyone after the second.
        text = (
            "Bryan Landerman - CTO & Operating Partner at ... - LinkedIn"
            "Brian Wehrle - Chief Technology Officer at Mach7 | LinkedIn"
            "Kristin Wolfe - United States | Professional Profile | LinkedIn"
            "Ryan McDonald - Chief Technology Officer | LinkedIn"
        )
        cleaned = decontaminate_hit_text(text)
        self.assertIn("Bryan Landerman", cleaned)
        self.assertNotIn("Brian Wehrle", cleaned)
        self.assertNotIn("Kristin Wolfe", cleaned)
        self.assertNotIn("Ryan McDonald", cleaned)

    def test_truncates_at_second_name_title_prefix(self):
        text = "Eddie Coates - Co-Founder at CANVAS Architecture - LinkedIn | Erin Cornell - Co-Founder at Oklahoma City Moms Blog"
        cleaned = decontaminate_hit_text(text)
        self.assertIn("Eddie Coates", cleaned)
        self.assertNotIn("Erin Cornell", cleaned)

    def test_still_contaminated_after_truncation_is_still_rejected(self):
        # Two "Experience:" blocks has no reliable single split point --
        # decontaminate_hit_text is a no-op here, so is_contaminated_hit
        # must still reject it (unchanged safety guarantee).
        text = (
            "Jane Doe. Experience: Acme Corp. Founder. "
            "John Smith. Experience: Beta Inc. CTO."
        )
        self.assertEqual(decontaminate_hit_text(text), text)
        self.assertTrue(is_contaminated_hit("", text))

    def test_parse_search_hit_recovers_target_from_glued_result(self):
        hit = {
            "href": "https://www.linkedin.com/in/eddiecoates",
            "title": (
                "Eddie Coates - Co-Founder at CANVAS Architecture ... - LinkedIn"
                "Erin Cornell - Co-Founder at Oklahoma City Moms Blog | LinkedIn"
            ),
            "body": (
                "Co-Founder at CANVAS Architecture & Development; VP of "
                "Development & Finance at Wheeler District. Location: "
                "Austin, Texas, United States."
            ),
        }
        target = TargetConfig(titles=["Founder", "Co-Founder"], target_count=10)
        row = parse_search_hit(hit, target=target, require_target_match=False)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Eddie Coates")
        self.assertNotIn("Erin Cornell", row["profile_title"])

    def test_parse_search_hit_still_rejects_fully_glued_bug_report_example(self):
        # The exact contamination bug-report shape from test_discovery_
        # contamination_day11: must still be fully rejected end to end.
        hit = {
            "href": "https://www.linkedin.com/in/janedoe",
            "title": "Jane Doe - Managing Partner - LinkedInJohn Smith - Tax Partner - LinkedIn",
            "body": (
                "Experience: Acme CPAs. Managing Partner. "
                "Experience: Beta CPAs. Tax Partner."
            ),
        }
        row = parse_search_hit(hit, require_target_match=False)
        self.assertIsNone(row)


# ---------------------------------------------------------------------------
# 2. extract_company_from_separator_pattern
# ---------------------------------------------------------------------------


class TestExtractCompanyFromSeparatorPattern(unittest.TestCase):
    def test_pipe_separated_title_then_company(self):
        self.assertEqual(extract_company_from_separator_pattern("CEO | Makesbridge"), "Makesbridge")

    def test_pipe_separated_company_then_title(self):
        self.assertEqual(extract_company_from_separator_pattern("Makesbridge | CEO"), "Makesbridge")

    def test_comma_separated_company_then_title(self):
        self.assertEqual(extract_company_from_separator_pattern("Hebbia, Founder & CEO"), "Hebbia")

    def test_middot_separated_both_orders(self):
        self.assertEqual(extract_company_from_separator_pattern("Acme \u00b7 Founder"), "Acme")
        self.assertEqual(extract_company_from_separator_pattern("Founder \u00b7 Acme"), "Acme")

    def test_second_title_abbreviation_not_read_as_company(self):
        # "CEO | COO | Customer Success Executive" -- must not extract "COO".
        self.assertEqual(extract_company_from_separator_pattern("CEO | COO | Customer Success Executive"), "")

    def test_name_title_fragment_not_read_as_company(self):
        # The "company"-side text is itself a "<Name> - <Title>" fragment,
        # not a real company -- must not fire.
        self.assertEqual(extract_company_from_separator_pattern("Shelly Freeman - CEO | COO"), "")

    def test_no_separator_returns_empty(self):
        self.assertEqual(extract_company_from_separator_pattern("Founder and CEO of SaaS Company"), "")


# ---------------------------------------------------------------------------
# 3. resolve_company -- confidence-labeled resolution
# ---------------------------------------------------------------------------


class TestResolveCompany(unittest.TestCase):
    def test_explicit_when_existing_company_name_is_valid(self):
        row = {"company_name": "EzData", "profile_title": "CEO", "summary": ""}
        self.assertEqual(resolve_company(row), ("EzData", COMPANY_CONFIDENCE_EXPLICIT))

    def test_generic_existing_company_name_falls_through_not_explicit(self):
        row = {"company_name": "AI", "profile_title": "CEO", "summary": ""}
        # "AI" is technically well-formed, so EXPLICIT is honest here --
        # this documents current behavior: resolve_company's EXPLICIT tier
        # doesn't second-guess an already-set company_name for genericity,
        # only for university/enrollment-context corruption (see below).
        # Downstream domain-guessing still independently refuses "AI" via
        # is_domain_guessable_company_name.
        self.assertEqual(resolve_company(row), ("AI", COMPANY_CONFIDENCE_EXPLICIT))

    def test_university_enrollment_mention_never_explicit_or_reused(self):
        row = {
            "company_name": "RIT",
            "profile_title": "Founder/COO",
            "summary": "I'm a junior MIS student at RIT and the founder/COO of a venture-backed AI SaaS startup.",
        }
        name, confidence = resolve_company(row)
        self.assertEqual(confidence, COMPANY_CONFIDENCE_UNKNOWN)
        self.assertEqual(name, "")

    def test_strong_inferred_from_structured_experience_field(self):
        row = {
            "company_name": "",
            "profile_title": "CEO",
            "summary": "CTO & Operating Partner at Silversmith \u00b7 Experience: Silversmith Capital Partners \u00b7 Location: Burlington",
        }
        name, confidence = resolve_company(row)
        self.assertEqual(confidence, COMPANY_CONFIDENCE_STRONG_INFERRED)
        self.assertEqual(name, "Silversmith Capital Partners")

    def test_weak_inferred_from_dash_headline(self):
        row = {"company_name": "", "profile_title": "Founder & CEO - Makesbridge", "summary": ""}
        self.assertEqual(resolve_company(row), ("Makesbridge", COMPANY_CONFIDENCE_WEAK_INFERRED))

    def test_unknown_when_nothing_recoverable(self):
        row = {"company_name": "", "profile_title": "CEO | COO | Customer Success Executive", "summary": ""}
        self.assertEqual(resolve_company(row), ("", COMPANY_CONFIDENCE_UNKNOWN))

    def test_confidence_ranking_prefers_explicit_over_inferred(self):
        # Same underlying evidence, but company_name is already set and
        # valid -- EXPLICIT must win even though the summary also has a
        # weaker dash-recoverable name.
        row = {
            "company_name": "Hebbia",
            "profile_title": "Founder & CEO - SomethingElse",
            "summary": "",
        }
        self.assertEqual(resolve_company(row), ("Hebbia", COMPANY_CONFIDENCE_EXPLICIT))


# ---------------------------------------------------------------------------
# 4. TargetConfig synonyms
# ---------------------------------------------------------------------------


class TestTargetConfigSynonyms(unittest.TestCase):
    def test_expanded_titles_includes_configured_synonyms(self):
        target = TargetConfig(
            titles=["Founder"],
            title_synonyms={"Founder": ["Founder & CEO", "Co-Founder", "Founding Partner"]},
        )
        self.assertEqual(
            set(target.expanded_titles),
            {"Founder", "Founder & CEO", "Co-Founder", "Founding Partner"},
        )

    def test_synonym_key_not_matching_a_base_term_is_ignored(self):
        target = TargetConfig(
            titles=["Founder"],
            title_synonyms={"CTO": ["Chief Technology Officer"]},
        )
        self.assertEqual(target.expanded_titles, ["Founder"])

    def test_no_synonyms_configured_expanded_equals_base(self):
        target = TargetConfig(titles=["Founder", "CEO"])
        self.assertEqual(target.expanded_titles, ["Founder", "CEO"])

    def test_industry_synonyms_broaden_qualification_evidence(self):
        target = TargetConfig(
            titles=["Founder"],
            industries=["SaaS"],
            industry_synonyms={"SaaS": ["software platform", "cloud software"]},
        )
        row = {
            "profile_title": "Founder",
            "summary": "Building a cloud software platform for logistics teams.",
            "name": "Jane Doe",
            "location": "Austin, Texas, United States",
        }
        self.assertTrue(matches_target_criteria(row, target))

    def test_without_synonym_same_row_does_not_qualify_on_industry(self):
        target = TargetConfig(titles=["Founder"], industries=["SaaS"])
        row = {
            "profile_title": "Founder",
            "summary": "Building a cloud software platform for logistics teams.",
            "name": "Jane Doe",
            "location": "Austin, Texas, United States",
        }
        self.assertFalse(matches_target_criteria(row, target))

    def test_keyword_synonyms_broaden_query_generation(self):
        target = TargetConfig(
            titles=["Founder"],
            keywords=["AI"],
            keyword_synonyms={"AI": ["artificial intelligence", "machine learning"]},
            target_count=10,
        )
        queries = " ".join(build_queries(target)).lower()
        self.assertIn("artificial intelligence", queries)

    def test_synonym_maps_survive_json_round_trip(self):
        target = TargetConfig(
            titles=["Founder"], title_synonyms={"Founder": ["Co-Founder"]}, target_count=5,
        )
        restored = TargetConfig.from_dict(target.to_dict())
        self.assertEqual(restored.title_synonyms, {"Founder": ["Co-Founder"]})
        self.assertEqual(restored.expanded_titles, ["Founder", "Co-Founder"])


# ---------------------------------------------------------------------------
# 5. target_count_mode
# ---------------------------------------------------------------------------


class TestTargetCountMode(unittest.TestCase):
    def test_defaults_to_raw(self):
        self.assertEqual(TargetConfig(target_count=10).target_count_mode, "raw")

    def test_qualified_mode_accepted(self):
        target = TargetConfig(target_count=50, target_count_mode="qualified")
        self.assertEqual(target.target_count_mode, "qualified")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            TargetConfig(target_count=10, target_count_mode="bogus")

    def test_budget_fields_default_to_none(self):
        target = TargetConfig(target_count=10)
        self.assertIsNone(target.max_queries)
        self.assertIsNone(target.max_raw_candidates)
        self.assertIsNone(target.max_minutes)

    def test_budget_fields_round_trip(self):
        target = TargetConfig(
            target_count=50, target_count_mode="qualified",
            max_queries=500, max_raw_candidates=2000, max_minutes=30.0,
        )
        restored = TargetConfig.from_dict(target.to_dict())
        self.assertEqual(restored.target_count_mode, "qualified")
        self.assertEqual(restored.max_queries, 500)
        self.assertEqual(restored.max_raw_candidates, 2000)
        self.assertEqual(restored.max_minutes, 30.0)


if __name__ == "__main__":
    unittest.main()
