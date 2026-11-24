"""
HiddenAudio for Windows -- the same app as the Android version (same three
screens: main menu, sender, receiver; same hidden-audio protocol, so a phone
and this app can talk to each other), built with Tkinter since this is a
desktop build.

Run:
    pip install -r requirements.txt
    python main.py

Build a standalone .exe (on an actual Windows machine, with Python+pip):
    pip install pyinstaller
    pyinstaller --onefile --windowed --add-data "assets;assets" main.py
"""

from __future__ import annotations

import io
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk, ImageFile

import audio_codec as ac
import crypto_util
from stream_decoder import StreamDecoder, Callback as DecoderCallback

# Let PIL render whatever rows of a JPEG made it in before the stream was
# truncated, instead of raising -- this is what makes a received image look
# like it's loading in live, top to bottom.
ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    import sounddevice as sd
except Exception as e:  # pragma: no cover - surfaced to the user at runtime
    sd = None
    _SD_IMPORT_ERROR = e

try:
    import pygame
except Exception as e:  # pragma: no cover
    pygame = None
    _PYGAME_IMPORT_ERROR = e

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

WIN_W, WIN_H = 460, 820

BG_COLOR = "#0b1026"
COLOR_WHITE = "#FFFFFF"
COLOR_DIM = "#B3B3C8"
COLOR_FAINT = "#80869c"
COLOR_GREEN = "#69F0AE"
COLOR_AMBER = "#FFD180"
COLOR_RED = "#FF8A80"
COLOR_CYAN = "#00E5FF"
PANEL_BG = "#161B33"
PANEL_BORDER = "#2A3365"
BTN_PRIMARY = "#1E3A5F"
BTN_DANGER = "#5F1E2A"

GRADIENT_START = (0x0B, 0x10, 0x26)
GRADIENT_CENTER = (0x1B, 0x14, 0x46)
GRADIENT_END = (0x06, 0x42, 0x4F)


# --------------------------------------------------------------------------
# Background: the chosen photo, center-cropped like Android's centerCrop,
# with the same dark diagonal gradient (alpha 0.72) laid over it.
# --------------------------------------------------------------------------

def build_background(photo_path: str, width: int, height: int) -> Image.Image:
    base = Image.new("RGB", (width, height), GRADIENT_START)
    if photo_path and os.path.exists(photo_path):
        try:
            photo = Image.open(photo_path).convert("RGB")
            pw, ph = photo.size
            scale = max(width / pw, height / ph)
            new_w, new_h = int(pw * scale) + 1, int(ph * scale) + 1
            photo = photo.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - width) // 2
            top = (new_h - height) // 2
            base = photo.crop((left, top, left + width, top + height))
        except Exception:
            pass

    # Diagonal 3-stop gradient overlay, same stops as the Android bg_app drawable.
    xs = np.linspace(0.0, 1.0, width)
    ys = np.linspace(0.0, 1.0, height)
    gx, gy = np.meshgrid(xs, ys)
    t = (gx + gy) / 2.0

    def lerp(c0, c1, f):
        f = np.clip(f, 0.0, 1.0)
        return tuple(c0[i] + (c1[i] - c0[i]) * f for i in range(3))

    first_half = t < 0.5
    t2 = np.where(first_half, t * 2.0, (t - 0.5) * 2.0)
    grad = np.zeros((height, width, 3), dtype=np.uint8)
    for ch in range(3):
        lo = np.where(first_half, GRADIENT_START[ch], GRADIENT_CENTER[ch])
        hi = np.where(first_half, GRADIENT_CENTER[ch], GRADIENT_END[ch])
        grad[:, :, ch] = (lo + (hi - lo) * t2).astype(np.uint8)
    gradient_img = Image.fromarray(grad)

    return Image.blend(base, gradient_img, alpha=0.64)


# --------------------------------------------------------------------------
# Small UI helpers
# --------------------------------------------------------------------------


def add_photo_bg(screen: tk.Frame, app: "App") -> None:
    """Put the background photo behind everything in this screen."""
    lbl = tk.Label(screen, image=app.bg_photo, bd=0, highlightthickness=0)
    lbl.place(x=0, y=0, relwidth=1, relheight=1)
    lbl.lower()
    screen._photo_lbl = lbl


def make_transparent_tree(app: "App", widget: tk.Misc) -> None:
    """Fake transparency: tint each plain Label/Frame with the average photo
    colour right underneath it (Tk has no real alpha)."""
    for w in widget.winfo_children():
        if w is getattr(widget, "_photo_lbl", None):
            continue
        try:
            plain = isinstance(w, (tk.Label, tk.Frame)) and \
                str(w.cget("bg")).lower() == BG_COLOR.lower()
        except Exception:
            plain = False
        if plain:
            def upd(_e=None, w=w):
                try:
                    x = w.winfo_rootx() - app.root.winfo_rootx()
                    y = w.winfo_rooty() - app.root.winfo_rooty()
                    box = (max(0, x), max(0, y),
                           min(WIN_W, x + w.winfo_width()),
                           min(WIN_H, y + w.winfo_height()))
                    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                        return
                    c = app.bg_image.crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))
                    w.configure(bg="#%02x%02x%02x" % tuple(c[:3]))
                except Exception:
                    pass
            w.bind("<Map>", lambda e, u=upd: e.widget.after(20, u))
            w.bind("<Configure>", lambda e, u=upd: e.widget.after(20, u))
        make_transparent_tree(app, w)


