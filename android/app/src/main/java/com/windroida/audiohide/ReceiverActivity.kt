package com.windroida.audiohide

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.View
import android.widget.EditText
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlin.concurrent.thread

class ReceiverActivity : AppCompatActivity() {

    private lateinit var tvState: TextView
    private lateinit var tvResult: TextView
    private lateinit var imgResult: ImageView
    private lateinit var tvBadge: TextView
    private lateinit var progress: ProgressBar
    private lateinit var btnListen: TextView
    private lateinit var btnStop: View
    private lateinit var btnCopy: View
    private lateinit var btnSaveImage: View
    private lateinit var btnRetryDecrypt: View
    private lateinit var etPassphrase: EditText
    private lateinit var pulse: View

    @Volatile private var isListening = false
    private var finalText: String = ""
    private var finalImageBytes: ByteArray? = null

    // Set whenever a frame arrives encrypted but couldn't be decrypted yet
    // (wrong/empty passphrase), so "retry" can re-try without re-listening.
    private var pendingEncryptedContent: ByteArray? = null
    private var pendingBaseType: Int = AudioCodec.TYPE_TEXT

    private val requestMicPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) beginListening()
        else Toast.makeText(this, "بدون دسترسی میکروفون نمی‌شه شنید", Toast.LENGTH_SHORT).show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_receiver)

        tvState = findViewById(R.id.tvState)
        tvResult = findViewById(R.id.tvResult)
        imgResult = findViewById(R.id.imgResult)
        tvBadge = findViewById(R.id.tvBadge)
        progress = findViewById(R.id.progress)
        btnListen = findViewById(R.id.btnListen)
        btnStop = findViewById(R.id.btnStop)
        btnCopy = findViewById(R.id.btnCopy)
        btnSaveImage = findViewById(R.id.btnSaveImage)
        btnRetryDecrypt = findViewById(R.id.btnRetryDecrypt)
        etPassphrase = findViewById(R.id.etPassphrase)
        pulse = findViewById(R.id.pulse)

        btnListen.setOnClickListener {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED
            ) beginListening()
            else requestMicPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
        btnStop.setOnClickListener { stopListening("متوقف شد") }
        btnCopy.setOnClickListener {
            if (finalText.isNotEmpty()) {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("message", finalText))
                Toast.makeText(this, "کپی شد", Toast.LENGTH_SHORT).show()
            }
        }
        btnSaveImage.setOnClickListener { saveReceivedImage() }
        btnRetryDecrypt.setOnClickListener { retryDecrypt() }
        setListeningUi(false)
    }

    private fun setListeningUi(listening: Boolean) {
        btnListen.visibility = if (listening) View.GONE else View.VISIBLE
        btnStop.visibility = if (listening) View.VISIBLE else View.GONE
        if (listening) Ui.startPulse(pulse) else Ui.stopPulse(pulse)
    }

    private fun beginListening() {
        if (isListening) return
        isListening = true
        finalText = ""
        finalImageBytes = null
        pendingEncryptedContent = null
        tvResult.text = ""
        tvResult.visibility = View.VISIBLE
        imgResult.visibility = View.GONE
        imgResult.setImageDrawable(null)
        tvBadge.text = ""
        progress.progress = 0
        btnCopy.visibility = View.GONE
        btnSaveImage.visibility = View.GONE
        btnRetryDecrypt.visibility = View.GONE
        setListeningUi(true)
        tvState.text = "در حال گوش دادن... آهنگ فرستنده را نزدیک میکروفون بگیر"
        thread { runDecodeLoop() }
    }

    /** Called after a frame decoded but decryption failed; re-tries with whatever passphrase is typed now. */
    private fun retryDecrypt() {
        val encrypted = pendingEncryptedContent ?: return
        applyDecoded(pendingBaseType, encrypted, wasEncrypted = true)
    }

    /**
     * Handles one fully decoded frame: decrypts if needed, then shows the
     * result as text or image. On a wrong/missing passphrase it stashes the
     * raw bytes so [retryDecrypt] can try again without re-listening.
     */
    private fun applyDecoded(baseType: Int, content: ByteArray, wasEncrypted: Boolean) {
        val finalContent: ByteArray
        if (wasEncrypted) {
            val pass = etPassphrase.text.toString()
            if (pass.isEmpty()) {
                pendingEncryptedContent = content
                pendingBaseType = baseType
                tvBadge.text = "🔒 این پیام رمزنگاری شده - رمز را بنویس"
                tvBadge.setTextColor(0xFFFFD180.toInt())
                tvResult.visibility = View.VISIBLE
                tvResult.text = ""
                imgResult.visibility = View.GONE
                btnRetryDecrypt.visibility = View.VISIBLE
                btnCopy.visibility = View.GONE
                btnSaveImage.visibility = View.GONE
                return
            }
            try {
                finalContent = Crypto.decrypt(content, pass)
            } catch (e: Crypto.WrongPassphraseException) {
                pendingEncryptedContent = content
                pendingBaseType = baseType
                tvBadge.text = "⚠ ${e.message}"
                tvBadge.setTextColor(0xFFFF8A80.toInt())
                tvResult.visibility = View.VISIBLE
                tvResult.text = ""
                imgResult.visibility = View.GONE
                btnRetryDecrypt.visibility = View.VISIBLE
                btnCopy.visibility = View.GONE
                btnSaveImage.visibility = View.GONE
                return
            }
        } else {
            finalContent = content
        }

        pendingEncryptedContent = null
        btnRetryDecrypt.visibility = View.GONE
        tvBadge.text = if (wasEncrypted) "✓ رمزگشایی شد و سالم است" else "✓ سالم و تأیید شد"
        tvBadge.setTextColor(0xFF69F0AE.toInt())

        if (baseType == AudioCodec.TYPE_IMAGE) {
            val bmp = try {
                BitmapFactory.decodeByteArray(finalContent, 0, finalContent.size)
            } catch (_: Exception) {
                null
            }
            if (bmp != null) {
                finalImageBytes = finalContent
                finalText = ""
                tvResult.visibility = View.GONE
                imgResult.visibility = View.VISIBLE
                imgResult.setImageBitmap(bmp)
                btnCopy.visibility = View.GONE
                btnSaveImage.visibility = View.VISIBLE
            } else {
                tvResult.visibility = View.VISIBLE
                tvResult.alpha = 1f
                tvResult.text = "عکس دریافت شد ولی قابل نمایش نیست"
                btnCopy.visibility = View.GONE
                btnSaveImage.visibility = View.GONE
            }
        } else {
            val text = String(finalContent, Charsets.UTF_8)
            finalText = text
            finalImageBytes = null
            imgResult.visibility = View.GONE
            tvResult.visibility = View.VISIBLE
            tvResult.alpha = 1f
            tvResult.text = text
            btnCopy.visibility = View.VISIBLE
            btnSaveImage.visibility = View.GONE
        }
    }

    private fun saveReceivedImage() {
        val bytes = finalImageBytes ?: return
        try {
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                ?: throw IllegalStateException("عکس قابل خواندن نیست")
            val name = "hiddenaudio_${System.currentTimeMillis()}.jpg"
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, name)
                    put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                    put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES)
                }
                val uri = contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
                    ?: throw IllegalStateException("ذخیره‌سازی ممکن نشد")
                contentResolver.openOutputStream(uri)?.use { out ->
                    bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 95, out)
                } ?: throw IllegalStateException("ذخیره‌سازی ممکن نشد")
            } else {
                @Suppress("DEPRECATION")
                val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES)
                if (!dir.exists()) dir.mkdirs()
                val file = java.io.File(dir, name)
                java.io.FileOutputStream(file).use { out ->
                    bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 95, out)
                }
                MediaStore.Images.Media.insertImage(contentResolver, file.absolutePath, name, null)
            }
            Toast.makeText(this, "عکس در گالری ذخیره شد", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "خطا در ذخیره عکس: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    private fun stopListening(msg: String) {
        isListening = false
        runOnUiThread {
            setListeningUi(false)
            tvState.text = msg
        }
    }

    private fun createRecord(bufBytes: Int): AudioRecord? {
        val sources = intArrayOf(
            MediaRecorder.AudioSource.UNPROCESSED,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC
        )
        for (src in sources) {
            try {
                val r = AudioRecord(
                    src, AudioCodec.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufBytes
                )
                if (r.state == AudioRecord.STATE_INITIALIZED) return r
                r.release()
            } catch (_: Exception) {
            }
        }
        return null
    }

    private fun runDecodeLoop() {
        val minBuf = AudioRecord.getMinBufferSize(
            AudioCodec.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            stopListening("این دستگاه از ضبط صدا پشتیبانی نمی‌کنه")
            return
        }
        val record = createRecord(minBuf * 4)
        if (record == null) {
            stopListening("میکروفون آماده نشد")
            return
        }

        val decoder = StreamDecoder(object : StreamDecoder.Callback {
            override fun onSearching() {
                runOnUiThread { if (isListening) tvState.text = "منتظر شروع بعدی (sync)..." }
            }

            override fun onSyncLocked() {
                runOnUiThread {
                    if (isListening) {
                        tvState.text = "سیگنال پیدا شد، در حال خواندن زنده..."
                        tvBadge.text = "● زنده"
                        tvBadge.setTextColor(0xFF69F0AE.toInt())
                    }
                }
            }

            override fun onProgress(bitsDone: Int, bitsTotal: Int, type: Int, liveText: String, liveImageBytes: ByteArray?) {
                runOnUiThread {
                    if (!isListening) return@runOnUiThread
                    progress.max = bitsTotal
                    progress.progress = bitsDone
                    if (type == -1) return@runOnUiThread // type byte hasn't arrived yet

                    val baseType = type and AudioCodec.TYPE_MASK
                    val encrypted = (type and AudioCodec.FLAG_ENCRYPTED) != 0
                    val percent = if (bitsTotal > 0) bitsDone * 100 / bitsTotal else 0

                    if (baseType == AudioCodec.TYPE_IMAGE && !encrypted) {
                        // Most JPEG decoders happily render whatever rows made
                        // it in before a truncated stream cuts off, so trying
                        // this on every new byte makes the picture visibly
                        // paint in live, top to bottom, well before it's done.
                        val bmp = liveImageBytes?.let {
                            try { BitmapFactory.decodeByteArray(it, 0, it.size) } catch (_: Exception) { null }
                        }
                        if (bmp != null) {
                            tvResult.visibility = View.GONE
                            imgResult.visibility = View.VISIBLE
                            imgResult.setImageBitmap(bmp)
                        } else if (imgResult.visibility != View.VISIBLE) {
                            tvResult.visibility = View.VISIBLE
                            tvResult.alpha = 0.7f
                            tvResult.text = "در حال دریافت عکس... $percent٪"
                        }
                        tvBadge.text = "📷 در حال لود شدن زنده... $percent٪"
                        tvBadge.setTextColor(0xFF69F0AE.toInt())
                        return@runOnUiThread
                    }

                    imgResult.visibility = View.GONE
                    tvResult.visibility = View.VISIBLE
                    tvResult.alpha = 0.7f
                    tvResult.text = when {
                        encrypted -> "در حال دریافت داده‌ی رمزنگاری‌شده... $percent٪"
                        baseType == AudioCodec.TYPE_TEXT -> if (liveText.isNotEmpty()) liveText else tvResult.text
                        else -> tvResult.text
                    }
                }
            }

            override fun onFrameFailed(reason: String, attempts: Int) {
                runOnUiThread {
                    if (isListening) {
                        tvBadge.text = "⚠ $reason (تلاش $attempts)"
                        tvBadge.setTextColor(0xFFFFD180.toInt())
                        progress.progress = 0
                    }
                }
            }

            override fun onSuccess(type: Int, data: ByteArray) {
                isListening = false
                val baseType = type and AudioCodec.TYPE_MASK
                val encrypted = (type and AudioCodec.FLAG_ENCRYPTED) != 0
                runOnUiThread {
                    setListeningUi(false)
                    tvState.text = "پیام کامل دریافت شد"
                    progress.progress = progress.max
                    applyDecoded(baseType, data, encrypted)
                }
            }
        })

        try {
            record.startRecording()
            val chunk = ShortArray(2048)
            while (isListening) {
                val n = record.read(chunk, 0, chunk.size)
                if (n < 0) break
                if (n > 0) decoder.feed(chunk, n)
            }
        } catch (e: Exception) {
            if (isListening) stopListening("خطا: ${e.message}")
        } finally {
            try { record.stop() } catch (_: Exception) {}
            try { record.release() } catch (_: Exception) {}
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isListening = false
    }
}
