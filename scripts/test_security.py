"""Security regressions using synthetic files, mocked providers and no real keys."""
import contextlib
import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import setup_dependencies as setup
import studio_runtime as runtime
import voice_settings as settings


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(previous)


class SecurityTests(unittest.TestCase):
    def test_environment_check_does_not_import_project_python_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "imported-project-code"
            package = root / "websockets"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
            )
            # Both cwd and PYTHONPATH are attacker-controlled in this scenario.
            with working_directory(root), patch.dict(os.environ, {"PYTHONPATH": str(root)}), patch.object(setup.shutil, "which", return_value=None), patch.object(settings, "api_key_present", return_value=False), patch.object(settings, "load_profile", side_effect=ValueError("unconfigured")):
                setup.inspect({"tts"}, Path(sys.executable), None, None)
            self.assertFalse(marker.exists(), "The check executed a module supplied by the project")

    @unittest.skipIf(os.name == "nt", "POSIX native bootstrap")
    def test_native_bootstrap_ignores_project_python_startup_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "startup-code-executed"
            (root / "sitecustomize.py").write_text(
                "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
            )
            environment = {"PATH": os.environ.get("PATH", os.defpath),
                           "XDG_DATA_HOME": str(root / "data"),
                           "STORY_STUDIO_CONFIG": str(root / "runtime.json"),
                           "PYTHONPATH": str(root)}
            result = subprocess.run(["bash", str(runtime.ROOT / "scripts/bootstrap.sh"), "check", "--features", "write"], cwd=root, env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ready"])
            self.assertFalse(marker.exists())

    def test_transcription_uses_node_instead_of_a_windows_cmd_shim(self):
        source = runtime.ROOT / "modules/history-image/scripts/transcribe_with_coli.py"
        spec = importlib.util.spec_from_file_location("security_transcription", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = root / "coli.cmd"
            shim.write_text("@echo This batch file must never run\n")
            node = root / "node.exe"
            node.touch()
            package = root / "node_modules/@marswave/coli"
            package.mkdir(parents=True)
            entry = package / "distribution/source/cli.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("// synthetic CLI\n")
            (package / "package.json").write_text(json.dumps({
                "name": "@marswave/coli", "bin": {"coli": "distribution/source/cli.js"}
            }))
            audio = root / "a&ver&b.mp3"
            audio.write_bytes(b"synthetic audio; never decoded")
            asr = {"tokens": ["测试"], "timestamps": [0.0], "duration": 1.0}
            completed = subprocess.CompletedProcess([], 0, json.dumps(asr), "")
            with patch.object(module, "find_executable", return_value=str(shim)), patch.object(runtime.shutil, "which", side_effect=lambda name: str(node) if name in ("node", "node.exe") else None), patch.object(module.subprocess, "run", return_value=completed) as execute, patch.object(sys, "argv", ["transcribe", "--audio", str(audio), "--out-dir", str(root / "out")]), contextlib.redirect_stdout(io.StringIO()):
                module.main()
            command = execute.call_args.args[0]
            self.assertEqual(command[:2], [str(node.resolve()), str(entry.resolve())])
            self.assertEqual(command[-1], str(audio.resolve()))
            self.assertFalse(execute.call_args.kwargs.get("shell", False))

    def test_python_installation_requires_locked_hashes_and_wheels(self):
        report = {"ready": False, "checks": [
            {"dependency": "TTS Python imports", "ready": False, "required": True}
        ]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(setup, "data_dir", return_value=Path(temporary)), patch.object(setup, "load_config", return_value={}), patch.object(setup, "prepare_tool_paths"), patch.object(setup, "save_config"), patch.object(setup, "inspect", return_value=report), patch.object(setup, "run", return_value=subprocess.CompletedProcess([], 0)) as execute, patch.object(sys, "argv", ["setup", "install", "--features", "tts"]), contextlib.redirect_stdout(io.StringIO()):
            setup.main()
        command = next(call.args[0] for call in execute.call_args_list if "pip" in call.args[0])
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("-I", command)

    def test_optional_skill_rejects_a_moving_branch_before_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(setup, "run", side_effect=AssertionError("A moving ref reached git")), patch.object(setup.shutil, "which", return_value="git"):
            with self.assertRaises(ValueError):
                setup.install_skill("owner/repository", "main", ".", Path(temporary) / "skill")

    def test_macos_key_is_saved_to_keychain_without_a_plaintext_file(self):
        with patch.object(settings.sys, "platform", "darwin"), patch.object(settings, "_keychain_write", create=True) as keychain, patch.object(settings, "private_write") as write:
            settings.save_api_key("synthetic-key-not-a-credential")
        keychain.assert_called_once_with("synthetic-key-not-a-credential")
        write.assert_not_called()

    def test_keychain_failure_does_not_fall_back_to_plaintext(self):
        with patch.object(settings.sys, "platform", "darwin"), patch.object(settings, "_keychain_write", side_effect=settings.VoiceConfigError("Keychain unavailable"), create=True), patch.object(settings, "private_write") as write:
            with self.assertRaises(settings.VoiceConfigError):
                settings.save_api_key("synthetic-key-not-a-credential")
        write.assert_not_called()


    def test_linux_plaintext_storage_requires_explicit_opt_in(self):
        if os.name == "nt":
            self.skipTest("Linux file storage branch")
        with patch.object(settings.sys, "platform", "linux"), patch.object(settings, "private_write") as write:
            with self.assertRaises(settings.VoiceConfigError):
                settings.save_api_key("synthetic-key-not-a-credential")
        write.assert_not_called()

    def test_keychain_does_not_automatically_read_a_legacy_plaintext_file(self):
        with patch.object(settings.sys, "platform", "darwin"), patch.dict(os.environ, {"DOUBAO_TTS_API_KEY": ""}), patch.object(settings, "_keychain", return_value=None), patch.object(settings, "secret_path", side_effect=AssertionError("Legacy file was accessed")):
            with self.assertRaises(settings.VoiceConfigError):
                settings.read_api_key()

    def test_unknown_batch_files_are_rejected(self):
        with self.assertRaises(ValueError):
            runtime.tool_command("unknown.cmd", "a&ver&b")

    @unittest.skipUnless(shutil.which("node"), "Optional native Node argument round-trip")
    def test_node_entry_receives_shell_characters_literally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "node_modules/@marswave/coli"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({
                "name": "@marswave/coli", "bin": {"coli": "cli.cjs"}
            }))
            (package / "cli.cjs").write_text("console.log(JSON.stringify(process.argv.slice(2)));\n")
            shim = root / "coli.cmd"
            shim.write_text("This shim must never execute\n")
            arguments = ["asr", "a&ver&b.mp3", "literal%PATH%^!|<>"]
            command = runtime.tool_command(str(shim), *arguments)
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), arguments)

    def test_batch_entry_cannot_escape_the_npm_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "node_modules/@marswave/coli"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({
                "name": "@marswave/coli", "bin": {"coli": "../../../outside.js"}
            }))
            (root / "outside.js").write_text("// must not run\n")
            with self.assertRaises(ValueError):
                runtime.tool_command(str(root / "coli.cmd"), "asr")

    def test_coli_installer_uses_frozen_npm_tree_without_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            def install(command, **kwargs):
                prefix = Path(command[command.index("--prefix") + 1])
                binary = prefix / "node_modules/.bin" / ("coli.cmd" if os.name == "nt" else "coli")
                binary.parent.mkdir(parents=True)
                binary.touch()
                return subprocess.CompletedProcess(command, 0)
            with patch.object(setup, "data_dir", return_value=root), patch.object(setup, "run", side_effect=install) as execute:
                binary = Path(setup.install_coli("npm"))
            command = execute.call_args.args[0]
            self.assertEqual(command[1], "ci")
            self.assertIn("--ignore-scripts", command)
            self.assertNotIn("--global", command)
            self.assertTrue(binary.is_relative_to(root))
            self.assertEqual((binary.parents[2] / "package-lock.json").read_bytes(), (setup.DEPENDENCIES / "coli/package-lock.json").read_bytes())


