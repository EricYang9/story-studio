"""Isolated credential, binary-protocol and workflow tests; no paid service calls."""
import asyncio
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import doubao_tts as tts
import setup_dependencies as setup
import studio_runtime as runtime
import voice_settings as settings

PROFILE = {"speaker": "fixture-voice", "resource_id": "seed-tts-2.0", "model": None,
           "speech_rate": 0, "loudness_rate": 0}


def server_frame(event, payload=b"{}", session="fixture-session", kind=9, compression=0):
    body = gzip.compress(payload) if compression else payload
    value = bytes([0x11, (kind << 4) | 4, 0x10 | compression, 0]) + struct.pack(">I", event)
    identifier = b"fixture-connection" if event in {50, 51, 52} else session.encode()
    value += struct.pack(">I", len(identifier)) + identifier
    return value + struct.pack(">I", len(body)) + body


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
    async def send(self, payload):
        self.sent.append(payload)
    async def recv(self):
        await asyncio.sleep(0)
        try:
            return next(self.frames)
        except StopIteration:
            raise EOFError("fixture connection closed") from None


class VoiceSettingsTests(unittest.TestCase):
    def test_profile_has_no_inherited_voice_or_embedded_key(self):
        with self.assertRaises(ValueError):
            settings.validate_profile({"resource_id": "seed-tts-2.0"})
        with self.assertRaises(ValueError):
            settings.validate_profile(dict(PROFILE, api_key="fixture-only"))

    def test_ordinary_and_clone_model_fields_are_distinct(self):
        with self.assertRaises(ValueError):
            settings.validate_profile(dict(PROFILE, model="seed-tts-2.0-standard"))
        profile = settings.validate_profile(dict(PROFILE, resource_id="seed-icl-2.0", model="seed-tts-2.0-standard"))
        self.assertEqual(profile["resource_id"], "seed-icl-2.0")
        self.assertEqual(profile["model"], "seed-tts-2.0-standard")

    def test_key_saved_outside_source_and_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = "fixture-only-not-a-real-key"
            stored = {}
            def keychain(operation, value=None):
                if operation == "write":
                    stored["key"] = value
                return bool(stored) if operation == "present" else stored.get("key")
            with patch.object(settings, "data_dir", return_value=Path(temporary)), patch.object(settings, "_keychain", side_effect=keychain), patch.dict(os.environ, {"DOUBAO_TTS_API_KEY": ""}), patch.object(sys, "argv", ["voice_settings.py", "configure", "--speaker", "fixture-voice", "--resource-id", "seed-tts-2.0", "--key-stdin", "--allow-plaintext-key-file"]), patch.object(sys, "stdin", io.StringIO(marker + "\n")), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(settings.main(), 0)
                self.assertNotIn(marker, output.getvalue())
                self.assertNotIn(marker, settings.profile_path().read_text())
                self.assertEqual(settings.read_api_key(), marker)
                if sys.platform == "darwin":
                    self.assertFalse(settings.secret_path().exists())
                elif os.name == "nt":
                    self.assertNotIn(marker.encode(), settings.secret_path().read_bytes())
                else:
                    self.assertEqual(settings.secret_path().stat().st_mode & 0o777, 0o600)

    def test_configuration_cannot_be_saved_in_skill_source(self):
        with patch.object(settings, "data_dir", return_value=runtime.ROOT):
            with self.assertRaises(ValueError):
                settings.profile_path()
            with self.assertRaises(ValueError):
                settings.secret_path()

    def test_environment_key_overrides_local_storage_without_reading_file(self):
        with patch.dict(os.environ, {"DOUBAO_TTS_API_KEY": "fixture-env-value"}), patch.object(settings, "secret_path") as stored:
            self.assertEqual(settings.read_api_key(), "fixture-env-value")
            stored.assert_not_called()

    def test_missing_profile_or_key_reports_incomplete_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(settings, "data_dir", return_value=Path(temporary)), patch.object(settings, "_keychain", return_value=False), patch.dict(os.environ, {"DOUBAO_TTS_API_KEY": ""}), patch.object(sys, "argv", ["voice_settings.py", "check"]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(settings.main(), 1)
            report = json.loads(output.getvalue())
            self.assertFalse(report["profile_ready"])
            self.assertFalse(report["api_key_present"])
            self.assertFalse(report["billable_request_sent"])


class ProtocolTests(unittest.TestCase):
    def test_connection_request_matches_v3_wire_layout(self):
        self.assertEqual(tts.pack(1, {}), bytes.fromhex("1114100000000001000000027b7d"))

    def test_session_request_includes_id_length_before_payload(self):
        frame = tts.pack(102, {}, "s")
        self.assertEqual(frame, bytes.fromhex("11141000000000660000000173000000027b7d"))

    def test_gzip_audio_preserves_raw_payload_and_session(self):
        result = tts.unpack(server_frame(352, b"fixture-audio", kind=11, compression=1))
        self.assertEqual(result.payload, b"fixture-audio")
        self.assertEqual(result.session_id, "fixture-session")

    def test_truncated_and_text_frames_fail(self):
        for bad in (b"", b"\x11\x94\x10\x00", server_frame(150)[:-1], "untrusted text"):
            with self.subTest(frame_type=type(bad)), self.assertRaises(tts.TtsError):
                tts.unpack(bad)

    def test_service_errors_do_not_expose_payloads(self):
        marker = "fixture-private-header-value"
        frame = server_frame(153, json.dumps({"code": 403, "message": marker}).encode())
        with self.assertRaises(tts.TtsError) as result:
            tts.unpack(frame)
        self.assertIn("403", str(result.exception))
        self.assertNotIn(marker, str(result.exception))

    def test_binary_error_frame_has_numeric_code_without_echoing_body(self):
        payload = b"fixture-untrusted-provider-message"
        frame = b"\x11\xf0\x10\x00" + struct.pack(">II", 403, len(payload)) + payload
        with self.assertRaisesRegex(tts.TtsError, "403") as result:
            tts.unpack(frame)
        self.assertNotIn("untrusted", str(result.exception))

    def test_transport_chunks_cover_all_unicode_text(self):
        text = ("中文历史故事。English 🐈\n" * 300) + "结尾不能遗漏。"
        chunks = list(tts.text_chunks(text))
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode()) <= 800 for chunk in chunks))

    def test_provider_subtitle_service_is_disabled(self):
        request = tts.request_body(PROFILE, 200, "正文")
        self.assertFalse(request["req_params"]["audio_params"]["enable_subtitle"])
        self.assertNotIn("model", request["req_params"])
        cloned = dict(PROFILE, resource_id="seed-icl-2.0", model="seed-tts-2.0-standard")
        self.assertEqual(tts.request_body(cloned, 100)["req_params"]["model"], cloned["model"])

    def test_finished_session_is_required_and_full_text_is_sent(self):
        text = "整篇正文不能漏读。" * 200
        socket = Socket([server_frame(50), server_frame(150), server_frame(352, b"first", kind=11),
                         server_frame(350), server_frame(352, b"last", kind=11), server_frame(152), server_frame(52)])
        target = io.BytesIO()
        with patch.object(tts.uuid, "uuid4", return_value="fixture-session"):
            size = asyncio.run(tts.stream_session(socket, text, PROFILE, target))
        self.assertEqual(size, 9)
        self.assertEqual(target.getvalue(), b"firstlast")
        requests = []
        for frame in socket.sent:
            event = struct.unpack(">I", frame[4:8])[0]
            if event == 200:
                sid_length = struct.unpack(">I", frame[8:12])[0]
                offset = 12 + sid_length
                payload_length = struct.unpack(">I", frame[offset:offset+4])[0]
                body = json.loads(frame[offset+4:offset+4+payload_length])
                requests.append(body["req_params"]["text"])
        self.assertEqual("".join(requests), text)

    def test_connection_close_without_session_finish_is_incomplete(self):
        socket = Socket([server_frame(50), server_frame(150), server_frame(352, b"partial", kind=11)])
        target = io.BytesIO()
        with patch.object(tts.uuid, "uuid4", return_value="fixture-session"), self.assertRaises(EOFError):
            asyncio.run(tts.stream_session(socket, "正文", PROFILE, target))
        self.assertEqual(target.getvalue(), b"partial")

    def test_success_event_without_audio_is_rejected(self):
        socket = Socket([server_frame(50), server_frame(150), server_frame(152)])
        with patch.object(tts.uuid, "uuid4", return_value="fixture-session"), self.assertRaisesRegex(tts.TtsError, "without audio"):
            asyncio.run(tts.stream_session(socket, "正文", PROFILE, io.BytesIO()))

    def test_different_session_audio_is_rejected(self):
        socket = Socket([server_frame(50), server_frame(150), server_frame(352, b"wrong", kind=11, session="other")])
        with patch.object(tts.uuid, "uuid4", return_value="fixture-session"), self.assertRaisesRegex(tts.TtsError, "different session"):
            asyncio.run(tts.stream_session(socket, "正文", PROFILE, io.BytesIO()))

    def test_connection_close_error_after_finished_audio_does_not_fail_synthesis(self):
        socket = Socket([server_frame(50), server_frame(150), server_frame(352, b"complete", kind=11), server_frame(152)])
        with patch.object(tts.uuid, "uuid4", return_value="fixture-session"):
            self.assertEqual(asyncio.run(tts.stream_session(socket, "正文", PROFILE, io.BytesIO())), 8)


