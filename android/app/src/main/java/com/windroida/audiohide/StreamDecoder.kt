package com.windroida.audiohide

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction

/**
 * Streaming decoder: feed raw mic samples, get live text + a final CRC-verified result.
 * Not thread-safe; call feed() from one thread only.
 *
 * Sync: integrates the 19kHz energy over 150ms and compares it to a running
 * baseline, then locates the exact end of the sync tone with a step detector
 * (works at much lower SNR than per-block thresholds).
 */
class StreamDecoder(private val cb: Callback) {

    interface Callback {
        fun onSearching()
        fun onSyncLocked()
        /**
         * [type] is -1 until the first content byte (the type marker) has
         * arrived. [liveImageBytes] carries the image content decoded so
         * far (type byte already stripped) whenever the frame is an
         * unencrypted image, so the UI can try rendering it right away --
         * most JPEG decoders will happily show whatever scanlines made it
         * in before the stream cuts off, which is what makes the picture
         * look like it is loading in live, top to bottom. It is null for
         * every other kind of update (encrypted content has nothing
         * meaningful to preview, since decryption only succeeds once the
         * whole thing has arrived and passed its integrity check).
         */
        fun onProgress(bitsDone: Int, bitsTotal: Int, type: Int, liveText: String, liveImageBytes: ByteArray?)
        fun onFrameFailed(reason: String, attempts: Int)
        /** [data] is the content only (type byte already stripped off). */
        fun onSuccess(type: Int, data: ByteArray)
    }

    private enum class State { SEARCH, DATA, DONE }

    private var buf = ShortArray(AudioCodec.SAMPLE_RATE * 2)
    private var size = 0
    private var state = State.SEARCH

    // SEARCH: per-block history
    private var pos = 0
    private var locked = false
    private val hE19 = ArrayList<Double>()
    private val hE0 = ArrayList<Double>()
    private val hE1 = ArrayList<Double>()
    private val hStart = ArrayList<Int>()

    // DATA
    private var dataStart = 0
    private var bitIdx = 0
    private var totalBits = AudioCodec.HEADER_BYTES * 8
    private val maxBits = (AudioCodec.HEADER_BYTES + AudioCodec.MAX_PAYLOAD_BYTES) * 8
    private val soft = FloatArray(maxBits)

    // voting across repeated frames
    private var accSoft: FloatArray? = null
    private var accLen = -1
    private var accCount = 0
    private var attempts = 0

    private val blockSamples = AudioCodec.msToSamples(AudioCodec.BLOCK_MS)
    private val bitSamples = AudioCodec.msToSamples(AudioCodec.BIT_MS)
    private val marginSamples = AudioCodec.msToSamples(AudioCodec.MARGIN_MS)
    private val maxHist = 160

    fun feed(chunk: ShortArray, n: Int) {
        if (state == State.DONE || n <= 0) return
        if (size + n > buf.size) {
            var newCap = buf.size * 2
            while (newCap < size + n) newCap *= 2
            buf = buf.copyOf(newCap)
        }
        System.arraycopy(chunk, 0, buf, size, n)
        size += n
        process()
    }

    private fun process() {
        while (state != State.DONE) {
            val progressed = if (state == State.SEARCH) searchStep() else dataStep()
            if (!progressed) return
        }
    }

    private fun clearHistory() {
        hE19.clear(); hE0.clear(); hE1.clear(); hStart.clear()
        locked = false
    }

    private fun windowFlag(): Boolean {
        val w = AudioCodec.SYNC_WINDOW_BLOCKS
        val n = hE19.size
        if (n < w + 20) return false
        var e = 0.0; var e0 = 0.0; var e1 = 0.0
        for (i in n - w until n) { e += hE19[i]; e0 += hE0[i]; e1 += hE1[i] }
        if (e < AudioCodec.SYNC_ABS_FLOOR) return false
        if (e < AudioCodec.SYNC_VS_DATA * maxOf(e0, e1)) return false
        val older = DoubleArray(n - w) { hE19[it] }
        older.sort()
        val baseline = older[older.size / 2]
        return e > AudioCodec.SYNC_VS_BASELINE * w * baseline
    }

