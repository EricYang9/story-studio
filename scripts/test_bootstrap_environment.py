"""Cold-start simulations: no network, real software installs or live configuration writes."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import bootstrap_environment as bootstrap
import setup_dependencies as setup
import studio_runtime as runtime


class BootstrapTests(unittest.TestCase):
    def test_checksum_failure_never_writes_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "installer.exe"
            with patch.object(bootstrap, "request", return_value=b"changed payload"):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    bootstrap.checked_download("https://example.invalid/file", "0" * 64, target)
            self.assertFalse(target.exists())

    def test_valid_cache_does_not_download_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "archive.zip"
            target.write_bytes(b"verified")
            with patch.object(bootstrap, "request") as download:
                bootstrap.checked_download("https://example.invalid/file", hashlib.sha256(b"verified").hexdigest(), target)
                download.assert_not_called()

    def test_zip_traversal_rejected_before_any_file_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive.zip"
            destination = Path(temporary) / "tools"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("allowed/file", "allowed")
                output.writestr("../escape", "blocked")
            with self.assertRaises(ValueError):
                bootstrap.safe_extract(archive, destination)
            self.assertFalse(destination.exists())
            self.assertFalse((Path(temporary) / "escape").exists())

    def test_tar_links_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                entry = tarfile.TarInfo("alias")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "../outside"
                output.addfile(entry)
            with self.assertRaises(ValueError):
                bootstrap.safe_extract(archive, Path(temporary) / "tools")
            self.assertFalse((Path(temporary) / "tools").exists())

    @unittest.skipIf(os.name == "nt", "POSIX npm file symlinks")
    def test_node_style_tar_file_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "node.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                entry = tarfile.TarInfo("node/lib/npm.js")
                entry.size = 3
                output.addfile(entry, io.BytesIO(b"npm"))
                link = tarfile.TarInfo("node/bin/npm")
                link.type = tarfile.SYMTYPE
                link.linkname = "../lib/npm.js"
                output.addfile(link)
            destination = Path(temporary) / "tools"
            bootstrap.safe_extract(archive, destination)
            self.assertEqual((destination / "node/bin/npm").read_bytes(), b"npm")

    def test_node_requires_supported_version_and_npm(self):
        for version, npm, expected in (("v18.20.0", "npm", False), ("v24.0.0", None, False), ("v20.1.0", "npm", True)):
            with self.subTest(version=version, npm=npm), patch.object(bootstrap, "find_executable", side_effect=lambda name: "node" if name == "node" else npm), patch.object(bootstrap, "native", return_value=subprocess.CompletedProcess([], 0, version)):
                self.assertEqual(bootstrap.node_ready(), expected)

    def test_official_node_archive_is_verified_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            name = "node-v24.1.0-win-x64.zip"
            versions = json.dumps([{"version": "v25.0.0", "lts": False}, {"version": "v24.1.0", "lts": "fixture"}]).encode()
            def fetch(url):
                return versions if url.endswith("index.json") else ("a" * 64 + "  " + name).encode()
            with patch.object(bootstrap.platform, "system", return_value="Windows"), patch.object(bootstrap.platform, "machine", return_value="AMD64"), patch.object(bootstrap, "data_dir", return_value=data), patch.object(bootstrap, "request", side_effect=fetch), patch.object(bootstrap, "checked_download", return_value=data / name) as download, patch.object(bootstrap, "safe_extract"):
                binary = bootstrap.install_node()
                self.assertTrue(binary.is_relative_to(data / "tools"))
                self.assertEqual(download.call_args.args[1], "a" * 64)
                self.assertEqual(download.call_args.args[0], "https://nodejs.org/dist/v24.1.0/" + name)

    def test_ffmpeg_archive_must_also_include_ffprobe(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            def extract(archive, destination):
                destination.mkdir(parents=True)
                (destination / "ffmpeg.exe").touch()
            with patch.object(bootstrap, "data_dir", return_value=data), patch.object(bootstrap, "request", return_value=b"a" * 64), patch.object(bootstrap, "checked_download", return_value=data / "archive"), patch.object(bootstrap, "safe_extract", side_effect=extract):
                with self.assertRaisesRegex(RuntimeError, "ffprobe"):
                    bootstrap.install_windows_ffmpeg()

    def test_cold_plan_is_read_only_and_lists_missing_components(self):
        report = {"checks": [{"dependency": name, "required": True, "ready": False} for name in ("ffmpeg", "Coli", "draft Python imports")]}
        with patch.object(setup, "inspect", return_value=report), patch.object(bootstrap, "load_config", return_value={}), patch.object(bootstrap, "find_executable", return_value=None), patch.object(bootstrap, "node_ready", return_value=False), patch.object(bootstrap, "jianying_present", return_value=False), patch.object(bootstrap, "save_config") as save, patch.object(bootstrap, "request") as request:
            result = bootstrap.plan({"asr", "draft"}, True)
            self.assertFalse(result["ready"])
            self.assertIn("Node.js LTS and npm", result["configuration_plan"])
            self.assertFalse(result["manual_commands_required"])
            save.assert_not_called()
            request.assert_not_called()

    def test_write_only_does_not_install_media_or_app(self):
        with patch.object(sys, "argv", ["bootstrap_environment.py", "install", "--features", "write", "--install-jianying"]), patch.object(bootstrap, "prepare_tool_paths"), patch.object(bootstrap, "load_config", return_value={"python": sys.executable}), patch.object(bootstrap, "node_ready") as node, patch.object(bootstrap, "find_executable") as tool, patch.object(bootstrap, "install_jianying") as app, patch.object(bootstrap, "configure_drafts") as drafts, patch.object(bootstrap, "native", return_value=subprocess.CompletedProcess([], 0, '{"ready":true}')) as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(), 0)
            for unused in (node, tool, app, drafts):
                unused.assert_not_called()
            self.assertIn("write", execute.call_args.args[0])

    def test_cold_media_install_wires_private_paths_and_draft_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            saved = []
            with patch.object(sys, "argv", ["bootstrap_environment.py", "install", "--features", "draft,asr"]), patch.object(bootstrap, "prepare_tool_paths"), patch.object(bootstrap, "data_dir", return_value=data), patch.object(bootstrap, "load_config", return_value={}), patch.object(bootstrap, "save_config", side_effect=lambda value: saved.append(dict(value))), patch.object(bootstrap, "node_ready", return_value=False), patch.object(bootstrap, "install_node", return_value=data / "node") as node, patch.object(bootstrap, "find_executable", return_value=None), patch.object(bootstrap, "install_windows_ffmpeg", return_value=data / "ffmpeg") as ffmpeg, patch.object(bootstrap, "configure_drafts", return_value=data / "drafts"), patch.object(bootstrap, "native", return_value=subprocess.CompletedProcess([], 0, '{"ready":true}')) as execute, contextlib.redirect_stdout(io.StringIO()):
                if os.name != "nt":
                    self.skipTest("Windows orchestration branch")
                self.assertEqual(bootstrap.main(), 0)
                node.assert_called_once()
                ffmpeg.assert_called_once()
                self.assertEqual(saved[-1]["tool_dirs"], [str(data / "node"), str(data / "ffmpeg")])
                self.assertIn(str(data / "drafts"), execute.call_args.args[0])

    def test_ready_environment_skips_download_and_installers(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(sys, "argv", ["bootstrap_environment.py", "install", "--features", "draft,asr"]), patch.object(bootstrap, "prepare_tool_paths"), patch.object(bootstrap, "load_config", return_value={"python": sys.executable}), patch.object(bootstrap, "node_ready", return_value=True), patch.object(bootstrap, "find_executable", return_value="existing-tool"), patch.object(bootstrap, "install_node") as node, patch.object(bootstrap, "install_windows_ffmpeg") as ffmpeg, patch.object(bootstrap, "request") as request, patch.object(bootstrap, "configure_drafts", return_value=Path(temporary)), patch.object(bootstrap, "native", return_value=subprocess.CompletedProcess([], 0, '{"ready":true}')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(), 0)
                for unused in (node, ffmpeg, request):
                    unused.assert_not_called()

    def test_existing_jianying_is_not_reinstalled(self):
        with patch.object(bootstrap, "jianying_present", return_value=True), patch.object(bootstrap, "native") as execute, patch.object(bootstrap, "request") as download:
            bootstrap.install_jianying()
            execute.assert_not_called()
            download.assert_not_called()

    def test_new_draft_directory_is_created_in_current_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(bootstrap, "choose_drafts_root", side_effect=RuntimeError("not yet configured")), patch.dict(os.environ, {"LOCALAPPDATA": temporary}), patch.object(bootstrap, "data_dir", return_value=Path(temporary)):
                selected = bootstrap.configure_drafts(None)
                self.assertTrue(selected.is_relative_to(Path(temporary).resolve()))
                self.assertTrue(selected.is_dir())

    def test_tool_paths_are_process_local_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"PATH": "existing"}), patch.object(runtime, "load_config", return_value={"tool_dirs": [temporary]}):
                runtime.prepare_tool_paths()
                runtime.prepare_tool_paths()
                self.assertEqual(os.environ["PATH"].split(os.pathsep), [str(Path(temporary).resolve()), "existing"])

    def test_removed_private_coli_is_discovered_as_missing(self):
        with patch.dict(os.environ, {"COLI_BIN": ""}), patch.object(runtime, "load_config", return_value={"coli_bin": "missing-private-coli"}), patch.object(runtime.shutil, "which", return_value=None):
            self.assertIsNone(runtime.find_executable("coli"))

    def test_https_skill_fallback_needs_no_git_and_preserves_license(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("repository-main/LICENSE", "upstream license")
            archive.writestr("repository-main/skills/helper/SKILL.md", "fixture helper")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "helper"
            with patch.object(setup.shutil, "which", return_value=None), patch.object(setup.urllib.request, "urlopen", return_value=io.BytesIO(payload.getvalue())), patch.object(setup, "run") as execute:
                setup.install_skill("owner/repository", "a" * 40, "skills/helper", destination)
                execute.assert_not_called()
            self.assertEqual((destination / "LICENSE").read_text(), "upstream license")
            self.assertTrue((destination / "SKILL.md").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows settings")
    def test_custom_draft_setting_is_discovered_without_user_copying_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary) / "custom-drafts"
            setting = Path(temporary) / "JianyingPro/User Data/Config/globalSetting"
            setting.parent.mkdir(parents=True)
            setting.write_text("[General]\ncurrentCustomDraftPath=" + str(expected), encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary, "JY_PROJECTS_ROOT": ""}), patch.object(runtime, "load_config", return_value={}):
                self.assertEqual(runtime.choose_drafts_root(), expected.resolve())


if __name__ == "__main__":
    unittest.main()