class WorkflowTests(unittest.TestCase):
    def invoke(self, directory, extra, **patches):
        text = Path(directory) / "narration.txt"
        text.write_text("原始正文。", encoding="utf-8")
        output = Path(directory) / "narration.mp3"
        with contextlib.ExitStack() as stack:
            client = types.ModuleType("websockets.asyncio.client")
            client.connect = object()
            stack.enter_context(patch.dict(sys.modules, {"websockets": types.ModuleType("websockets"),
                                                        "websockets.asyncio": types.ModuleType("websockets.asyncio"),
                                                        "websockets.asyncio.client": client}))
            stack.enter_context(patch.object(sys, "argv", ["doubao_tts.py", "--text-file", str(text), "--output", str(output), *extra]))
            stack.enter_context(patch.object(tts, "load_profile", return_value=PROFILE))
            mocks = {name: stack.enter_context(patch.object(tts, name, **options)) for name, options in patches.items()}
            captured = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = tts.main()
        return code, json.loads(captured.getvalue()), mocks

    def test_dry_run_never_reads_key_or_starts_network(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report, mocks = self.invoke(directory, ["--dry-run"], read_api_key={}, synthesize={"new_callable": AsyncMock})
            self.assertEqual(code, 0)
            self.assertFalse(report["network"])
            mocks["read_api_key"].assert_not_called()
            mocks["synthesize"].assert_not_called()

    def test_missing_confirmation_stops_before_credentials_and_network(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report, mocks = self.invoke(directory, [], read_api_key={}, synthesize={"new_callable": AsyncMock})
            self.assertEqual(code, 2)
            self.assertFalse(report["request_may_have_been_billed"])
            mocks["read_api_key"].assert_not_called()
            mocks["synthesize"].assert_not_called()

    def test_existing_mp3_is_not_replaced_or_synthesized_again(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "narration.mp3"
            output.write_bytes(b"keep prior success")
            code, report, mocks = self.invoke(directory, ["--voice-confirmed"], synthesize={"new_callable": AsyncMock})
            self.assertEqual(code, 2)
            self.assertEqual(output.read_bytes(), b"keep prior success")
            mocks["synthesize"].assert_not_called()

    def test_completed_result_records_hashes_and_never_records_key(self):
        async def complete(text, profile, key, partial):
            partial.write_bytes(b"fixture-complete-mp3")
        with tempfile.TemporaryDirectory() as directory, patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            code, report, mocks = self.invoke(directory, ["--voice-confirmed"], read_api_key={"return_value": "fixture-only-private-value"}, find_executable={"return_value": "fixture-media-tool"}, synthesize={"new_callable": AsyncMock, "side_effect": complete}, validate_audio={"return_value": 1.25})
            self.assertEqual(code, 0)
            self.assertEqual(report["duration"], 1.25)
            self.assertFalse(report["listening_verified"])
            metadata = (Path(directory) / "narration.voice.json").read_text()
            self.assertNotIn("fixture-only-private-value", metadata)
            self.assertEqual(len(report["audio_sha256"]), 64)
            self.assertEqual(list(Path(directory).glob("*.partial")), [])
            mocks["synthesize"].assert_awaited_once()

    def test_interrupted_paid_attempt_keeps_partial_without_retry_or_key_echo(self):
        async def interrupted(text, profile, key, partial):
            partial.write_bytes(b"fixture-partial-audio")
            raise ValueError("untrusted exception with fixture-only-private-value")
        with tempfile.TemporaryDirectory() as directory, patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            code, report, mocks = self.invoke(directory, ["--voice-confirmed"], read_api_key={"return_value": "fixture-only-private-value"}, find_executable={"return_value": "fixture-media-tool"}, synthesize={"new_callable": AsyncMock, "side_effect": interrupted})
            self.assertEqual(code, 2)
            self.assertTrue(report["request_may_have_been_billed"])
            self.assertFalse(report["automatic_retry"])
            self.assertNotIn("fixture-only-private-value", json.dumps(report))
            self.assertFalse((Path(directory) / "narration.mp3").exists())
            self.assertEqual(len(list(Path(directory).glob("*.partial"))), 1)
            mocks["synthesize"].assert_awaited_once()

    def test_output_created_during_synthesis_is_not_overwritten(self):
        async def collide(text, profile, key, partial):
            partial.write_bytes(b"new generated audio")
            (partial.parent / "narration.mp3").write_bytes(b"concurrent existing output")
        with tempfile.TemporaryDirectory() as directory, patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            code, report, mocks = self.invoke(directory, ["--voice-confirmed"], read_api_key={"return_value": "fixture-only-private-value"}, find_executable={"return_value": "fixture-media-tool"}, synthesize={"new_callable": AsyncMock, "side_effect": collide}, validate_audio={"return_value": 1.0})
            self.assertEqual(code, 2)
            self.assertEqual((Path(directory) / "narration.mp3").read_bytes(), b"concurrent existing output")
            self.assertFalse((Path(directory) / "narration.voice.json").exists())

    def test_missing_key_stops_before_billable_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            code, report, mocks = self.invoke(directory, ["--voice-confirmed"], read_api_key={"side_effect": settings.VoiceConfigError("Configure the speech key")}, find_executable={"return_value": "fixture-media-tool"}, synthesize={"new_callable": AsyncMock})
            self.assertEqual(code, 2)
            self.assertFalse(report["request_may_have_been_billed"])
            mocks["synthesize"].assert_not_called()

    def test_tts_dependency_profile_does_not_install_draft_or_coli(self):
        self.assertEqual(setup.python_packages({"tts"}), ["websockets==15.0.1"])
        packages = setup.python_packages({"draft", "tts"})
        self.assertIn("pyJianYingDraft==0.3.1", packages)
        self.assertIn("websockets==15.0.1", packages)

    def test_validation_requires_actual_mp3_and_complete_decode(self):
        with patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"format":{"duration":"1"},"streams":[{"codec_name":"mp3"}]}')) as run:
            self.assertEqual(tts.validate_audio(Path("fixture.partial"), "ffprobe", "ffmpeg"), 1.0)
            self.assertEqual(run.call_count, 2)
            self.assertIn("-xerror", run.call_args.args[0])
        with patch.object(tts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"format":{"duration":"1"},"streams":[{"codec_name":"aac"}]}')):
            with self.assertRaises(tts.TtsError):
                tts.validate_audio(Path("fixture.partial"), "ffprobe", "ffmpeg")

    def test_real_mp3_survives_mock_protocol_stream_and_full_decode(self):
        ffmpeg, ffprobe = runtime.find_executable("ffmpeg"), runtime.find_executable("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("Optional local media integration requires FFmpeg")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.mp3"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3", "-c:a", "libmp3lame", str(source)], capture_output=True, check=True)
            payload = source.read_bytes()
            middle = len(payload) // 2
            socket = Socket([server_frame(50), server_frame(150), server_frame(352, payload[:middle], kind=11), server_frame(352, payload[middle:], kind=11), server_frame(152), server_frame(52)])
            target = Path(directory) / "received.mp3"
            with patch.object(tts.uuid, "uuid4", return_value="fixture-session"), target.open("wb") as audio:
                asyncio.run(tts.stream_session(socket, "测试正文", PROFILE, audio))
            self.assertEqual(target.read_bytes(), payload)
            self.assertGreater(tts.validate_audio(target, ffprobe, ffmpeg), 0)


if __name__ == "__main__":
    unittest.main()
