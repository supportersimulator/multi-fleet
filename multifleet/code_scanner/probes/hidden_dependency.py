"""Hidden Dependency Probe -- detects undeclared coupling between modules.

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

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.hidden_dependency")

# Standard library modules (Python 3.10+) -- not exhaustive but covers common ones
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


def _get_declared_deps(repo: Path) -> set[str]:
    """Extract declared dependencies from requirements.txt and pyproject.toml."""
    deps: set[str] = set()

    # requirements.txt
    for req_file in repo.glob("requirements*.txt"):
        try:
            for line in req_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # Extract package name (before version specifier)
                pkg = re.split(r"[><=!~\[]", line)[0].strip().lower()
                if pkg:
                    # Normalize: dashes to underscores
                    deps.add(pkg.replace("-", "_"))
        except OSError:
            continue

    # pyproject.toml (basic parsing)
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(errors="replace")
            # Match dependencies = ["pkg", ...] or requires = ["pkg", ...]
            for m in re.finditer(r'["\']([\w\-]+)', text):
                deps.add(m.group(1).lower().replace("-", "_"))
        except OSError:
            pass

    return deps


def _check_undeclared_imports(repo: Path, py_files: list[Path],
                               declared: set[str]) -> list[Finding]:
    """Check 1: Python imports not in declared dependencies."""
    findings = []
    undeclared: dict[str, list[str]] = {}  # package -> [files using it]

    for py_file in py_files[:50]:  # Cap scan
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0].lower()
                    if top not in _STDLIB and top not in declared:
                        # Check if it's a local module
                        if not (repo / top).exists() and not (repo / f"{top}.py").exists():
                            if not (repo / "src" / top).exists():
                                undeclared.setdefault(top, []).append(
                                    str(py_file.relative_to(repo))
                                )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0].lower()
                    if top not in _STDLIB and top not in declared:
                        if not (repo / top).exists() and not (repo / f"{top}.py").exists():
                            if not (repo / "src" / top).exists():
                                undeclared.setdefault(top, []).append(
                                    str(py_file.relative_to(repo))
                                )

    for pkg, files in undeclared.items():
        findings.append(Finding(
            title=f"Undeclared dependency: {pkg}",
            summary=f"Package '{pkg}' imported in {len(files)} file(s) but not in requirements",
            severity="medium",
            confidence=0.7,
            blast_radius="medium" if len(files) > 3 else "low",
            evidence_refs=[f"Used in: {f}" for f in files[:5]],
            suggested_actions=[f"Add {pkg} to requirements.txt or verify it's a local module"],
            file_path=files[0] if files else "",
        ))

    return findings


def _check_subprocess_binaries(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 2: Subprocess calls to undocumented binaries."""
    findings = []
    # Common safe binaries
    safe_bins = frozenset({
        "python", "python3", "pip", "pip3", "git", "bash", "sh", "zsh",
        "echo", "cat", "ls", "mkdir", "cp", "mv", "rm", "grep", "find",
        "sed", "awk", "sort", "uniq", "wc", "head", "tail", "tee",
        "curl", "wget", "ssh", "scp", "rsync", "tar", "gzip",
        "docker", "npm", "node", "make", "which", "env", "test",
    })

    binaries_found: dict[str, list[str]] = {}

    for py_file in py_files[:50]:
        try:
            source = py_file.read_text(errors="replace")
        except OSError:
            continue
        for m in _SUBPROCESS_RE.finditer(source):
            binary = m.group(1) or m.group(2)
            if binary and binary not in safe_bins:
                # Strip path prefixes
                binary = binary.split("/")[-1]
                if binary and binary not in safe_bins:
                    binaries_found.setdefault(binary, []).append(
                        str(py_file.relative_to(repo))
                    )

    for binary, files in binaries_found.items():
        findings.append(Finding(
            title=f"Subprocess calls undocumented binary: {binary}",
            summary=f"Code shells out to '{binary}' -- ensure it's installed on all nodes",
            severity="low",
            confidence=0.6,
            blast_radius="low",
            evidence_refs=[f"Called from: {f}" for f in files[:5]],
            suggested_actions=[f"Document {binary} as a system dependency"],
            file_path=files[0] if files else "",
        ))

    return findings


