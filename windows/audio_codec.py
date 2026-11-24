"""
Wire protocol v3 (near-ultrasonic FSK, repeated frames, CRC-16).
Direct port of the Android app's AudioCodec.kt, so a phone and this Windows
app can talk to each other over the exact same hidden-audio channel.

Frame layout: SYNC tone (19kHz, 400ms), then a gap (100ms), then the header
-- len16 repeated 3 times, crc16 repeated 2 times -- then the type byte,
then the content bytes. Each bit is a 100ms tone: 0 = 17.5kHz, 1 = 18.5kHz,
MSB first.

"content" is whatever TYPE_TEXT/TYPE_IMAGE says it is: UTF-8 text bytes, or
a small compressed JPEG, optionally AES-256-GCM encrypted (see crypto_util.py
and FLAG_ENCRYPTED below). The sender repeats the frame until stopped.
"""

import math
import numpy as np

SAMPLE_RATE = 44100

SYNC_FREQ = 19000.0
FREQ_ZERO = 17500.0
FREQ_ONE = 18500.0

SYNC_MS = 400
GAP_MS = 100
BIT_MS = 100
MARGIN_MS = 12       # ignore edges of each bit window
BLOCK_MS = 10        # sync detection block
RAMP_MS = 5          # fade in/out of each tone (no clicks)
PAUSE_BETWEEN_FRAMES_MS = 500

SYNC_WINDOW_BLOCKS = 15     # sync is detected by integrating 150ms
SYNC_ABS_FLOOR = 1.7e5      # on the summed (Hann) energy of the window
SYNC_VS_BASELINE = 5.0      # vs median 19kHz energy of the last ~1.5s
SYNC_VS_DATA = 4.0          # vs 17.5k / 18.5k energy
EDGE_BEFORE_BLOCKS = 30
EDGE_AFTER_BLOCKS = 8

# Content type stored as the first payload byte. Bit 7 is a separate
# "this content is AES-encrypted" flag (see FLAG_ENCRYPTED). AudioCodec
# itself does not know or care about encryption; it only ever moves opaque
# bytes. Encryption is handled by crypto_util + the UI.
TYPE_TEXT = 0
TYPE_IMAGE = 1
TYPE_MASK = 0x7F
FLAG_ENCRYPTED = 0x80

# MAX_PAYLOAD_BYTES counts the type byte + the actual content. Kept modest
# on purpose: every extra byte costs ~0.8s of air time.
MAX_PAYLOAD_BYTES = 3000
# len (2 bytes x3 copies = 6) + crc16 (2 bytes x2 copies = 4) = 10, soft-combined.
HEADER_BYTES = 10
DEFAULT_AMPLITUDE = 0.35


def ms_to_samples(ms: float) -> int:
    return int(SAMPLE_RATE * ms / 1000)


def frame_duration_ms(payload_bytes: int) -> int:
    """payload_bytes = type byte + content byte count."""
    return SYNC_MS + GAP_MS + (HEADER_BYTES + payload_bytes) * 8 * BIT_MS


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


_hann_cache: dict[int, np.ndarray] = {}


def _hann(n: int) -> np.ndarray:
    cached = _hann_cache.get(n)
    if cached is not None:
        return cached
    if n <= 1:
        w = np.ones(max(n, 1), dtype=np.float64)
    else:
        idx = np.arange(n, dtype=np.float64)
        w = 0.5 - 0.5 * np.cos(2.0 * math.pi * idx / (n - 1))
    _hann_cache[n] = w
    return w


def goertzel_energy(samples: np.ndarray, offset: int, length: int, freq: float) -> float:
    """Goertzel with a Hann window (less leakage from loud music)."""
    if length <= 0:
        return 0.0
    omega = 2.0 * math.pi * freq / SAMPLE_RATE
    coeff = 2.0 * math.cos(omega)
    w = _hann(length)
    seg = samples[offset:offset + length].astype(np.float64) * w
    q1 = 0.0
    q2 = 0.0
    for s in seg:
        q0 = coeff * q1 - q2 + s
        q2 = q1
        q1 = q0
    return q1 * q1 + q2 * q2 - coeff * q1 * q2


def _write_tone(dest: np.ndarray, start: int, freq_hz: float, n: int, amplitude: float) -> int:
    """Writes one ramped tone into dest starting at start; returns the next free offset."""
    ramp = min(ms_to_samples(RAMP_MS), n // 2)
    i = np.arange(n, dtype=np.float64)
    env = np.ones(n, dtype=np.float64)
    if ramp > 0:
        env[:ramp] = 0.5 - 0.5 * np.cos(math.pi * i[:ramp] / ramp)
        env[n - ramp:] = 0.5 - 0.5 * np.cos(math.pi * (n - 1 - i[n - ramp:]) / ramp)
    values = np.sin(2.0 * math.pi * i * freq_hz / SAMPLE_RATE) * amplitude * env
    dest[start:start + n] = (values * 32767.0).astype(np.int16)
    return start + n


def build_frame(type_: int, content: bytes, amplitude: float = DEFAULT_AMPLITUDE) -> np.ndarray:
    """One complete frame: sync + gap + header + payload, as int16 mono PCM samples."""
    max_content = max(MAX_PAYLOAD_BYTES - 1, 0)
    content = content[:max_content]
    data = bytes([type_ & 0xFF]) + content

    length = len(data)
    crc = crc16(data)
    payload = bytearray(HEADER_BYTES + length)
    # length: 2 bytes, repeated 3 times (bytes 0-5)
    payload[0] = (length >> 8) & 0xFF; payload[1] = length & 0xFF
    payload[2] = (length >> 8) & 0xFF; payload[3] = length & 0xFF
    payload[4] = (length >> 8) & 0xFF; payload[5] = length & 0xFF
    # crc16: 2 bytes, repeated 2 times (bytes 6-9)
    payload[6] = (crc >> 8) & 0xFF; payload[7] = crc & 0xFF
    payload[8] = (crc >> 8) & 0xFF; payload[9] = crc & 0xFF
    payload[HEADER_BYTES:HEADER_BYTES + length] = data

    sync_samples = ms_to_samples(SYNC_MS)
    gap_samples = ms_to_samples(GAP_MS)
    bit_samples = ms_to_samples(BIT_MS)
    total_samples = sync_samples + gap_samples + len(payload) * 8 * bit_samples
    result = np.zeros(total_samples, dtype=np.int16)

    offset = _write_tone(result, 0, SYNC_FREQ, sync_samples, amplitude)
    offset += gap_samples  # silence: array is already zero-filled
    for b in payload:
        for bit_index in range(7, -1, -1):
            bit = (b >> bit_index) & 1
            freq = FREQ_ONE if bit == 1 else FREQ_ZERO
            offset = _write_tone(result, offset, freq, bit_samples, amplitude)
    return result


def generate_silence(duration_ms: int) -> np.ndarray:
    return np.zeros(ms_to_samples(duration_ms), dtype=np.int16)
