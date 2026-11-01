"""Discover user-local dependencies without storing machine paths in the skill."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "story-studio"


def config_path() -> Path:
    override = os.environ.get("STORY_STUDIO_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (data_dir() / "runtime.json").expanduser().resolve()


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Story Studio runtime config must be a JSON object")
    return value


def save_config(value: dict) -> None:
    path = config_path()
    if path.is_relative_to(ROOT):
        raise ValueError("Runtime configuration must stay outside the skill directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def skill_dirs() -> list[Path]:
    result = [ROOT.parent]
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        result.append(Path(codex_home).expanduser() / "skills")
    result.extend(Path.home() / part for part in (
        ".agents/skills", ".codex/skills", ".cursor/skills", ".claude/skills"
    ))
    return list(dict.fromkeys(p.resolve() for p in result))


def find_skill(name: str) -> Path | None:
    for base in skill_dirs():
        for path in (base / name, base / ".system" / name):
            if (path / "SKILL.md").is_file():
                return path
    return None


def prepare_tool_paths() -> None:
    """Use managed executables in this process, without changing system PATH."""
    entries = load_config().get("tool_dirs", [])
    if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
        raise ValueError("tool_dirs must be a list of directory paths")
    directories = [str(Path(item).expanduser().resolve()) for item in entries if Path(item).expanduser().is_dir()]
    if directories:
        current = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(directories + current))


def jianying_configured_root() -> Path | None:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return None
    path = Path(os.environ["LOCALAPPDATA"]) / "JianyingPro/User Data/Config/globalSetting"
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        return None
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    match = re.search(r'(?m)^\s*currentCustomDraftPath\s*=\s*(.*?)\s*$', text)
    value = match.group(1).strip('"') if match else None
    if value is None:
        try:
            value = json.loads(text).get("currentCustomDraftPath")
        except (ValueError, AttributeError):
            return None
    if isinstance(value, str) and value and Path(value).is_absolute():
        return Path(value).expanduser().resolve()
    return None


def choose_drafts_root(explicit: str | None = None) -> Path:
    selected = explicit or os.environ.get("JY_PROJECTS_ROOT") or load_config().get("drafts_root")
    if selected:
        return Path(selected).expanduser().resolve()
    configured = jianying_configured_root()
    if configured:
        return configured
    candidates = []
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "JianyingPro/User Data/Projects/com.lveditor.draft")
    elif sys.platform == "darwin":
        candidates.extend([
            Path.home() / "Movies/JianyingPro/User Data/Projects/com.lveditor.draft",
            Path.home() / "Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro/User Data/Projects/com.lveditor.draft",
        ])
    existing = list(dict.fromkeys(p.resolve() for p in candidates if p.is_dir()))
    if len(existing) == 1:
        return existing[0]
    raise RuntimeError("Set --drafts-root or JY_PROJECTS_ROOT to the folder shown in Jianying settings")


def find_executable(name: str) -> str | None:
    prepare_tool_paths()
    if name == "coli":
        selected = os.environ.get("COLI_BIN") or load_config().get("coli_bin")
        if selected:
            path = Path(selected).expanduser()
            if not path.is_file():
                if os.environ.get("COLI_BIN"):
                    raise FileNotFoundError("Explicit COLI_BIN executable is missing; Agent must repair the override")
                return shutil.which("coli.cmd") or shutil.which("coli")
            return str(path.resolve())
    if os.name == "nt":
        return shutil.which(name + ".cmd") or shutil.which(name)
    return shutil.which(name)


def tool_command(executable: str, *arguments: str) -> list[str]:
    """Run npm CLIs through Node instead of exposing arguments to cmd.exe."""
    selected = shutil.which(executable) or executable
    shim = Path(selected).expanduser().absolute()
    if shim.suffix.lower() not in {".cmd", ".bat"}:
        return [executable, *arguments]
    packages = {"coli": "@marswave/coli", "npm": "npm"}
    name = shim.stem.lower()
    package_name = packages.get(name)
    if package_name is None:
        raise ValueError("Batch-file tools require a verified native or Node entry point")
    for package in (shim.parent / "node_modules" / package_name,
                    shim.parent.parent / package_name):
        manifest = package / "package.json"
        if not manifest.is_file():
            continue
        metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
        bins = metadata.get("bin", {})
        relative = bins.get(name) if isinstance(bins, dict) else bins
        if metadata.get("name") != package_name or not isinstance(relative, str):
            raise ValueError("Unexpected npm CLI package metadata")
        entry = (package / relative).resolve()
        if not entry.is_relative_to(package.resolve()) or not entry.is_file() or entry.suffix not in {".js", ".cjs", ".mjs"}:
            raise ValueError("The npm CLI entry point must remain inside its package")
        adjacent = shim.parent / "node.exe"
        node = str(adjacent) if adjacent.is_file() else shutil.which("node.exe") or shutil.which("node")
        if not node or Path(node).suffix.lower() in {".cmd", ".bat"}:
            raise RuntimeError("A native Node executable is required to run this CLI safely")
        return [str(Path(node).resolve()), str(entry), *arguments]
    raise RuntimeError("Could not locate the npm package behind the batch-file tool")


def activate_runtime() -> None:
    """Entry scripts can re-exec the interpreter configured by the installer."""
    prepare_tool_paths()
    config = load_config()
    target = config.get("python")
    if target:
        # Keep venv symlinks: resolving them would select the base interpreter.
        executable = Path(target).expanduser().absolute()
        if not executable.is_file():
            raise FileNotFoundError("Managed Python environment is missing; run dependency setup")
        if os.path.normcase(str(executable)) != os.path.normcase(os.path.abspath(sys.executable)):
            if os.environ.get("STORY_STUDIO_REEXEC") == "1":
                raise RuntimeError("Managed Python could not be activated")
            env = dict(os.environ, STORY_STUDIO_REEXEC="1", PYTHONUTF8="1")
            # A child interpreter preserves Windows DLL loading and pipe handles.
            result = subprocess.run([str(executable), "-I", "-X", "utf8", *sys.argv], env=env)
            raise SystemExit(result.returncode)
    if config.get("drafts_root") and not os.environ.get("JY_PROJECTS_ROOT"):
        os.environ["JY_PROJECTS_ROOT"] = config["drafts_root"]
