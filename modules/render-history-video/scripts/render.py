"""Render a reviewed image timeline. Python 3.10+, Pillow, NumPy, ffmpeg."""
import argparse
import concurrent.futures
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from studio_runtime import activate_runtime, tool_command

if __name__ == "__main__":
    activate_runtime()

import numpy as np
from PIL import Image, ImageDraw

from motion import resolve_plan_motion, zoom_at


def run(args):
    return subprocess.run(tool_command(args[0], *args[1:]), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                           '-of', 'json', str(path)]).stdout)


def load_plan(path):
    plan = json.loads(path.read_text(encoding='utf-8-sig'))
    for key, default in [('width', 1080), ('height', 1920), ('fps', 30), ('crf', 18)]:
        plan.setdefault(key, default)
    w, h, fps = plan['width'], plan['height'], plan['fps']
    if any(type(x) is not int or x <= 0 for x in (w, h, fps)) or w % 2 or h % 2 or fps > 120:
        raise ValueError('Positive even dimensions and integer fps 1..120 required')
    if not 0 <= plan['crf'] <= 51:
        raise ValueError('CRF must be between 0 and 51')
    if not plan.get('shots'):
        raise ValueError('No shots')
    cursor, ids = 0, set()
    for shot in plan['shots']:
        sid = shot['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', sid) or sid in ids:
            raise ValueError('Shot IDs must be unique safe filenames')
        ids.add(sid)
        a, b = float(shot['start']), float(shot['end'])
        if not math.isfinite(a + b) or b <= a:
            raise ValueError(f'{sid}: invalid times')
        fa, fb = round(a * fps), round(b * fps)
        if fa != cursor or fb <= fa:
            raise ValueError(f'{sid}: gap, overlap, or zero-frame shot')
        shot.update(start_frame=fa, end_frame=fb, frames=fb-fa)
        cursor = fb
        if shot.get('video'):
            shot['video'] = str((path.parent / shot['video']).resolve())
            info = probe(shot['video'])
            if not any(s['codec_type'] == 'video' for s in info['streams']):
                raise ValueError(f'{sid}: video input has no video stream')
        if shot.get('image'):
            shot['image'] = str((path.parent / shot['image']).resolve())
            with Image.open(shot['image']) as im:
                im.verify()
        elif not shot.get('video'):
            raise ValueError(f'{sid}: image or video required')
        shot.setdefault('zoom', [1.0, 1.0] if shot.get('video') else [1.0, 1.07])
        shot.setdefault('pivot', [0.5, 0.5])
        if len(shot['zoom']) != 2 or any(not math.isfinite(z) or z < 1 for z in shot['zoom']):
            raise ValueError(f'{sid}: zoom must contain two finite values >= 1')
        if len(shot['pivot']) != 2 or any(not math.isfinite(p) or not 0 <= p <= 1 for p in shot['pivot']):
            raise ValueError(f'{sid}: pivot must contain two values within 0..1')
        shot['duration_s'] = shot['frames'] / fps
    plan['duration'] = cursor / fps
    resolve_plan_motion(plan)
    if plan.get('audio'):
        plan['audio'] = str((path.parent / plan['audio']).resolve())
        info = probe(plan['audio'])
        if not any(s['codec_type'] == 'audio' for s in info['streams']):
            raise ValueError('Audio input has no audio stream')
        plan['audio_duration'] = float(info['format']['duration'])
        if plan['audio_duration'] > plan['duration'] + 1/fps:
            raise ValueError('Timeline truncates audio; extend final shot')
    if plan.get('subtitles'):
        plan['subtitles'] = str((path.parent / plan['subtitles']).resolve())
        if not Path(plan['subtitles']).is_file():
            raise ValueError(f"Subtitles file missing: {plan['subtitles']}")
    return plan