def glass_frame(parent) -> tk.Frame:
    return tk.Frame(parent, bg=PANEL_BG, highlightbackground=PANEL_BORDER,
                     highlightthickness=1, bd=0)


def styled_button(parent, text, command, danger=False) -> tk.Button:
    bg = BTN_DANGER if danger else BTN_PRIMARY
    return tk.Button(
        parent, text=text, command=command, bg=bg, fg=COLOR_WHITE,
        activebackground=bg, activeforeground=COLOR_WHITE,
        font=("Segoe UI", 12, "bold"), relief="flat", bd=0,
        padx=10, pady=12, cursor="hand2",
    )


def glass_button(parent, text, command) -> tk.Button:
    return tk.Button(
        parent, text=text, command=command, bg=PANEL_BG, fg=COLOR_WHITE,
        activebackground=PANEL_BORDER, activeforeground=COLOR_WHITE,
        font=("Segoe UI", 11), relief="flat", bd=0,
        padx=10, pady=10, cursor="hand2",
        highlightbackground=PANEL_BORDER, highlightthickness=1,
    )


# --------------------------------------------------------------------------
# Sender engine: builds the hidden tone, plays the chosen song on loop
# through pygame's mixer, and streams the tone through a separate audio
# output stream -- the two mix naturally at the OS/sound-card level, just
# like MediaPlayer + AudioTrack did on Android.
# --------------------------------------------------------------------------

class SenderEngine:
    def __init__(self, on_status, on_finished):
        self.on_status = on_status
        self.on_finished = on_finished
        self._running = False
        self._thread: threading.Thread | None = None

    def is_playing(self) -> bool:
        return self._thread is not None

    def play(self, song_path: str, type_: int, content: bytes, amplitude: float,
              encrypt: bool, passphrase: str):
        self.stop()
        self._running = True

        def worker():
            try:
                self.on_status("در حال آماده‌سازی...")
                final_type, final_content = type_, content
                if encrypt:
                    final_type = type_ | ac.FLAG_ENCRYPTED
                    final_content = crypto_util.encrypt(content, passphrase)

                frame = ac.build_frame(final_type, final_content, amplitude)
                pause = ac.generate_silence(ac.PAUSE_BETWEEN_FRAMES_MS)

                if not self._running:
                    return

                self._start_music(song_path)

                if sd is None:
                    self.on_status(f"خطا: sounddevice نصب نیست ({_SD_IMPORT_ERROR})")
                    return

                with sd.OutputStream(samplerate=ac.SAMPLE_RATE, channels=1, dtype="int16") as stream:
                    round_no = 0
                    while self._running:
                        round_no += 1
                        self.on_status(f"در حال پخش آهنگ + داده مخفی (دور {round_no})")
                        if not self._write_all(stream, frame):
                            break
                        if not self._write_all(stream, pause):
                            break
                self.on_status("متوقف شد" if not self._running else "پخش تمام شد")
            except Exception as e:
                self.on_status(f"خطا: {e}")
            finally:
                self._stop_music()
                self._running = False
                self._thread = None
                self.on_finished()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _write_all(self, stream, data: np.ndarray, chunk: int = 4096) -> bool:
        off = 0
        n = len(data)
        while off < n:
            if not self._running:
                return False
            end = min(off + chunk, n)
            stream.write(data[off:end])
            off = end
        return True

    def _start_music(self, song_path: str):
        if pygame is None:
            self.on_status(f"خطا: pygame نصب نیست ({_PYGAME_IMPORT_ERROR})")
            return
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(song_path)
            pygame.mixer.music.play(loops=-1)
        except Exception as e:
            self.on_status(f"خطا در پخش آهنگ: {e}")

    def _stop_music(self):
        if pygame is not None:
            try:
                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
            except Exception:
                pass

    def stop(self):
        self._running = False
        self._stop_music()


# --------------------------------------------------------------------------
# Receiver engine: captures mic audio on a background thread and feeds it
# to StreamDecoder; callbacks are handed to the UI via a thread-safe queue
# that the UI drains on a Tk `after()` timer (Tk is not thread-safe).
# --------------------------------------------------------------------------

