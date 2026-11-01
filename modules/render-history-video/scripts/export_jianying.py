"""Create a self-contained Jianying draft with upstream pyJianYingDraft."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from studio_runtime import activate_runtime, choose_drafts_root

if __name__ == "__main__":
    activate_runtime()

from PIL import Image
import pyJianYingDraft as draft
from motion import resolve_plan_motion, zoom_at
from render import load_plan, probe

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def safe_name(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(". ")
    if not value or value in {".", ".."} or ".." in value:
        raise ValueError("Use a nonempty draft name without parent-directory components")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        raise ValueError("Reserved filesystem name")
    return value


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def copy_material(source: str, directory: Path) -> Path:
    path = Path(source)
    target = directory / (file_hash(path)[:24] + path.suffix.lower())
    if not target.exists():
        shutil.copy2(path, target)
    return target


def media_size(path: str) -> tuple[int, int]:
    if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        with Image.open(path) as image:
            return image.size
    stream = next(item for item in probe(path)["streams"] if item.get("codec_type") == "video")
    return int(stream["width"]), int(stream["height"])


def cover_scale(src_w: int, src_h: int, width: int, height: int) -> float:
    contain = min(width / src_w, height / src_h)
    return max(width / src_w, height / src_h) / contain * 1.04


def ken_burns(zoom: float, pivot: list[float], cover: float) -> tuple[float, float, float]:
    scale = cover * zoom
    extra = max(0.0, scale - 1.0)
    return scale, (0.5 - pivot[0]) * 2 * extra, (0.5 - pivot[1]) * 2 * extra


def motion_samples(shot: dict, fps: int) -> list[tuple[int, float]]:
    """Sample the shared easing at each output frame; no video is encoded."""
    frames = shot["frames"]
    duration = frames / fps
    points = [
        (int(round(frame * 1_000_000 / fps)),
         zoom_at(shot, frame / max(1, frames - 1) * duration, duration))
        for frame in range(frames)
    ]
    end = int(round(duration * 1_000_000)) - 1
    if end > points[-1][0]:
        points.append((end, points[-1][1]))
    return points


def apply_motion(segment, shot: dict, fps: int, cover: float) -> None:
    points = motion_samples(shot, fps)
    poses = [ken_burns(z, shot["pivot"], cover) for _, z in points]
    scale, x, y = poses[0]
    segment.clip_settings.scale_x = segment.clip_settings.scale_y = scale
    segment.clip_settings.transform_x, segment.clip_settings.transform_y = x, y
    need_x = any(abs(p[1]) > 1e-8 for p in poses)
    need_y = any(abs(p[2]) > 1e-8 for p in poses)
    for (stamp, _), (scale, x, y) in zip(points, poses):
        segment.add_keyframe(draft.KeyframeProperty.uniform_scale, stamp, scale)
        if need_x:
            segment.add_keyframe(draft.KeyframeProperty.position_x, stamp, x)
        if need_y:
            segment.add_keyframe(draft.KeyframeProperty.position_y, stamp, y)
    if shot.get("cut_punch_anim"):
        if shot.get("has_punch_in"):
            segment.add_animation(draft.IntroType.动感缩小, duration=round(shot["punch_in_dur"] * 1_000_000))
        if shot.get("has_punch_out"):
            segment.add_animation(draft.OutroType.放大, duration=round(shot["punch_out_dur"] * 1_000_000))


def srt_seconds(stamp: str) -> float:
    match = re.match(r"^(\d+):(\d{2}):(\d{2})[,.](\d{3})(?:\s|$)", stamp.strip())
    if not match:
        raise ValueError("Invalid SRT timestamp")
    hours, minutes, seconds, ms = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + ms / 1000


def parse_srt(path: Path) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start, end = map(srt_seconds, lines[0].split("-->", 1))
        text = "\n".join(lines[1:]).strip()
        if text and end > start:
            cues.append({"start": start, "end": end, "text": text})
    if not cues:
        raise ValueError("No usable subtitle cues")
    return cues


def relocate(value, staging: Path, target: Path):
    """Only relocate strings rooted in this newly created staging directory."""
    if isinstance(value, dict):
        return {key: relocate(item, staging, target) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, staging, target) for item in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        prefix = staging.as_posix()
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return target.as_posix() + normalized[len(prefix):]
    return value


def relocate_backup(backup: Path, previous: Path) -> dict[Path, bytes]:
    snapshots = {}
    try:
        for filename in ("draft_content.json", "draft_info.json", "draft_meta_info.json"):
            path = backup / filename
            if not path.is_file():
                continue
            original = path.read_bytes()
            value = relocate(json.loads(original.decode("utf-8-sig")), previous, backup)
            if filename == "draft_meta_info.json":
                value.update(draft_name=backup.name, draft_fold_path=backup.as_posix())
            else:
                value["name"] = backup.name
            snapshots[path] = original
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:
        for path, original in snapshots.items():
            path.write_bytes(original)
        raise
    return snapshots


def export_plan(plan_path: Path, name: str, overwrite: bool = False,
                drafts_root: str | None = None, no_subtitles: bool = False) -> dict:
    plan_path = plan_path.resolve()
    plan = load_plan(plan_path)
    root = choose_drafts_root(drafts_root)
    root.mkdir(parents=True, exist_ok=True)
    name = safe_name(name)
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError("Draft target must remain inside the selected root")
    if target.exists() and not overwrite:
        raise FileExistsError("Draft name exists; use a new name")
    if target.exists() and not target.is_dir():
        raise FileExistsError("Draft target is not a directory")
    staging_name = ".story-studio-" + uuid.uuid4().hex
    staging = root / staging_name
    warnings = []
    cues = parse_srt(Path(plan["subtitles"])) if plan.get("subtitles") and not no_subtitles else []
    if cues and max(c["end"] for c in cues) > plan["duration"] + 1 / plan["fps"]:
        raise ValueError("Subtitles extend beyond the timeline")
    backup = None
    backup_snapshots = {}
    try:
        script = draft.DraftFolder(str(root)).create_draft(
            staging_name, plan["width"], plan["height"], plan["fps"], maintrack_adsorb=False
        )
        materials = staging / "materials"
        materials.mkdir()
        audio = None
        if plan.get("audio"):
            audio = draft.AudioMaterial(str(copy_material(plan["audio"], materials)))
            if abs(audio.duration / 1_000_000 - plan["audio_duration"]) > 1 / plan["fps"] + 0.001:
                raise ValueError("Audio duration probes disagree by more than one frame")
            if audio.duration > round(plan["duration"] * 1_000_000):
                last = plan["shots"][-1]
                end_frame = math.ceil(audio.duration * plan["fps"] / 1_000_000)
                if not last.get("image") or end_frame - last["end_frame"] > 1:
                    raise ValueError("Recheck the final shot: it does not fully cover narration")
                last.update(end_frame=end_frame, frames=end_frame-last["start_frame"],
                            end=end_frame/plan["fps"], duration_s=(end_frame-last["start_frame"])/plan["fps"])
                plan["duration"] = end_frame/plan["fps"]
                resolve_plan_motion(plan)
                warnings.append("Final photo extended by one frame to preserve full narration")
        script.append_track(draft.TrackSpec(draft.TrackType.video, "VideoTrack"))
        for shot in plan["shots"]:
            source = shot.get("image") or shot["video"]
            copied = copy_material(source, materials)
            material = draft.VideoMaterial(str(copied))
            if shot.get("image") and material.material_type != "photo":
                raise ValueError(f"{shot['id']}: image was not classified as a photo")
            start = round(shot["start_frame"] * 1_000_000 / plan["fps"])
            end = round(shot["end_frame"] * 1_000_000 / plan["fps"])
            segment = draft.VideoSegment(material, draft.Timerange(start, end - start), volume=0)
            width, height = media_size(source)
            apply_motion(segment, shot, plan["fps"], cover_scale(width, height, plan["width"], plan["height"]))
            script.add_segment(segment, "VideoTrack")
        if audio is not None:
            script.append_track(draft.TrackSpec(draft.TrackType.audio, "Narration"))
            script.add_segment(draft.AudioSegment(audio, draft.Timerange(0, audio.duration)), "Narration")
        if cues:
            script.append_track(draft.TrackSpec(draft.TrackType.text, "Subtitles"))
            for cue in cues:
                script.add_segment(draft.TextSegment(
                    cue["text"], draft.Timerange(round(cue["start"] * 1_000_000), round((cue["end"] - cue["start"]) * 1_000_000)),
                    style=draft.TextStyle(size=5, align=1, auto_wrapping=True, max_line_width=0.86),
                    border=draft.TextBorder(width=40),
                    clip_settings=draft.ClipSettings(transform_y=-0.8),
                ), "Subtitles")
        script.save()
        content_path = staging / "draft_content.json"
        content = relocate(json.loads(content_path.read_text(encoding="utf-8")), staging, target)
        content["name"] = name
        payload = json.dumps(content, ensure_ascii=False, indent=2)
        content_path.write_text(payload, encoding="utf-8")
        (staging / "draft_info.json").write_text(payload, encoding="utf-8")
        meta_path = staging / "draft_meta_info.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        now = int(time.time() * 1_000_000)
        meta.update(draft_name=name, draft_fold_path=target.as_posix(), draft_root_path=root.as_posix(),
                    draft_timeline_materials_size_=sum(p.stat().st_size for p in materials.iterdir()),
                    tm_draft_create=now, tm_draft_modified=now,
                    tm_duration=content["duration"], draft_id=content.get("id", uuid.uuid4().hex))
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            backup = root / (name + ".backup-" + uuid.uuid4().hex[:8])
            if not backup.resolve().is_relative_to(root):
                raise ValueError("Backup target escapes drafts root")
            target.rename(backup)
            backup_snapshots = relocate_backup(backup, target)
        staging.rename(target)
        info = {
            "backend": "pyJianYingDraft",
            "backend_version": importlib.metadata.version("pyJianYingDraft"),
            "draft_name": name, "draft_path": str(target),
            "draft_info": str(target / "draft_info.json"),
            "draft_content": str(target / "draft_content.json"),
            "width": plan["width"], "height": plan["height"], "fps": plan["fps"],
            "duration": content["duration"] / 1_000_000, "shots": len(plan["shots"]),
            "audio": bool(plan.get("audio")), "subtitles": bool(cues),
            "subtitle_cues": len(cues), "motion": "frame_sampled_easing",
            "warnings": warnings,
        }
        if backup:
            info["previous_draft_backup"] = str(backup)
        (plan_path.parent / "jianying_draft.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        (plan_path.parent / "resolved_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return info
    except BaseException:
        if staging.is_dir() and staging.parent.resolve() == root and staging.name == staging_name:
            shutil.rmtree(staging)
        if backup is not None and backup.is_dir() and not target.exists():
            for path, original in backup_snapshots.items():
                path.write_bytes(original)
            backup.rename(target)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--drafts-root")
    parser.add_argument("--overwrite", action="store_true", help="Keep the previous draft as a backup before replacing")
    parser.add_argument("--no-subtitles", action="store_true")
    args = parser.parse_args()
    path = args.plan.resolve()
    name = args.name or f"{path.parent.parent.parent.name}_{path.parent.name}"
    print(json.dumps(export_plan(path, name, args.overwrite, args.drafts_root, args.no_subtitles), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