def transformed(im, shot, k, w, h):
    duration_s = shot.get('duration_s', shot['end'] - shot['start'])
    t = (k / max(1, shot['frames'] - 1)) * duration_s
    z = zoom_at(shot, t, duration_s)
    scale = min(im.width/w, im.height/h) / z
    px, py = shot['pivot']
    x, y = (im.width-w*scale)*px, (im.height-h*scale)*py
    return im.transform((w, h), Image.Transform.AFFINE,
                        (scale, 0, x, 0, scale, y), Image.Resampling.BICUBIC)


def render_video_shot(shot, plan, dest):
    w, h, fps = plan['width'], plan['height'], plan['fps']
    gop = max(10000, shot['frames'] + 1)
    cmd = ['ffmpeg', '-v', 'error', '-y', '-i', shot['video'], '-an',
           '-vf', f'scale={w}:{h}:flags=lanczos,fps={fps},setsar=1',
           '-frames:v', str(shot['frames']),
           '-c:v', 'libx264', '-preset', 'fast', '-crf', str(plan['crf']),
           '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0',
           '-threads', '2', '-pix_fmt', 'yuv420p',
           '-video_track_timescale', str(fps*512), '-movflags', '+faststart', str(dest)]
    with dest.with_suffix('.log').open('w') as log:
        proc = subprocess.run(tool_command(cmd[0], *cmd[1:]), stderr=log)
    if proc.returncode != 0:
        raise RuntimeError(f'{shot["id"]}: ffmpeg failed; inspect log')
    v = probe(dest)['streams'][0]
    if int(v['nb_frames']) != shot['frames']:
        raise RuntimeError('Encoded frame count mismatch')
    print(f"{shot['id']}: {shot['frames']} frames OK (video)", flush=True)
    return dest


def render_shot(shot, plan, out):
    w, h, fps = plan['width'], plan['height'], plan['fps']
    dest = out/'clips'/f"{shot['id']}.mp4"
    if shot.get('video'):
        return render_video_shot(shot, plan, dest)
    im = Image.open(shot['image']).convert('RGB')
    gop = max(10000, shot['frames'] + 1)
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'rgb24',
           '-video_size', f'{w}x{h}', '-framerate', str(fps), '-i', 'pipe:0', '-an',
           '-c:v', 'libx264', '-preset', 'fast', '-crf', str(plan['crf']),
           '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0',
           '-threads', '2', '-pix_fmt', 'yuv420p', '-vf', 'setsar=1',
           '-video_track_timescale', str(fps*512), '-movflags', '+faststart', str(dest)]
    with dest.with_suffix('.log').open('w') as log:
        proc = subprocess.Popen(tool_command(cmd[0], *cmd[1:]), stdin=subprocess.PIPE, stderr=log)
        try:
            for k in range(shot['frames']):
                proc.stdin.write(transformed(im, shot, k, w, h).tobytes())
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f'{shot["id"]}: ffmpeg failed; inspect log')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    v = probe(dest)['streams'][0]
    if int(v['nb_frames']) != shot['frames']:
        raise RuntimeError('Encoded frame count mismatch')
    print(f"{shot['id']}: {shot['frames']} frames OK", flush=True)
    return dest


def frame(path, number, width=180):
    vf = f"select=eq(n\\,{number}),scale={width}:-1"
    data = run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', vf, '-frames:v', '1',
                '-fps_mode', 'passthrough', '-f', 'image2pipe', '-vcodec', 'png', '-']).stdout
    return Image.open(io.BytesIO(data)).convert('RGB')


