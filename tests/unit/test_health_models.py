from __future__ import annotations

from eios_core.health import ComponentHealth, build_report


def test_no_checks_is_ok() -> None:
    report = build_report("svc")
    assert (report.status, report.checks, report.service) == ("ok", [], "svc")


def test_all_ok() -> None:
    report = build_report("svc", [ComponentHealth(name="a", status="ok")])
    assert report.status == "ok"


def test_any_failure_fails_the_report() -> None:
    report = build_report(
        "svc",
        [ComponentHealth(name="a", status="ok"), ComponentHealth(name="b", status="fail")],
    )
    assert report.status == "fail"
    assert report.version
