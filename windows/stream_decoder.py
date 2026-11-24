"""
Streaming decoder: feed raw mic samples, get live text/image preview + a
final CRC-verified result. Direct port of the Android app's StreamDecoder.kt.
Not thread-safe; call feed() from one thread only.

Sync: integrates the 19kHz energy over 150ms and compares it to a running
baseline, then locates the exact end of the sync tone with a step detector
(works at much lower SNR than per-block thresholds).
"""

from __future__ import annotations

import numpy as np

import audio_codec as ac


class Callback:
    """Override these in a subclass (or just assign matching callables)."""

    def on_searching(self):
        pass

    def on_sync_locked(self):
        pass

    def on_progress(self, bits_done: int, bits_total: int, type_: int,
                     live_text: str, live_image_bytes: bytes | None):
        pass

    def on_frame_failed(self, reason: str, attempts: int):
        pass

    def on_success(self, type_: int, data: bytes):
        pass


class StreamDecoder:
    SEARCH, DATA, DONE = range(3)

    def __init__(self, cb: Callback):
        self.cb = cb

        self.buf = np.zeros(ac.SAMPLE_RATE * 2, dtype=np.int16)
        self.size = 0
        self.state = StreamDecoder.SEARCH

        # SEARCH: per-block history
        self.pos = 0
        self.locked = False
        self.hE19: list[float] = []
        self.hE0: list[float] = []
        self.hE1: list[float] = []
        self.hStart: list[int] = []

        # DATA
        self.data_start = 0
        self.bit_idx = 0
        self.total_bits = ac.HEADER_BYTES * 8
        self.max_bits = (ac.HEADER_BYTES + ac.MAX_PAYLOAD_BYTES) * 8
        self.soft = np.zeros(self.max_bits, dtype=np.float32)

        # voting across repeated frames
        self.acc_soft: np.ndarray | None = None
        self.acc_len = -1
        self.acc_count = 0
        self.attempts = 0

        self.block_samples = ac.ms_to_samples(ac.BLOCK_MS)
        self.bit_samples = ac.ms_to_samples(ac.BIT_MS)
        self.margin_samples = ac.ms_to_samples(ac.MARGIN_MS)
        self.max_hist = 160

    # ---- public API ----------------------------------------------------

    def feed(self, chunk: np.ndarray, n: int):
        if self.state == StreamDecoder.DONE or n <= 0:
            return
        if self.size + n > self.buf.size:
            new_cap = self.buf.size * 2
            while new_cap < self.size + n:
                new_cap *= 2
            grown = np.zeros(new_cap, dtype=np.int16)
            grown[:self.size] = self.buf[:self.size]
            self.buf = grown
        self.buf[self.size:self.size + n] = chunk[:n]
        self.size += n
        self._process()

    # ---- internals -------------------------------------------------------

    def _process(self):
        while self.state != StreamDecoder.DONE:
            progressed = self._search_step() if self.state == StreamDecoder.SEARCH else self._data_step()
            if not progressed:
                return

    def _clear_history(self):
        self.hE19.clear(); self.hE0.clear(); self.hE1.clear(); self.hStart.clear()
        self.locked = False

    def _window_flag(self) -> bool:
        w = ac.SYNC_WINDOW_BLOCKS
        n = len(self.hE19)
        if n < w + 20:
            return False
        e = sum(self.hE19[n - w:n])
        e0 = sum(self.hE0[n - w:n])
        e1 = sum(self.hE1[n - w:n])
        if e < ac.SYNC_ABS_FLOOR:
            return False
        if e < ac.SYNC_VS_DATA * max(e0, e1):
            return False
        older = sorted(self.hE19[:n - w])
        baseline = older[len(older) // 2]
        return e > ac.SYNC_VS_BASELINE * w * baseline

    def _find_edge(self) -> int:
        """Finds the block index where the sync tone stops (step detector)."""
        n = len(self.hE19)
        before = ac.EDGE_BEFORE_BLOCKS
        after = ac.EDGE_AFTER_BLOCKS
        best = -float("inf")
        best_j = -1
        start_from = max(before + 5, n - 90)
        for j in range(start_from, n - 5):
            sb = sum(self.hE19[j - before:j])
            tail = self.hE19[j:min(j + after, n)]
            sa = sum(tail)
            cnt = len(tail)
            if cnt == 0:
                continue
            score = sb / before - sa / cnt
            if score > best:
                best = score
                best_j = j
        return best_j

    def _search_step(self) -> bool:
        if self.size - self.pos < self.block_samples:
            return False

        # drop old audio so memory stays small while idle
        if self.pos > ac.SAMPLE_RATE * 6:
            drop = self.pos - ac.SAMPLE_RATE * 3
            self.buf[0:self.size - drop] = self.buf[drop:self.size]
            self.size -= drop
            self.pos -= drop
            self.hStart = [s - drop for s in self.hStart]

        self.hE19.append(ac.goertzel_energy(self.buf, self.pos, self.block_samples, ac.SYNC_FREQ))
        self.hE0.append(ac.goertzel_energy(self.buf, self.pos, self.block_samples, ac.FREQ_ZERO))
        self.hE1.append(ac.goertzel_energy(self.buf, self.pos, self.block_samples, ac.FREQ_ONE))
        self.hStart.append(self.pos)
        if len(self.hE19) > self.max_hist:
            del self.hE19[0]; del self.hE0[0]; del self.hE1[0]; del self.hStart[0]
        self.pos += self.block_samples

        if self._window_flag():
            self.locked = True
        elif self.locked:
            self.locked = False
            j = self._find_edge()
            if j >= 0 and self.hStart[j] >= 0:
                edge = self.hStart[j]
                self.data_start = edge + ac.ms_to_samples(ac.GAP_MS) - ac.ms_to_samples(5)
                self.bit_idx = 0
                self.total_bits = ac.HEADER_BYTES * 8
                self._clear_history()
                self.state = StreamDecoder.DATA
                self.cb.on_sync_locked()
        return True

    def _bit_sum(self, src: np.ndarray, start_byte: int, copies: int, byte_count: int) -> int:
        """Combines [copies] repeated groups of [byte_count] bytes starting at [start_byte]."""
        v = 0
        for b in range(byte_count * 8):
            s = 0.0
            for c in range(copies):
                s += src[(start_byte + c * byte_count) * 8 + b]
            v = (v << 1) | (1 if s > 0.0 else 0)
        return v

    def _header_len(self, src: np.ndarray) -> int:
        # len: 2 bytes, repeated 3 times at byte offsets 0,2,4 (10-byte header).
        return self._bit_sum(src, 0, 3, 2)

    def _header_crc(self, src: np.ndarray) -> int:
        # crc16: 2 bytes, repeated 2 times at byte offsets 6,8.
        return self._bit_sum(src, 6, 2, 2)

    def _data_byte(self, src: np.ndarray, index: int) -> int:
        v = 0
        base = (ac.HEADER_BYTES + index) * 8
        for i in range(8):
            v = (v << 1) | (1 if src[base + i] > 0.0 else 0)
        return v

    def _data_step(self) -> bool:
        ws = self.data_start + self.bit_idx * self.bit_samples + self.margin_samples
        we = self.data_start + (self.bit_idx + 1) * self.bit_samples - self.margin_samples
        if self.size < we:
            return False

        length = we - ws
        e0 = ac.goertzel_energy(self.buf, ws, length, ac.FREQ_ZERO)
        e1 = ac.goertzel_energy(self.buf, ws, length, ac.FREQ_ONE)
        self.soft[self.bit_idx] = (e1 - e0) / (e1 + e0 + 1e-9)
        self.bit_idx += 1

        header_bits = ac.HEADER_BYTES * 8
        if self.bit_idx == header_bits:
            l = self._header_len(self.soft)
            if l > ac.MAX_PAYLOAD_BYTES or l == 0:
                self._fail_frame("هدر خراب")
                return True
            self.total_bits = header_bits + l * 8

        if self.bit_idx % 8 == 0:
            if self.bit_idx > header_bits:
                data_len = (self.bit_idx - header_bits) // 8
                type_ = self._data_byte(self.soft, 0)
                base_type = type_ & ac.TYPE_MASK
                encrypted = (type_ & ac.FLAG_ENCRYPTED) != 0
                if encrypted:
                    # Encrypted bytes are ciphertext either way, so there is
                    # nothing meaningful to preview live for them.
                    self.cb.on_progress(self.bit_idx, self.total_bits, type_, "", None)
                elif base_type == ac.TYPE_TEXT:
                    content_len = data_len - 1
                    live = bytes(self._data_byte(self.soft, i + 1) for i in range(content_len))
                    self.cb.on_progress(self.bit_idx, self.total_bits, type_, self._safe_utf8(live), None)
                elif base_type == ac.TYPE_IMAGE and data_len > 1:
                    content_len = data_len - 1
                    partial = bytes(self._data_byte(self.soft, i + 1) for i in range(content_len))
                    self.cb.on_progress(self.bit_idx, self.total_bits, type_, "", partial)
                else:
                    self.cb.on_progress(self.bit_idx, self.total_bits, type_, "", None)
            else:
                self.cb.on_progress(self.bit_idx, self.total_bits, -1, "", None)

        if self.bit_idx >= self.total_bits:
            self._finish_frame()
        return True

    def _verify(self, src: np.ndarray, length: int) -> bytes | None:
        data = bytes(self._data_byte(src, i) for i in range(length))
        return data if ac.crc16(data) == self._header_crc(src) else None

    def _finish_frame(self):
        length = self._header_len(self.soft)

        result = self._verify(self.soft, length)
        if result is not None:
            self._succeed(result)
            return

        # CRC failed -> combine with previous repeats of the same message
        if self.acc_soft is not None and self.acc_len == length:
            self.acc_soft[:self.total_bits] += self.soft[:self.total_bits]
            self.acc_count += 1
        else:
            self.acc_soft = self.soft.copy()
            self.acc_len = length
            self.acc_count = 1

        if self.acc_count >= 2:
            result = self._verify(self.acc_soft, length)
            if result is not None:
                self._succeed(result)
                return
        if self.acc_count >= 8:
            self.acc_soft = None
            self.acc_count = 0

        self._fail_frame("CRC نامعتبر - منتظر تکرار بعدی")

    def _succeed(self, data: bytes):
        self.state = StreamDecoder.DONE
        type_ = data[0] if data else ac.TYPE_TEXT
        content = data[1:] if len(data) > 1 else b""
        self.cb.on_success(type_, content)

    def _fail_frame(self, reason: str):
        self.attempts += 1
        self.state = StreamDecoder.SEARCH
        self.pos = min(max(self.data_start + self.bit_idx * self.bit_samples, 0), self.size)
        self._clear_history()
        self.cb.on_frame_failed(reason, self.attempts)
        self.cb.on_searching()

    @staticmethod
    def _safe_utf8(b: bytes) -> str:
        for cut in range(0, 4):
            if len(b) - cut < 0:
                break
            try:
                return b[:len(b) - cut].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")