    /** Finds the block index where the sync tone stops (step detector). */
    private fun findEdge(): Int {
        val n = hE19.size
        val before = AudioCodec.EDGE_BEFORE_BLOCKS
        val after = AudioCodec.EDGE_AFTER_BLOCKS
        var best = -Double.MAX_VALUE
        var bestJ = -1
        val from = maxOf(before + 5, n - 90)
        for (j in from until n - 5) {
            var sb = 0.0
            for (i in j - before until j) sb += hE19[i]
            var sa = 0.0
            var cnt = 0
            for (i in j until minOf(j + after, n)) { sa += hE19[i]; cnt++ }
            val sc = sb / before - sa / cnt
            if (sc > best) { best = sc; bestJ = j }
        }
        return bestJ
    }

    private fun searchStep(): Boolean {
        if (size - pos < blockSamples) return false

        // drop old audio so memory stays small while idle
        if (pos > AudioCodec.SAMPLE_RATE * 6) {
            val drop = pos - AudioCodec.SAMPLE_RATE * 3
            System.arraycopy(buf, drop, buf, 0, size - drop)
            size -= drop
            pos -= drop
            for (i in hStart.indices) hStart[i] = hStart[i] - drop
        }

        hE19.add(AudioCodec.goertzelEnergy(buf, pos, blockSamples, AudioCodec.SYNC_FREQ))
        hE0.add(AudioCodec.goertzelEnergy(buf, pos, blockSamples, AudioCodec.FREQ_ZERO))
        hE1.add(AudioCodec.goertzelEnergy(buf, pos, blockSamples, AudioCodec.FREQ_ONE))
        hStart.add(pos)
        if (hE19.size > maxHist) {
            hE19.removeAt(0); hE0.removeAt(0); hE1.removeAt(0); hStart.removeAt(0)
        }
        pos += blockSamples

        if (windowFlag()) {
            locked = true
        } else if (locked) {
            locked = false
            val j = findEdge()
            if (j >= 0 && hStart[j] >= 0) {
                val edge = hStart[j]
                dataStart = edge +
                    AudioCodec.msToSamples(AudioCodec.GAP_MS) -
                    AudioCodec.msToSamples(5)
                bitIdx = 0
                totalBits = AudioCodec.HEADER_BYTES * 8
                clearHistory()
                state = State.DATA
                cb.onSyncLocked()
            }
        }
        return true
    }

    private fun bitSum(src: FloatArray, startByte: Int, copies: Int, byteCount: Int, byteOffset: Int): Int {
        // combines [copies] repeated groups of [byteCount] bytes starting at [startByte]
        var v = 0
        for (b in 0 until byteCount * 8) {
            var s = 0f
            for (c in 0 until copies) s += src[(startByte + c * byteCount) * 8 + b]
            v = (v shl 1) or (if (s > 0f) 1 else 0)
        }
        return v
    }

    // len: 2 bytes, repeated 3 times at byte offsets 0,2,4 (10-byte header).
    private fun headerLen(src: FloatArray): Int = bitSum(src, 0, 3, 2, 0)
    // crc16: 2 bytes, repeated 2 times at byte offsets 6,8.
    private fun headerCrc(src: FloatArray): Int = bitSum(src, 6, 2, 2, 0)

    private fun dataByte(src: FloatArray, index: Int): Int {
        var v = 0
        val base = (AudioCodec.HEADER_BYTES + index) * 8
        for (i in 0 until 8) v = (v shl 1) or (if (src[base + i] > 0f) 1 else 0)
        return v
    }

