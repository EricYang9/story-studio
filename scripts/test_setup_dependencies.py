"""Isolated behavioral tests; no real installs, network or user config writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import setup_dependencies as setup
import studio_runtime as runtime

REVISION = "a" * 40

class RuntimeTests(unittest.TestCase):
    def test_config_refuses_skill_directory(self):
        with patch.object(runtime, "config_path", return_value=runtime.ROOT / ".forbidden-runtime.json"):
            with self.assertRaises(ValueError):
                runtime.save_config({"drafts_root": "private runtime value"})
        self.assertFalse((runtime.ROOT / ".forbidden-runtime.json").exists())

    def test_config_round_trip_outside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            with patch.object(runtime, "config_path", return_value=path):
                runtime.save_config({"schema_version": 1})
                self.assertEqual(runtime.load_config(), {"schema_version": 1})

    def test_explicit_draft_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "drafts"
            with patch.dict(os.environ, {"JY_PROJECTS_ROOT": "different"}), patch.object(runtime, "load_config", return_value={}):
                self.assertEqual(runtime.choose_drafts_root(str(selected)), selected.resolve())

    def test_environment_draft_root_overrides_saved_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)
            with patch.dict(os.environ, {"JY_PROJECTS_ROOT": str(selected)}), patch.object(runtime, "load_config", return_value={"drafts_root": "different"}):
                self.assertEqual(runtime.choose_drafts_root(), selected.resolve())

    def test_private_coli_prefix_does_not_need_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "coli"
            binary.touch()
            with patch.dict(os.environ, {"COLI_BIN": ""}), patch.object(runtime, "load_config", return_value={"coli_bin": str(binary)}), patch.object(runtime.shutil, "which", return_value=None):
                self.assertEqual(runtime.find_executable("coli"), str(binary.resolve()))

    def test_reexec_keeps_selected_interpreter_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "venv-python"
            binary.touch()
            with patch.object(runtime, "load_config", return_value={"python": str(binary)}), patch.dict(os.environ, {"STORY_STUDIO_REEXEC": ""}), patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as execute:
                with self.assertRaises(SystemExit) as result:
                    runtime.activate_runtime()
                self.assertEqual(result.exception.code, 0)
                self.assertEqual(execute.call_args.args[0][0], str(binary.absolute()))


class InstallerTests(unittest.TestCase):
    def test_write_only_never_discovers_or_installs_media_dependencies(self):
        with patch.object(sys, "argv", ["setup_dependencies.py", "install", "--features", "write"]), patch.object(setup, "load_config", return_value={}), patch.object(setup, "choose_drafts_root") as location, patch.object(setup, "find_executable") as executable, patch.object(setup, "find_skill") as skill, patch.object(setup, "run") as run, patch.object(setup, "save_config") as save, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.main(), 0)
            for mock in (location, executable, skill, run, save):
                mock.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["configuration_saved"])

    def test_existing_skill_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing"
            destination.mkdir()
            sentinel = destination / "SKILL.md"
            sentinel.write_text("keep existing", encoding="utf-8")
            with patch.object(setup, "run") as run:
                with self.assertRaises(FileExistsError):
                    setup.install_skill("owner/repository", REVISION, ".", destination)
                run.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep existing")

    def test_repository_cannot_embed_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                address = "https://" + "fixture-user" + ":" + "fixture-value" + "@example.invalid/repo"
                setup.install_skill(address, REVISION, ".", Path(temporary) / "target")

    def test_download_failure_leaves_no_partial_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "target"

            def command(args, **kwargs):
                if "fetch" in args:
                    raise subprocess.CalledProcessError(128, args)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(setup.shutil, "which", return_value="git"), patch.object(setup, "run", side_effect=command):
                with self.assertRaises(subprocess.CalledProcessError):
                    setup.install_skill("owner/repository", REVISION, ".", destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_installed_skill_keeps_license_and_excludes_local_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "target"

            def command(args, **kwargs):
                if "checkout" in args:
                    checkout = Path(args[2])
                    (checkout / "SKILL.md").write_text("fixture skill", encoding="utf-8")
                    (checkout / "LICENSE").write_text("fixture license", encoding="utf-8")
                    (checkout / ".env").write_text("fixture only", encoding="utf-8")
                    (checkout / ".git").mkdir()
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(setup.shutil, "which", return_value="git"), patch.object(setup, "run", side_effect=command):
                setup.install_skill("owner/repository", REVISION, ".", destination)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "LICENSE").is_file())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / ".git").exists())

    def test_import_failure_output_does_not_echo_machine_paths(self):
        failed = subprocess.CalledProcessError(1, [], stderr="ImportError: private runtime path not to echo")
        with patch.object(setup, "run", side_effect=failed):
            report = setup.inspect({"draft"}, Path(sys.executable), None, None)
        self.assertFalse(report["ready"])
        self.assertNotIn("private runtime path", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
