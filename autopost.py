#!/usr/bin/env python3
"""
autopost.py: posts the next item(s) from queue/ to Instagram.
Designed to run on GitHub Actions, so no computer of your own is needed.

What you put in queue/ (posted in name order, so prefix names 001_, 002_, ...):
  video  (.mp4 .mov .m4v .mkv .webm)  -> series title + English subtitles burned in -> Reel
  image  (.jpg .jpeg .png .webp)      -> photo post
  folder with 2-10 images             -> carousel post

Optional text file with the same name (e.g. 003_beach.txt next to 003_beach.mp4):
  TITLE: something      -> on-screen title for this video only ("TITLE: none" = no title)
  CAPTION:              -> everything after this line is the caption, word for word
  any other text        -> notes the AI uses when writing the caption

Look and series settings live in config.json.

After posting, the item moves to posted/. If it fails, it moves to failed/ with
an error file next to it, so one bad file never blocks the queue.

Environment variables (GitHub secrets / variables):
  IG_USER_ID, IG_ACCESS_TOKEN   required for posting
  ANTHROPIC_API_KEY             optional, writes captions (else notes/transcript is used)
  WHISPER_MODEL                 default "small.en"
  POSTS_PER_RUN                 default 1
  DRY_RUN=1                     render into out/ but don't post or commit
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests

API_VERSION = "v23.0"  # bump if Meta retires this version
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}"

ROOT = Path(__file__).resolve().parent
QUEUE, POSTED, FAILED = ROOT / "queue", ROOT / "posted", ROOT / "failed"
STAGING, OUT = ROOT / "staging", ROOT / "out"
LOG = POSTED / "log.json"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

DEFAULT_CONFIG = {
    "title": "",               # e.g. "Songs I'll be manifesting\n({n}/{total})"
    "series_start": 1,         # number of the first video
    "series_total": 30,        # used for {total}
    "title_color": "#FFD60A",
    "subtitles": True,
    "subtitle_style": "clean",  # "clean" (small white, like most reels) or "bold" (big, outlined)
    "skip_silence": True,      # set false if videos are songs and sung lines go missing
    "font": "DejaVu Sans",
}

# Sizes are fractions of the video height so any resolution looks the same.
SUB_STYLES = {
    "clean": dict(size=0.024, bold=1, outline=0.0, shadow=0.0025, margin=0.30,
                  words=8, chars=44, seconds=3.4),
    "bold": dict(size=0.034, bold=1, outline=0.0045, shadow=0.0, margin=0.22,
                 words=5, chars=26, seconds=2.6),
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


# ───────────────────────── speech to subtitles ─────────────────────────
def transcribe(video: Path, cfg: dict):
    """Return (list of words with timestamps, full transcript)."""
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "small.en")
    print(f"  transcribing with {model_name}...")
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(video), language="en", word_timestamps=True,
                                   vad_filter=bool(cfg["skip_silence"]))
    words, full = [], []
    for seg in segments:
        full.append(seg.text.strip())
        words.extend(seg.words or [])
    return words, " ".join(full).strip()


def chunk_words(words, style: dict):
    """Group words into subtitle lines (break on punctuation, length, or time)."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        text = "".join(x.word for x in cur).strip()
        ends_phrase = w.word.strip()[-1:] in ".?!;"
        if (len(cur) >= style["words"] or len(text) >= style["chars"] or ends_phrase
                or cur[-1].end - cur[0].start >= style["seconds"]):
            chunks.append([cur[0].start, cur[-1].end, text])
            cur = []
    if cur:
        chunks.append([cur[0].start, cur[-1].end, "".join(x.word for x in cur).strip()])
    for a, b in zip(chunks, chunks[1:]):  # close small gaps so lines don't flicker
        if 0 < b[0] - a[1] < 0.4:
            a[1] = b[0]
    return [tuple(c) for c in chunks]