    private fun dataStep(): Boolean {
        val ws = dataStart + bitIdx * bitSamples + marginSamples
        val we = dataStart + (bitIdx + 1) * bitSamples - marginSamples
        if (size < we) return false

        val len = we - ws
        val e0 = AudioCodec.goertzelEnergy(buf, ws, len, AudioCodec.FREQ_ZERO)
        val e1 = AudioCodec.goertzelEnergy(buf, ws, len, AudioCodec.FREQ_ONE)
        soft[bitIdx] = ((e1 - e0) / (e1 + e0 + 1e-9)).toFloat()
        bitIdx++

        val headerBits = AudioCodec.HEADER_BYTES * 8
        if (bitIdx == headerBits) {
            val l = headerLen(soft)
            if (l > AudioCodec.MAX_PAYLOAD_BYTES || l == 0) {
                failFrame("هدر خراب")
                return true
            }
            totalBits = headerBits + l * 8
        }

        if (bitIdx % 8 == 0) {
            if (bitIdx > headerBits) {
                // Byte 0 of the data section is always the type/flags marker.
                val dataLen = (bitIdx - headerBits) / 8
                val type = dataByte(soft, 0)
                val baseType = type and AudioCodec.TYPE_MASK
                val encrypted = (type and AudioCodec.FLAG_ENCRYPTED) != 0
                when {
                    // Encrypted bytes are ciphertext either way, so there is
                    // nothing meaningful to preview live for them.
                    encrypted -> cb.onProgress(bitIdx, totalBits, type, "", null)
                    baseType == AudioCodec.TYPE_TEXT -> {
                        val contentLen = dataLen - 1
                        val live = ByteArray(contentLen) { dataByte(soft, it + 1).toByte() }
                        cb.onProgress(bitIdx, totalBits, type, safeUtf8(live), null)
                    }
                    baseType == AudioCodec.TYPE_IMAGE && dataLen > 1 -> {
                        val contentLen = dataLen - 1
                        val partial = ByteArray(contentLen) { dataByte(soft, it + 1).toByte() }
                        cb.onProgress(bitIdx, totalBits, type, "", partial)
                    }
                    else -> cb.onProgress(bitIdx, totalBits, type, "", null)
                }
            } else {
                cb.onProgress(bitIdx, totalBits, -1, "", null)
            }
        }

        if (bitIdx >= totalBits) finishFrame()
        return true
    }

    private fun verify(src: FloatArray, len: Int): ByteArray? {
        val data = ByteArray(len) { dataByte(src, it).toByte() }
        return if (AudioCodec.crc16(data) == headerCrc(src)) data else null
    }

    private fun finishFrame() {
        val len = headerLen(soft)

        verify(soft, len)?.let { succeed(it); return }

        // CRC failed -> combine with previous repeats of the same message
        val prev = accSoft
        if (prev != null && accLen == len) {
            for (i in 0 until totalBits) prev[i] += soft[i]
            accCount++
        } else {
            accSoft = soft.copyOf()
            accLen = len
            accCount = 1
        }
        val acc = accSoft!!
        if (accCount >= 2) {
            verify(acc, len)?.let { succeed(it); return }
        }
        if (accCount >= 8) {
            accSoft = null
            accCount = 0
        }
        failFrame("CRC نامعتبر - منتظر تکرار بعدی")
    }

    private fun succeed(data: ByteArray) {
        state = State.DONE
        val type = if (data.isNotEmpty()) data[0].toInt() and 0xFF else AudioCodec.TYPE_TEXT
        val content = if (data.size > 1) data.copyOfRange(1, data.size) else ByteArray(0)
        cb.onSuccess(type, content)
    }

    private fun failFrame(reason: String) {
        attempts++
        state = State.SEARCH
        pos = (dataStart + bitIdx * bitSamples).coerceIn(0, size)
        clearHistory()
        cb.onFrameFailed(reason, attempts)
        cb.onSearching()
    }

    private fun safeUtf8(b: ByteArray): String {
        for (cut in 0..3) {
            if (b.size - cut < 0) break
            try {
                val dec = Charsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                return dec.decode(ByteBuffer.wrap(b, 0, b.size - cut)).toString()
            } catch (_: CharacterCodingException) {
            }
        }
        return String(b, Charsets.UTF_8)
    }
}
