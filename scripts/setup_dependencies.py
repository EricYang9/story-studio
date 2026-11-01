"""Check or install stage-specific, user-local Story Studio dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_runtime import (ROOT, data_dir, choose_drafts_root, find_executable,
                            find_skill, load_config, save_config, prepare_tool_paths,
                            tool_command)

DRAFT_PACKAGE = "pyJianYingDraft==0.3.1"
COLI_PACKAGE = "@marswave/coli@0.0.20"
TTS_PACKAGE = "websockets==15.0.1"
IMAGEGEN_COMMIT = "49f948faa9258a0c61caceaf225e179651397431"
FEATURES = {"write", "storyboard", "images", "tts", "asr", "draft"}
DEPENDENCIES = ROOT / "scripts/dependencies"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("cwd", ROOT)
    return subprocess.run(tool_command(command[0], *command[1:]), check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600, **kwargs)


def requirement_files(features: set[str]) -> list[Path]:
    files = [DEPENDENCIES / f"requirements-{name}.lock" for name in ("draft", "tts") if name in features]
    if any(not path.is_file() for path in files):
        raise FileNotFoundError("The reviewed Python dependency lock is missing")
    return files


def python_packages(features: set[str]) -> list[str]:
    packages = []
    for path in requirement_files(features):
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r'([A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+)(?: ; sys_platform == "(win32)")? \\', line)
            if match and (not match[2] or sys.platform == match[2]):
                packages.append(match[1])
                count += 1
        if not count:
            raise ValueError("The Python dependency lock contains no applicable pinned requirements")
    return packages


def version_check(features: set[str]) -> str:
    expected = dict(item.split("==", 1) for item in python_packages(features))
    return ("from importlib.metadata import version;"
            f"assert all(version(n)==v for n,v in {expected!r}.items()),'Locked dependency versions differ';")


def install_coli(npm: str) -> str:
    """Install the reviewed npm closure without lifecycle scripts or global writes."""
    source = DEPENDENCIES / "coli"
    lock_bytes = (source / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    if lock.get("lockfileVersion") != 3 or lock.get("packages", {}).get("", {}).get("dependencies") != {"@marswave/coli": "0.0.20"}:
        raise ValueError("Unexpected Coli dependency lock")
    for name, package in lock["packages"].items():
        if name and (not package.get("resolved", "").startswith("https://registry.npmjs.org/") or not re.fullmatch(r"sha512-[A-Za-z0-9+/]+=*", package.get("integrity", ""))):
            raise ValueError("Every npm dependency needs a registry URL and integrity hash")
    prefix = data_dir() / "npm" / hashlib.sha256(lock_bytes).hexdigest()[:16]
    binary = prefix / "node_modules/.bin" / ("coli.cmd" if os.name == "nt" else "coli")
    if not binary.is_file():
        prefix.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "package.json", prefix / "package.json")
        (prefix / "package-lock.json").write_bytes(lock_bytes)
        run([npm, "ci", "--prefix", str(prefix), "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"])
    if not binary.is_file():
        raise RuntimeError("Locked Coli installation did not produce its CLI executable")
    return str(binary)


def install_skill(repo: str, ref: str, subdir: str, destination: Path) -> None:
    """Fetch an explicit revision into staging; never overwrite an existing skill."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Repository must be a public GitHub owner/repo, without credentials")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", ref):
        raise ValueError("External skills require a full immutable Git commit SHA")
    if destination.exists():
        raise FileExistsError("Dependency destination exists; choose a different directory or reuse it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".story-studio-install-", dir=destination.parent) as temporary:
        checkout = Path(temporary) / "checkout"
        checkout.mkdir()
        if shutil.which("git"):
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            run(["git", "init", str(checkout)], env=env)
            run(["git", "-C", str(checkout), "remote", "add", "origin", f"https://github.com/{repo}.git"], env=env)
            run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", ref], env=env)
            run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], env=env)
        else:
            from bootstrap_environment import safe_extract
            url = f"https://codeload.github.com/{repo}/zip/{quote(ref, safe='')}"
            with urllib.request.urlopen(url, timeout=120) as response:
                payload = response.read(256 * 1024 * 1024 + 1)
            if len(payload) > 256 * 1024 * 1024:
                raise ValueError("Skill archive exceeds size limit")
            archive = Path(temporary) / "dependency.zip"
            archive.write_bytes(payload)
            safe_extract(archive, checkout)
            children = list(checkout.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                raise ValueError("Unexpected skill archive layout")
            checkout = children[0]
        checkout = checkout.resolve()
        source = (checkout / subdir).resolve()
        if not source.is_relative_to(checkout) or not (source / "SKILL.md").is_file():
            raise ValueError("Downloaded dependency has no SKILL.md at the selected path")
        # Preserve upstream LICENSE and vendor licenses; exclude repository and local runtime data.
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", ".env", ".env.*", ".venv"
        ))
        for license_name in ("LICENSE", "NOTICE"):
            if (checkout / license_name).is_file() and not (destination / license_name).exists():
                shutil.copy2(checkout / license_name, destination / license_name)


