import asyncio
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import edge_tts
import imageio_ffmpeg
import requests


APP_NAME = "Script to Shorts"
APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "ScriptToShorts"
CONFIG_FILE = APP_DIR / "config.json"
TEMP_DIR = APP_DIR / "temp"
DEFAULT_OUTPUT = Path.home() / "Videos" / "ScriptToShorts"
PEXELS_API = "https://api.pexels.com/videos/search"
VOICE = "en-US-GuyNeural"
BRIDGE_PORT = 8787
WEB_JOBS = {}
STOP_WORDS = {
    "there", "were", "only", "inside", "into", "from", "with", "that", "this",
    "then", "when", "while", "before", "after", "under", "over", "they", "them",
    "their", "have", "had", "been", "what", "where", "which", "would", "could",
    "should", "someone", "something", "about", "your", "you're", "you", "his",
    "her", "she", "him", "and", "but", "for", "the", "was", "are", "our", "out",
    "all", "not", "who", "why", "how", "its", "it's", "said", "says", "just",
}


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pexels_key": "", "output_dir": str(DEFAULT_OUTPUT)}


def save_config(data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_process(args, log, cwd=None):
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    process = subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", startupinfo=startup,
    )
    for line in process.stdout:
        if "time=" in line or "Error" in line:
            log(line.strip())
    code = process.wait()
    if code:
        raise RuntimeError(f"Video engine stopped with code {code}.")


def split_scenes(script, count=6):
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script) if s.strip()]
    if not sentences:
        return [script]
    target = max(1, (len(sentences) + count - 1) // count)
    return [" ".join(sentences[i:i + target]) for i in range(0, len(sentences), target)][:count]


def search_terms(text):
    words = re.findall(r"[A-Za-z]{3,}", text.lower())
    useful = []
    for word in words:
        if word not in STOP_WORDS and word not in useful:
            useful.append(word)
    return " ".join(useful[:5]) or "dark cinematic mystery"


def srt_time(ms):
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def ass_time(ms):
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours}:{minutes:02}:{seconds:02}.{ms // 10:02}"


def ass_escape(text):
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


async def create_voice_and_captions(script, label, audio_path, captions_path):
    communicator = edge_tts.Communicate(script, VOICE, rate="-8%", pitch="-8Hz")
    words = []
    with audio_path.open("wb") as audio:
        async for chunk in communicator.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = int(chunk["offset"] / 10_000)
                duration = int(chunk["duration"] / 10_000)
                words.append((start, start + duration, chunk["text"]))

    events = []
    for i in range(0, len(words), 4):
        group = words[i:i + 4]
        if not group:
            continue
        start, end = group[0][0], group[-1][1]
        text = ass_escape(" ".join(x[2] for x in group))
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{text}")

    ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,155,1
Style: PartLabel,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H70000000,-1,0,0,0,100,100,1,0,3,3,0,8,70,70,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    label_event = f"Dialogue: 1,0:00:00.00,9:59:59.00,PartLabel,,0,0,0,,{ass_escape(label.upper())}"
    captions_path.write_text(ass + label_event + "\n" + "\n".join(events) + "\n", encoding="utf-8")


def audio_duration(ffmpeg, audio):
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if probe.exists():
        command = [str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio)]
        return float(subprocess.check_output(command, text=True).strip())
    result = subprocess.run([ffmpeg, "-i", str(audio)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise RuntimeError("Could not read narration length.")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def choose_video_file(item):
    portrait = sorted(
        item.get("video_files", []),
        key=lambda f: (f.get("height", 0) >= f.get("width", 0), f.get("height", 0)),
        reverse=True,
    )
    for file in portrait:
        if file.get("link") and file.get("height", 0) >= 720:
            return file["link"]
    return portrait[0].get("link") if portrait else None


def download_stock(query, api_key, destination, page=1):
    response = requests.get(
        PEXELS_API, headers={"Authorization": api_key},
        params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 10, "page": page},
        timeout=30,
    )
    if response.status_code == 401:
        raise RuntimeError("The Pexels API key is invalid. Open Settings and enter a valid key.")
    response.raise_for_status()
    videos = response.json().get("videos", [])
    if not videos:
        return False
    link = choose_video_file(videos[(page - 1) % len(videos)])
    if not link:
        return False
    with requests.get(link, stream=True, timeout=60) as media:
        media.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in media.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)
    return True


