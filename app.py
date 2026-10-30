"""MLBB Ghost: best-effort visual, near-live analysis of public match streams."""
import base64
import hashlib
import io
import json
import os
import queue
import re
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parent
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
INTERVAL = min(120, max(5, int(os.getenv("CAPTURE_INTERVAL_SECONDS", "12"))))
PASSWORD = os.getenv("APP_PASSWORD", "")
USER = os.getenv("APP_USER", "admin")
MAX_UPLOAD = 70 * 1024 * 1024
app = FastAPI(title="MLBB Ghost", docs_url=None, redoc_url=None)


class Access(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if PASSWORD and request.url.path != "/health":
            auth = request.headers.get("Authorization", "")
            try:
                scheme, blob = auth.split(" ", 1)
                username, password = base64.b64decode(blob, validate=True).decode().split(":", 1)
            except (ValueError, UnicodeError):
                scheme, username, password = "", "", ""
            if scheme.lower() != "basic" or not secrets.compare_digest(username, USER) or not secrets.compare_digest(password, PASSWORD):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="MLBB Ghost"'})
        return await call_next(request)

app.add_middleware(Access)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_state():
    return {
        "id": secrets.token_hex(6), "mode": "none", "status": "idle",
        "message": "أدخل رابط بث TikTok LIVE للتحليل المباشر.", "updated_at": None,
        "frame_at": None, "frame_count": 0, "analysis_count": 0, "frame_version": 0,
        "phase": "unknown", "game_clock": None, "blue": {"name": None, "picks": [], "bans": []},
        "red": {"name": None, "picks": [], "bans": []}, "kills": {"blue": None, "red": None},
        "series_score": {"blue": None, "red": None}, "gold": {"blue": None, "red": None},
        "winner": None, "objectives": [], "positions": [], "movements": [],
        "analysis": {"blue_win_condition": "", "red_win_condition": "", "current_macro": "", "warnings": []},
        "notes": [], "confidence": None, "events": [],
    }

lock = threading.RLock()
state = init_state()
current_jpeg = None
previous_jpeg = None
stop_event = threading.Event()
live_process = None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    with lock:
        session = json.loads(json.dumps(state))
    return {"configured": bool(API_KEY), "model": MODEL, "interval": INTERVAL, "session": session}


@app.get("/api/frame")
def frame():
    with lock:
        jpeg = current_jpeg
    if not jpeg:
        raise HTTPException(404, "لا توجد لقطة من الفيديو بعد")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def end_previous():
    global live_process
    stop_event.set()
    if live_process and live_process.poll() is None:
        try:
            live_process.terminate()
        except OSError:
            pass
    live_process = None


@app.post("/api/stop")
def stop():
    with lock:
        end_previous()
        if state["status"] in ("connecting", "watching"):
            state["status"], state["message"] = "stopped", "تم إيقاف متابعة اللايف."
    return status()


@app.post("/api/reset")
def reset():
    global state, current_jpeg, previous_jpeg, stop_event
    with lock:
        end_previous()
        stop_event = threading.Event()
        state = init_state()
        current_jpeg = None
        previous_jpeg = None
    return status()


def valid_url(value: str):
    value = value.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ("tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"):
        raise HTTPException(400, "الرابط لازم يكون https من TikTok فقط.")
    if parsed.username or parsed.password or parsed.port:
        raise HTTPException(400, "عنوان غير مسموح.")
    return value


class Start(BaseModel):
    url: str


@app.post("/api/start")
def start(data: Start):
    global stop_event, current_jpeg, previous_jpeg, state
    url = valid_url(data.url)
    if not API_KEY:
        raise HTTPException(503, "أضف OPENAI_API_KEY في Railway Variables لتفعيل التعرف والتحليل.")
    with lock:
        end_previous()
        stop_event = threading.Event()
        state = init_state()
        state.update({"mode": "live", "status": "connecting", "message": "أتحقق من توفر فيديو البث العام..."})
        current_jpeg = previous_jpeg = None
        session_id = state["id"]
        local_stop = stop_event
    threading.Thread(target=live_worker, args=(url, session_id, local_stop), daemon=True).start()
    return status()


def extract_source(url: str):
    commands = [
        ["yt-dlp", "--no-warnings", "--no-playlist", "--socket-timeout", "12", "--retries", "1", "-g", url],
        ["streamlink", "--stream-url", url, "best"],
    ]
    messages = []
    for cmd in commands:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=38)
            candidates = [line.strip() for line in r.stdout.splitlines() if line.strip().startswith("https://")]
            if r.returncode == 0 and candidates:
                return candidates[0]
            messages.append((r.stderr or "stream not found").strip()[-230:])
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            messages.append(type(exc).__name__)
    raise RuntimeError("تعذر استخراج فيديو تيكتوك. يمكن أن يكون البث خاصاً أو محجوباً عن أدوات الاستخراج. " + (messages[-1] if messages else ""))


