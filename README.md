# HiddenAudio

Hide a short message (text or a small image) inside a song, and decode it live on the other side.
Optional AES-256 encryption with a passphrase.

| Folder | What it is |
|---|---|
| [`android/`](android) | Android app (Kotlin, Android Studio project) |
| [`windows/`](windows) | Windows desktop app (Python + Tkinter) |

## Windows

Requires Python 3.10+ (tick "Add to PATH" when installing).

```
cd windows
run.bat
```

`run.bat` creates a `.venv`, installs `requirements.txt` on first run, then starts the app.
Optional: drop a `background.jpg` into `windows/assets/` to use it as the app background.

## Android

Open the `android/` folder in Android Studio, let Gradle sync, then Run.
Package: `com.windroida.audiohide`.

## Notes

- Share the passphrase through a different channel than the audio itself.
- Add a `LICENSE` file of your choice before publishing.

---

## فارسی

پیام کوتاه (متن یا عکس کوچک) رو داخل آهنگ پنهان کن و سمت گیرنده به‌صورت زنده بخونش. رمزنگاری AES-256 اختیاریه.

- **ویندوز:** پایتون نصب کن، داخل پوشه‌ی `windows` فایل `run.bat` رو اجرا کن.
- **اندروید:** پوشه‌ی `android` رو با Android Studio باز کن و Run بزن.