def inspect(features: set[str], python: Path,
            coli: str | None, imagegen: Path | None) -> dict:
    rows = []

    def add(name: str, required: bool, ready: bool, detail: str) -> None:
        rows.append({"dependency": name, "required": required, "ready": ready, "detail": detail})

    add("Python", True, sys.version_info >= (3, 10), "Python 3.10+; examples prefer 3.11")
    if features & {"tts", "asr", "draft"}:
        for executable in ("ffmpeg", "ffprobe"):
            location = shutil.which(executable)
            ready = False
            if location:
                try:
                    run([location, "-version"])
                    ready = True
                except (OSError, subprocess.SubprocessError):
                    pass
            add(executable, True, ready, "Executable must run; bootstrap installs a private tool if missing")
    if "asr" in features:
        ready = False
        if coli:
            try:
                help_result = run([coli, "--help"])
                ready = bool(re.search(r"\basr\b", help_result.stdout or ""))
            except (OSError, subprocess.SubprocessError):
                pass
        add("Coli", True, ready, "Local ASR CLI must run; first transcription may download the model")
    if "images" in features:
        add("imagegen", False, bool(imagegen), "Optional helper; a working host image tool/provider is still required")
    if "tts" in features:
        try:
            run([str(python), "-I", "-X", "utf8", "-B", "-c", version_check({"tts"}) + "from websockets.asyncio.client import connect"])
            add("TTS Python imports", True, True, "websockets 15.0.1; no speech request sent")
        except (OSError, subprocess.SubprocessError):
            add("TTS Python imports", True, False, "Install the pinned WebSocket client in the managed environment")
    if "draft" in features:
        try:
            choose_drafts_root()
            add("draft directory", True, True, "Configured directory or one existing standard directory found; confirm in Jianying settings")
        except RuntimeError:
            add("draft directory", True, False, "Set --drafts-root during install or provide JY_PROJECTS_ROOT")
        if not python.is_file():
            add("draft Python imports", True, False, "Python environment is missing")
        else:
            code = version_check({"draft"}) + (
                "import PIL,numpy,pyJianYingDraft as d;"
                "from importlib.metadata import version,distribution;from pathlib import Path;"
                "assert version('pyJianYingDraft')=='0.3.1','Unsupported package version';"
                "assert Path(d.__file__).resolve().is_relative_to(Path(distribution('pyJianYingDraft').locate_file('pyJianYingDraft')).resolve()),'Unexpected module source';"
                "from pyJianYingDraft import DraftFolder,TrackSpec,VideoSegment,AudioSegment,TextSegment;"
                "from pymediainfo import MediaInfo;assert MediaInfo.can_parse(),'System libmediainfo is missing'"
            )
            try:
                run([str(python), "-I", "-X", "utf8", "-B", "-c", code], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                add("draft Python imports", True, True, "Upstream pyJianYingDraft 0.3.1, media libraries and libmediainfo are available")
            except subprocess.CalledProcessError as exc:
                # Do not print machine paths, upstream config or arbitrary subprocess output.
                error = re.search(r"ModuleNotFoundError: No module named ['\"]([A-Za-z0-9_.-]+)['\"]", exc.stderr or "")
                assertion = re.search(r"AssertionError: ([A-Za-z0-9 ._-]+)", exc.stderr or "")
                detail = f"Missing Python module: {error.group(1)}" if error else assertion.group(1) if assertion else "Dependency import failed; inspect the selected environment"
                add("draft Python imports", True, False, detail)
    voice_configuration_required = []
    if "tts" in features:
        from voice_settings import api_key_present, load_profile
        if not api_key_present():
            voice_configuration_required.append("Doubao speech API key (secure input)")
        try:
            load_profile()
        except (OSError, ValueError):
            voice_configuration_required.append("speaker ID and matching model resource_id")
    return {"ready": all(row["ready"] for row in rows if row["required"]),
            "features": sorted(features), "checks": rows,
            "voice_configuration_required": voice_configuration_required,
            "tts_service_verified": False,
            "image_generation_verified": False,
            "note": "Checks do not generate images, download ASR models, open Jianying, or verify a real project."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--features", default="draft,asr", help="Comma-separated write,storyboard,images,tts,asr,draft")
    parser.add_argument("--skills-dir", type=Path, help="Directory in which to install missing external skills")
    parser.add_argument("--with-imagegen", action="store_true", help="Install OpenAI's imagegen instructions if missing; does not enable an image tool")
    parser.add_argument("--drafts-root", type=Path, help="Save the current user's chosen Jianying draft location outside the skill")
    args = parser.parse_args()
    features = {item.strip() for item in args.features.split(",") if item.strip()}
    if not features or features - FEATURES:
        parser.error("Unknown or empty --features")
    if args.with_imagegen:
        features.add("images")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10+ is required")
    try:
        config = load_config() if features & {"tts", "draft", "asr"} else {}
        original_config = dict(config)
        config = {key: value for key, value in config.items() if key in {"python", "coli_bin", "drafts_root", "tool_dirs"}}
        if features & {"tts", "draft", "asr"}:
            prepare_tool_paths()
        configuration_saved = False
        imagegen = find_skill("imagegen") if "images" in features else None
        coli = find_executable("coli") if "asr" in features else None
        python = Path(config.get("python") or sys.executable)
        if args.action == "install":
            if data_dir().expanduser().resolve().is_relative_to(ROOT):
                raise ValueError("Dependency runtime data must stay outside the skill directory")
            destination = (args.skills_dir.expanduser().resolve() if args.skills_dir else
                           ROOT.parent if ROOT.parent.name == "skills" else Path.home() / ".agents/skills")
            if args.with_imagegen and imagegen is None:
                target = destination / "imagegen"
                install_skill("openai/skills", IMAGEGEN_COMMIT, "skills/.system/imagegen", target)
                imagegen = target
            packages = python_packages(features)
            if packages:
                # Reuse a working interpreter; otherwise create an isolated environment.
                current = inspect(features & {"draft", "tts"}, python, coli, imagegen)
                imports_ready = all(row["ready"] for row in current["checks"] if row["dependency"].endswith("Python imports"))
                if not imports_ready:
                    venv = data_dir() / "venv"
                    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    if not python.is_file():
                        run([sys.executable, "-I", "-m", "venv", str(venv)])
                    requirements = [argument for path in requirement_files(features) for argument in ("-r", str(path))]
                    run([str(python), "-I", "-m", "pip", "--isolated", "install", "--require-hashes", "--only-binary=:all:", "--no-input", "--index-url", "https://pypi.org/simple", *requirements])
                config["python"] = str(python.absolute())
            if "asr" in features:
                node, npm = shutil.which("node"), shutil.which("npm.cmd") or shutil.which("npm")
                if not node or not npm:
                    raise RuntimeError("Node.js 20+ and npm are required to install Coli")
                version = run([node, "--version"]).stdout.strip()
                if int(version.lstrip("v").split(".")[0]) < 20:
                    raise RuntimeError("Coli requires Node.js 20+ for this dependency profile")
                coli = install_coli(npm)
            if coli:
                config["coli_bin"] = coli
            if args.drafts_root:
                config["drafts_root"] = str(args.drafts_root.expanduser().resolve())
            if config != original_config:
                save_config(config)
                configuration_saved = True
        report = inspect(features, python, coli, imagegen)
        report["configuration_saved"] = configuration_saved
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Keep logs shareable: no command lines or user-local paths in the report.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError)) else "Dependency setup failed; check network access, permissions and system prerequisites"
        print(json.dumps({"ready": False, "error": message, "automatic_retry": False}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