def encode_jpeg(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue()


SYSTEM_PROMPT = """You are an MLBB esports VISUAL ANALYST observing screenshots from a public streamed Mobile Legends: Bang Bang match.
Current screenshot and sometimes earlier screenshot are supplied chronologically. The previous observed match-state JSON may be given as context, but is NOT evidence for new events.
Your entire evidence is actual pixels visible in those images. No game telemetry, no x-ray vision, no inference about fog-of-war, invisible heroes, or exact actions between distant frames.
Determine whether the visible scene is draft, in-game, a result screen, a commercial, or unrelated. A vertical TikTok stream may have overlays.
Extract officially locked five picks and bans on each side when icons or labels are legible. Distinguish pre-picks, bans, spectators and overlays. Canonical English hero names only; do not invent hero identification from vague silhouettes. If the side cannot be mapped safely, leave lists empty.
During game, identify visible heroes, locations and possible changes from previous minimap positions. Label movement as OBSERVED only when both frames show reliable indicators, otherwise use 'uncertain' and describe rather than assert a route. Positions: top, mid, bottom, own jungle, river, turtle, lord, base, unknown. If teams cannot be determined do not assign them.
For objectives, towers, gold, kills, match/series score, winner: only report if on-screen readable. Never equate kill score with series score. Never invent winning team or accurate numeric stats.
Provide draft synergies, counters, wave/prio, rotations, objectives and macro recommendations in clear Arabic grounded in actual visible situation, not generic hallucination. Avoid strong predictions unless supported. Mention uncertainties.
Output JSON object ONLY, using exactly:
{"phase":"draft|game|result|other|unknown","blue":{"name":null,"picks":[],"bans":[]},"red":{"name":null,"picks":[],"bans":[]},
"game_clock":null,"kills":{"blue":null,"red":null},"series_score":{"blue":null,"red":null},"gold":{"blue":null,"red":null},
"winner":null,"objectives":[],"positions":[{"team":"blue|red|unknown","hero":"name or unknown","zone":"top|mid|bottom|jungle|river|turtle|lord|base|unknown","certainty":"high|medium|low"}],
"movements":[{"team":"blue|red|unknown","hero":"name or unknown","from":"zone or unknown","to":"zone or unknown","event":"short Arabic visible movement or positioning","certainty":"high|medium|low"}],
"analysis":{"blue_win_condition":"","red_win_condition":"","current_macro":"","warnings":[]},"notes":[],"confidence":0.0}
Limit positions and movements to 8 entries each, objectives to 6, warnings to 4, notes to 4. Keep text concise Arabic, numeric confidence 0-1 as IMAGE VISIBILITY not prediction. If no reliable game information return empty arrays and warnings.
"""


def vision(jpeg: bytes, previous: bytes | None, snapshot: dict):
    from openai import OpenAI
    client = OpenAI(api_key=API_KEY, timeout=60, max_retries=1)
    content = [{"type": "text", "text": "Previous confirmed observations for context (NOT evidence of new movements): " + json.dumps({
        k: snapshot.get(k) for k in ("phase", "blue", "red", "kills", "game_clock", "positions")
    }, ensure_ascii=False)[:1600]}]
    if previous:
        content.extend([
            {"type": "text", "text": "Earlier screenshot; compare if clear:"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(previous).decode(), "detail": "high"}}
        ])
    content.extend([
        {"type": "text", "text": "Latest screenshot (this is the current state):"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(), "detail": "high"}}
    ])
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_tokens=1600,
    )
    raw = response.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("لم يرجع التحليل بصيغة JSON")
    return parsed


def string_list(source, max_items=6):
    if not isinstance(source, list):
        return []
    unique = []
    for item in source:
        if isinstance(item, str) and item.strip() and item.strip().casefold() not in [x.casefold() for x in unique]:
            unique.append(item.strip()[:180])
        if len(unique) >= max_items:
            break
    return unique


