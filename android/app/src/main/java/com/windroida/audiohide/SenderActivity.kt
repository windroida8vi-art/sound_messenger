package com.windroida.audiohide

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaPlayer
import android.net.Uri
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlin.math.max

class SenderActivity : AppCompatActivity() {

    private var songUri: Uri? = null
    private var mediaPlayer: MediaPlayer? = null
    @Volatile private var audioTrack: AudioTrack? = null
    private val running = AtomicBoolean(false)

    // When non-null, sending mode is "image" and the text field is ignored.
    private var pickedImageBytes: ByteArray? = null

    private lateinit var tvSongName: TextView
    private lateinit var tvStatus: TextView
    private lateinit var tvCounter: TextView
    private lateinit var etMessage: EditText
    private lateinit var btnSend: View
    private lateinit var btnStop: View
    private lateinit var pulse: View
    private lateinit var seekPower: android.widget.SeekBar
    private lateinit var tvPower: TextView

    private lateinit var imagePreviewRow: View
    private lateinit var imgPreview: ImageView
    private lateinit var tvImageInfo: TextView
    private lateinit var btnRemoveImage: View

    private lateinit var cbEncrypt: CheckBox
    private lateinit var etPassphrase: EditText

    private val pickSongLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        if (uri != null) {
            songUri = uri
            try {
                contentResolver.takePersistableUriPermission(
                    uri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            } catch (_: Exception) {
                // Some providers don't support persistable permissions; the
                // Uri is still usable for the rest of this session.
            }
            tvSongName.text = uri.lastPathSegment?.substringAfterLast('/') ?: "آهنگ انتخاب شد"
        }
    }

    private val pickImageLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        if (uri != null) loadAndCompressImage(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_sender)

        tvSongName = findViewById(R.id.tvSongName)
        tvStatus = findViewById(R.id.tvStatus)
        tvCounter = findViewById(R.id.tvCounter)
        etMessage = findViewById(R.id.etMessage)
        btnSend = findViewById(R.id.btnSend)
        btnStop = findViewById(R.id.btnStop)
        pulse = findViewById(R.id.pulse)
        seekPower = findViewById(R.id.seekPower)
        tvPower = findViewById(R.id.tvPower)

        imagePreviewRow = findViewById(R.id.imagePreviewRow)
        imgPreview = findViewById(R.id.imgPreview)
        tvImageInfo = findViewById(R.id.tvImageInfo)
        btnRemoveImage = findViewById(R.id.btnRemoveImage)

        cbEncrypt = findViewById(R.id.cbEncrypt)
        etPassphrase = findViewById(R.id.etPassphrase)

