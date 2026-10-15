from __future__ import annotations

from eios_domain.policy import Risk
from eios_workflow import ProjectFacts, TeamSignals, merge_signals
from eios_workflow.signals import max_risk, snapshot


def test_risk_is_the_higher_of_the_caller_and_the_analysis() -> None:
    low = TeamSignals(risk=Risk.LOW, paths=("a.py",))
    assert merge_signals(low, ProjectFacts(risk=Risk.HIGH)).risk is Risk.HIGH
    high = TeamSignals(risk=Risk.CRITICAL)
    assert merge_signals(high, ProjectFacts(risk=Risk.LOW)).risk is Risk.CRITICAL
    assert max_risk(Risk.MEDIUM, Risk.LOW, Risk.HIGH) is Risk.HIGH


def test_facts_only_raise_what_the_caller_stated() -> None:
    base = TeamSignals(
        risk=Risk.MEDIUM, dependents=12, tests_missing=True, touches_sql=False, coverage=0.9
    )
    facts = ProjectFacts(
        risk=Risk.LOW, dependents=3, tests_missing=False, touches_sql=True, coverage=0.1
    )
    merged = merge_signals(base, facts)
    assert merged.dependents == 12  # the larger blast radius wins
    assert merged.tests_missing and merged.touches_sql  # either source is enough
    assert merged.coverage == 0.9  # a stated coverage is kept; the analysis only fills a gap
    assert merge_signals(TeamSignals(), facts).coverage == 0.1


def test_named_targets_make_the_targets_explicit_even_without_an_analysis() -> None:
    assert merge_signals(TeamSignals(paths=("a.py",)), None).explicit_targets
    assert merge_signals(TeamSignals(symbols=("A.b",)), None).explicit_targets
    assert not merge_signals(TeamSignals(), None).explicit_targets


def test_snapshot_is_json_able_and_keeps_where_the_values_came_from() -> None:
    import json

    facts = ProjectFacts(risk=Risk.HIGH, dependents=17, summary={"consumers": ["x.py"]})
    base = TeamSignals(risk=Risk.LOW, symbols=("PolicyEngine.evaluate",))
    snap = snapshot(merge_signals(base, facts), caller_risk=Risk.LOW, facts=facts)
    assert json.loads(json.dumps(snap)) == snap
    assert snap["risk"] == "high" and snap["caller_risk"] == "low"
    assert snap["symbols"] == ["PolicyEngine.evaluate"] and snap["impact"] == {
        "consumers": ["x.py"]
    }
    failed = snapshot(base, caller_risk=Risk.LOW, facts=None, error="NotFoundError: nope")
    assert failed["impact"] is None and failed["impact_error"] == "NotFoundError: nope"
