"""The impact risk model: every point has a named factor, and the scale reaches the top."""

from __future__ import annotations

import uuid

import pytest

from eios_domain.policy import Risk
from eios_retrieval.impact import ImpactConfig, ImpactEntry, ImpactReport, assess_risk
from tests.conftest import make_settings

CONFIG = ImpactConfig()


def _report(
    *,
    consumers: int = 0,
    tests_missing: bool = False,
    db_scripts: list[str] | None = None,
    consumer_paths: list[str] | None = None,
) -> ImpactReport:
    paths = consumer_paths or [f"app/mod{n}.py" for n in range(consumers)]
    return ImpactReport(
        project_id=uuid.uuid4(),
        branch="main",
        targets=["file:x"],
        consumers=paths,
        blast_radius=len(paths),
        tests_missing=tests_missing,
        db_scripts=db_scripts or [],
    )


def _risk(report: ImpactReport, *targets: str) -> Risk:
    assess_risk(report, list(targets), CONFIG)
    return report.risk


def test_the_scale_reaches_high_and_critical() -> None:
    assert _risk(_report(consumers=2), "app/util.py") is Risk.LOW  # 1 point
    assert _risk(_report(consumers=6, tests_missing=True), "app/util.py") is Risk.MEDIUM  # 2 + 2
    assert _risk(_report(consumers=16, tests_missing=True), "app/util.py") is Risk.HIGH  # 3 + 2
    wide = _report(consumers=45, tests_missing=True)
    assert _risk(wide, "app/util.py") is Risk.HIGH  # 4 + 2
    assert any("very wide blast radius" in f for f in wide.risk_factors)
    critical = _report(consumers=45, tests_missing=True)
    assert _risk(critical, "packages/policy/src/eios_policy/engine.py") is Risk.CRITICAL  # 4+2+2


def test_security_critical_code_is_never_below_high_whatever_its_blast_radius() -> None:
    report = _report(consumers=0)
    assert _risk(report, "packages/policy/src/eios_policy/engine.py") is Risk.HIGH
    assert any("security-critical" in f for f in report.risk_factors)
    for path in (
        "apps/api/src/eios_api/routes/admin.py",
        "packages/policy/src/eios_policy/audit.py",
        "app/sandbox.py",
        "app/auth/tokens.py",
    ):
        assert _risk(_report(), path) is Risk.HIGH, path


def test_schema_changes_are_database_changes_and_never_low() -> None:
    for path in (
        "migrations/versions/0006_policy_approvals_and_audit_log.py",
        "packages/storage/src/eios_storage/tables/events.py",
        "db/schema.sql",
        "scripts/cleanup.sql",
    ):
        report = _report()
        assert _risk(report, path) is not Risk.LOW, path
        assert any("database schema" in f for f in report.risk_factors), path
    assert _risk(_report(db_scripts=["db/x.sql"]), "app/repo.py") is Risk.LOW  # +1 only


def test_a_route_module_without_import_tests_is_penalised_less() -> None:
    plain = _report(tests_missing=True)
    route = _report(tests_missing=True)
    assess_risk(plain, ["app/util.py"], CONFIG)
    assess_risk(route, ["app/routes/items.py"], CONFIG)
    assert any("over HTTP" in f for f in route.risk_factors)
    assert not any("over HTTP" in f for f in plain.risk_factors)
    assert _risk(_report(consumers=15, tests_missing=True), "app/util.py") is Risk.HIGH  # 3 + 2
    # a route module is also a public contract surface: the missing-test point is traded for it
    assert any("public contract surface" in f for f in route.risk_factors)
    assert [f for f in plain.risk_factors if "no tests" in f] == [
        "no tests found for the targets or their dependents"
    ]


def test_contract_surfaces_add_a_point() -> None:
    report = _report(consumers=5)  # moderate: 2 points
    assert _risk(report, "packages/domain/src/eios_domain/events.py") is Risk.MEDIUM  # 2 + 1
    assert any("public contract surface" in f for f in report.risk_factors)


def test_name_only_callers_are_listed_flagged_and_not_counted() -> None:
    def weak(n: int) -> ImpactEntry:
        return ImpactEntry(
            key=f"symbol:app/m{n}.py::f",
            path=f"app/m{n}.py",
            label="f",
            kind="function",
            depth=1,
            confidence=0.2,
            via="unresolved-name",
            low_confidence=True,
        )

    report = _report(consumers=5)  # moderate blast radius: 2 points
    report.callers = [weak(n) for n in range(6)]
    report.low_confidence_edges = 6
    assess_risk(report, ["app/util.py"], CONFIG)
    assert any("matched by name only" in f for f in report.risk_factors)
    assert report.blast_radius == 5  # they never inflate the radius...
    assert report.risk is Risk.MEDIUM  # ...but six files calling by name add a point (2 + 1)
    few = _report(consumers=5)
    few.callers = [weak(n) for n in range(2)]
    few.low_confidence_edges = 2
    assert _risk(few, "app/util.py") is Risk.LOW  # two unverified files are only listed


def test_paths_come_from_settings_not_from_the_analyzer() -> None:
    custom = ImpactConfig.from_settings(make_settings(impact_security_critical_paths=["*/vault/*"]))
    assert custom.is_security_critical("svc/vault/keys.py")
    assert not custom.is_security_critical("packages/policy/src/eios_policy/engine.py")
    report = _report()
    assess_risk(report, ["svc/vault/keys.py"], custom)
    assert report.risk is Risk.HIGH


@pytest.mark.parametrize("path", ["README.md", "docs/guide.md", "app/util.py"])
def test_ordinary_files_are_not_security_critical(path: str) -> None:
    assert not CONFIG.is_security_critical(path)
