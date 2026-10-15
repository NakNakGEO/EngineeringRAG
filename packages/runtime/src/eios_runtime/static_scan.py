"""A static look at source text: secrets, risky calls, install hooks, binaries. Nothing is run.

Used by the ``security_scan`` capability (indexed project files) and by tool onboarding (the files
of a downloaded repository, which are only ever read as bytes). Heuristics, not proof: a clean
result means "nothing obvious", never "safe".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

SEVERITIES = ("high", "medium", "low")
MAX_FILE_BYTES = 512 * 1024
MAX_FINDINGS = 300

# (rule id, severity, pattern, why it matters)
RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("private-key", "high", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "a private key is committed"),
    ("aws-access-key", "high", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "looks like an AWS access key id"),
    ("hardcoded-secret", "medium", re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['"][A-Za-z0-9_\-/+=]{16,}['"]"""), "a long literal assigned to a secret-like name"),
    ("download-and-run", "high", re.compile(r"\b(?:curl|wget)\b[^\n|;]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"), "downloads a script and pipes it to a shell"),
    ("decode-and-run", "high", re.compile(r"base64[^\n]{0,60}\b(?:exec|eval)\b|\b(?:exec|eval)\b[^\n]{0,60}base64"), "decodes a payload and executes it"),
    ("eval-exec", "medium", re.compile(r"(?<![\w.])(?:eval|exec)\s*\("), "evaluates text as code"),
    ("shell-out", "medium", re.compile(r"\bos\.system\s*\(|\bsubprocess\.\w+\([^)\n]*shell\s*=\s*True|\bchild_process\b|\bProcess\.Start\b|\bRuntime\.getRuntime\(\)\.exec"), "starts a shell or another program"),
    ("unsafe-deserialise", "medium", re.compile(r"\bpickle\.loads?\s*\(|\byaml\.load\s*\((?![^)\n]*Loader)|\bmarshal\.loads?\s*\("), "deserialises data in a way that can run code"),
    ("sql-concatenation", "medium", re.compile(r"""(?i)\b(?:select|insert|update|delete)\b[^\n"']{0,80}["']\s*(?:\+|%|\.format|\bf["'])"""), "builds SQL by string concatenation"),
    ("network-use", "low", re.compile(r"\b(?:requests|httpx|aiohttp|urllib\.request|http\.client)\b|socket\.socket|\bfetch\s*\(|\bXMLHttpRequest\b|\bnet/http\b"), "talks to the network"),
    ("writes-outside", "low", re.compile(r"""open\s*\(\s*['"](?:/etc|/usr|/root|~|C:\\)"""), "writes or reads system paths"),
]

# files that run something when a package is installed or a project is built
INSTALL_HOOK_NAMES = {"setup.py", "setup.cfg", "pyproject.toml", "package.json", "makefile", "dockerfile", "build.rs", "build.gradle", "cmakelists.txt", "install.sh", "postinstall.js", "preinstall.js", "go.mod", "cargo.toml"}
_PACKAGE_SCRIPTS = re.compile(r'"(?:pre|post)?install"\s*:|"prepare"\s*:|"prepublish\w*"\s*:')
_SETUP_HOOKS = re.compile(r"\bcmdclass\b|\bsetup_requires\b|\bos\.system\b|\bsubprocess\b")
_BINARY_MAGIC = {b"\x7fELF": "ELF executable", b"MZ": "Windows executable", b"\xcf\xfa\xed\xfe": "Mach-O executable", b"\xca\xfe\xba\xbe": "Mach-O / Java class", b"PK\x03\x04": "zip / jar archive"}

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go",
    ".rs": "rust", ".java": "java", ".kt": "kotlin", ".cs": "csharp", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".rb": "ruby", ".php": "php", ".sh": "shell", ".ps1": "powershell", ".sql": "sql", ".md": "markdown", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".html": "html", ".css": "css", ".dll": "binary", ".exe": "binary", ".so": "binary",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    why: str
    text: str


def language_of(path: str) -> str:
    dot = path.rfind(".")
    return LANGUAGES.get(path[dot:].lower(), "other") if dot > path.rfind("/") else "other"


def binary_kind(head: bytes) -> str | None:
    for magic, name in _BINARY_MAGIC.items():
        if head.startswith(magic):
            return name
    return None


def scan_text(path: str, text: str) -> list[Finding]:
    """Findings in one text file (line by line; long lines are cut)."""
    out: list[Finding] = []
    name = path.rsplit("/", 1)[-1].lower()
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw[:400]
        for rule, severity, pattern, why in RULES:
            if pattern.search(line):
                out.append(Finding(severity, rule, path, number, why, line.strip()[:140]))
        if name == "package.json" and _PACKAGE_SCRIPTS.search(line):
            out.append(Finding("high", "install-hook", path, number, "runs a script when the package is installed", line.strip()[:140]))
        if name in {"setup.py"} and _SETUP_HOOKS.search(line):
            out.append(Finding("high", "install-hook", path, number, "runs code while the package is installed", line.strip()[:140]))
    return out


def summarise(by_rule: Counter[str], by_severity: Counter[str]) -> dict[str, Any]:
    """Counts and verdict over every finding, including the ones too many to list."""
    if by_severity.get("high"):
        verdict = "review needed: high-severity findings"
    elif by_severity.get("medium"):
        verdict = "review advised: medium-severity findings"
    else:
        verdict = "nothing obvious found (this is not proof of safety)"
    return {"by_severity": {s: by_severity.get(s, 0) for s in SEVERITIES}, "by_rule": dict(by_rule.most_common()), "verdict": verdict}


_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def _most_serious(findings: list[Finding]) -> list[Finding]:
    """The first MAX_FINDINGS by severity, then path and line."""
    return sorted(findings, key=lambda f: (_RANK[f.severity], f.path, f.line))[:MAX_FINDINGS]


def scan_files(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Scan ``(path, bytes)`` pairs: text rules, install hooks, binaries, languages. Pure.

    Every file is scanned and every finding is counted; only the listing is capped, and what is
    kept is the most serious, never simply the first ones met."""
    kept: list[Finding] = []
    by_rule: Counter[str] = Counter()
    by_severity: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    binaries: list[dict[str, str]] = []
    hooks: list[str] = []
    for path, data in files:
        languages[language_of(path)] += 1
        name = path.rsplit("/", 1)[-1].lower()
        if name in INSTALL_HOOK_NAMES:
            hooks.append(path)
        kind = binary_kind(data[:8])
        if kind and not path.lower().endswith((".zip", ".jar", ".docx", ".xlsx", ".png", ".jpg")):
            binaries.append({"path": path, "kind": kind})
            continue
        if b"\x00" in data[:2048] or len(data) > MAX_FILE_BYTES:
            continue
        found = scan_text(path, data.decode("utf-8", errors="replace"))
        by_rule.update(f.rule for f in found)
        by_severity.update(f.severity for f in found)
        kept += found
        if len(kept) > 4 * MAX_FINDINGS:
            kept = _most_serious(kept)
    listed = _most_serious(kept)
    total = sum(by_severity.values())
    return {
        "files": len(files),
        "languages": dict(languages.most_common()),
        "build_or_install_files": sorted(hooks)[:40],
        "binaries": binaries[:40],
        "findings": [asdict(f) for f in listed],
        "total_findings": total,
        "truncated": total > len(listed),
        **summarise(by_rule, by_severity),
    }