class Generator:
    def __init__(self, log, progress):
        self.log = log
        self.progress = progress
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def create(self, script, label, api_key, output_dir):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job = TEMP_DIR / stamp
        job.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_label = label.lower().replace(" ", "-")
        final = output_dir / f"horror-short-{safe_label}-{stamp}.mp4"

        self.progress(5)
        self.log("Creating American-English narration...")
        audio = job / "narration.mp3"
        captions = job / "captions.ass"
        asyncio.run(create_voice_and_captions(script, label, audio, captions))
        duration = audio_duration(self.ffmpeg, audio)

        scenes = split_scenes(script, 6)
        clip_length = max(3.0, duration / len(scenes) + 0.4)
        rendered = []
        self.log(f"Preparing {len(scenes)} visual scenes...")
        for index, scene in enumerate(scenes):
            raw = job / f"stock-{index}.mp4"
            query = search_terms(scene)
            self.log(f"Scene {index + 1}: searching “{query}”")
            found = download_stock(query, api_key, raw, page=index + 1)
            if not found:
                found = download_stock("dark cinematic mystery", api_key, raw, page=index + 1)
            if not found:
                raise RuntimeError(f"No stock footage found for scene {index + 1}.")
            clip = job / f"scene-{index}.mp4"
            run_process([
                self.ffmpeg, "-y", "-stream_loop", "-1", "-i", str(raw), "-t", f"{clip_length:.3f}",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,eq=brightness=-0.08:saturation=0.75",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", str(clip)
            ], self.log)
            rendered.append(clip)
            self.progress(20 + int(45 * (index + 1) / len(scenes)))

        concat_file = job / "clips.txt"
        concat_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in rendered), encoding="utf-8")
        joined = job / "joined.mp4"
        run_process([self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)], self.log)

        self.progress(72)
        self.log("Adding narration, subtitles and part label...")
        escaped_captions = str(captions).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf = f"subtitles='{escaped_captions}'"
        run_process([
            self.ffmpeg, "-y", "-i", str(joined), "-i", str(audio), "-vf", vf,
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(final)
        ], self.log)
        self.progress(100)
        self.log(f"Finished: {final}")
        return final


def run_web_job(job_id, script, label):
    config = load_config()
    key = config.get("pexels_key", "").strip()
    if not key:
        WEB_JOBS[job_id].update(status="error", message="Open the renderer Settings and add your Pexels API key.")
        return
    try:
        output_dir = Path(config.get("output_dir", str(DEFAULT_OUTPUT)))
        def log(message):
            WEB_JOBS[job_id]["message"] = message
        def progress(value):
            WEB_JOBS[job_id].update(status="running", progress=value)
        result = Generator(log, progress).create(script, label, key, output_dir)
        WEB_JOBS[job_id].update(status="done", progress=100, message="Your video is ready.", path=str(result), filename=result.name)
    except Exception as exc:
        WEB_JOBS[job_id].update(status="error", message=str(exc))


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True, "name": APP_NAME})
            return
        match = re.fullmatch(r"/jobs/([a-f0-9-]+)(/download)?", self.path)
        if not match or match.group(1) not in WEB_JOBS:
            self.send_json(404, {"error": "Not found."})
            return
        job = WEB_JOBS[match.group(1)]
        if match.group(2):
            path = Path(job.get("path", ""))
            if job.get("status") != "done" or not path.is_file():
                self.send_json(409, {"error": "Video is not ready."})
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
            return
        public = {key: value for key, value in job.items() if key != "path"}
        self.send_json(200, public)

    def do_POST(self):
        if self.path != "/generate":
            self.send_json(404, {"error": "Not found."})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 200_000)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            script = str(payload.get("script", "")).strip()
            label = str(payload.get("part", "Part 1"))
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid request."})
            return
        if len(script.split()) < 20:
            self.send_json(400, {"error": "The story must contain at least 20 words."})
            return
        if label not in {"Part 1", "Part 2", "Final Part"}:
            label = "Part 1"
        job_id = str(uuid.uuid4())
        job = {"id": job_id, "status": "queued", "progress": 1, "message": "Video queued."}
        WEB_JOBS[job_id] = job
        threading.Thread(target=run_web_job, args=(job_id, script, label), daemon=True).start()
        self.send_json(202, job)