# ───────────────────────── rendering ─────────────────────────
def video_size(video: Path) -> tuple[int, int]:
    """Displayed width/height (accounts for phone rotation metadata)."""
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(video)],
        check=True, capture_output=True, text=True).stdout)["streams"][0]
    w, h = int(info["width"]), int(info["height"])
    rot = int(info.get("tags", {}).get("rotate", 0))
    for sd in info.get("side_data_list", []):
        rot = int(sd.get("rotation", rot))
    return (h, w) if abs(rot) % 180 == 90 else (w, h)


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def ass_text(t: str) -> str:
    return (t.replace("\\", "/").replace("{", "(").replace("}", ")")
             .replace("\r", "").replace("\n", "\\N"))


def ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def write_ass(path: Path, size: tuple[int, int], chunks, title: str, cfg: dict):
    W, H = size
    st = SUB_STYLES.get(cfg["subtitle_style"], SUB_STYLES["clean"])
    side = round(W * 0.08)
    sub = (f"Style: Sub,{cfg['font']},{round(H * st['size'])},&H00FFFFFF,&H000000FF,&H00000000,"
           f"&H96000000,{st['bold']},0,0,0,100,100,0,0,1,{round(H * st['outline'], 1)},"
           f"{round(H * st['shadow'], 1)},2,{side},{side},{round(H * st['margin'])},1")
    ttl = (f"Style: Title,{cfg['font']},{round(H * 0.034)},{ass_color(cfg['title_color'])},"
           f"&H000000FF,&H00000000,&H64000000,1,0,0,0,94,100,0,0,1,0,{round(H * 0.002, 1)},"
           f"8,{side},{side},{round(H * 0.17)},1")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        sub, ttl, "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Title,,0,0,0,,{ass_text(title)}")
    for s, e, t in chunks:
        lines.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,{ass_text(t)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_video(video: Path, ass: Path | None, out: Path):
    """Burn title/subtitles and encode in an Instagram-friendly format."""
    print("  rendering video...")
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2" + (f",subtitles={ass.name}" if ass else "")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video.resolve()), "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
           str(out.resolve())]
    # run inside the subtitle folder so the filter needs no path escaping
    subprocess.run(cmd, cwd=(ass.parent if ass else out.parent), check=True)