class ReceiverEngine:
    def __init__(self, ui_event_queue: queue.Queue):
        self.ui_event_queue = ui_event_queue
        self._listening = False
        self._thread: threading.Thread | None = None
        self._stream = None

    def is_listening(self) -> bool:
        return self._listening

    def start(self):
        if self._listening:
            return
        self._listening = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._listening = False

    def _emit(self, kind: str, *args):
        self.ui_event_queue.put((kind, args))

    def _run(self):
        if sd is None:
            self._emit("status", f"خطا: sounddevice نصب نیست ({_SD_IMPORT_ERROR})")
            self._listening = False
            return

        engine = self

        class _Cb(DecoderCallback):
            def on_searching(self):
                engine._emit("searching")

            def on_sync_locked(self):
                engine._emit("sync_locked")

            def on_progress(self, bits_done, bits_total, type_, live_text, live_image_bytes):
                engine._emit("progress", bits_done, bits_total, type_, live_text, live_image_bytes)

            def on_frame_failed(self, reason, attempts):
                engine._emit("frame_failed", reason, attempts)

            def on_success(self, type_, data):
                engine._emit("success", type_, data)

        decoder = StreamDecoder(_Cb())
        audio_q: queue.Queue = queue.Queue()

        def audio_callback(indata, frames, time_info, status):
            audio_q.put(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=ac.SAMPLE_RATE, channels=1, dtype="int16",
                callback=audio_callback, blocksize=2048,
            )
            self._stream.start()
        except Exception as e:
            self._emit("status", f"خطا در راه‌اندازی میکروفون: {e}")
            self._listening = False
            return

        self._emit("status", "در حال گوش دادن... آهنگ فرستنده را نزدیک میکروفون بگیر")
        try:
            while self._listening:
                try:
                    chunk = audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                decoder.feed(chunk, len(chunk))
        finally:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._listening = False


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

class MainMenuScreen(tk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app.content, bg=BG_COLOR)
        self.app = app
        add_photo_bg(self, app)

        tk.Label(self, text="آهنگ مخفی", bg=BG_COLOR, fg=COLOR_WHITE,
                  font=("Segoe UI", 26, "bold")).pack(pady=(40, 4))
        tk.Label(self, text="پیام را داخل آهنگ پنهان کن", bg=BG_COLOR, fg=COLOR_DIM,
                  font=("Segoe UI", 13)).pack(pady=(0, 36))

        sender_panel = glass_frame(self)
        sender_panel.pack(fill="x", padx=24, pady=(0, 16), ipady=14)
        tk.Label(sender_panel, text="🎵  فرستنده", bg=PANEL_BG, fg=COLOR_WHITE,
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=16, pady=(4, 2))
        tk.Label(sender_panel, text="ارسال متن داخل آهنگ", bg=PANEL_BG, fg=COLOR_DIM,
                  font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=(0, 4))
        for w in (sender_panel,) + tuple(sender_panel.winfo_children()):
            w.bind("<Button-1>", lambda e: app.show_sender())
            w.configure(cursor="hand2")

        receiver_panel = glass_frame(self)
        receiver_panel.pack(fill="x", padx=24, pady=(0, 16), ipady=14)
        tk.Label(receiver_panel, text="🎧  گیرنده", bg=PANEL_BG, fg=COLOR_WHITE,
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=16, pady=(4, 2))
        tk.Label(receiver_panel, text="شنیدن و نمایش زنده متن", bg=PANEL_BG, fg=COLOR_DIM,
                  font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=(0, 4))
        for w in (receiver_panel,) + tuple(receiver_panel.winfo_children()):
            w.bind("<Button-1>", lambda e: app.show_receiver())
            w.configure(cursor="hand2")