def start_bridge():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    except OSError:
        return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("900x720")
        self.minsize(760, 620)
        self.configure(bg="#0d0b12")
        self.config_data = load_config()
        self.bridge = start_bridge()
        self.events = queue.Queue()
        self.build_ui()
        self.after(100, self.process_events)

    def build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#0d0b12")
        style.configure("TLabel", background="#0d0b12", foreground="#e9e4f2")
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"), foreground="#c4a4ff")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=10)
        style.configure("Accent.TButton", background="#7c4dff", foreground="white")
        style.configure("TCombobox", fieldbackground="#191521", foreground="white")
        style.configure("Horizontal.TProgressbar", troughcolor="#211b2b", background="#7c4dff")

        root = ttk.Frame(self, padding=28)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Script to Shorts", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="Paste a story. Generate a narrated vertical horror video.", font=("Segoe UI", 11)).pack(anchor="w", pady=(4, 20))
        bridge_text = "Website renderer ready on this PC" if self.bridge else "Website renderer port is already in use"
        ttk.Label(root, text=bridge_text, foreground="#79d9a7").pack(anchor="w", pady=(0, 12))

        row = ttk.Frame(root)
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="Episode label:").pack(side="left")
        self.part = ttk.Combobox(row, state="readonly", values=["Part 1", "Part 2", "Final Part"], width=16)
        self.part.set("Part 1")
        self.part.pack(side="left", padx=10)
        ttk.Button(row, text="Settings", command=self.settings).pack(side="right")

        self.script = tk.Text(root, height=18, wrap="word", bg="#17131f", fg="#f4effa", insertbackground="white", relief="flat", padx=16, pady=14, font=("Segoe UI", 11))
        self.script.pack(fill="both", expand=True)
        self.script.insert("1.0", "Paste your English horror script here...")

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(14, 8))
        self.status = ttk.Label(root, text="Ready")
        self.status.pack(anchor="w")
        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(12, 0))
        self.generate_button = ttk.Button(controls, text="Generate Video", style="Accent.TButton", command=self.start)
        self.generate_button.pack(side="left")
        ttk.Button(controls, text="Open Output Folder", command=self.open_output).pack(side="left", padx=10)

    def settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("620x230")
        win.configure(bg="#0d0b12")
        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Pexels API key").grid(row=0, column=0, sticky="w")
        key = ttk.Entry(frame, width=52, show="•")
        key.insert(0, self.config_data.get("pexels_key", ""))
        key.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 15))
        ttk.Label(frame, text="Output folder").grid(row=2, column=0, sticky="w")
        folder = ttk.Entry(frame, width=52)
        folder.insert(0, self.config_data.get("output_dir", str(DEFAULT_OUTPUT)))
        folder.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Button(frame, text="Browse", command=lambda: self.pick_folder(folder)).grid(row=3, column=1, padx=8)
        def apply():
            self.config_data = {"pexels_key": key.get().strip(), "output_dir": folder.get().strip()}
            save_config(self.config_data)
            win.destroy()
        ttk.Button(frame, text="Save", style="Accent.TButton", command=apply).grid(row=4, column=0, sticky="w", pady=14)

    def pick_folder(self, entry):
        selected = filedialog.askdirectory()
        if selected:
            entry.delete(0, "end")
            entry.insert(0, selected)

    def start(self):
        script = self.script.get("1.0", "end").strip()
        if not script or script.startswith("Paste your"):
            messagebox.showwarning(APP_NAME, "Paste a script first.")
            return
        key = self.config_data.get("pexels_key", "").strip()
        if not key:
            messagebox.showwarning(APP_NAME, "Open Settings and enter your free Pexels API key.")
            return
        self.generate_button.configure(state="disabled")
        self.progress["value"] = 0
        threading.Thread(target=self.worker, args=(script, self.part.get(), key), daemon=True).start()

    def worker(self, script, label, key):
        try:
            output = Path(self.config_data.get("output_dir", str(DEFAULT_OUTPUT)))
            result = Generator(lambda m: self.events.put(("log", m)), lambda v: self.events.put(("progress", v))).create(script, label, key, output)
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log": self.status.configure(text=value)
                elif kind == "progress": self.progress["value"] = value
                elif kind == "done":
                    self.generate_button.configure(state="normal")
                    messagebox.showinfo(APP_NAME, f"Video created successfully:\n\n{value}")
                elif kind == "error":
                    self.generate_button.configure(state="normal")
                    messagebox.showerror(APP_NAME, value)
        except queue.Empty:
            pass
        self.after(100, self.process_events)

    def open_output(self):
        folder = Path(self.config_data.get("output_dir", str(DEFAULT_OUTPUT)))
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt": os.startfile(folder)
        else: subprocess.Popen(["xdg-open", str(folder)])


if __name__ == "__main__":
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    App().mainloop()
