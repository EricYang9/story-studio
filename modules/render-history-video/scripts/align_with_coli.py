"""Run local Coli ASR and align storyboard boundaries to token timestamps."""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from studio_runtime import find_executable, tool_command


def norm(text: str) -> str:
    # Conservative normalization: never rewrite names or collapse Chinese years.
    return "".join(c.lower() for c in text if c.isalnum())


def align_storyboard(sb, asr):
    """Return evidence-backed timing candidates; image semantics need review."""
    import math
    tokens, times = asr.get("tokens"), asr.get("timestamps")
    if not isinstance(tokens, list) or not isinstance(times, list) or not tokens or len(tokens) != len(times):
        raise ValueError("One timestamp per token required; partial timestamps are not accepted")
    duration = asr.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("A finite positive ASR duration is required")
    chars, char_times, token_ids = [], [], []
    previous = -1.0
    for i, (tok, ts) in enumerate(zip(tokens, times)):
        if not isinstance(tok, str) or type(ts) not in (int, float) or not math.isfinite(ts) or not 0 <= ts < duration or ts < previous:
            raise ValueError("Invalid token or non-monotonic/out-of-range timestamp")
        previous = ts
        for c in norm(tok):
            chars.append(c); char_times.append(float(ts)); token_ids.append(i)
    transcript = "".join(chars)
    shots = sb.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("Nonempty storyboard shots required")
    narration = [shot.get("narration") for shot in shots]
    if any(not isinstance(n, str) or not norm(n) for n in narration):
        raise ValueError("Every shot requires its full original narration")
    manuscript = sb.get("manuscript", "".join(narration))
    if "".join(narration) != manuscript:
        raise ValueError("Narration does not reproduce manuscript; reconcile versions first")
    cursor = 0
    ids = set()
    for shot, text in zip(shots, narration):
        if not shot.get("id") or shot["id"] in ids:
            raise ValueError("Missing or duplicate shot id")
        ids.add(shot["id"])
        if ("source_start" in shot or "source_end" in shot) and (shot.get("source_start") != cursor or shot.get("source_end") != cursor + len(text)):
            raise ValueError(f"{shot['id']}: source offsets disagree with narration")
        cursor += len(text)
    source = "".join(norm(n) for n in narration)
    matcher = difflib.SequenceMatcher(None, source, transcript, autojunk=False)
    mapping = {}
    changes = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            mapping.update((a+k, c+k) for k in range(b-a))
        else:
            changes.append({"kind": tag, "source_range": [a,b], "asr_range": [c,d],
                            "source_text": source[a:b], "asr_text": transcript[c:d]})
    boundaries, evidence = [], []
    offset = 0
    for i, (shot, text) in enumerate(zip(shots, narration)):
        # Both sides of each cut must match in order. Never skip the first words.
        left, right = max(0, offset-6), min(len(source), offset+12)
        indexes = [mapping.get(k) for k in range(left, right)]
        if not indexes or any(k is None for k in indexes) or indexes != list(range(indexes[0], indexes[0]+len(indexes))):
            raise ValueError(f"{shot['id']}: boundary context not reliably matched; review local audio/SRT")
        context = source[left:right]
        if transcript.count(context) != 1:
            raise ValueError(f"{shot['id']}: repeated boundary context; manually verify occurrence")
        idx = mapping[offset]
        if i == 0 and idx != 0:
            raise ValueError("Unmatched speech precedes first shot; reconcile opening narration")
        if idx and token_ids[idx] == token_ids[idx-1]:
            raise ValueError(f"{shot['id']}: cut lies inside a multi-character token; no exact word timing")
        t = char_times[idx]
        if boundaries and t <= boundaries[-1]:
            raise ValueError(f"{shot['id']}: non-increasing cut timestamp")
        boundaries.append(t)
        evidence.append({"source_normalized_offset": offset, "asr_normalized_offset": idx,
                         "source_context": context, "asr_context": transcript[indexes[0]:indexes[-1]+1],
                         "matched_token_index": token_ids[idx], "matched_token_time": t})
        offset += len(norm(text))
    # A deleted/added sentence or ASR substitution still needs review even if cuts match.
    if changes:
        raise ValueError("Transcript differs from manuscript; reconcile before automatic alignment: " + json.dumps(changes[:8], ensure_ascii=False))
    aligned = []
    for i, shot in enumerate(shots):
        start = 0.0 if i == 0 else boundaries[i]
        end = boundaries[i+1] if i+1 < len(shots) else float(duration)
        aligned.append({"id": shot["id"], "audio_start": boundaries[i], "audio_end": end,
                        "start": start, "end": end, "narration": narration[i],
                        "anchor": shot.get("start_anchor"), "boundary_evidence": evidence[i],
                        "alignment_source": "coli-token-timestamps-full-text",
                        "semantic_review_status": "pending"})
    return aligned, float(duration)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--asr-json", help="Reuse a previously saved Coli JSON result")
    ap.add_argument("--model", default="sensevoice")
    ap.add_argument("--language", default="zh")
    args = ap.parse_args()

    audio = Path(args.audio).resolve()
    storyboard_path = Path(args.storyboard).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any((out / name).exists() for name in ("alignment.json", "coli_aligned.srt", "alignment_review.json")):
        raise SystemExit("Use a new alignment output directory; preserve previous evidence and avoid stale results.")
    if args.asr_json:
        asr = json.loads(Path(args.asr_json).read_text(encoding="utf-8"))
    else:
        coli = find_executable("coli")
        if not coli:
            raise SystemExit("Local Coli CLI not found on PATH; install/enable coli before rendering without SRT.")
        cmd = tool_command(coli, "asr", "-j", "--model", args.model, "--language", args.language, str(audio))
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
        if proc.returncode != 0:
            raise SystemExit(f"Coli ASR failed ({proc.returncode}): {proc.stderr.strip()}")
        try:
            asr = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Coli ASR did not return JSON: {exc}") from exc
    (out / "coli_asr.json").write_text(json.dumps(asr, ensure_ascii=False, indent=2), encoding="utf-8")

    sb = json.loads(storyboard_path.read_text(encoding="utf-8"))
    try:
        aligned, duration = align_storyboard(sb, asr)
    except ValueError as exc:
        (out / "alignment_review.json").write_text(json.dumps({"status": "review_required", "reason": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(f"Alignment requires review; do not reuse old alignment outputs: {exc}") from exc
    (out / "alignment.json").write_text(json.dumps({"audio": str(audio), "model": asr.get("model", args.model),
        "duration": duration, "source": "local-coli", "semantic_review_status": "pending", "shots": aligned}, ensure_ascii=False, indent=2), encoding="utf-8")
    # Also emit a standard SRT for downstream inspection; subtitles are not
    # automatically added to the Jianying timeline.
    def tc(sec):
        ms = round(sec * 1000)
        h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    srt = []
    for i, item in enumerate(aligned, 1):
        text = sb["shots"][i-1].get("narration", "").strip().replace("\n", "")
        srt += [str(i), f"{tc(item['start'])} --> {tc(item['end'])}", text, ""]
    (out / "coli_aligned.srt").write_text("\n".join(srt), encoding="utf-8")
    print(json.dumps({"source": "local-coli", "shots": len(aligned), "duration": duration,
                      "alignment": str(out / "alignment.json"), "srt": str(out / "coli_aligned.srt")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
