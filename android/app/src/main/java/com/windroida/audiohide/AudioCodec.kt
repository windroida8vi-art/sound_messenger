package com.windroida.audiohide

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * Wire protocol v3 (near-ultrasonic FSK, repeated frames, CRC-16).
 *
 * Frame layout: SYNC tone (19kHz, 400ms), then a gap (100ms), then the
 * header -- len16 repeated 3 times, crc16 repeated 2 times, then the type
 * byte, then the content bytes. Each bit is a 100ms tone: 0 = 17.5kHz,
 * 1 = 18.5kHz, MSB first.
 *
 * "content" is whatever TYPE_TEXT/TYPE_IMAGE says it is: UTF-8 text bytes,
 * or a small compressed JPEG. The sender repeats the frame until stopped.
 * The receiver validates the length/CRC (each triplicated/duplicated and
 * bit-voted) and also votes across repeated frames, so a corrupted message
 * is never shown as "final".
 */
object AudioCodec {

    const val SAMPLE_RATE = 44100

    const val SYNC_FREQ = 19000.0
    const val FREQ_ZERO = 17500.0
    const val FREQ_ONE = 18500.0
    const val CONTROL_FREQ_1 = 15000.0
    const val CONTROL_FREQ_2 = 16200.0

    const val SYNC_MS = 400
    const val GAP_MS = 100
    const val BIT_MS = 100
    const val MARGIN_MS = 12      // ignore edges of each bit window
    const val BLOCK_MS = 10       // sync detection block
    const val RAMP_MS = 5         // fade in/out of each tone (no clicks)
    const val PAUSE_BETWEEN_FRAMES_MS = 500

    const val SYNC_WINDOW_BLOCKS = 15        // sync is detected by integrating 150ms
    const val SYNC_ABS_FLOOR = 1.7e5         // on the summed (Hann) energy of the window
    const val SYNC_VS_BASELINE = 5.0         // vs median 19kHz energy of the last ~1.5s
    const val SYNC_VS_DATA = 4.0             // vs 17.5k / 18.5k energy
    const val EDGE_BEFORE_BLOCKS = 30
    const val EDGE_AFTER_BLOCKS = 8

    // Content type stored as the first payload byte. Bit 7 is a separate
    // "this content is AES-encrypted" flag, so a received byte of e.g.
    // (TYPE_TEXT or FLAG_ENCRYPTED) means "encrypted text". AudioCodec
    // itself does not know or care about encryption; it only ever moves
    // opaque bytes. Encryption is handled by Crypto + the activities.
    const val TYPE_TEXT = 0
    const val TYPE_IMAGE = 1
    const val TYPE_MASK = 0x7F
    const val FLAG_ENCRYPTED = 0x80

    // MAX_PAYLOAD_BYTES counts the type byte + the actual content. Kept
    // modest on purpose: every extra byte costs ~0.8s of air time and a few
    // more KB of memory for the generated tone buffer.
    const val MAX_PAYLOAD_BYTES = 3000
    // len (2 bytes x3 copies = 6) + crc16 (2 bytes x2 copies = 4) = 10, soft-combined.
    const val HEADER_BYTES = 10
    const val DEFAULT_AMPLITUDE = 0.35

    fun msToSamples(ms: Int): Int = SAMPLE_RATE * ms / 1000

    /** [payloadBytes] = type byte + content byte count. */
    fun frameDurationMs(payloadBytes: Int): Long =
        (SYNC_MS + GAP_MS + (HEADER_BYTES + payloadBytes) * 8 * BIT_MS).toLong()

    fun crc16(data: ByteArray, length: Int = data.size): Int {
        var crc = 0xFFFF
        for (i in 0 until length) {
            crc = crc xor ((data[i].toInt() and 0xFF) shl 8)
            for (b in 0 until 8) {
                crc = if (crc and 0x8000 != 0) ((crc shl 1) xor 0x1021) and 0xFFFF
                else (crc shl 1) and 0xFFFF
            }
        }
        return crc
    }

    fun generateTone(freqHz: Double, durationMs: Int, amplitude: Double): ShortArray {
        val n = msToSamples(durationMs)
        val ramp = msToSamples(RAMP_MS).coerceAtMost(n / 2)
        val out = ShortArray(n)
        for (i in 0 until n) {
            var env = 1.0
            if (i < ramp) env = 0.5 - 0.5 * cos(PI * i / ramp)
            else if (i >= n - ramp) env = 0.5 - 0.5 * cos(PI * (n - 1 - i) / ramp)
            val v = sin(2.0 * PI * i * freqHz / SAMPLE_RATE) * amplitude * env
            out[i] = (v * Short.MAX_VALUE).toInt().toShort()
        }
        return out
    }