def verify(plan, out, paths):
    info = probe(out/'final.mp4')
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    if (int(video['nb_frames']) != plan['shots'][-1]['end_frame'] or
            (video['width'], video['height']) != (plan['width'], plan['height']) or
            abs(float(info['format']['duration'])-plan['duration']) > 1/plan['fps'] + .05):
        raise RuntimeError('Final format, duration or frame count mismatch')
    if len(info['streams']) != (2 if plan.get('audio') else 1):
        raise RuntimeError('Unexpected output streams')
    run(['ffmpeg', '-v', 'error', '-i', str(out/'final.mp4'), '-f', 'null', '-'])
    metrics = []
    for shot, path in zip(plan['shots'], paths):
        n = shot['frames']
        thumbs = [frame(path, k) for k in [0, n//2, n-1]]
        th = thumbs[0].height
        sheet = Image.new('RGB', (540, th+24), (24, 24, 24))
        for i, im in enumerate(thumbs):
            sheet.paste(im, (i*180, 0))
        ImageDraw.Draw(sheet).text((5, th+3), shot['id']+' start / middle / end', fill='white')
        sheet.save(out/'verify'/f"{shot['id']}.jpg")
        count = min(n, 60)
        start = max(0, n//2-count//2)
        vf = f'select=between(n\\,{start}\\,{start+count-1}),scale=160:90,format=gray'
        raw = run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', vf,
                   '-frames:v', str(count), '-fps_mode', 'passthrough', '-f', 'rawvideo', '-']).stdout
        a = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160).astype(np.float32)
        diff = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
        metrics.append({'id': shot['id'], 'sample_start_frame': start,
                        'mean_adjacent_difference': float(diff.mean()) if diff.size else None,
                        'variation': float(diff.std()/diff.mean()) if diff.size and diff.mean() else None,
                        'note': 'Diagnostic only; not a perceptual pass/fail score'})
        cut = shot['start_frame']
        if cut:
            before, after = frame(out/'final.mp4', cut-1), frame(out/'final.mp4', cut)
            pair = Image.new('RGB', (360, before.height))
            pair.paste(before, (0, 0))
            pair.paste(after, (180, 0))
            pair.save(out/'verify'/f"cut_{shot['id']}.jpg")
        print(f"{shot['id']}: verification samples saved", flush=True)
    (out/'verify'/'report.json').write_text(json.dumps(
        {'full_decode': 'PASS', 'probe': info, 'motion_diagnostics': metrics}, indent=2), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('plan', type=Path)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args()
    path = args.plan.resolve()
    plan = load_plan(path)
    if args.check_only:
        print(f"Validated {len(plan['shots'])} shots, {plan['duration']:.3f}s")
        for shot in plan['shots']:
            flags = []
            if shot.get('has_punch_in'):
                flags.append(f"punch_in {shot['punch_in']:.2f}x/{shot['punch_in_dur']:.2f}s")
            if shot.get('has_punch_out'):
                flags.append(f"punch_out {shot['punch_out']:.2f}x/{shot['punch_out_dur']:.2f}s")
            print(f"  {shot['id']}: {shot['duration_s']:.3f}s zoom={shot['zoom']}"
                  f"{(' ' + ', '.join(flags)) if flags else ' punch=off'}")
        return
    out = path.parent
    if (out/'final.mp4').exists():
        raise FileExistsError('Use a new version directory; final.mp4 already exists')
    for name in ['clips', 'verify']:
        (out/name).mkdir(exist_ok=True)
    (out/'resolved_plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        paths = list(pool.map(lambda s: render_shot(s, plan, out), plan['shots']))
    # Only safe local basenames go into ffconcat; Unicode and apostrophes in parent paths remain safe.
    (out/'concat.txt').write_text('\n'.join(f"file 'clips/{p.name}'" for p in paths), encoding='utf-8')
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(out/'concat.txt')]
    if plan.get('audio'):
        dur = plan['audio_duration']
        fade = min(.03, dur/2)
        cmd += ['-i', plan['audio'], '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'aac', '-b:a', '192k',
                '-af', f'afade=t=in:st=0:d={fade},afade=t=out:st={max(0,dur-fade)}:d={fade},apad']
    else:
        cmd += ['-map', '0:v:0', '-an']
    cmd += ['-c:v', 'copy', '-t', str(plan['duration']), '-movflags', '+faststart', str(out/'final.mp4')]
    run(cmd)
    verify(plan, out, paths)
    print(str(out/'final.mp4'), flush=True)


if __name__ == '__main__':
    main()
