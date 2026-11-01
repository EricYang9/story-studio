"""Read-only checks for a plan and its current semantic-review evidence.

This verifies artifact identity and recorded review coverage, not image meaning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_runtime import activate_runtime

if __name__ == "__main__":
    activate_runtime()

MODULE_SCRIPTS = Path(__file__).resolve().parents[1] / "modules/render-history-video/scripts"
sys.path.insert(0, str(MODULE_SCRIPTS))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def resolve(base: Path, value: str) -> Path:
    return (base / value).resolve()


def check(plan_path: Path, review_path: Path) -> dict:
    from render import load_plan

    errors = []
    plan = load_plan(plan_path)
    review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    if not isinstance(review, dict):
        raise ValueError("Review must be a JSON object")
    base = review_path.parent
    artifacts = review.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Review needs an artifacts object; see semantic-alignment.md")
    checked = {}
    for key, item in artifacts.items():
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            errors.append(f"artifacts.{key}: path and actual sha256 required")
            continue
        path = resolve(base, item["path"])
        if not path.is_file() or digest(path) != item["sha256"]:
            errors.append(f"artifacts.{key}: missing or changed file: {path}")
        else:
            checked[key] = path
    if "srt" not in checked:
        errors.append("Current verified SRT with sha256 is required")
    if plan.get("audio"):
        if checked.get("audio") != Path(plan["audio"]):
            errors.append("Review audio does not identify the plan's current audio")
        if review.get("workflow") != "audio_first":
            errors.append("Audio plan requires workflow=audio_first")
    elif review.get("workflow") != "srt_only":
        errors.append("Silent plan requires workflow=srt_only and verified SRT timing")
    text_review = review.get("text_audio_review", {})
    if not isinstance(text_review, dict) or text_review.get("status") != "verified" or not text_review.get("notes"):
        errors.append("Recorded text/timing review with status=verified and notes required")
    if "srt" in checked:
        srt = checked["srt"].read_text(encoding="utf-8-sig")
        ends = re.findall(r"-->\s*(\d+):(\d+):(\d+)[,.](\d{3})", srt)
        if not ends:
            errors.append("SRT has no usable cue end timestamps")
        else:
            last_end = max(int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000 for h, m, s, ms in ends)
            if last_end > plan["duration"] + 1/plan["fps"]:
                errors.append("Plan truncates the final SRT cue")
    entries = review.get("shots")
    if not isinstance(entries, list):
        raise ValueError("Review needs a shots list")
    by_id = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Each review shot must be an object")
        sid = item.get("shot_id") or item.get("id")
        if not isinstance(sid, str) or not sid or sid in by_id:
            errors.append(f"Missing or duplicate review shot ID: {sid}")
            continue
        by_id[sid] = item
    plan_ids = {shot["id"] for shot in plan["shots"]}
    if set(by_id) != plan_ids:
        errors.append(f"Shot coverage differs: missing={sorted(plan_ids-set(by_id))}, extra={sorted(set(by_id)-plan_ids)}")
    for shot in plan["shots"]:
        sid = shot["id"]
        item = by_id.get(sid)
        if not item:
            continue
        if item.get("review_status") != "verified":
            errors.append(f"{sid}: semantic review is not verified")
        for field in ("narration", "observed_content", "timing_evidence"):
            if not item.get(field):
                errors.append(f"{sid}: missing {field}")
        for field in ("start", "end"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                errors.append(f"{sid}: invalid reviewed {field}")
            elif abs(value - shot[f"{field}_frame"]/plan["fps"]) > 1/plan["fps"] + 1e-9:
                errors.append(f"{sid}: reviewed {field} differs from plan by more than one frame")
        media = shot.get("image") or shot.get("video")
        field = "image" if shot.get("image") else "video"
        recorded_path = item.get(f"{field}_path")
        if not recorded_path or resolve(base, recorded_path) != Path(media):
            errors.append(f"{sid}: reviewed media does not match plan")
        elif item.get(f"{field}_sha256") != digest(Path(media)):
            errors.append(f"{sid}: media changed since review or hash missing")
    return {
        "ok": not errors,
        "plan": str(plan_path),
        "review": str(review_path),
        "shots": len(plan["shots"]),
        "duration": plan["duration"],
        "errors": errors,
        "scope": "Artifact identity, timeline and recorded coverage only; does not verify semantic truth or playback.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = check(args.plan.resolve(), args.review.resolve())
    except Exception as exc:
        print(json.dumps({"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