    fun generateSilence(durationMs: Int): ShortArray = ShortArray(msToSamples(durationMs))

    /** Convenience overload for plain text frames. */
    fun buildFrame(text: String, amplitude: Double = DEFAULT_AMPLITUDE): ShortArray =
        buildFrame(TYPE_TEXT, text.toByteArray(Charsets.UTF_8), amplitude)

    /**
     * One complete frame: sync + gap + header + payload.
     * [type] (TYPE_TEXT or TYPE_IMAGE) is stored as the first payload byte,
     * so the receiver knows how to interpret the bytes once decoded.
     */
    fun buildFrame(type: Int, content: ByteArray, amplitude: Double = DEFAULT_AMPLITUDE): ShortArray {
        val maxContent = (MAX_PAYLOAD_BYTES - 1).coerceAtLeast(0)
        val contentLen = content.size.coerceAtMost(maxContent)
        val data = ByteArray(1 + contentLen)
        data[0] = type.toByte()
        System.arraycopy(content, 0, data, 1, contentLen)

        val len = data.size
        val crc = crc16(data)
        val payload = ByteArray(HEADER_BYTES + len)
        // length: 2 bytes, repeated 3 times (bytes 0-5)
        payload[0] = (len shr 8).toByte(); payload[1] = (len and 0xFF).toByte()
        payload[2] = (len shr 8).toByte(); payload[3] = (len and 0xFF).toByte()
        payload[4] = (len shr 8).toByte(); payload[5] = (len and 0xFF).toByte()
        // crc16: 2 bytes, repeated 2 times (bytes 6-9)
        payload[6] = (crc shr 8).toByte(); payload[7] = (crc and 0xFF).toByte()
        payload[8] = (crc shr 8).toByte(); payload[9] = (crc and 0xFF).toByte()
        System.arraycopy(data, 0, payload, HEADER_BYTES, len)

        // Written directly into one pre-sized buffer (instead of building many
        // small arrays and concatenating them) so a large image frame doesn't
        // briefly need double the memory just to assemble itself.
        val syncSamples = msToSamples(SYNC_MS)
        val gapSamples = msToSamples(GAP_MS)
        val bitSamples = msToSamples(BIT_MS)
        val totalSamples = syncSamples + gapSamples + payload.size * 8 * bitSamples
        val result = ShortArray(totalSamples)

        var offset = writeTone(result, 0, SYNC_FREQ, syncSamples, amplitude)
        offset += gapSamples // silence: array is already zero-filled
        for (b in payload) {
            val v = b.toInt() and 0xFF
            for (bitIndex in 7 downTo 0) {
                val bit = (v shr bitIndex) and 1
                val freq = if (bit == 1) FREQ_ONE else FREQ_ZERO
                offset = writeTone(result, offset, freq, bitSamples, amplitude)
            }
        }
        return result
    }

    /** Writes one ramped tone into [dest] starting at [start]; returns the next free offset. */
    private fun writeTone(dest: ShortArray, start: Int, freqHz: Double, n: Int, amplitude: Double): Int {
        val ramp = msToSamples(RAMP_MS).coerceAtMost(n / 2)
        for (i in 0 until n) {
            var env = 1.0
            if (i < ramp) env = 0.5 - 0.5 * cos(PI * i / ramp)
            else if (i >= n - ramp) env = 0.5 - 0.5 * cos(PI * (n - 1 - i) / ramp)
            val v = sin(2.0 * PI * i * freqHz / SAMPLE_RATE) * amplitude * env
            dest[start + i] = (v * Short.MAX_VALUE).toInt().toShort()
        }
        return start + n
    }

    private val hannCache = HashMap<Int, DoubleArray>()

    @Synchronized
    private fun hann(n: Int): DoubleArray = hannCache.getOrPut(n) {
        DoubleArray(n) { 0.5 - 0.5 * cos(2.0 * PI * it / (n - 1).coerceAtLeast(1)) }
    }

    /** Goertzel with a Hann window (less leakage from loud music). */
    fun goertzelEnergy(samples: ShortArray, offset: Int, length: Int, freq: Double): Double {
        if (length <= 0) return 0.0
        val omega = 2.0 * PI * freq / SAMPLE_RATE
        val coeff = 2.0 * cos(omega)
        val w = hann(length)
        var q1 = 0.0
        var q2 = 0.0
        for (i in 0 until length) {
            val q0 = coeff * q1 - q2 + samples[offset + i] * w[i]
            q2 = q1
            q1 = q0
        }
        return q1 * q1 + q2 * q2 - coeff * q1 * q2
    }
}
