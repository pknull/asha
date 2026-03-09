#!/usr/bin/env python3
"""
Verify - Unified code verification engine with pluggable checkers

Runs type checking, linting, tests, and security scans based on project type.
Supports quick (post-edit), standard (on-demand), and full (CI) levels.

Usage:
    verify.py --quick --file src/auth.ts    # Fast checks on single file
    verify.py --level standard              # Standard verification
    verify.py --level full                  # Full suite including security
    verify.py --list                        # Show available checkers
"""

import os
import sys
import json
import argparse
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
import time


@dataclass
class CheckResult:
    name: str
    passed: bool
    duration: float
    output: str = ""
    error: str = ""


@dataclass
class VerifyResult:
    level: str
    project_type: str
    checks: List[CheckResult] = field(default_factory=list)
    passed: bool = True
    duration: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "level": self.level,
            "project_type": self.project_type,
            "passed": self.passed,
            "duration": round(self.duration, 2),
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "duration": round(c.duration, 2),
                    "output": c.output[:500] if c.output else "",
                    "error": c.error[:500] if c.error else "",
                }
                for c in self.checks
            ],
            "summary": f"{sum(1 for c in self.checks if c.passed)}/{len(self.checks)} passed"
        }


def detect_project_root() -> Path:
    """Find project root via git or upward search"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Upward search for common markers
    search_dir = Path.cwd()
    markers = ["package.json", "pyproject.toml", "go.mod", "pom.xml", "Cargo.toml"]
    while search_dir != search_dir.parent:
        for marker in markers:
            if (search_dir / marker).exists():
                return search_dir
        search_dir = search_dir.parent

    return Path.cwd()


def cmd_exists(cmd: str) -> bool:
    """Check if command exists in PATH"""
    return shutil.which(cmd) is not None


def run_check(name: str, cmd: List[str], cwd: Path, timeout: int = 60) -> CheckResult:
    """Run a single check command"""
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        duration = time.time() - start
        passed = result.returncode == 0
        return CheckResult(
            name=name,
            passed=passed,
            duration=duration,
            output=result.stdout,
            error=result.stderr if not passed else ""
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            passed=False,
            duration=timeout,
            error=f"Timeout after {timeout}s"
        )
    except Exception as e:
        return CheckResult(
            name=name,
            passed=False,
            duration=time.time() - start,
            error=str(e)
        )


# =============================================================================
# Checkers by Language/Framework
# =============================================================================

CHECKERS = {}


def register_checker(name: str):
    """Decorator to register a checker"""
    def decorator(cls):
        CHECKERS[name] = cls()
        return cls
    return decorator


class BaseChecker:
    name: str = "base"

    def detect(self, root: Path) -> bool:
        """Return True if this checker applies to the project"""
        return False

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        """Return list of checks to run for given level"""
        return []


@register_checker("typescript")
class TypeScriptChecker(BaseChecker):
    name = "typescript"

    def detect(self, root: Path) -> bool:
        return (root / "tsconfig.json").exists()

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        checks = []

        # Type checking
        if cmd_exists("tsc"):
            checks.append({
                "name": "tsc",
                "cmd": ["tsc", "--noEmit"],
                "timeout": 60,
                "levels": ["quick", "standard", "full"]
            })

        # Linting/formatting
        if cmd_exists("biome"):
            if file and level == "quick":
                checks.append({
                    "name": "biome-format",
                    "cmd": ["biome", "format", str(file)],
                    "timeout": 10,
                    "levels": ["quick"]
                })
            else:
                checks.append({
                    "name": "biome-check",
                    "cmd": ["biome", "check", "."],
                    "timeout": 30,
                    "levels": ["standard", "full"]
                })
        elif cmd_exists("eslint"):
            checks.append({
                "name": "eslint",
                "cmd": ["eslint", ".", "--max-warnings=0"],
                "timeout": 60,
                "levels": ["standard", "full"]
            })

        # Tests
        if level in ["standard", "full"]:
            if (root / "package.json").exists():
                pkg = json.loads((root / "package.json").read_text())
                if "test" in pkg.get("scripts", {}):
                    checks.append({
                        "name": "test",
                        "cmd": ["npm", "test"],
                        "timeout": 120,
                        "levels": ["standard", "full"]
                    })

        # Security (full only)
        if level == "full" and cmd_exists("npm"):
            checks.append({
                "name": "npm-audit",
                "cmd": ["npm", "audit", "--audit-level=high"],
                "timeout": 60,
                "levels": ["full"]
            })

        return [c for c in checks if level in c["levels"]]


@register_checker("python")
class PythonChecker(BaseChecker):
    name = "python"

    def detect(self, root: Path) -> bool:
        return (root / "pyproject.toml").exists() or (root / "setup.py").exists()

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        checks = []

        # Type checking
        if cmd_exists("mypy"):
            if file and level == "quick":
                checks.append({
                    "name": "mypy",
                    "cmd": ["mypy", str(file), "--ignore-missing-imports"],
                    "timeout": 30,
                    "levels": ["quick"]
                })
            else:
                checks.append({
                    "name": "mypy",
                    "cmd": ["mypy", ".", "--ignore-missing-imports"],
                    "timeout": 60,
                    "levels": ["standard", "full"]
                })

        # Linting
        if cmd_exists("ruff"):
            if file and level == "quick":
                checks.append({
                    "name": "ruff-format",
                    "cmd": ["ruff", "format", "--check", str(file)],
                    "timeout": 10,
                    "levels": ["quick"]
                })
            else:
                checks.append({
                    "name": "ruff-check",
                    "cmd": ["ruff", "check", "."],
                    "timeout": 30,
                    "levels": ["standard", "full"]
                })

        # Tests
        if level in ["standard", "full"] and cmd_exists("pytest"):
            checks.append({
                "name": "pytest",
                "cmd": ["pytest", "-x", "-q"],
                "timeout": 120,
                "levels": ["standard", "full"]
            })

        # Security
        if level == "full":
            if cmd_exists("bandit"):
                checks.append({
                    "name": "bandit",
                    "cmd": ["bandit", "-r", ".", "-ll"],
                    "timeout": 60,
                    "levels": ["full"]
                })
            if cmd_exists("safety"):
                checks.append({
                    "name": "safety",
                    "cmd": ["safety", "check"],
                    "timeout": 60,
                    "levels": ["full"]
                })

        return [c for c in checks if level in c["levels"]]


@register_checker("go")
class GoChecker(BaseChecker):
    name = "go"

    def detect(self, root: Path) -> bool:
        return (root / "go.mod").exists()

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        checks = []

        if not cmd_exists("go"):
            return checks

        # Build check (type checking equivalent)
        checks.append({
            "name": "go-build",
            "cmd": ["go", "build", "./..."],
            "timeout": 60,
            "levels": ["quick", "standard", "full"]
        })

        # Vet
        checks.append({
            "name": "go-vet",
            "cmd": ["go", "vet", "./..."],
            "timeout": 30,
            "levels": ["quick", "standard", "full"]
        })

        # Formatting check
        if level == "quick" and file:
            checks.append({
                "name": "gofmt",
                "cmd": ["gofmt", "-l", str(file)],
                "timeout": 10,
                "levels": ["quick"]
            })

        # Staticcheck
        if cmd_exists("staticcheck") and level in ["standard", "full"]:
            checks.append({
                "name": "staticcheck",
                "cmd": ["staticcheck", "./..."],
                "timeout": 60,
                "levels": ["standard", "full"]
            })

        # Tests
        if level in ["standard", "full"]:
            checks.append({
                "name": "go-test",
                "cmd": ["go", "test", "-race", "./..."],
                "timeout": 120,
                "levels": ["standard", "full"]
            })

        # Security
        if level == "full" and cmd_exists("gosec"):
            checks.append({
                "name": "gosec",
                "cmd": ["gosec", "./..."],
                "timeout": 60,
                "levels": ["full"]
            })

        return [c for c in checks if level in c["levels"]]


@register_checker("java")
class JavaChecker(BaseChecker):
    name = "java"

    def detect(self, root: Path) -> bool:
        return (root / "pom.xml").exists() or (root / "build.gradle").exists()

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        checks = []

        is_maven = (root / "pom.xml").exists()
        is_gradle = (root / "build.gradle").exists() or (root / "build.gradle.kts").exists()

        if is_maven:
            # Compile (type check)
            checks.append({
                "name": "mvn-compile",
                "cmd": ["mvn", "compile", "-q"],
                "timeout": 120,
                "levels": ["quick", "standard", "full"]
            })

            if level in ["standard", "full"]:
                checks.append({
                    "name": "mvn-test",
                    "cmd": ["mvn", "test", "-q"],
                    "timeout": 180,
                    "levels": ["standard", "full"]
                })

            if level == "full":
                checks.append({
                    "name": "mvn-verify",
                    "cmd": ["mvn", "verify", "-q"],
                    "timeout": 300,
                    "levels": ["full"]
                })

        elif is_gradle:
            gradle_cmd = "./gradlew" if (root / "gradlew").exists() else "gradle"

            checks.append({
                "name": "gradle-compile",
                "cmd": [gradle_cmd, "compileJava", "-q"],
                "timeout": 120,
                "levels": ["quick", "standard", "full"]
            })

            if level in ["standard", "full"]:
                checks.append({
                    "name": "gradle-test",
                    "cmd": [gradle_cmd, "test", "-q"],
                    "timeout": 180,
                    "levels": ["standard", "full"]
                })

        return [c for c in checks if level in c["levels"]]


@register_checker("rust")
class RustChecker(BaseChecker):
    name = "rust"

    def detect(self, root: Path) -> bool:
        return (root / "Cargo.toml").exists()

    def get_checks(self, level: str, root: Path, file: Optional[Path] = None) -> List[Dict]:
        checks = []

        if not cmd_exists("cargo"):
            return checks

        # Check (compile + lint)
        checks.append({
            "name": "cargo-check",
            "cmd": ["cargo", "check"],
            "timeout": 120,
            "levels": ["quick", "standard", "full"]
        })

        # Clippy
        if cmd_exists("cargo-clippy") or True:  # clippy is usually available
            checks.append({
                "name": "cargo-clippy",
                "cmd": ["cargo", "clippy", "--", "-D", "warnings"],
                "timeout": 120,
                "levels": ["standard", "full"]
            })

        # Format check
        checks.append({
            "name": "cargo-fmt",
            "cmd": ["cargo", "fmt", "--check"],
            "timeout": 30,
            "levels": ["quick", "standard", "full"]
        })

        # Tests
        if level in ["standard", "full"]:
            checks.append({
                "name": "cargo-test",
                "cmd": ["cargo", "test"],
                "timeout": 180,
                "levels": ["standard", "full"]
            })

        # Security audit
        if level == "full" and cmd_exists("cargo-audit"):
            checks.append({
                "name": "cargo-audit",
                "cmd": ["cargo", "audit"],
                "timeout": 60,
                "levels": ["full"]
            })

        return [c for c in checks if level in c["levels"]]


# =============================================================================
# Main Verification Engine
# =============================================================================

def detect_project_type(root: Path) -> List[str]:
    """Detect all applicable project types"""
    types = []
    for name, checker in CHECKERS.items():
        if checker.detect(root):
            types.append(name)
    return types if types else ["unknown"]


def run_verification(
    level: str = "standard",
    root: Optional[Path] = None,
    file: Optional[Path] = None,
    parallel: bool = True
) -> VerifyResult:
    """Run verification checks"""
    if root is None:
        root = detect_project_root()

    project_types = detect_project_type(root)
    result = VerifyResult(
        level=level,
        project_type=",".join(project_types)
    )

    start_time = time.time()

    # Collect all checks
    all_checks = []
    for ptype in project_types:
        if ptype in CHECKERS:
            checks = CHECKERS[ptype].get_checks(level, root, file)
            all_checks.extend(checks)

    if not all_checks:
        result.checks.append(CheckResult(
            name="detect",
            passed=True,
            duration=0,
            output=f"No checks found for project types: {project_types}"
        ))
        result.duration = time.time() - start_time
        return result

    # Run checks
    if parallel and len(all_checks) > 1:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    run_check, c["name"], c["cmd"], root, c.get("timeout", 60)
                ): c for c in all_checks
            }
            for future in as_completed(futures):
                check_result = future.result()
                result.checks.append(check_result)
                if not check_result.passed:
                    result.passed = False
    else:
        for check in all_checks:
            check_result = run_check(
                check["name"], check["cmd"], root, check.get("timeout", 60)
            )
            result.checks.append(check_result)
            if not check_result.passed:
                result.passed = False

    result.duration = time.time() - start_time
    return result


def format_result(result: VerifyResult, verbose: bool = False) -> str:
    """Format result for display"""
    lines = []

    status = "PASS" if result.passed else "FAIL"
    lines.append(f"Verification: {status} ({result.duration:.1f}s)")
    lines.append(f"Project: {result.project_type} | Level: {result.level}")
    lines.append("")

    for check in result.checks:
        icon = "✓" if check.passed else "✗"
        lines.append(f"  {icon} {check.name} ({check.duration:.1f}s)")
        if verbose and check.error:
            for line in check.error.split("\n")[:5]:
                lines.append(f"      {line}")

    passed = sum(1 for c in result.checks if c.passed)
    total = len(result.checks)
    lines.append("")
    lines.append(f"Summary: {passed}/{total} checks passed")

    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Unified code verification engine",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--level", "-l",
        choices=["quick", "standard", "full"],
        default="standard",
        help="Verification level (default: standard)"
    )
    parser.add_argument(
        "--quick", "-q",
        action="store_true",
        help="Shorthand for --level quick"
    )
    parser.add_argument(
        "--full", "-f",
        action="store_true",
        help="Shorthand for --level full"
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Single file to check (for quick level)"
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Project root directory"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of formatted text"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show error details"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available checkers"
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Run checks sequentially"
    )

    args = parser.parse_args()

    if args.list:
        print("Available checkers:")
        for name, checker in CHECKERS.items():
            print(f"  - {name}")
        return

    # Determine level
    level = args.level
    if args.quick:
        level = "quick"
    elif args.full:
        level = "full"

    # Run verification
    result = run_verification(
        level=level,
        root=args.root,
        file=args.file,
        parallel=not args.no_parallel
    )

    # Output
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_result(result, verbose=args.verbose))

    # Exit code
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