        seekPower.max = 90
        seekPower.progress = 25
        updatePower()
        seekPower.setOnSeekBarChangeListener(object : android.widget.SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: android.widget.SeekBar?, p: Int, fromUser: Boolean) { updatePower() }
            override fun onStartTrackingTouch(sb: android.widget.SeekBar?) {}
            override fun onStopTrackingTouch(sb: android.widget.SeekBar?) {}
        })

        findViewById<View>(R.id.btnPickSong).setOnClickListener {
            pickSongLauncher.launch(arrayOf("audio/*"))
        }
        findViewById<View>(R.id.btnPickImage).setOnClickListener {
            pickImageLauncher.launch(arrayOf("image/*"))
        }
        btnRemoveImage.setOnClickListener { clearPickedImage() }

        btnSend.setOnClickListener { confirmAndSend() }
        btnStop.setOnClickListener { stopAll("متوقف شد") }

        etMessage.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) { updateCounter() }
        })
        cbEncrypt.setOnCheckedChangeListener { _, _ -> updateCounter() }
        updateCounter()
        setPlayingUi(false)
    }

    private fun powerValue(): Double = (seekPower.progress + 10) / 100.0

    private fun updatePower() {
        tvPower.text = "قدرت سیگنال: ${seekPower.progress + 10}٪  (کمتر = مخفی‌تر، بیشتر = برد دورتر)"
    }

    private fun updateCounter() {
        if (pickedImageBytes != null) return // image info line already shows its own estimate
        val overhead = if (cbEncrypt.isChecked) 44 else 0
        val n = etMessage.text.toString().toByteArray(Charsets.UTF_8).size
        val max = AudioCodec.MAX_PAYLOAD_BYTES - 1 - overhead
        val sec = AudioCodec.frameDurationMs((n + 1 + overhead).coerceAtMost(AudioCodec.MAX_PAYLOAD_BYTES)) / 1000
        tvCounter.text = "$n / $max بایت  •  هر دور حدود $sec ثانیه"
        tvCounter.setTextColor(if (n > max) 0xFFFF8A80.toInt() else 0xB3FFFFFF.toInt())
    }

    /**
     * Downscales and compresses a picked image so it fits comfortably inside
     * the payload budget, since every extra byte costs ~0.8s of air time.
     */
    private fun loadAndCompressImage(uri: Uri) {
        try {
            val input = contentResolver.openInputStream(uri) ?: throw IllegalStateException("فایل باز نشد")
            val original = input.use { BitmapFactory.decodeStream(it) }
                ?: throw IllegalStateException("این فایل یک عکس معتبر نیست")

            // Kept deliberately tiny: this channel moves roughly 10 bytes/second,
            // so every extra byte adds real minutes to the send time.
            val maxDim = 64
            val scale = max(original.width, original.height).let { if (it <= 0) 1.0 else maxDim.toDouble() / it }
            val targetW = (original.width * scale).toInt().coerceAtLeast(1)
            val targetH = (original.height * scale).toInt().coerceAtLeast(1)
            val resized = if (scale < 1.0) {
                Bitmap.createScaledBitmap(original, targetW, targetH, true)
            } else {
                original
            }

            // Try progressively lower JPEG qualities until the result fits.
            var bytes: ByteArray? = null
            for (quality in intArrayOf(40, 25, 15, 8, 5)) {
                val out = ByteArrayOutputStream()
                resized.compress(Bitmap.CompressFormat.JPEG, quality, out)
                val candidate = out.toByteArray()
                if (candidate.size <= AudioCodec.MAX_PAYLOAD_BYTES - 1) {
                    bytes = candidate
                    break
                }
            }
            if (resized !== original) resized.recycle()
            original.recycle()

            if (bytes == null) {
                Toast.makeText(this, "این عکس حتی بعد از فشرده‌سازی خیلی بزرگه", Toast.LENGTH_SHORT).show()
                return
            }

            pickedImageBytes = bytes
            val bmpPreview = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            imgPreview.setImageBitmap(bmpPreview)
            imagePreviewRow.visibility = View.VISIBLE
            etMessage.isEnabled = false
            etMessage.alpha = 0.4f

            val sec = AudioCodec.frameDurationMs(bytes.size + 1) / 1000
            tvImageInfo.text = "${bytes.size} بایت  •  هر دور حدود $sec ثانیه"
            tvCounter.text = "حالت ارسال: عکس (متن نادیده گرفته می‌شود)"
            tvCounter.setTextColor(0xB3FFFFFF.toInt())
        } catch (e: Exception) {
            Toast.makeText(this, "خطا در خواندن عکس: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    private fun clearPickedImage() {
        pickedImageBytes = null
        imagePreviewRow.visibility = View.GONE
        imgPreview.setImageDrawable(null)
        etMessage.isEnabled = true
        etMessage.alpha = 1f
        updateCounter()
    }

    private fun setPlayingUi(playing: Boolean) {
        btnSend.visibility = if (playing) View.GONE else View.VISIBLE
        btnStop.visibility = if (playing) View.VISIBLE else View.GONE
        if (playing) Ui.startPulse(pulse) else Ui.stopPulse(pulse)
    }

    private fun confirmAndSend() {
        val uri = songUri
        if (uri == null) {
            Toast.makeText(this, "اول یه آهنگ انتخاب کن", Toast.LENGTH_SHORT).show()
            return
        }

        val encryptOn = cbEncrypt.isChecked
        val passphrase = etPassphrase.text.toString()
        if (encryptOn && passphrase.isEmpty()) {
            Toast.makeText(this, "برای رمزنگاری، یه رمز عبور بنویس (یا تیک رمزنگاری رو بردار)", Toast.LENGTH_SHORT).show()
            return
        }
        // Encryption adds ~44 bytes of overhead (salt+iv+tag), so the raw
        // content budget is a bit smaller whenever it's turned on.
        val overhead = if (encryptOn) 44 else 0
        val budget = AudioCodec.MAX_PAYLOAD_BYTES - 1 - overhead

        val image = pickedImageBytes
        if (image == null) {
            val text = etMessage.text.toString()
            if (text.isEmpty()) {
                Toast.makeText(this, "یه متن بنویس یا یه عکس انتخاب کن", Toast.LENGTH_SHORT).show()
                return
            }
            if (text.toByteArray(Charsets.UTF_8).size > budget) {
                Toast.makeText(this, "متن خیلی بلنده، کوتاهش کن", Toast.LENGTH_SHORT).show()
                return
            }
            startSending(AudioCodec.TYPE_TEXT, text.toByteArray(Charsets.UTF_8), encryptOn, passphrase)
            return
        }

        if (image.size > budget) {
            Toast.makeText(this, "این عکس با رمزنگاری جا نمی‌شه، یه عکس کوچیک‌تر انتخاب کن", Toast.LENGTH_SHORT).show()
            return
        }

        // Images take much longer over this very low-bandwidth channel, so
        // ask for confirmation with a clear time estimate first.
        val seconds = AudioCodec.frameDurationMs(image.size + 1 + overhead) / 1000
        val minutes = seconds / 60.0
        AlertDialog.Builder(this)
            .setTitle("ارسال عکس")
            .setMessage("فرستادن این عکس هر دور حدود ${"%.1f".format(minutes)} دقیقه طول می‌کشد و تا وقتی دکمه توقف را نزنی تکرار می‌شود. ادامه بدهم؟")
            .setPositiveButton("بله، بفرست") { _, _ -> startSending(AudioCodec.TYPE_IMAGE, image, encryptOn, passphrase) }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun startSending(type: Int, content: ByteArray, encrypt: Boolean, passphrase: String) {
        val uri = songUri ?: return

        stopAll(null)
        running.set(true)
        setPlayingUi(true)
        tvStatus.text = "در حال آماده‌سازی..."

        val amp = powerValue()
        thread {
            try {
                // PBKDF2 + AES-GCM run here, off the UI thread, since the
                // key derivation is deliberately a bit slow (by design, to
                // resist brute-forcing a weak passphrase).
                val (finalType, finalContent) = if (encrypt) {
                    Pair(type or AudioCodec.FLAG_ENCRYPTED, Crypto.encrypt(content, passphrase))
                } else {
                    Pair(type, content)
                }
                val frame = AudioCodec.buildFrame(finalType, finalContent, amp)
                runOnUiThread {
                    if (running.get()) {
                        startMusic(uri)
                        startToneLoop(frame)
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { stopAll("خطا در آماده‌سازی: ${e.message}") }
            }
        }
    }

    private fun startMusic(uri: Uri) {
        try {
            val mp = MediaPlayer()
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            )
            mp.setDataSource(this, uri)
            mp.isLooping = true
            mp.setOnPreparedListener { player ->
                if (running.get()) {
                    try {
                        player.start()
                    } catch (e: Exception) {
                        stopAll("خطا در پخش آهنگ: ${e.message}")
                    }
                }
            }
            mp.setOnErrorListener { _, _, _ ->
                stopAll("خطا در پخش آهنگ")
                true
            }
            mediaPlayer = mp
            mp.prepareAsync()
        } catch (e: Exception) {
            stopAll("خطا: ${e.message}")
        }
    }

    private fun startToneLoop(frame: ShortArray) {
        thread {
            var track: AudioTrack? = null
            try {
                val minBuf = AudioTrack.getMinBufferSize(
                    AudioCodec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT
                )
                if (minBuf <= 0) throw IllegalStateException("این دستگاه از پخش صدا پشتیبانی نمی‌کند")

                track = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build()
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(AudioCodec.SAMPLE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build()
                    )
                    .setBufferSizeInBytes(maxOf(minBuf * 2, 8192))
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build()
                audioTrack = track
                if (!running.get()) return@thread
                track.play()

                val pause = AudioCodec.generateSilence(AudioCodec.PAUSE_BETWEEN_FRAMES_MS)
                var round = 0
                while (running.get()) {
                    round++
                    val r = round
                    runOnUiThread {
                        if (running.get()) tvStatus.text = "در حال پخش آهنگ + داده مخفی (دور $r)"
                    }
                    if (!writeAll(track, frame)) break
                    if (!writeAll(track, pause)) break
                }
            } catch (e: Exception) {
                if (running.get()) runOnUiThread { stopAll("خطا در پخش داده: ${e.message}") }
            } finally {
                try { track?.pause() } catch (_: Exception) {}
                try { track?.flush() } catch (_: Exception) {}
                try { track?.stop() } catch (_: Exception) {}
                try { track?.release() } catch (_: Exception) {}
                if (audioTrack === track) audioTrack = null
            }
        }
    }

    private fun writeAll(track: AudioTrack, data: ShortArray): Boolean {
        var off = 0
        val chunk = 2048
        while (off < data.size) {
            if (!running.get()) return false
            val n = minOf(chunk, data.size - off)
            val w = track.write(data, off, n)
            if (w < 0) return false
            off += w
        }
        return true
    }

    /** Stops music and tones immediately. */
    private fun stopAll(status: String?) {
        running.set(false)
        mediaPlayer?.let {
            try { it.stop() } catch (_: Exception) {}
            try { it.release() } catch (_: Exception) {}
        }
        mediaPlayer = null
        // Cut queued tone audio right now; the tone thread releases the track.
        audioTrack?.let {
            try { it.pause() } catch (_: Exception) {}
            try { it.flush() } catch (_: Exception) {}
        }
        setPlayingUi(false)
        if (status != null) tvStatus.text = status
    }

    override fun onDestroy() {
        super.onDestroy()
        running.set(false)
        mediaPlayer?.let {
            try { it.stop() } catch (_: Exception) {}
            try { it.release() } catch (_: Exception) {}
        }
        mediaPlayer = null
        audioTrack?.let {
            try { it.pause() } catch (_: Exception) {}
            try { it.flush() } catch (_: Exception) {}
        }
    }
}