@unittest.skipUnless(sys.platform == "darwin", "macOS CoreFoundation ABI")
class KeychainBindingTests(unittest.TestCase):
    """Use real CF dictionaries and buffers; never invoke a live SecItem operation."""

    def libraries(self):
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
        security.SecItemAdd = Mock(side_effect=AssertionError("Unexpected live keychain operation"))
        security.SecItemUpdate = Mock(side_effect=AssertionError("Unexpected live keychain operation"))
        security.SecItemCopyMatching = Mock(side_effect=AssertionError("Unexpected live keychain operation"))
        return security, foundation

    def loader(self, security, foundation):
        return patch.object(ctypes, "CDLL", side_effect=lambda path: security if path.endswith("/Security") else foundation)

    def test_write_passes_key_as_cfdata_and_creates_missing_item(self):
        security, foundation = self.libraries()
        marker = b"synthetic-key-not-a-credential"
        security.SecItemUpdate = Mock(return_value=-25300)
        captured = []
        def add(query, output):
            key = ctypes.c_void_p.in_dll(security, "kSecValueData").value
            data = foundation.CFDictionaryGetValue(query, key)
            captured.append(ctypes.string_at(foundation.CFDataGetBytePtr(data), foundation.CFDataGetLength(data)))
            return 0
        security.SecItemAdd = Mock(side_effect=add)
        with self.loader(security, foundation):
            settings._keychain_write(marker.decode())
        self.assertEqual(captured, [marker])
        security.SecItemCopyMatching.assert_not_called()

    def test_read_decodes_native_cfdata_without_touching_keychain(self):
        security, foundation = self.libraries()
        marker = b"synthetic-key-not-a-credential"
        def copy(query, output):
            buffer = ctypes.create_string_buffer(marker)
            result = foundation.CFDataCreate(None, buffer, len(marker))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = result
            return 0
        security.SecItemCopyMatching = Mock(side_effect=copy)
        with self.loader(security, foundation):
            self.assertEqual(settings._keychain("read"), marker.decode())

    def test_presence_query_requests_attributes_instead_of_secret_data(self):
        security, foundation = self.libraries()
        def copy(query, output):
            secret_key = ctypes.c_void_p.in_dll(security, "kSecReturnData").value
            self.assertIsNone(foundation.CFDictionaryGetValue(query, secret_key))
            attributes = ctypes.c_void_p.in_dll(security, "kSecReturnAttributes").value
            self.assertTrue(foundation.CFDictionaryGetValue(query, attributes))
            result = foundation.CFDictionaryCreateMutable(None, 0, None, None)
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = result
            return 0
        security.SecItemCopyMatching = Mock(side_effect=copy)
        with self.loader(security, foundation):
            self.assertTrue(settings._keychain("present"))


if __name__ == "__main__":
    unittest.main()