def score_int(value):
    return value if type(value) is int and 0 <= value <= 999999 else None


def merge(d: dict):
    with lock:
        phase = d.get("phase")
        if phase in ("draft", "game", "result", "other"):
            state["phase"] = phase
        if isinstance(d.get("game_clock"), str) and re.fullmatch(r"\d{1,2}:\d{2}", d["game_clock"]):
            state["game_clock"] = d["game_clock"]
        for side in ("blue", "red"):
            team = d.get(side)
            if isinstance(team, dict):
                name = team.get("name")
                if isinstance(name, str) and name.strip():
                    state[side]["name"] = name.strip()[:80]
                for label in ("picks", "bans"):
                    # Never replace locked earlier selections because the broadcast moved on.
                    state[side][label] = string_list(state[side][label] + string_list(team.get(label), 5), 5)
        for label in ("kills", "series_score", "gold"):
            scores = d.get(label)
            if isinstance(scores, dict):
                for side in ("blue", "red"):
                    n = score_int(scores.get(side))
                    if n is not None:
                        state[label][side] = n
        if d.get("winner") in ("blue", "red") and phase == "result":
            state["winner"] = d["winner"]
        for label in ("objectives", "notes"):
            vals = string_list(d.get(label), 6)
            if vals:
                state[label] = vals
        for label in ("positions", "movements"):
            items = d.get(label)
            if isinstance(items, list):
                clean = []
                for item in items[:8]:
                    if isinstance(item, dict) and item.get("team") in ("blue", "red", "unknown"):
                        clean.append({k: str(item.get(k, ""))[:160] for k in (
                            ("team", "hero", "zone", "certainty") if label == "positions"
                            else ("team", "hero", "from", "to", "event", "certainty"))})
                state[label] = clean
        analysis = d.get("analysis")
        if isinstance(analysis, dict):
            for key in ("blue_win_condition", "red_win_condition", "current_macro"):
                if isinstance(analysis.get(key), str) and analysis[key].strip():
                    state["analysis"][key] = analysis[key].strip()[:800]
            state["analysis"]["warnings"] = string_list(analysis.get("warnings"), 4)
        confidence = d.get("confidence")
        if isinstance(confidence, (float, int)):
            state["confidence"] = min(1, max(0, float(confidence)))
        state["analysis_count"] += 1
        state["updated_at"] = now()
        state["events"] = [{
            "time": state["updated_at"], "clock": state["game_clock"], "phase": state["phase"],
            "movements": state["movements"][:4], "objectives": state["objectives"][:4],
            "note": state["analysis"]["current_macro"][:240], "confidence": state["confidence"]
        }] + state["events"][:49]


def error_text(exc):
    s = str(exc).strip()[:350]
    if "401" in s or "api_key" in s or "authentication" in s.lower():
        return "مفتاح OpenAI API غير صالح أو مرفوض."
    if "quota" in s.lower() or "billing" in s.lower():
        return "OpenAI API يحتاج رصيد Billing مستقل عن ChatGPT."
    return s or "خطأ غير معروف"


def analyze_frame(jpeg: bytes, sid: str):
    global previous_jpeg, current_jpeg
    jpeg = encode_jpeg(jpeg)
    with lock:
        if state["id"] != sid:
            return
        old = previous_jpeg
        snapshot = json.loads(json.dumps(state))
        current_jpeg = jpeg
        state["frame_count"] += 1
        state["frame_version"] += 1
        state["frame_at"] = now()
    result = vision(jpeg, old, snapshot)
    with lock:
        if state["id"] != sid:
            return
        merge(result)
        previous_jpeg = jpeg
        if state["status"] == "connecting":
            state["status"] = "watching"
        state["message"] = "جارٍ تحليل فريمات الفيديو المتتابعة؛ آخر تحديث " + now()


