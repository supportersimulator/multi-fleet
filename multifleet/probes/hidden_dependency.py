"""Hidden Dependency Probe — detects undeclared coupling between modules.

Comprehensive hidden dependency detection (merged from code_scanner + probe_engine).
Checks:
1. Python imports not in requirements.txt / pyproject.toml
2. Subprocess calls to binaries not documented as dependencies
3. Cross-module imports that bypass declared API surface
4. File reads to paths outside the module's directory
"""

import ast
import logging
import re
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.hidden_dependency")

# Standard library modules (Python 3.10+)
_STDLIB = frozenset({
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
    "asyncore", "atexit", "base64", "bdb", "binascii", "binhex",
    "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk",
    "cmath", "cmd", "code", "codecs", "codeop", "collections",
    "colorsys", "compileall", "concurrent", "configparser", "contextlib",
    "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
    "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
    "difflib", "dis", "distutils", "doctest", "email", "encodings",
    "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
    "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
    "getpass", "gettext", "glob", "grp", "gzip", "hashlib", "heapq",
    "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp",
    "importlib", "inspect", "io", "ipaddress", "itertools", "json",
    "keyword", "lib2to3", "linecache", "locale", "logging", "lzma",
    "mailbox", "mailcap", "marshal", "math", "mimetypes", "mmap",
    "modulefinder", "multiprocessing", "netrc", "nis", "nntplib",
    "numbers", "operator", "optparse", "os", "ossaudiodev",
    "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil",
    "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
    "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr",
    "pydoc", "queue", "quopri", "random", "re", "readline", "reprlib",
    "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
    "selectors", "shelve", "shlex", "shutil", "signal", "site",
    "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "sqlite3",
    "ssl", "stat", "statistics", "string", "stringprep", "struct",
    "subprocess", "sunau", "symtable", "sys", "sysconfig", "syslog",
    "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "test",
    "textwrap", "threading", "time", "timeit", "tkinter", "token",
    "tokenize", "tomllib", "trace", "traceback", "tracemalloc",
    "tty", "turtle", "turtledemo", "types", "typing", "unicodedata",
    "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
    "weakref", "webbrowser", "winreg", "winsound", "wsgiref",
    "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
    "_thread", "__future__",
})

# Common subprocess binary calls
_SUBPROCESS_RE = re.compile(
    r"""subprocess\.(?:run|call|check_call|check_output|Popen)\s*\(\s*"""
    r"""(?:\[?\s*['"](\S+?)['"]|\s*['"](\S+?)\s)""",
)

# Safe binaries that don't need documenting
_SAFE_BINS = frozenset({
    "python", "python3", "pip", "pip3", "git", "bash", "sh", "zsh",
    "echo", "cat", "ls", "mkdir", "cp", "mv", "rm", "grep", "find",
    "sed", "awk", "sort", "uniq", "wc", "head", "tail", "tee",
    "curl", "wget", "ssh", "scp", "rsync", "tar", "gzip",
    "docker", "npm", "node", "make", "which", "env", "test",
})

# File read patterns
_FILE_READ_RE = re.compile(
    r"""(?:open|Path)\s*\(\s*['"]([^'"]+)['"]|"""
    r"""\.read_text\s*\(|\.read_bytes\s*\(""",
)


def _get_declared_deps(repo: Path) -> set[str]:
    """Extract declared dependencies from requirements.txt and pyproject.toml."""
    deps: set[str] = set()

    for req_file in repo.rglob("requirements*.txt"):
        try:
            for line in req_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                pkg = re.split(r"[><=!~\[\];]", line)[0].strip().lower()
                if pkg:
                    deps.add(pkg.replace("-", "_"))
        except OSError:
            continue

    for toml_file in repo.rglob("pyproject.toml"):
        try:
            text = toml_file.read_text(errors="replace")
            for m in re.finditer(r'"([a-zA-Z][a-zA-Z0-9_-]*)', text):
                deps.add(m.group(1).lower().replace("-", "_"))
        except OSError:
            pass

    return deps


def _get_local_packages(repo: Path) -> set[str]:
    """Find local Python package names (dirs with __init__.py)."""
    packages = set()
    for init in repo.rglob("__init__.py"):
        parts = init.parent.relative_to(repo).parts
        if parts:
            packages.add(parts[0])
    return packages


def _collect_py_files(repo: Path) -> list[Path]:
    """Collect Python files, skipping venvs and caches."""
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", "dist", "build"}
    py_files = []
    for py_file in repo.rglob("*.py"):
        if any(p in py_file.parts for p in skip):
            continue
        py_files.append(py_file)
        if len(py_files) >= 200:
            break
    return py_files


def _is_undeclared(module: str, declared: set[str], local_packages: set[str]) -> bool:
    """Check if a module is undeclared (not stdlib, not declared, not local)."""
    if not module or module.startswith("_"):
        return False
    if module in _STDLIB:
        return False
    normalized = module.lower().replace("-", "_")
    if normalized in declared:
        return False
    if module in local_packages:
        return False
    return True


