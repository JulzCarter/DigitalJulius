"""Autonomous dependency installation.

When the user asks DigitalJulius to wire up a new LLM provider whose Python
SDK isn't installed, we offer to `pip install --user <pkg>` for them. To
keep the attack surface narrow, only an allowlisted set of known SDKs can be
installed this way.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TypedDict


class PipAllowlistEntry(TypedDict):
    package: str
    import_module: str

# Pure-PyPI SDKs we'll install on the user's behalf. Anything not here must
# be installed manually.
PIP_ALLOWLIST: dict[str, str | PipAllowlistEntry] = {
    # provider name → pip package + import module
    "anthropic": {"package": "anthropic", "import_module": "anthropic"},
    "openai": {"package": "openai", "import_module": "openai"},
    "openrouter": {"package": "openai", "import_module": "openai"},  # OpenAI-compatible
    "groq": {"package": "groq", "import_module": "groq"},
    "mistralai": {"package": "mistralai", "import_module": "mistralai"},
    "cohere": {"package": "cohere", "import_module": "cohere"},
    "deepseek": {"package": "openai", "import_module": "openai"},  # OpenAI-compatible
    "together": {"package": "openai", "import_module": "openai"},
    "fireworks": {"package": "openai", "import_module": "openai"},
    "cerebras": {"package": "openai", "import_module": "openai"},
    "ollama": {"package": "ollama", "import_module": "ollama"},
    "google-genai": {"package": "google-genai", "import_module": "google.genai"},
}

NPM_ALLOWLIST: dict[str, str] = {
    # cli name → npm package
    "gemini":    "@google/gemini-cli",
    "claude":    "@anthropic-ai/claude-code",
    "opencode":  "@opencode/cli",
}


@dataclass
class InstallResult:
    ok: bool
    package: str
    via: str           # "pip" or "npm" or "skip"
    output: str = ""


def _pip_package_and_import(entry: str | PipAllowlistEntry) -> tuple[str, str]:
    if isinstance(entry, str):
        return entry, entry
    return entry["package"], entry["import_module"]


def is_pip_pkg_available(pkg_or_import: str | PipAllowlistEntry) -> bool:
    """Cheap availability check: look for the configured import module."""
    _package, import_module = _pip_package_and_import(pkg_or_import)
    try:
        return importlib.util.find_spec(import_module) is not None
    except Exception:
        return False


def ensure_pip_pkg(
    provider_name: str,
    on_log=None,
    confirm=None,
) -> InstallResult:
    """Make sure the Python SDK backing `provider_name` is importable.

    on_log(line) is called with each line of pip output.
    confirm(pkg) returns True to proceed with install, False to abort.
    """
    log = on_log or (lambda _l: None)
    entry = PIP_ALLOWLIST.get(provider_name)
    if entry is None:
        return InstallResult(ok=False, package=provider_name, via="skip",
                             output="provider not in pip allowlist; install manually")
    pkg, _import_module = _pip_package_and_import(entry)
    if is_pip_pkg_available(entry):
        return InstallResult(ok=True, package=pkg, via="skip", output="already installed")

    if confirm and not confirm(pkg):
        return InstallResult(ok=False, package=pkg, via="pip",
                             output="user declined install")

    log(f"installing {pkg} via pip (user site)...")
    argv = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", pkg]
    return _stream_subprocess(argv, "pip", pkg, log)


def ensure_npm_pkg(
    cli_name: str,
    on_log=None,
    confirm=None,
) -> InstallResult:
    """Install a Node CLI globally if it's not on PATH yet."""
    log = on_log or (lambda _l: None)
    pkg = NPM_ALLOWLIST.get(cli_name)
    if pkg is None:
        return InstallResult(ok=False, package=cli_name, via="skip",
                             output="CLI not in npm allowlist; install manually")
    if shutil.which(cli_name) is not None:
        return InstallResult(ok=True, package=pkg, via="skip", output="already on PATH")
    if shutil.which("npm") is None:
        return InstallResult(ok=False, package=pkg, via="npm",
                             output="npm not found — install Node first")

    if confirm and not confirm(pkg):
        return InstallResult(ok=False, package=pkg, via="npm",
                             output="user declined install")

    log(f"installing {pkg} via npm global...")
    argv = ["npm", "install", "-g", pkg]
    return _stream_subprocess(argv, "npm", pkg, log)


def _stream_subprocess(argv: list, via: str, pkg: str, log) -> InstallResult:
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        return InstallResult(ok=False, package=pkg, via=via, output=str(e))

    captured = []
    for line in proc.stdout or []:
        line = line.rstrip("\n")
        captured.append(line)
        log(line)
    rc = proc.wait()
    return InstallResult(
        ok=(rc == 0),
        package=pkg,
        via=via,
        output="\n".join(captured[-20:]),
    )
