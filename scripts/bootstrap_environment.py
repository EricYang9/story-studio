"""Provision missing tools after the native bootstrap has supplied Python."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_runtime import (ROOT, data_dir, load_config, prepare_tool_paths,
                            choose_drafts_root, find_executable, save_config,
                            tool_command)


def request(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Dependency downloads require HTTPS")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "story-studio-bootstrap"}), timeout=120) as response:
        return response.read(1024 * 1024 * 1024 + 1)


def checked_download(url: str, digest: str, path: Path) -> Path:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("Missing or invalid SHA-256 checksum")
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest().lower() == digest.lower():
        return path
    payload = request(url)
    if len(payload) > 1024 * 1024 * 1024:
        raise ValueError("Dependency archive exceeds the size limit")
    if hashlib.sha256(payload).hexdigest().lower() != digest.lower():
        raise ValueError("Dependency checksum mismatch; nothing was executed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def safe_extract(archive: Path, destination: Path) -> None:
    base = destination.resolve()

    def target(name: str) -> Path:
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("Absolute archive member")
        result = (base / normalized).resolve()
        if not result.is_relative_to(base):
            raise ValueError("Archive member escapes extraction directory")
        return result

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if sum(x.file_size for x in entries) > 1024**3 or len(entries) > 30000:
                raise ValueError("Archive size limit exceeded")
            for item in entries:
                target(item.filename)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("ZIP symlinks are not supported")
            destination.mkdir(parents=True, exist_ok=True)
            source.extractall(destination)
    else:
        with tarfile.open(archive) as source:
            entries = source.getmembers()
            if sum(x.size for x in entries) > 1024**3 or len(entries) > 30000:
                raise ValueError("Archive size limit exceeded")
            regular_files = {target(item.name) for item in entries if item.isfile()}
            link_paths = {target(item.name) for item in entries if item.issym()}
            for item in entries:
                item_path = target(item.name)
                if item.islnk() or item.isdev():
                    raise ValueError("Unsupported archive member type")
                if any(parent in link_paths for parent in item_path.parents):
                    raise ValueError("Archive member traverses a symlink")
                if item.issym():
                    # Official Node archives use file symlinks for npm/npx.
                    # Restrict links to regular members; reject chains and directory pivots,
                    # including on Python versions without tarfile.data_filter.
                    link = item.linkname.replace("\\", "/")
                    linked = (item_path.parent / link).resolve()
                    if link.startswith("/") or re.match(r"^[A-Za-z]:", link) or not linked.is_relative_to(base) or linked not in regular_files:
                        raise ValueError("Archive symlink must target a regular file inside the archive")
            destination.mkdir(parents=True, exist_ok=True)
            if hasattr(tarfile, "data_filter"):
                source.extractall(destination, filter="data")
            else:
                source.extractall(destination)


def native(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(tool_command(command[0], *command[1:]), cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def node_ready() -> bool:
    node = find_executable("node")
    if not node or not find_executable("npm"):
        return False
    result = native([node, "--version"], 20)
    match = re.match(r"v(\d+)\.", result.stdout.strip())
    return result.returncode == 0 and bool(match) and int(match[1]) >= 20


def install_node() -> Path:
    os_id = {"Windows": "win", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not os_id or not arch:
        raise RuntimeError("This platform needs an Agent-selected official Node.js installer")
    versions = json.loads(request("https://nodejs.org/dist/index.json"))
    selected = next(row for row in versions if row.get("lts") and int(row["version"][1:].split(".")[0]) >= 20)
    version = selected["version"]
    extension = "zip" if os_id == "win" else "tar.gz"
    filename = f"node-{version}-{os_id}-{arch}.{extension}"
    prefix = f"https://nodejs.org/dist/{version}/"
    checksums = request(prefix + "SHASUMS256.txt").decode()
    checksum = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename)
    archive = checked_download(prefix + filename, checksum, data_dir() / "downloads" / filename)
    destination = data_dir() / "tools/node" / version
    safe_extract(archive, destination)
    binary = destination / f"node-{version}-{os_id}-{arch}"
    return binary if os_id == "win" else binary / "bin"


def install_windows_ffmpeg() -> Path:
    prefix = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    checksum = request(prefix + ".sha256").decode().strip().split()[0]
    archive = checked_download(prefix, checksum, data_dir() / "downloads/ffmpeg-essentials.zip")
    destination = data_dir() / "tools/ffmpeg" / checksum[:16]
    safe_extract(archive, destination)
    binary = next(destination.rglob("ffmpeg.exe"))
    if not binary.with_name("ffprobe.exe").is_file():
        raise RuntimeError("FFmpeg download did not contain ffprobe")
    return binary.parent


def install_posix_media() -> None:
    packages = []
    if shutil.which("brew"):
        packages = ["brew", "install", "ffmpeg", "libmediainfo"]
    elif shutil.which("apt-get"):
        prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo", "-n"]
        if prefix and not shutil.which("sudo"):
            raise RuntimeError("Agent needs system-package installation permission")
        update = native(prefix + ["apt-get", "update"])
        if update.returncode:
            raise RuntimeError("Agent must obtain system-package permission before continuing")
        packages = prefix + ["apt-get", "install", "-y", "ffmpeg", "libmediainfo0v5"]
    if not packages:
        raise RuntimeError("Agent must install the platform package manager or use an official media installer, then retry; do not ask the user to run commands")
    if native(packages).returncode:
        raise RuntimeError("System package installation needs Agent review or OS authorization")


def jianying_present() -> bool:
    if os.name != "nt":
        return False
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro"
    return any(app.glob("Apps/*/JianyingPro.exe")) or (app / "JianyingPro.exe").is_file()


def install_jianying() -> None:
    if jianying_present():
        return
    winget = shutil.which("winget")
    if winget:
        result = native([winget, "install", "--id", "ByteDance.JianyingPro", "--exact", "--scope", "user", "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"])
        if result.returncode in (0, 3010):
            return
        raise RuntimeError("Jianying installation needs Agent review or an OS authorization prompt")
    base = "https://api.github.com/repos/microsoft/winget-pkgs/contents/manifests/b/ByteDance/JianyingPro"
    entries = json.loads(request(base))
    versions = [item["name"] for item in entries if re.fullmatch(r"\d+(?:\.\d+)+", item["name"])]
    version = max(versions, key=lambda x: tuple(map(int, x.split("."))))
    url = f"https://raw.githubusercontent.com/microsoft/winget-pkgs/master/manifests/b/ByteDance/JianyingPro/{version}/ByteDance.JianyingPro.installer.yaml"
    manifest = request(url).decode()
    if not re.search(r"(?mi)^Scope:\s*user\s*$", manifest):
        raise RuntimeError("Jianying installer requires a different scope; Agent must review it")
    block = re.search(r"(?ms)^- Architecture: x64\s*\n(.*?)(?=^- Architecture:|\Z)", manifest)
    if not block:
        raise RuntimeError("No supported Jianying desktop installer in the official package manifest")
    download = re.search(r"InstallerUrl:\s*(https://\S+)", block[1])
    checksum = re.search(r"InstallerSha256:\s*([A-Fa-f0-9]{64})", block[1])
    if not download or not checksum or not re.search(r"Silent:\s*/silent_install=1", manifest):
        raise RuntimeError("Jianying installer format changed; Agent must review the official manifest")
    installer = checked_download(download[1], checksum[1], data_dir() / "downloads/JianyingSetup.exe")
    result = native([str(installer), "/silent_install=1"])
    if result.returncode not in (0, 3010):
        raise RuntimeError("Jianying installer needs OS authorization or Agent review")


def configure_drafts(explicit: str | None) -> Path:
    try:
        selected = choose_drafts_root(explicit)
    except RuntimeError:
        if os.name == "nt":
            selected = Path(os.environ["LOCALAPPDATA"]) / "JianyingPro/User Data/Projects/com.lveditor.draft"
        else:
            selected = data_dir() / "drafts"
    selected.mkdir(parents=True, exist_ok=True)
    return selected.resolve()


def plan(features: set[str], install_app: bool) -> dict:
    from setup_dependencies import inspect
    from studio_runtime import find_skill
    python = Path(load_config().get("python") or sys.executable)
    report = inspect(features, python, find_executable("coli") if "asr" in features else None,
                     find_skill("imagegen") if "images" in features else None)
    missing = [row["dependency"] for row in report["checks"] if row["required"] and not row["ready"]]
    if "asr" in features and not node_ready():
        missing.append("Node.js LTS and npm")
    if install_app and "draft" in features and os.name == "nt" and not jianying_present():
        missing.append("Jianying desktop application")
    return {"features": sorted(features), "configuration_plan": missing, "ready": not missing,
            "voice_configuration_required": report.get("voice_configuration_required", []), "tts_service_verified": False,
            "agent_runs_installation": True, "manual_commands_required": False, "image_generation_verified": False,
            "note": "OS authorization, account login and image-tool access remain separate user actions when required."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--features", default="draft,asr,images,tts")
    parser.add_argument("--drafts-root")
    parser.add_argument("--with-imagegen", action="store_true")
    parser.add_argument("--install-jianying", action="store_true")
    args = parser.parse_args()
    features = set(args.features.split(","))
    if args.with_imagegen:
        features.add("images")
    if not features or features - {"write", "storyboard", "images", "tts", "asr", "draft"}:
        parser.error("Unknown --features")
    try:
        prepare_tool_paths()
        if args.action == "check":
            report = plan(features, args.install_jianying)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 1
        if data_dir().resolve().is_relative_to(ROOT):
            raise ValueError("Managed tools must remain outside the skill source")
        config = load_config()
        if not config.get("python") or not Path(config["python"]).is_file():
            config["python"] = str(Path(sys.executable).absolute())
            save_config(config)
        directories = list(config.get("tool_dirs", []))
        if "asr" in features and not node_ready():
            directories.append(str(install_node()))
        if features & {"tts", "asr", "draft"} and (not find_executable("ffmpeg") or not find_executable("ffprobe")):
            if os.name == "nt":
                directories.append(str(install_windows_ffmpeg()))
            else:
                install_posix_media()
        if directories:
            config["tool_dirs"] = list(dict.fromkeys(directories))
            save_config(config)
            prepare_tool_paths()
        if args.install_jianying and "draft" in features and os.name == "nt":
            install_jianying()
            if not jianying_present():
                raise RuntimeError("Installer completed but Jianying was not detected; Agent must inspect the installation before declaring it ready")
        command = [sys.executable, "-I", "-X", "utf8", "-B", str(ROOT / "scripts/setup_dependencies.py"), "install", "--features", ",".join(sorted(features))]
        if "draft" in features:
            command.extend(["--drafts-root", str(configure_drafts(args.drafts_root))])
        if args.with_imagegen:
            command.append("--with-imagegen")
        result = native(command)
        if result.returncode and os.name != "nt" and "libmediainfo is missing" in result.stdout:
            install_posix_media()
            result = native(command)
        if result.stdout:
            print(result.stdout)
        return result.returncode
    except (OSError, ValueError, RuntimeError, StopIteration, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Bootstrap failed; Agent should inspect network, platform support and permissions"
        print(json.dumps({"ready": False, "agent_action_required": message, "manual_commands_required": False, "automatic_retry": False}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