def live_worker(url, sid, stopped):
    global live_process, current_jpeg
    try:
        source = extract_source(url)
        if stopped.is_set():
            return
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", source,
               "-an", "-vf", "fps=1/" + str(INTERVAL) + ",scale='min(1600,iw)':-2",
               "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        with lock:
            if state["id"] != sid or stopped.is_set():
                proc.terminate()
                return
            live_process = proc
        frames = queue.Queue(maxsize=1)
        captured = threading.Event()

        def vision_loop():
            while not stopped.is_set():
                try:
                    jpg = frames.get(timeout=2)
                except queue.Empty:
                    if captured.is_set():
                        return
                    continue
                try:
                    analyze_frame(jpg, sid)
                except Exception as exc:
                    with lock:
                        if state["id"] == sid and not stopped.is_set():
                            state["message"] = "تعذر تحليل آخر فريم: " + error_text(exc)

        worker = threading.Thread(target=vision_loop, daemon=True)
        worker.start()
        buffer = bytearray()
        end_marker = b"\xff\xd9"
        start_marker = b"\xff\xd8"
        count = 0
        while not stopped.is_set() and proc.poll() is None:
            chunk = proc.stdout.read(32768)
            if not chunk:
                break
            buffer.extend(chunk)
            while not stopped.is_set():
                start = buffer.find(start_marker)
                if start == -1:
                    buffer.clear()
                    break
                if start:
                    del buffer[:start]
                end = buffer.find(end_marker, 2)
                if end < 0:
                    break
                image = bytes(buffer[:end + 2])
                del buffer[:end + 2]
                count += 1
                with lock:
                    if state["id"] == sid and not stopped.is_set():
                        current_jpeg = image
                        state["frame_at"] = now()
                        state["frame_version"] += 1
                        state["status"] = "watching"
                        state["message"] = "وصل فريم حقيقي من البث؛ جارٍ تحليل اللعبة."
                if frames.full():
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        pass
                frames.put_nowait(image)
            if len(buffer) > 8 * 1024 * 1024:
                buffer.clear()
        captured.set()
        if proc.poll() is None:
            proc.terminate()
        with lock:
            if state["id"] == sid and not stopped.is_set():
                state["status"] = "error"
                state["message"] = "انقطع بث الفيديو أو انتهى المصدر. حاول إعادة تشغيل المتابعة." if count else "رابط البث انفتح لكن ما وصل أي فريم صالح. جرّب مقطع مرفوع."
    except Exception as exc:
        with lock:
            if state["id"] == sid and not stopped.is_set():
                state["status"] = "error"
                state["message"] = error_text(exc)


def video_snapshots(data: bytes, suffix: str):
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / ("clip" + suffix)
        source.write_bytes(data)
        output = Path(folder) / "frame_%02d.jpg"
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-i", str(source), "-vf", "fps=1/5,scale='min(1600,iw)':-2",
               "-q:v", "4", "-frames:v", "6", str(output)]
        result = subprocess.run(cmd, capture_output=True, timeout=55)
        images = sorted(Path(folder).glob("frame_*.jpg"))
        if result.returncode or not images:
            raise ValueError("فشل استخراج لقطات من المقطع، جرب MP4.")
        return [f.read_bytes() for f in images]


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    if not API_KEY:
        raise HTTPException(503, "أضف OPENAI_API_KEY أولاً في Railway.")
    filename = Path(file.filename or "image.jpg").name.lower()
    suffix = Path(filename).suffix
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v", ".webm"):
        raise HTTPException(400, "الملفات المدعومة JPG/PNG/WEBP و MP4/MOV.")
    data = file.file.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(413, "الفيديو أكبر من 70MB أو الملف فارغ.")
    try:
        shots = video_snapshots(data, suffix) if suffix in (".mp4", ".mov", ".m4v", ".webm") else [encode_jpeg(data)]
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(400, error_text(exc))
    with lock:
        sid = state["id"]
        if state["status"] not in ("watching", "connecting"):
            state["mode"] = "upload"
            state["message"] = "تحليل الصور أو الفيديو المرفوع..."
    seen, errors = 0, []
    for image in shots:
        try:
            analyze_frame(image, sid)
            seen += 1
        except Exception as exc:
            errors.append(error_text(exc))
            if not seen:
                break
    if not seen:
        raise HTTPException(502, errors[0] if errors else "فشل تحليل الملف")
    with lock:
        if state["id"] == sid:
            state["message"] = "تم تحليل " + str(seen) + " لقطة فعلية من الملف."
    return {"analyzed": seen, "errors": errors, **status()}


@app.get("/api/export")
def export():
    with lock:
        data = json.loads(json.dumps(state))
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="mlbb-ghost-session.json"'})
