"""Age filtering (Aug 2026 requirements): prove the person-age filter
(age_min/age_max on TargetConfig) is actually applied by the existing
"age" + "qualify" phases, and that unknown ages are never rejected --
matching the documented, pre-existing behaviour in quality._age_ok().

This does not change qualification logic; it only proves the existing
methodology behaves the way age_min/age_max are now exposed on the
campaign-creation UI/API expect.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from pipeline.quality import qualify_row  # noqa: E402
from pipeline.target_config import TargetConfig  # noqa: E402

BASE_ROW = {
    "name": "Jane Doe",
    "profile_title": "Founder",
    "summary": "Founder building an AI SaaS company.",
    "linkedin_url": "https://www.linkedin.com/in/janedoe",
    "location": "United States",
    "industries": "",
}


def _target(**kw) -> TargetConfig:
    return TargetConfig(name="age_test", titles=["Founder"], target_count=10, **kw)


def test_no_age_filter_never_rejects_on_age():
    target = _target()  # age_min/age_max both None
    row = dict(BASE_ROW, age="60", age_confidence="high")
    ok, reason = qualify_row(row, target)
    assert ok, reason


def test_known_age_inside_range_passes():
    target = _target(age_min=25, age_max=45)
    row = dict(BASE_ROW, age="30", age_confidence="high", age_source="explicit")
    ok, reason = qualify_row(row, target)
    assert ok, reason


def test_known_age_below_range_is_excluded():
    target = _target(age_min=25, age_max=45)
    row = dict(BASE_ROW, age="19", age_confidence="high", age_source="explicit")
    ok, reason = qualify_row(row, target)
    assert not ok
    assert "age" in reason.lower()


def test_known_age_above_range_is_excluded():
    target = _target(age_min=25, age_max=45)
    row = dict(BASE_ROW, age="60", age_confidence="high", age_source="explicit")
    ok, reason = qualify_row(row, target)
    assert not ok
    assert "age" in reason.lower()


def test_unknown_age_is_never_rejected_even_with_filter_configured():
    """The core safety requirement: minimum age = 25 with an unknown-age
    prospect must NOT be excluded on the assumption their seniority implies
    they're 25+. Unknown stays unknown and is not penalized."""
    target = _target(age_min=25, age_max=45)
    row = dict(BASE_ROW, age="", age_confidence="none")
    ok, reason = qualify_row(row, target)
    assert ok, reason


def test_age_confidence_none_string_also_treated_as_unknown():
    target = _target(age_min=25, age_max=45)
    row = dict(BASE_ROW, age="", age_confidence="")
    ok, reason = qualify_row(row, target)
    assert ok, reason


def test_minimum_only_filter():
    target = _target(age_min=30)
    assert qualify_row(dict(BASE_ROW, age="35", age_confidence="high"), target)[0]
    assert not qualify_row(dict(BASE_ROW, age="20", age_confidence="high"), target)[0]
    # unknown still passes
    assert qualify_row(dict(BASE_ROW, age="", age_confidence="none"), target)[0]


def test_maximum_only_filter():
    target = _target(age_max=40)
    assert qualify_row(dict(BASE_ROW, age="35", age_confidence="high"), target)[0]
    assert not qualify_row(dict(BASE_ROW, age="50", age_confidence="high"), target)[0]
    assert qualify_row(dict(BASE_ROW, age="", age_confidence="none"), target)[0]


def test_target_config_rejects_min_greater_than_max():
    import pytest

    with pytest.raises(ValueError):
        TargetConfig(name="bad", age_min=45, age_max=25)