class SenderScreen(tk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app.content, bg=BG_COLOR)
        self.app = app
        add_photo_bg(self, app)
        self.song_path: str | None = None
        self.picked_image_bytes: bytes | None = None
        self.engine = SenderEngine(self._on_status, self._on_finished)

        tk.Label(self, text="🎵  فرستنده", bg=BG_COLOR, fg=COLOR_WHITE,
                  font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=20, pady=(16, 10))

        card1 = glass_frame(self)
        card1.pack(fill="x", padx=20, pady=(0, 14))
        styled_button(card1, "انتخاب آهنگ", self._pick_song).pack(fill="x", padx=14, pady=(14, 6))
        self.lbl_song = tk.Label(card1, text="هنوز آهنگی انتخاب نشده", bg=PANEL_BG,
                                   fg=COLOR_DIM, font=("Segoe UI", 10))
        self.lbl_song.pack(anchor="w", padx=16, pady=(0, 10))

        self.txt_message = tk.Text(card1, height=5, wrap="word", bg="#0f1430", fg=COLOR_WHITE,
                                     insertbackground=COLOR_WHITE, relief="flat",
                                     font=("Segoe UI", 11), padx=10, pady=10)
        self.txt_message.pack(fill="x", padx=14, pady=(0, 6))
        self.txt_message.bind("<KeyRelease>", lambda e: self._update_counter())

        self.lbl_counter = tk.Label(card1, text="", bg=PANEL_BG, fg=COLOR_DIM, font=("Segoe UI", 9))
        self.lbl_counter.pack(anchor="w", padx=16, pady=(0, 14))

        card2 = glass_frame(self)
        card2.pack(fill="x", padx=20, pady=(0, 14))
        tk.Label(card2, text="یا به‌جای متن، یک عکس مخفی بفرست", bg=PANEL_BG, fg=COLOR_WHITE,
                  font=("Segoe UI", 11)).pack(anchor="w", padx=14, pady=(14, 8))
        styled_button(card2, "📷  انتخاب عکس", self._pick_image).pack(fill="x", padx=14)

        self.image_row = tk.Frame(card2, bg=PANEL_BG)
        self.lbl_thumb = tk.Label(self.image_row, bg=PANEL_BG)
        self.lbl_thumb.pack(side="left", padx=(0, 10))
        self.lbl_image_info = tk.Label(self.image_row, bg=PANEL_BG, fg=COLOR_DIM,
                                         font=("Segoe UI", 9), justify="left")
        self.lbl_image_info.pack(side="left", fill="x", expand=True)
        tk.Button(self.image_row, text="حذف", command=self._clear_image, bg=BTN_DANGER,
                   fg=COLOR_WHITE, relief="flat", font=("Segoe UI", 9)).pack(side="right")
        # image_row is packed/unpacked on demand (hidden until an image is picked)

        card3 = glass_frame(self)
        card3.pack(fill="x", padx=20, pady=(0, 14))
        self.var_encrypt = tk.BooleanVar(value=True)  # checked by default
        tk.Checkbutton(card3, text="🔒  رمزنگاری قوی (AES-256) فعال", variable=self.var_encrypt,
                        bg=PANEL_BG, fg=COLOR_WHITE, selectcolor="#0f1430",
                        activebackground=PANEL_BG, activeforeground=COLOR_WHITE,
                        font=("Segoe UI", 11, "bold"), command=self._update_counter
                        ).pack(anchor="w", padx=12, pady=(14, 6))
        self.entry_passphrase = tk.Entry(card3, show="•", bg="#0f1430", fg=COLOR_WHITE,
                                           insertbackground=COLOR_WHITE, relief="flat",
                                           font=("Segoe UI", 11))
        self.entry_passphrase.pack(fill="x", padx=14, pady=(0, 6), ipady=6)
        tk.Label(card3, text="این رمز را فقط حضوری یا از راه دیگری به گیرنده بگو، نه داخل همین آهنگ.",
                  bg=PANEL_BG, fg=COLOR_FAINT, font=("Segoe UI", 8), wraplength=380,
                  justify="left").pack(anchor="w", padx=14, pady=(0, 12))

        card4 = glass_frame(self)
        card4.pack(fill="x", padx=20, pady=(0, 14))
        self.lbl_power = tk.Label(card4, bg=PANEL_BG, fg=COLOR_WHITE, font=("Segoe UI", 10))
        self.lbl_power.pack(anchor="w", padx=14, pady=(14, 2))
        self.var_power = tk.IntVar(value=35)
        self.scale_power = tk.Scale(card4, from_=10, to=100, orient="horizontal", variable=self.var_power,
                                      bg=PANEL_BG, fg=COLOR_WHITE, highlightthickness=0, troughcolor="#0f1430",
                                      showvalue=False, command=lambda v: self._update_power())
        self.scale_power.pack(fill="x", padx=14, pady=(0, 14))

        btn_row = tk.Frame(self, bg=BG_COLOR)
        btn_row.pack(fill="x", padx=20, pady=(0, 10))
        self.btn_send = styled_button(btn_row, "شروع ارسال", self._confirm_and_send)
        self.btn_send.pack(fill="x", ipady=8)
        self.btn_stop = styled_button(btn_row, "■  توقف", self._stop, danger=True)

        status_card = glass_frame(self)
        status_card.pack(fill="x", padx=20, pady=(0, 20))
        self.lbl_status = tk.Label(status_card, text="آماده", bg=PANEL_BG, fg=COLOR_WHITE,
                                     font=("Segoe UI", 11), wraplength=380, justify="left")
        self.lbl_status.pack(anchor="w", padx=14, pady=14)

        back = tk.Label(self, text="← بازگشت", bg=BG_COLOR, fg=COLOR_DIM, font=("Segoe UI", 10), cursor="hand2")
        back.pack(anchor="w", padx=20, pady=(0, 10))
        back.bind("<Button-1>", lambda e: app.show_main())

        self._update_power()
        self._update_counter()

    # ---- UI callbacks ----------------------------------------------------

    def _pick_song(self):
        path = filedialog.askopenfilename(
            title="انتخاب آهنگ",
            filetypes=[("فایل صوتی", "*.mp3 *.wav *.ogg *.flac"), ("همه فایل‌ها", "*.*")],
        )
        if path:
            self.song_path = path
            self.lbl_song.configure(text=os.path.basename(path))

    def _pick_image(self):
        path = filedialog.askopenfilename(
            title="انتخاب عکس",
            filetypes=[("تصویر", "*.png *.jpg *.jpeg *.bmp *.gif"), ("همه فایل‌ها", "*.*")],
        )
        if not path:
            return
        try:
            original = Image.open(path).convert("RGB")
            max_dim = 64
            scale = max_dim / max(original.size)
            if scale < 1.0:
                resized = original.resize(
                    (max(1, int(original.width * scale)), max(1, int(original.height * scale))),
                    Image.LANCZOS,
                )
            else:
                resized = original

            data = None
            for quality in (40, 25, 15, 8, 5):
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=quality)
                candidate = buf.getvalue()
                if len(candidate) <= ac.MAX_PAYLOAD_BYTES - 1:
                    data = candidate
                    break
            if data is None:
                messagebox.showinfo("خطا", "این عکس حتی بعد از فشرده‌سازی خیلی بزرگه")
                return

            self.picked_image_bytes = data
            thumb = resized.copy()
            thumb.thumbnail((64, 64))
            self._thumb_photo = ImageTk.PhotoImage(thumb)
            self.lbl_thumb.configure(image=self._thumb_photo)
            self.image_row.pack(fill="x", padx=14, pady=(10, 14))
            sec = ac.frame_duration_ms(len(data) + 1) // 1000
            self.lbl_image_info.configure(text=f"{len(data)} بایت  •  هر دور حدود {sec} ثانیه")
            self.txt_message.configure(state="disabled")
            self.lbl_counter.configure(text="حالت ارسال: عکس (متن نادیده گرفته می‌شود)")
        except Exception as e:
            messagebox.showerror("خطا در خواندن عکس", str(e))

    def _clear_image(self):
        self.picked_image_bytes = None
        self.image_row.pack_forget()
        self.txt_message.configure(state="normal")
        self._update_counter()

    def _update_power(self):
        self.lbl_power.configure(
            text=f"قدرت سیگنال: {self.var_power.get()}٪  (کمتر = مخفی‌تر، بیشتر = برد دورتر)"
        )

    def _power_value(self) -> float:
        return self.var_power.get() / 100.0

    def _update_counter(self):
        if self.picked_image_bytes is not None:
            return
        overhead = 44 if self.var_encrypt.get() else 0
        n = len(self.txt_message.get("1.0", "end-1c").encode("utf-8"))
        budget = ac.MAX_PAYLOAD_BYTES - 1 - overhead
        sec = ac.frame_duration_ms(min(n + 1 + overhead, ac.MAX_PAYLOAD_BYTES)) // 1000
        self.lbl_counter.configure(text=f"{n} / {budget} بایت  •  هر دور حدود {sec} ثانیه")
        self.lbl_counter.configure(fg=COLOR_RED if n > budget else COLOR_DIM)

    def _set_playing_ui(self, playing: bool):
        if playing:
            self.btn_send.pack_forget()
            self.btn_stop.pack(fill="x", ipady=8)
        else:
            self.btn_stop.pack_forget()
            self.btn_send.pack(fill="x", ipady=8)

    def _confirm_and_send(self):
        if not self.song_path:
            messagebox.showinfo("توجه", "اول یه آهنگ انتخاب کن")
            return

        encrypt_on = self.var_encrypt.get()
        passphrase = self.entry_passphrase.get()
        if encrypt_on and not passphrase:
            messagebox.showinfo("توجه", "برای رمزنگاری، یه رمز عبور بنویس (یا تیک رمزنگاری رو بردار)")
            return

        overhead = 44 if encrypt_on else 0
        budget = ac.MAX_PAYLOAD_BYTES - 1 - overhead

        if self.picked_image_bytes is None:
            text = self.txt_message.get("1.0", "end-1c")
            if not text:
                messagebox.showinfo("توجه", "یه متن بنویس یا یه عکس انتخاب کن")
                return
            if len(text.encode("utf-8")) > budget:
                messagebox.showinfo("توجه", "متن خیلی بلنده، کوتاهش کن")
                return
            self._start_sending(ac.TYPE_TEXT, text.encode("utf-8"), encrypt_on, passphrase)
            return

        image = self.picked_image_bytes
        if len(image) > budget:
            messagebox.showinfo("توجه", "این عکس با رمزنگاری جا نمی‌شه، یه عکس کوچیک‌تر انتخاب کن")
            return

        seconds = ac.frame_duration_ms(len(image) + 1 + overhead) / 1000.0
        minutes = seconds / 60.0
        if messagebox.askyesno(
            "ارسال عکس",
            f"فرستادن این عکس هر دور حدود {minutes:.1f} دقیقه طول می‌کشد و تا وقتی دکمه توقف را نزنی تکرار می‌شود. ادامه بدهم؟",
        ):
            self._start_sending(ac.TYPE_IMAGE, image, encrypt_on, passphrase)

    def _start_sending(self, type_: int, content: bytes, encrypt: bool, passphrase: str):
        self._set_playing_ui(True)
        self.lbl_status.configure(text="در حال آماده‌سازی...")
        self.engine.play(self.song_path, type_, content, self._power_value(), encrypt, passphrase)

    def _stop(self):
        self.engine.stop()

    def _on_status(self, text: str):
        self.app.root.after(0, lambda: self.lbl_status.configure(text=text))

    def _on_finished(self):
        self.app.root.after(0, lambda: self._set_playing_ui(False))


