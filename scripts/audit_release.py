#!/usr/bin/env python3
"""Advisories in what a release ships, for scripts/release.sh.

    audit_release.py COMMIT FORMULA_FILE

Audits the packages a Homebrew install of COMMIT gets, as COMMIT locks
them: the Python packages for the extras the formula installs (uv.lock,
read with pip-audit), and every npm package its build installs (the
formula runs npm ci, build tools too: package-lock.json, read with npm
audit). Prints each package with an advisory: its version, the advisories
and what fixes them.

Exit 0: none. 3: some (release.sh stops unless --accept-advisories).
Anything else: the audit couldn't run (never 3, so a crash isn't taken for
advisories). Standard library only; needs git, uv and npm.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PIP_AUDIT = "pip-audit==2.10.1"
FOUND = 3  # the exit status that means advisories, and only that
# What the audit reads from the commit being released.
LOCKED = ("pyproject.toml", "uv.lock", "README.md", "src/ui/web/package.json", "src/ui/web/package-lock.json")


@dataclass(frozen=True)
class Finding:
    source: str  # "python" or "npm"
    package: str
    version: str  # the locked version, or npm's affected range
    advisories: tuple[str, ...]
    fixed_by: str  # a version, or what npm says it takes; "" when nothing does yet

    def line(self) -> str:
        fix = f"; fixed by {self.fixed_by}" if self.fixed_by else "; no fix yet"
        return f"{self.source} {self.package} {self.version}: {', '.join(self.advisories)}{fix}"


def formula_extras(formula: str) -> list[str]:
    """The extras the formula installs (`"--extra", "cli"` in its uv sync)."""
    return re.findall(r'"--extra",\s*"([A-Za-z0-9_-]+)"', formula)


def _version_key(version: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"[.+-]", version)]


def python_findings(report: dict) -> list[Finding]:
    """pip-audit's JSON report: each package with advisories, once."""
    found: dict[tuple[str, str], dict[str, list[str]]] = {}
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns") or []:
            found.setdefault((dep["name"], dep["version"]), {})[vuln["id"]] = vuln.get("fix_versions") or []
    out = []
    for (name, version), fixes in sorted(found.items()):
        known = [v for versions in fixes.values() for v in versions]
        fixed_by = max(known, key=_version_key) if known else ""
        unfixed = sum(1 for versions in fixes.values() if not versions)
        if fixed_by and unfixed:
            fixed_by += f" ({unfixed} with no fix yet)"
        out.append(Finding("python", name, version, tuple(sorted(fixes)), fixed_by))
    return out


def npm_findings(report: dict) -> list[Finding]:
    """npm audit's JSON report: each package with an advisory of its own
    (not those listed only for depending on one)."""
    out = []
    for name, vuln in sorted(report.get("vulnerabilities", {}).items()):
        ids = sorted({via["url"].rsplit("/", 1)[-1] for via in vuln.get("via", []) if isinstance(via, dict) and via.get("url")})
        if not ids:
            continue
        fix = vuln.get("fixAvailable")
        if isinstance(fix, dict):
            fixed_by = f"{fix['name']} {fix['version']}" + (" (a major upgrade)" if fix.get("isSemVerMajor") else "")
        else:
            fixed_by = "npm audit fix" if fix else ""
        out.append(Finding("npm", name, vuln.get("range", ""), tuple(ids), fixed_by))
    return out


def _run(cmd: list[str], *, text: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=text, **kwargs)


def audit(commit: str, formula: str, work: Path) -> list[Finding]:
    """Advisories in what `commit` ships. Raises RuntimeError when it can't tell."""
    archive = _run(["git", "archive", "--format=tar", commit, *LOCKED], text=False)
    if archive.returncode != 0:
        raise RuntimeError(f"git archive {commit}: {archive.stderr.decode(errors='replace').strip()}")
    subprocess.run(["tar", "-x", "-C", str(work)], input=archive.stdout, check=True)

    extras = formula_extras(formula)
    if not extras:
        raise RuntimeError("the formula names no extras (uv sync --extra ...): is it the right file?")
    export = _run(["uv", "export", "--project", str(work), "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                   "-q", *(arg for extra in extras for arg in ("--extra", extra))])
    if export.returncode != 0:
        raise RuntimeError(f"uv export: {export.stderr.strip()}")
    requirements = work / "requirements.txt"
    requirements.write_text(export.stdout)
    pip_audit = _run(["uvx", "--from", PIP_AUDIT, "pip-audit", "-r", str(requirements), "--no-deps", "--disable-pip",
                      "-f", "json", "--progress-spinner", "off"])
    if pip_audit.returncode not in (0, 1):
        raise RuntimeError(f"pip-audit: {pip_audit.stderr.strip()[-500:]}")
    npm = _run(["npm", "audit", "--package-lock-only", "--json"], cwd=work / "src" / "ui" / "web")
    try:
        npm_report = json.loads(npm.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"npm audit: {npm.stderr.strip()[-500:]}") from None
    if "error" in npm_report:
        raise RuntimeError(f"npm audit: {npm_report['error']}")
    return python_findings(json.loads(pip_audit.stdout)) + npm_findings(npm_report)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    commit, formula_file = argv
    try:
        with tempfile.TemporaryDirectory() as work:
            findings = audit(commit, Path(formula_file).read_text(), Path(work))
    except Exception as exc:  # whatever it is, the audit didn't happen
        print(f"The audit couldn't run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding.line())
    return FOUND if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
