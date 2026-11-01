"""Transcribe audio before storyboarding; export timestamp-backed SRT and cues."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from studio_runtime import find_executable, tool_command


def build_cues(asr):
    tokens, times, duration = asr.get("tokens"), asr.get("timestamps"), asr.get("duration")
    if not isinstance(tokens, list) or not tokens or not isinstance(times, list) or len(tokens) != len(times):
        raise ValueError("Complete token timestamps required; no proportional timing fallback")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Finite positive audio duration required")
    previous = -1
    for token, stamp in zip(tokens, times):
        if not isinstance(token, str) or type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp < duration or stamp < previous:
            raise ValueError("Invalid token or non-monotonic/out-of-range timestamp")
        previous = stamp
    if not any(t.strip() for t in tokens):
        raise ValueError("Empty transcript")
    # Boundaries only use measured token onsets, never character interpolation.
    cues = []
    start = 0
    for i in range(len(tokens)):
        text = "".join(tokens[start:i+1])
        last = i == len(tokens)-1
        split = text.rstrip().endswith(tuple("。！？.!?；;")) or len(text) >= 24 or times[i]-times[start] >= 5
        if last or (split and times[i+1] > times[start] and round(times[i+1]*1000) > round(times[start]*1000)):
            end = float(duration) if last else float(times[i+1])
            if round(end*1000) <= round(times[start]*1000):
                raise ValueError("Token timestamps cannot form a positive SRT cue")
            cues.append({"id": len(cues)+1, "start": float(times[start]), "end": end,
                         "text": text, "token_start": start, "token_end": i+1,
                         "end_basis": "audio_duration" if last else "next_token_onset"})
            start = i+1
    return cues


def timecode(seconds):
    ms = round(seconds*1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def srt_text(cues):
    return "\n".join(f"{c['id']}\n{timecode(c['start'])} --> {timecode(c['end'])}\n{c['text'].replace(chr(10), ' ').replace(chr(13), ' ')}\n" for c in cues)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--asr-json", type=Path, help="Reuse Coli result known to belong to this audio")
    parser.add_argument("--model", default="sensevoice")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    audio = args.audio.resolve()
    if not audio.is_file():
        raise SystemExit("Audio file is missing")
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    names = ["coli_asr.json", "transcript.srt", "transcript_timing.json", "transcription_review.json"]
    if any((out/n).exists() for n in names):
        raise SystemExit("Use a fresh transcription version directory")
    if args.asr_json:
        asr = json.loads(args.asr_json.read_text(encoding="utf-8-sig"))
    else:
        coli = find_executable("coli")
        if not coli:
            raise SystemExit("Local Coli unavailable; no cloud or estimated-timing fallback")
        result = subprocess.run(tool_command(coli, "asr", "-j", "--model", args.model, "--language", args.language, str(audio)), capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise SystemExit(f"Coli failed: {result.stderr.strip()}")
        asr = json.loads(result.stdout)
    (out/"coli_asr.json").write_text(json.dumps(asr, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        cues = build_cues(asr)
    except (ValueError, AttributeError) as exc:
        (out/"transcription_review.json").write_text(json.dumps({"status": "review_required", "reason": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(str(exc)) from exc
    (out/"transcript.srt").write_text(srt_text(cues), encoding="utf-8")
    manifest = {"audio": str(audio), "audio_sha256": file_hash(audio),
                "duration": asr["duration"], "source": "local-coli", "review_status": "pending",
                "asr_json_source": str(args.asr_json.resolve()) if args.asr_json else None,
                "source_audio_association": "requires_verification" if args.asr_json else "direct_invocation",
                "asr_sha256": file_hash(out/"coli_asr.json"), "srt_sha256": file_hash(out/"transcript.srt"),
                "note": "Cue ends are display bounds, not measured speech offsets. Cue grouping is not storyboard segmentation.",
                "cues": cues}
    (out/"transcript_timing.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"srt": str(out/"transcript.srt"), "timing": str(out/"transcript_timing.json"), "cues": len(cues), "review_status": "pending"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