class ReceiverScreen(tk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app.content, bg=BG_COLOR)
        self.app = app
        add_photo_bg(self, app)
        self.ui_queue: queue.Queue = queue.Queue()
        self.engine = ReceiverEngine(self.ui_queue)

        self.final_text = ""
        self.final_image_bytes: bytes | None = None
        self.pending_encrypted_content: bytes | None = None
        self.pending_base_type = ac.TYPE_TEXT
        self._result_photo = None  # keep a reference so Tk doesn't GC it

        tk.Label(self, text="🎧  گیرنده", bg=BG_COLOR, fg=COLOR_WHITE,
                  font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=20, pady=(16, 10))

        self.entry_passphrase = tk.Entry(self, show="•", bg="#0f1430", fg=COLOR_WHITE,
                                           insertbackground=COLOR_WHITE, relief="flat",
                                           font=("Segoe UI", 11))
        self.entry_passphrase.insert(0, "")
        self.entry_passphrase.configure(
            validate="none",
        )
        self._ph = tk.Label(self, text="", bg=BG_COLOR)  # placeholder spacer, unused
        self.entry_passphrase.pack(fill="x", padx=20, pady=(0, 14), ipady=6)
        self.entry_passphrase.insert(0, "")
        self._set_passphrase_placeholder()

        state_card = glass_frame(self)
        state_card.pack(fill="x", padx=20, pady=(0, 10))
        row = tk.Frame(state_card, bg=PANEL_BG)
        row.pack(fill="x", padx=14, pady=12)
        self.dot = tk.Canvas(row, width=18, height=18, bg=PANEL_BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 16, 16, fill=COLOR_CYAN, outline="")
        self.dot.pack(side="left", padx=(0, 12))
        self.lbl_state = tk.Label(row, text="برای شروع، دکمه شنیدن را بزن", bg=PANEL_BG,
                                    fg=COLOR_WHITE, font=("Segoe UI", 11), wraplength=330, justify="left")
        self.lbl_state.pack(side="left", fill="x", expand=True)

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=20, pady=(0, 10))

        result_card = glass_frame(self)
        result_card.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.lbl_badge = tk.Label(result_card, text="", bg=PANEL_BG, fg=COLOR_WHITE,
                                    font=("Segoe UI", 11, "bold"))
        self.lbl_badge.pack(anchor="w", padx=14, pady=(14, 6))
        self.lbl_result = tk.Label(result_card, text="متن یا عکس دریافتی اینجا زنده نمایش داده می‌شود",
                                     bg=PANEL_BG, fg=COLOR_WHITE, font=("Segoe UI", 14),
                                     wraplength=380, justify="left")
        self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
        self.lbl_image = tk.Label(result_card, bg=PANEL_BG)

        self.btn_copy = styled_button(self, "کپی متن", self._copy_text)
        self.btn_save_image = styled_button(self, "ذخیره عکس در گالری", self._save_image)
        self.btn_retry = styled_button(self, "🔓  تلاش دوباره با این رمز", self._retry_decrypt)

        btn_row = tk.Frame(self, bg=BG_COLOR)
        btn_row.pack(fill="x", padx=20, pady=(6, 10), side="bottom")
        self.btn_listen = styled_button(btn_row, "شروع شنیدن", self._toggle_listen)
        self.btn_listen.pack(fill="x", ipady=8)
        self.btn_stop = styled_button(btn_row, "■  توقف", self._stop_listen, danger=True)

        back = tk.Label(self, text="← بازگشت", bg=BG_COLOR, fg=COLOR_DIM, font=("Segoe UI", 10), cursor="hand2")
        back.pack(anchor="w", padx=20, pady=(0, 6), side="bottom")
        back.bind("<Button-1>", lambda e: app.show_main())

        self._poll_queue()

    def _set_passphrase_placeholder(self):
        # Tk Entry has no native placeholder text; emulate it minimally.
        if not self.entry_passphrase.get():
            pass  # kept simple: a real placeholder isn't essential to functionality

    # ---- listening toggle --------------------------------------------------

    def _toggle_listen(self):
        self._begin_listening()

    def _begin_listening(self):
        if self.engine.is_listening():
            return
        self.final_text = ""
        self.final_image_bytes = None
        self.pending_encrypted_content = None
        self.lbl_result.configure(text="")
        self.lbl_image.pack_forget()
        self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
        self.lbl_badge.configure(text="")
        self.progress["value"] = 0
        self.btn_copy.pack_forget()
        self.btn_save_image.pack_forget()
        self.btn_retry.pack_forget()
        self.btn_listen.pack_forget()
        self.btn_stop.pack(fill="x", ipady=8)
        self.lbl_state.configure(text="در حال گوش دادن... آهنگ فرستنده را نزدیک میکروفون بگیر")
        self.engine.start()

    def _stop_listen(self):
        self.engine.stop()
        self.btn_stop.pack_forget()
        self.btn_listen.pack(fill="x", ipady=8)
        self.lbl_state.configure(text="متوقف شد")

    # ---- queue polling (Tk is not thread-safe, so the engine hands events
    # over via a queue and this timer drains it on the main thread) ---------

    def _poll_queue(self):
        try:
            while True:
                kind, args = self.ui_queue.get_nowait()
                self._handle_event(kind, args)
        except queue.Empty:
            pass
        self.app.root.after(30, self._poll_queue)

    def _handle_event(self, kind: str, args: tuple):
        if kind == "status":
            self.lbl_state.configure(text=args[0])
        elif kind == "searching":
            self.lbl_state.configure(text="منتظر شروع بعدی (sync)...")
        elif kind == "sync_locked":
            self.lbl_state.configure(text="سیگنال پیدا شد، در حال خواندن زنده...")
            self.lbl_badge.configure(text="● زنده", fg=COLOR_GREEN)
        elif kind == "progress":
            self._on_progress(*args)
        elif kind == "frame_failed":
            reason, attempts = args
            self.lbl_badge.configure(text=f"⚠ {reason} (تلاش {attempts})", fg=COLOR_AMBER)
            self.progress["value"] = 0
        elif kind == "success":
            type_, data = args
            self._on_success(type_, data)

    def _on_progress(self, bits_done, bits_total, type_, live_text, live_image_bytes):
        self.progress["maximum"] = max(bits_total, 1)
        self.progress["value"] = bits_done
        if type_ == -1:
            return

        base_type = type_ & ac.TYPE_MASK
        encrypted = (type_ & ac.FLAG_ENCRYPTED) != 0
        percent = int(bits_done * 100 / bits_total) if bits_total > 0 else 0

        if base_type == ac.TYPE_IMAGE and not encrypted:
            bmp = None
            if live_image_bytes:
                try:
                    img = Image.open(io.BytesIO(live_image_bytes))
                    img.load()
                    bmp = img
                except Exception:
                    bmp = None
            if bmp is not None:
                self.lbl_result.pack_forget()
                shown = bmp.copy()
                shown.thumbnail((360, 260))
                self._result_photo = ImageTk.PhotoImage(shown)
                self.lbl_image.configure(image=self._result_photo)
                self.lbl_image.pack(anchor="w", padx=14, pady=(0, 10))
            elif not self.lbl_image.winfo_ismapped():
                self.lbl_result.configure(text=f"در حال دریافت عکس... {percent}٪")
                self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
            self.lbl_badge.configure(text=f"📷 در حال لود شدن زنده... {percent}٪", fg=COLOR_GREEN)
            return

        self.lbl_image.pack_forget()
        self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
        if encrypted:
            self.lbl_result.configure(text=f"در حال دریافت داده‌ی رمزنگاری‌شده... {percent}٪")
        elif base_type == ac.TYPE_TEXT and live_text:
            self.lbl_result.configure(text=live_text)

    def _on_success(self, type_: int, data: bytes):
        base_type = type_ & ac.TYPE_MASK
        encrypted = (type_ & ac.FLAG_ENCRYPTED) != 0
        self.btn_stop.pack_forget()
        self.btn_listen.pack(fill="x", ipady=8)
        self.lbl_state.configure(text="پیام کامل دریافت شد")
        self.progress["value"] = self.progress["maximum"]
        self._apply_decoded(base_type, data, encrypted)

    def _retry_decrypt(self):
        if self.pending_encrypted_content is None:
            return
        self._apply_decoded(self.pending_base_type, self.pending_encrypted_content, True)

    def _apply_decoded(self, base_type: int, content: bytes, was_encrypted: bool):
        final_content: bytes
        if was_encrypted:
            passphrase = self.entry_passphrase.get()
            if not passphrase:
                self.pending_encrypted_content = content
                self.pending_base_type = base_type
                self.lbl_badge.configure(text="🔒 این پیام رمزنگاری شده - رمز را بنویس", fg=COLOR_AMBER)
                self.lbl_image.pack_forget()
                self.lbl_result.configure(text="")
                self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
                self.btn_retry.pack(fill="x", ipady=6, padx=0, pady=(0, 6), before=self._bottom_anchor())
                self.btn_copy.pack_forget()
                self.btn_save_image.pack_forget()
                return
            try:
                final_content = crypto_util.decrypt(content, passphrase)
            except crypto_util.WrongPassphraseException as e:
                self.pending_encrypted_content = content
                self.pending_base_type = base_type
                self.lbl_badge.configure(text=f"⚠ {e}", fg=COLOR_RED)
                self.lbl_image.pack_forget()
                self.lbl_result.configure(text="")
                self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
                self.btn_retry.pack(fill="x", ipady=6, padx=0, pady=(0, 6))
                self.btn_copy.pack_forget()
                self.btn_save_image.pack_forget()
                return
        else:
            final_content = content

        self.pending_encrypted_content = None
        self.btn_retry.pack_forget()
        self.lbl_badge.configure(
            text="✓ رمزگشایی شد و سالم است" if was_encrypted else "✓ سالم و تأیید شد",
            fg=COLOR_GREEN,
        )

        if base_type == ac.TYPE_IMAGE:
            bmp = None
            try:
                img = Image.open(io.BytesIO(final_content))
                img.load()
                bmp = img
            except Exception:
                bmp = None
            if bmp is not None:
                self.final_image_bytes = final_content
                self.final_text = ""
                self.lbl_result.pack_forget()
                shown = bmp.copy()
                shown.thumbnail((360, 260))
                self._result_photo = ImageTk.PhotoImage(shown)
                self.lbl_image.configure(image=self._result_photo)
                self.lbl_image.pack(anchor="w", padx=14, pady=(0, 10))
                self.btn_copy.pack_forget()
                self.btn_save_image.pack(fill="x", ipady=6, pady=(0, 6))
            else:
                self.lbl_image.pack_forget()
                self.lbl_result.configure(text="عکس دریافت شد ولی قابل نمایش نیست")
                self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
                self.btn_copy.pack_forget()
                self.btn_save_image.pack_forget()
        else:
            text = final_content.decode("utf-8", errors="replace")
            self.final_text = text
            self.final_image_bytes = None
            self.lbl_image.pack_forget()
            self.lbl_result.configure(text=text)
            self.lbl_result.pack(anchor="w", padx=14, pady=(0, 10), fill="both", expand=True)
            self.btn_save_image.pack_forget()
            self.btn_copy.pack(fill="x", ipady=6, pady=(0, 6))

    def _bottom_anchor(self):
        return self.btn_listen if self.btn_listen.winfo_ismapped() else self.btn_stop

    def _copy_text(self):
        if self.final_text:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(self.final_text)
            messagebox.showinfo("کپی شد", "متن در کلیپ‌بورد کپی شد")

    def _save_image(self):
        if not self.final_image_bytes:
            return
        path = filedialog.asksaveasfilename(
            title="ذخیره عکس", defaultextension=".jpg",
            filetypes=[("JPEG", "*.jpg")], initialfile="hiddenaudio.jpg",
        )
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(self.final_image_bytes)
            messagebox.showinfo("ذخیره شد", "عکس ذخیره شد")
        except Exception as e:
            messagebox.showerror("خطا", f"خطا در ذخیره عکس: {e}")