def _check_cross_module_imports(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 3: Imports that reach into private module internals."""
    findings = []
    private_imports: list[tuple[str, str]] = []  # (import_path, file)

    for py_file in py_files[:50]:
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                # Detect importing from _private submodules of other packages
                if len(parts) >= 2:
                    for part in parts[1:]:
                        if part.startswith("_") and not part.startswith("__"):
                            # Importing from a private submodule
                            rel = str(py_file.relative_to(repo))
                            # Skip if same package
                            file_pkg = rel.split("/")[0] if "/" in rel else ""
                            import_pkg = parts[0]
                            if file_pkg != import_pkg:
                                private_imports.append((node.module, rel))
                            break

    if private_imports:
        findings.append(Finding(
            title=f"{len(private_imports)} cross-module private import(s) detected",
            summary="Code imports private (_prefixed) submodules from other packages",
            severity="low",
            confidence=0.8,
            blast_radius="low",
            evidence_refs=[f"{imp} in {f}" for imp, f in private_imports[:10]],
            suggested_actions=["Use public API instead of private imports"],
        ))

    return findings


def _check_external_file_reads(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 4: File reads to paths outside the module's directory."""
    findings = []
    _FILE_READ_RE = re.compile(
        r"""(?:open|Path)\s*\(\s*['"]([^'"]+)['"]|"""
        r"""\.read_text\s*\(|\.read_bytes\s*\(""",
    )
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
            # Check if it's an absolute path outside the repo
            if path_arg.startswith("/") and not path_arg.startswith(str(repo)):
                # Skip common system paths
                if any(path_arg.startswith(p) for p in
                       ("/tmp", "/dev", "/proc", "/var/log")):
                    continue
                external_reads.append((path_arg, str(py_file.relative_to(repo))))

    if external_reads:
        findings.append(Finding(
            title=f"{len(external_reads)} external file path(s) hardcoded",
            summary="Code reads files from absolute paths outside the repo",
            severity="low",
            confidence=0.6,
            blast_radius="low",
            evidence_refs=[f"{path} in {f}" for path, f in external_reads[:10]],
            suggested_actions=["Use config or env vars for external paths"],
        ))

    return findings


def _collect_py_files(repo: Path) -> list[Path]:
    """Collect Python files, skipping venvs and caches."""
    py_files = []
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", "dist", "build"}
    for py_file in repo.rglob("*.py"):
        if any(p in py_file.parts for p in skip):
            continue
        py_files.append(py_file)
        if len(py_files) >= 200:  # Hard cap
            break
    return py_files


def probe_hidden_dependency(context: ProbeContext) -> ProbeResult:
    """Detect undeclared coupling between modules."""
    repo = Path(context.repo_path).resolve()
    if not repo.is_dir():
        return ProbeResult.error("hidden_dependency", f"Repo path not found: {repo}")

    py_files = _collect_py_files(repo)
    if not py_files:
        return ProbeResult.skipped("hidden_dependency", ["no .py files found"])

    declared = _get_declared_deps(repo)
    all_findings: list[Finding] = []

    all_findings.extend(_check_undeclared_imports(repo, py_files, declared))
    all_findings.extend(_check_subprocess_binaries(repo, py_files))
    all_findings.extend(_check_cross_module_imports(repo, py_files))
    all_findings.extend(_check_external_file_reads(repo, py_files))

    if not all_findings:
        return ProbeResult.ok("hidden_dependency", confidence=0.85)

    return ProbeResult(
        probe_id="hidden_dependency",
        status="warn",
        confidence=0.7,
        findings=all_findings,
    )


def register(engine) -> None:
    """Auto-discovery hook."""
    engine.register(
        probe_id="hidden_dependency",
        probe_fn=probe_hidden_dependency,
        category="structure",
        cost="medium",  # AST parsing + file scanning
    )