# ───────────────────────── images ─────────────────────────
def prepare_image(src: Path, dst: Path) -> Path:
    """JPEG, max 1440px wide, aspect ratio padded into Instagram's 4:5 .. 1.91:1 range."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    ratio = img.width / img.height
    if ratio < 0.8 or ratio > 1.91:
        w, h = (round(img.height * 0.8), img.height) if ratio < 0.8 else (img.width, round(img.width / 1.91))
        canvas = Image.new("RGB", (w, h), "white")
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
        img = canvas
    if img.width > 1440:
        img = img.resize((1440, round(img.height * 1440 / img.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, "JPEG", quality=90, optimize=True)
    return dst


# ───────────────────────── notes & captions ─────────────────────────
def read_notes(item: Path) -> dict:
    """Parse the optional .txt next to an item into title / caption / notes."""
    nf = notes_file(item)
    out = {"title": None, "caption": None, "notes": ""}
    if not nf.exists():
        return out
    notes, lines = [], nf.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("TITLE:"):
            out["title"] = line.split(":", 1)[1].strip()
        elif line.strip().upper().startswith("CAPTION:"):
            first = line.split(":", 1)[1].strip()
            out["caption"] = "\n".join([first, *lines[i + 1:]]).strip()
            break
        else:
            notes.append(line)
    out["notes"] = "\n".join(notes).strip()
    return out


def make_caption(transcript: str, notes: str, title: str, images: list[Path]) -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return (notes or title or transcript)[:2200]

    import anthropic

    print("  writing caption...")
    content = []
    for p in images[:4]:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(p.read_bytes()).decode()}})
    content.append({"type": "text", "text": (
        "Write an Instagram caption in English for this post.\n"
        "- Strong first line (it's all people see before 'more')\n"
        "- Then 1-3 short lines about what the post shows or says\n"
        "- End with 5-10 relevant hashtags on their own line\n"
        "- At most 2-3 emojis. Don't invent facts that aren't in the material.\n"
        "- Don't quote song lyrics.\n"
        "- Output ONLY the caption.\n\n"
        f"On-screen title: {title or 'none'}\n"
        f"Creator's notes: {notes or 'none'}\n"
        + (f"\nVideo transcript:\n{transcript[:8000]}" if transcript else "")
    )})
    msg = anthropic.Anthropic().messages.create(
        model="claude-sonnet-5-5", max_tokens=700,
        messages=[{"role": "user", "content": content}])
    return msg.content[0].text.strip()[:2200]


# ───────────────────────── Instagram API ─────────────────────────
def api(method: str, url: str, **kw) -> dict:
    r = requests.request(method, url, timeout=600, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {r.status_code}: {r.text}")
    return r.json()


def wait_ready(cid: str, token: str):
    for _ in range(90):  # up to ~15 minutes
        s = api("GET", f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": token})
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the media: {s}")
        time.sleep(10)
    raise TimeoutError("Instagram didn't finish processing in time")


def publish(cid: str, user_id: str, token: str) -> str:
    return api("POST", f"{GRAPH}/{user_id}/media_publish",
               data={"creation_id": cid, "access_token": token})["id"]


def post_reel(video: Path, caption: str, user_id: str, token: str) -> str:
    print("  uploading reel...")
    cid = api("POST", f"{GRAPH}/{user_id}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": token})["id"]
    with open(video, "rb") as f:
        api("POST", f"{RUPLOAD}/{cid}", data=f, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(video.stat().st_size)})
    wait_ready(cid, token)
    return publish(cid, user_id, token)


def post_images(urls: list[str], caption: str, user_id: str, token: str) -> str:
    print(f"  posting {len(urls)} image(s)...")
    if len(urls) == 1:
        cid = api("POST", f"{GRAPH}/{user_id}/media", data={
            "image_url": urls[0], "caption": caption, "access_token": token})["id"]
    else:
        children = [api("POST", f"{GRAPH}/{user_id}/media", data={
            "image_url": u, "is_carousel_item": "true", "access_token": token})["id"] for u in urls]
        for c in children:
            wait_ready(c, token)
        cid = api("POST", f"{GRAPH}/{user_id}/media", data={
            "media_type": "CAROUSEL", "children": ",".join(children),
            "caption": caption, "access_token": token})["id"]
    wait_ready(cid, token)
    return publish(cid, user_id, token)


# ───────────────────────── git (used to host images publicly) ─────────────────────────
def git(*args, capture=False) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=capture)
    return r.stdout.strip() if capture else ""


def commit_and_push(message: str):
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-m", message)
    git("pull", "--rebase")
    git("push")


def raw_url(path: Path) -> str:
    repo = os.environ["GITHUB_REPOSITORY"]  # set automatically by GitHub Actions
    sha = git("rev-parse", "HEAD", capture=True)
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{path.relative_to(ROOT).as_posix()}"


# ───────────────────────── queue handling ─────────────────────────
def queue_items() -> list[Path]:
    if not QUEUE.exists():
        return []
    return [p for p in sorted(QUEUE.iterdir())
            if not p.name.startswith(".")
            and (p.is_dir() or p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS)]


def notes_file(item: Path) -> Path:
    return item.parent / f"{item.name if item.is_dir() else item.stem}.txt"


def load_log() -> list:
    return json.loads(LOG.read_text()) if LOG.exists() else []


def next_series_number(cfg: dict) -> int:
    nums = [e["series_n"] for e in load_log() if e.get("series_n")]
    return max(nums) + 1 if nums else int(cfg["series_start"])


def process(item: Path, cfg: dict, series_n: int, user_id: str, token: str) -> tuple[str, int | None]:
    """Returns (media id, series number used or None)."""
    meta = read_notes(item)
    base = item.name if item.is_dir() else item.stem
    work = OUT / base
    work.mkdir(parents=True, exist_ok=True)

    if item.is_file() and item.suffix.lower() in VIDEO_EXTS:
        template = meta["title"] if meta["title"] is not None else cfg["title"]
        if template.strip().lower() == "none":
            template = ""
        template = template.replace("\\n", "\n")
        title = template.format(n=series_n, total=cfg["series_total"]) if template else ""
        used_n = series_n if "{n}" in template else None

        words, transcript = transcribe(item, cfg) if (cfg["subtitles"] or not meta["caption"]) else ([], "")
        chunks = chunk_words(words, SUB_STYLES.get(cfg["subtitle_style"], SUB_STYLES["clean"])) \
            if cfg["subtitles"] else []
        ass = None
        if chunks or title:
            ass = work / "overlay.ass"
            write_ass(ass, video_size(item), chunks, title, cfg)
        reel = work / "reel.mp4"
        render_video(item, ass, reel)
        caption = meta["caption"] or make_caption(transcript, meta["notes"], title, [])
        (work / "caption.txt").write_text(caption, encoding="utf-8")
        if DRY_RUN:
            return "dry-run", used_n
        return post_reel(reel, caption, user_id, token), used_n

    # images: single file or a folder (carousel)
    srcs = [item] if item.is_file() else sorted(
        p for p in item.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    if not 1 <= len(srcs) <= 10:
        raise ValueError(f"a carousel folder needs 1-10 images, found {len(srcs)}")
    staged = [prepare_image(s, STAGING / base / f"{i:02}.jpg") for i, s in enumerate(srcs, 1)]
    for s in staged:
        shutil.copy(s, work / s.name)
    caption = meta["caption"] or make_caption("", meta["notes"], "", staged)
    (work / "caption.txt").write_text(caption, encoding="utf-8")
    if DRY_RUN:
        shutil.rmtree(STAGING / base, ignore_errors=True)
        return "dry-run", None
    commit_and_push(f"stage {base} for Instagram")  # makes the JPEGs reachable by URL
    try:
        return post_images([raw_url(s) for s in staged], caption, user_id, token), None
    finally:
        shutil.rmtree(STAGING / base, ignore_errors=True)


def move_out(item: Path, dest_dir: Path):
    dest_dir.mkdir(exist_ok=True)
    nf = notes_file(item)
    shutil.move(str(item), str(dest_dir / item.name))
    if nf.exists():
        shutil.move(str(nf), str(dest_dir / nf.name))


def main():
    cfg = load_config()
    user_id, token = os.getenv("IG_USER_ID"), os.getenv("IG_ACCESS_TOKEN")
    if not DRY_RUN and not (user_id and token):
        sys.exit("Missing IG_USER_ID / IG_ACCESS_TOKEN secrets.")

    items = queue_items()
    if not items:
        print("Queue is empty, nothing to post.")
        return
    targets = items if DRY_RUN else items[:int(os.getenv("POSTS_PER_RUN", "1"))]
    series_n = next_series_number(cfg)
    failed = False

    for item in targets:
        print(f"\n== {item.name}")
        try:
            media_id, used_n = process(item, cfg, series_n, user_id, token)
        except Exception as e:
            failed = True
            print(f"  FAILED: {e}")
            traceback.print_exc()
            if not DRY_RUN:
                move_out(item, FAILED)
                (FAILED / f"{item.name}.error.txt").write_text(f"{e}\n\n{traceback.format_exc()}")
                commit_and_push(f"failed: {item.name}")
            continue
        if used_n:
            series_n = used_n + 1
        if DRY_RUN:
            print(f"  ok, preview in out/{item.name if item.is_dir() else item.stem}/")
            continue
        move_out(item, POSTED)
        log = load_log()
        log.append({"item": item.name, "media_id": media_id, "series_n": used_n,
                    "posted_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())})
        LOG.write_text(json.dumps(log, indent=2))
        commit_and_push(f"posted: {item.name}")
        print(f"  posted! media id {media_id}")

    if failed:
        sys.exit(1)  # makes the GitHub run show red and email you


if __name__ == "__main__":
    main()