def _check_undeclared_imports(
    repo: Path, py_files: list[Path], declared: set[str], local_packages: set[str]
) -> list[ProbeFinding]:
    """Check 1: Python imports not in declared dependencies."""
    undeclared: dict[str, list[str]] = {}

    for py_file in py_files[:50]:
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        rel = str(py_file.relative_to(repo))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if _is_undeclared(top, declared, local_packages):
                        undeclared.setdefault(top, []).append(rel)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if _is_undeclared(top, declared, local_packages):
                    undeclared.setdefault(top, []).append(rel)

    findings = []
    for pkg, files in undeclared.items():
        unique_files = sorted(set(files))
        findings.append(ProbeFinding(
            title=f"Undeclared dependency: {pkg}",
            evidence=[f"Used in: {f}" for f in unique_files[:5]],
            blast_radius="medium" if len(unique_files) > 3 else "low",
            suggested_injections=[f"Add {pkg} to requirements.txt or verify it's a local module"],
            file_paths=unique_files[:5],
        ))
    return findings


def _check_subprocess_binaries(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check 2: Subprocess calls to undocumented binaries."""
    binaries_found: dict[str, list[str]] = {}

    for py_file in py_files[:50]:
        try:
            source = py_file.read_text(errors="replace")
        except OSError:
            continue
        for m in _SUBPROCESS_RE.finditer(source):
            binary = m.group(1) or m.group(2)
            if binary:
                binary = binary.split("/")[-1]
                if binary and binary not in _SAFE_BINS:
                    binaries_found.setdefault(binary, []).append(
                        str(py_file.relative_to(repo))
                    )

    findings = []
    for binary, files in binaries_found.items():
        findings.append(ProbeFinding(
            title=f"Subprocess calls undocumented binary: {binary}",
            evidence=[f"Called from: {f}" for f in files[:5]],
            blast_radius="low",
            suggested_injections=[f"Document {binary} as a system dependency"],
            file_paths=list(set(files))[:5],
        ))
    return findings


def _check_cross_module_imports(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check 3: Imports that reach into private module internals."""
    private_imports: list[tuple[str, str]] = []

    for py_file in py_files[:50]:
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if len(parts) >= 2:
                    for part in parts[1:]:
                        if part.startswith("_") and not part.startswith("__"):
                            rel = str(py_file.relative_to(repo))
                            file_pkg = rel.split("/")[0] if "/" in rel else ""
                            import_pkg = parts[0]
                            if file_pkg != import_pkg:
                                private_imports.append((node.module, rel))
                            break

    if not private_imports:
        return []

    return [ProbeFinding(
        title=f"{len(private_imports)} cross-module private import(s) detected",
        evidence=[f"{imp} in {f}" for imp, f in private_imports[:10]],
        blast_radius="low",
        suggested_injections=["Use public API instead of private imports"],
        file_paths=list(set(f for _, f in private_imports))[:5],
    )]


def _check_external_file_reads(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check 4: File reads to paths outside the module's directory."""
    external_reads: list[tuple[str, str]] = []

    for py_file in py_files[:30]:
        try:
            source = py_file.read_text(errors="replace")
        except OSError:
            continue

        for m in _FILE_READ_RE.finditer(source):
            path_arg = m.group(1)
            if not path_arg:
                continue
            if path_arg.startswith("/") and not path_arg.startswith(str(repo)):
                if any(path_arg.startswith(p) for p in ("/tmp", "/dev", "/proc", "/var/log")):
                    continue
                external_reads.append((path_arg, str(py_file.relative_to(repo))))

    if not external_reads:
        return []

    return [ProbeFinding(
        title=f"{len(external_reads)} external file path(s) hardcoded",
        evidence=[f"{path} in {f}" for path, f in external_reads[:10]],
        blast_radius="low",
        suggested_injections=["Use config or env vars for external paths"],
        file_paths=list(set(f for _, f in external_reads))[:5],
    )]


def probe_hidden_dependency(context: dict) -> ProbeResult:
    """Comprehensive hidden dependency detection.

    Context keys:
        repo_root (str): Path to repository root.
        changed_files (list[str]): Files to scan (optional, scans all .py if empty).
        scan_limit (int): Max files to scan (default 50).
    """
    repo = Path(context.get("repo_root", ".")).resolve()
    if not repo.is_dir():
        return ProbeResult.error("hidden_dependency", "structure", f"Repo path not found: {repo}")

    py_files = _collect_py_files(repo)
    if not py_files:
        return ProbeResult(
            probe_id="hidden_dependency",
            category="structure",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    declared = _get_declared_deps(repo)
    local_packages = _get_local_packages(repo)
    all_findings: list[ProbeFinding] = []

    all_findings.extend(_check_undeclared_imports(repo, py_files, declared, local_packages))
    all_findings.extend(_check_subprocess_binaries(repo, py_files))
    all_findings.extend(_check_cross_module_imports(repo, py_files))
    all_findings.extend(_check_external_file_reads(repo, py_files))

    if not all_findings:
        return ProbeResult(
            probe_id="hidden_dependency",
            category="structure",
            status="ok",
            confidence=0.85,
            severity="info",
        )

    severity = "high" if len(all_findings) >= 5 else ("medium" if len(all_findings) >= 2 else "low")

    return ProbeResult(
        probe_id="hidden_dependency",
        category="structure",
        status="warning",
        confidence=0.75,
        severity=severity,
        findings=all_findings,
    )