# --------------------------------------------------------------------------
# App shell
# --------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("آهنگ مخفی")
        root.geometry(f"{WIN_W}x{WIN_H}")
        root.resizable(False, False)

        bg_image = build_background(os.path.join(ASSETS_DIR, "background.jpg"), WIN_W, WIN_H)
        self.bg_image = bg_image
        self.bg_photo = ImageTk.PhotoImage(bg_image)
        self._bg_photo = self.bg_photo
        bg_label = tk.Label(root, image=self._bg_photo, bd=0)
        bg_label.place(x=0, y=0, relwidth=1, relheight=1)

        self.content = tk.Frame(root, bg=BG_COLOR)
        self.content.place(x=0, y=0, relwidth=1, relheight=1)
        # Make the content frame's own background transparent-looking by
        # never giving it (or its children, except explicit panels) a solid
        # background color -- Tk has no real alpha, so child widgets that
        # want to show the photo just use bg=BG_COLOR and we rely on them sitting
        # directly on top of the background label via place/pack ordering.
        self._apply_transparent(self.content)

        self.main_screen = MainMenuScreen(self)
        self.sender_screen = SenderScreen(self)
        self.receiver_screen = ReceiverScreen(self)
        for scr in (self.main_screen, self.sender_screen, self.receiver_screen):
            make_transparent_tree(self, scr)
        self.show_main()

    def _apply_transparent(self, widget):
        # Tk frames need *some* bg; match the window's default bg so frames
        # with bg=BG_COLOR fall back to something sane if ever shown standalone.
        try:
            widget.configure(bg=root_bg_fallback)
        except Exception:
            pass

    def _raise(self, screen: tk.Frame):
        for s in (self.main_screen, self.sender_screen, self.receiver_screen):
            s.place_forget()
        screen.place(x=0, y=0, relwidth=1, relheight=1)

    def show_main(self):
        self._raise(self.main_screen)

    def show_sender(self):
        self._raise(self.sender_screen)

    def show_receiver(self):
        self._raise(self.receiver_screen)


root_bg_fallback = BG_COLOR


def main():
    root = tk.Tk()
    try:
        root.configure(bg=root_bg_fallback)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
