"""
Bell 103 Modem Engine
=====================
Implements a 300 baud FSK modem per the Bell 103 standard using
Goertzel tone detection on a 1-bit window with timing recovery.

Frequencies:
  Originate (caller):  Mark=1270 Hz, Space=1070 Hz
  Answer  (answerer):  Mark=2225 Hz, Space=2025 Hz

Format: Async serial, 1 start bit (space), 8 data bits (LSB first),
        1 stop bit (mark). Speed: 300 baud.
"""

import math
import threading
import numpy as np
from collections import deque

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BELL103_ORIGINATE_MARK  = 1270.0
BELL103_ORIGINATE_SPACE = 1070.0
BELL103_ANSWER_MARK     = 2225.0
BELL103_ANSWER_SPACE    = 2025.0

BAUD_RATE = 300
SAMPLE_RATE = 48000
SAMPLES_PER_BIT = SAMPLE_RATE // BAUD_RATE  # 160
HALF_BIT = SAMPLES_PER_BIT // 2  # 80
HUNT_STRIDE = SAMPLES_PER_BIT // 8  # 20 samples — fine-grained hunt

DEFAULT_DATA_BITS = 8
DEFAULT_STOP_BITS = 1

# Energy threshold: Goertzel output below this is noise, not a valid tone
GOERTZEL_ENERGY_THRESHOLD = 10.0

# Timing recovery: how many consecutive space decisions confirm start
# At HUNT_STRIDE=20, 4 decisions = 80 samples = 50% of start bit
START_CONFIRM_THRESHOLD = 4
# After losing sync, how many mark decisions before we accept idle
IDLE_MARK_THRESHOLD = 4


def _goertzel(samples, target_freq):
    """Goertzel filter for exact frequency. Returns magnitude squared."""
    N = len(samples)
    omega = 2.0 * math.pi * target_freq / SAMPLE_RATE
    coeff = 2.0 * math.cos(omega)
    q1, q2 = 0.0, 0.0
    for s in samples:
        q0 = coeff * q1 - q2 + s
        q2, q1 = q1, q0
    return max(0.0, q1*q1 + q2*q2 - coeff*q1*q2)


def _goertzel_block(block, mark_freq, space_freq):
    """Compute Goertzel for both mark and space on a block. Returns (mark_mag, space_mag, bit)."""
    mm = _goertzel(block, mark_freq)
    sm = _goertzel(block, space_freq)
    return mm, sm, 1 if mm > sm else 0


class Bell103Modem:
    """
    Bell 103 FSK modem with Goertzel tone detection and timing recovery.
    """

    def __init__(self, mode='originate'):
        self.mode = mode
        self._set_frequencies(mode)

        # ---- Transmit ----
        self.tx_phase = 0.0
        self.tx_bits = deque()
        self.tx_bit_n = 0

        # ---- Receive buffer ----
        self.rx_buf = deque()

        # ---- Timing recovery state ----
        self.rx_state = 'HUNT'          # HUNT | LOCKED
        self.rx_last_hbit = 1           # last half-bit decision (start in mark)
        self.rx_space_streak = 0        # consecutive space decisions during hunt
        self.rx_mark_streak = 0         # consecutive mark decisions (idle check)
        self.rx_bits_until_sample = 0   # samples to consume before next full-bit decision

        self.rx_frame_state = 'IDLE'
        self.rx_frame_byte = 0
        self.rx_frame_nbits = 0
        self.rx_frame_active_start = False  # synthetic start bit flag

        # ---- Output ----
        self.rx_bytes = bytearray()
        self.rx_byte_callbacks = []

        # ---- Carrier detect ----
        self.carrier_energy = 0.0
        self.carrier_detect = False
        self.energy_alpha = 0.001
        self.carrier_on  = 0.02
        self.carrier_off = 0.01
        self.carrier_detected_callbacks = []
        self.carrier_lost_callbacks = []

        # ---- Misc ----
        self.debug = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    def _set_frequencies(self, mode):
        self.mode = mode
        if mode == 'originate':
            self.tx_mark   = BELL103_ORIGINATE_MARK
            self.tx_space  = BELL103_ORIGINATE_SPACE
            self.rx_mark   = BELL103_ANSWER_MARK
            self.rx_space  = BELL103_ANSWER_SPACE
        else:
            self.tx_mark   = BELL103_ANSWER_MARK
            self.tx_space  = BELL103_ANSWER_SPACE
            self.rx_mark   = BELL103_ORIGINATE_MARK
            self.rx_space  = BELL103_ORIGINATE_SPACE

    def set_mode(self, mode):
        self._set_frequencies(mode)
        self.reset()

    # ------------------------------------------------------------------
    # Transmit
    # ------------------------------------------------------------------
    def send_byte(self, byte):
        with self.lock:
            self._encode_byte(byte)

    def send_data(self, data):
        with self.lock:
            for b in data:
                self._encode_byte(b)

    def send_break(self, duration=0.25):
        with self.lock:
            n = int(duration * BAUD_RATE)
            self.tx_bits.extend([0] * n)

    def _encode_byte(self, byte):
        self.tx_bits.append(0)  # start
        for i in range(DEFAULT_DATA_BITS):
            self.tx_bits.append((byte >> i) & 1)
        for _ in range(DEFAULT_STOP_BITS):
            self.tx_bits.append(1)

    def generate_tx_samples(self, num_samples):
        out = np.zeros(num_samples, dtype=np.float64)
        for i in range(num_samples):
            bit = self.tx_bits[0] if self.tx_bits else 1
            freq = self.tx_mark if bit else self.tx_space
            out[i] = math.sin(self.tx_phase)
            self.tx_phase += 2.0 * math.pi * freq / SAMPLE_RATE
            if self.tx_phase > 2.0 * math.pi:
                self.tx_phase -= 2.0 * math.pi
            self.tx_bit_n += 1
            if self.tx_bit_n >= SAMPLES_PER_BIT:
                self.tx_bit_n = 0
                if self.tx_bits:
                    self.tx_bits.popleft()
        return out

    # ------------------------------------------------------------------
    # Receive / Demodulation
    # ------------------------------------------------------------------
    def feed_rx_samples(self, samples):
        """
        Feed received audio samples to the modem for demodulation.
        Samples are processed in 160-sample blocks (1 bit at 300 baud).

        For real-world use where sample alignment is unknown, call
        sync_from_samples() first to align bit timing, then use this
        method for ongoing demodulation.
        """
        self.rx_buf.extend(samples)

        while len(self.rx_buf) >= SAMPLES_PER_BIT:
            block = [self.rx_buf.popleft() for _ in range(SAMPLES_PER_BIT)]

            mark_mag, space_mag, bit = _goertzel_block(block, self.rx_mark, self.rx_space)

            self._process_bit(bit)

    def sync_from_samples(self, samples):
        """
        Find the start bit (first space after silence or mark) and align to D0.

        Strategy:
          1. Find the first sample with significant amplitude (|sample| > 0.01
             above the noise floor).  This marks the start of the transmission.
          2. Verify that the first full-bit block at that position is space-
             dominant (i.e., it is the start bit).  If it is mark-dominant
             (e.g. a carrier tone), scan forward for the mark→space transition
             using the half-bit look-back check.
          3. D0 = start_bit_position + SAMPLES_PER_BIT.
             Place remaining samples after D0 into rx_buf.

        After this call succeeds, use feed_rx_samples() for ongoing
        demodulation of aligned 160-sample blocks.
        """
        n = len(samples)
        qb = SAMPLES_PER_BIT // 4  # 40 — coarse step

        if n < SAMPLES_PER_BIT * 2:
            return

        # ---- Step 1: Find first non-silence sample ----
        # Use amplitude threshold (works better than RMS for exact edge)
        TH = 0.01  # Above expected noise floor (∼0.006 for stddev 0.002)
        signal_start = -1
        for i in range(min(n, SAMPLES_PER_BIT * 4)):  # search first 4 bits
            if abs(samples[i]) > TH:
                signal_start = i
                break

        if signal_start < 0:
            return  # No signal detected

        # ---- Step 2: Identify the start bit ----
        if signal_start + SAMPLES_PER_BIT > n:
            return  # Not enough samples for a full bit

        block = samples[signal_start:signal_start + SAMPLES_PER_BIT]
        mm = _goertzel(block, self.rx_mark)
        sm = _goertzel(block, self.rx_space)
        total = mm + sm

        if total < GOERTZEL_ENERGY_THRESHOLD:
            return  # Signal too weak

        if sm > mm:
            # Space-dominant at signal_start — this IS the start bit.
            transition = signal_start
        else:
            # Mark-dominant — this is a carrier / idle tone before data.
            # Scan forward for the first mark→space transition using the
            # half-bit look-back check.
            half = SAMPLES_PER_BIT // 2  # 80
            search_end = min(n - SAMPLES_PER_BIT, signal_start + SAMPLES_PER_BIT * 4)

            transition = -1
            for offset in range(signal_start, search_end):
                block = samples[offset:offset + SAMPLES_PER_BIT]
                mm = _goertzel(block, self.rx_mark)
                sm = _goertzel(block, self.rx_space)
                total = mm + sm

                if total < GOERTZEL_ENERGY_THRESHOLD:
                    continue

                if sm > mm:  # Space-dominant
                    if offset >= half:
                        prev_off = offset - half
                        prev_block = samples[prev_off:prev_off + SAMPLES_PER_BIT]
                        prev_mm = _goertzel(prev_block, self.rx_mark)
                        prev_sm = _goertzel(prev_block, self.rx_space)
                        prev_total = prev_mm + prev_sm

                        if prev_total < GOERTZEL_ENERGY_THRESHOLD or prev_sm <= prev_mm:
                            transition = offset
                            break
                    else:
                        # offset < half: look-back not possible.
                        # Since we know the signal starts before offset
                        # (at signal_start), this space is preceded by mark.
                        # Only accept if the preceding mark is confirmed.
                        check_mark = samples[signal_start:signal_start + SAMPLES_PER_BIT]
                        ck_mm = _goertzel(check_mark, self.rx_mark)
                        ck_sm = _goertzel(check_mark, self.rx_space)
                        if ck_mm > ck_sm:  # Confirmed mark before space
                            transition = offset
                            break

            if transition < 0:
                return  # No mark→space transition found

        # ---- Step 3: Align to D0 ----
        d0_start = transition + SAMPLES_PER_BIT
        if d0_start >= n:
            return

        remaining = samples[d0_start:]
        self.rx_buf.clear()
        self.rx_buf.extend(remaining)

        self.rx_frame_state = 'DATA'
        self.rx_frame_byte = 0
        self.rx_frame_nbits = 0
        self.rx_synced = True

        if self.debug:
            print(f"[MODEM] Sync: transition={transition}, "
                  f"D0={d0_start}, remaining={len(remaining)} samples")

    def is_synced(self):
        return getattr(self, 'rx_synced', False)

    # ------------------------------------------------------------------
    # Serial frame decoder  (same as before)
    # ------------------------------------------------------------------
    def _process_bit(self, bit):
        if self.rx_frame_state == 'IDLE':
            if bit == 0:  # start bit
                self.rx_frame_state = 'DATA'
                self.rx_frame_byte = 0
                self.rx_frame_nbits = 0
                if self.debug:
                    print("[MODEM] Start bit in frame")
        elif self.rx_frame_state == 'DATA':
            if bit:
                self.rx_frame_byte |= (1 << self.rx_frame_nbits)
            self.rx_frame_nbits += 1
            if self.rx_frame_nbits >= DEFAULT_DATA_BITS:
                self.rx_frame_state = 'STOP'
        elif self.rx_frame_state == 'STOP':
            if bit == 1:
                self._emit_byte(self.rx_frame_byte & 0xFF)
            elif self.debug:
                print("[MODEM] Framing error")
            self.rx_frame_state = 'IDLE'

    def _emit_byte(self, byte_val):
        self.rx_bytes.append(byte_val)
        for cb in self.rx_byte_callbacks:
            try:
                cb(byte_val)
            except Exception as e:
                print(f"[MODEM] byte cb: {e}")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_byte_received(self, callback):
        self.rx_byte_callbacks.append(callback)

    def on_carrier_detected(self, callback):
        self.carrier_detected_callbacks.append(callback)

    def on_carrier_lost(self, callback):
        self.carrier_lost_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def get_rx_bytes(self):
        data = bytes(self.rx_bytes)
        self.rx_bytes = bytearray()
        return data

    def is_busy(self):
        return len(self.tx_bits) > 0

    def is_carrier_present(self):
        return self.carrier_detect

    def reset(self):
        self.tx_phase = 0.0
        self.tx_bit_n = 0
        self.tx_bits.clear()
        self.rx_buf.clear()
        self.rx_state = 'HUNT'
        self.rx_last_hbit = 1
        self.rx_space_streak = 0
        self.rx_mark_streak = 0
        self.rx_bits_until_sample = 0
        self.rx_frame_state = 'IDLE'
        self.rx_bytes = bytearray()
        self.carrier_energy = 0.0
        self.carrier_detect = False
        self.rx_synced = False
        self.rx_byte_callbacks = []

    def generate_answer_tone(self, duration=3.0):
        n = int(duration * SAMPLE_RATE)
        t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
        return np.sin(2.0 * math.pi * BELL103_ANSWER_MARK * t)

    def generate_originate_tone(self, duration=3.0):
        n = int(duration * SAMPLE_RATE)
        t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
        return np.sin(2.0 * math.pi * BELL103_ORIGINATE_MARK * t)

    @staticmethod
    def resample_to_telephony(samples):
        return samples[::6]

    @staticmethod
    def resample_from_telephony(samples):
        return np.repeat(samples, 6)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def selftest():
    """Simple loopback test: aligned 160-sample chunks, basic demodulation."""
    print("Bell 103 Modem Self-Test (basic)")
    print("================================")

    tx = Bell103Modem(mode='originate')
    rx = Bell103Modem(mode='answer')

    received = bytearray()
    def on_byte(b):
        received.append(b)

    rx.on_byte_received(on_byte)

    test_data = b"Hello, BBS World! 12345"
    print(f"Sending: {test_data}")
    tx.send_data(test_data)

    import time
    timeout = time.time() + 5.0
    while tx.is_busy() and time.time() < timeout:
        chunk = SAMPLES_PER_BIT
        tx_samp = tx.generate_tx_samples(chunk)
        noise = np.random.normal(0, 0.002, len(tx_samp))
        rx.feed_rx_samples(tx_samp + noise)

    idle = np.zeros(SAMPLES_PER_BIT * 5, dtype=np.float64)
    rx.feed_rx_samples(idle)

    result = bytes(received)
    print(f"Received: {result}")

    if result == test_data:
        print("PASSED")
        return True
    else:
        print("FAILED")
        for i in range(max(len(test_data), len(result))):
            if i >= len(result):
                print(f"  Missing byte {i}: 0x{test_data[i]:02x}")
            elif i >= len(test_data):
                print(f"  Extra byte {i}: 0x{result[i]:02x}")
            elif test_data[i] != result[i]:
                print(f"  Diff at {i}: sent 0x{test_data[i]:02x} recv 0x{result[i]:02x}")
        return False


def selftest_offset():
    """
    Test timing recovery with a phase offset.
    Uses sync_from_samples() to align, then feed_rx_samples() for decoding.

    Includes a leading mark tone (simulating answer tone / idle carrier)
    so the signal always has a mark→space transition for the sync code to find.
    """
    import random as _random
    print("Bell 103 Offset Self-Test (sync + decode)")
    print("=========================================")

    tx = Bell103Modem(mode='originate')
    rx = Bell103Modem(mode='answer')

    test_data = b"Hello!"
    print(f"Sending: {test_data}")

    # Generate leading mark tone (simulating answer tone from remote modem)
    # Two full bits of mark = 320 samples at 300 baud
    lead_mark = []
    tx_phase = 0.0
    tx_mark_freq = BELL103_ORIGINATE_MARK
    n_lead = SAMPLES_PER_BIT * 4  # 4 bits of leading mark
    for i in range(n_lead):
        lead_mark.append(math.sin(tx_phase))
        tx_phase += 2.0 * math.pi * tx_mark_freq / SAMPLE_RATE
        if tx_phase > 2.0 * math.pi:
            tx_phase -= 2.0 * math.pi
    noise = np.random.normal(0, 0.002, n_lead)
    lead_mark = np.array(lead_mark, dtype=np.float64) + noise

    # Generate the data signal
    data_samples = []
    tx.send_data(test_data)
    import time
    timeout = time.time() + 3.0
    while tx.is_busy() and time.time() < timeout:
        chunk = SAMPLES_PER_BIT
        tx_samp = tx.generate_tx_samples(chunk)
        noise = np.random.normal(0, 0.002, len(tx_samp))
        data_samples.extend(tx_samp + noise)

    data_samples = np.array(data_samples, dtype=np.float64)

    # Full signal = leading mark + data
    all_samples = np.concatenate([lead_mark, data_samples])

    # Test various offsets within the DATA portion (after the leading mark)
    # Offset 0 = first sample of data = first start bit
    results = []
    for offset in [0, 40, 80, 120, 37, 99, 143]:
        rx.reset()
        received = bytearray()

        def _make_cb(buf):
            def cb(b):
                buf.append(b)
            return cb
        rx.on_byte_received(_make_cb(received))

        # Prefix with random silence (simulating real-world capture)
        silence = np.zeros(_random.randint(50, 200), dtype=np.float64)

        # Sync window: silence + leading mark tail + start of data at offset.
        # Always include at least 2 bits of leading mark so sync can find
        # the mark→space transition at the start of the data.
        sync_mark_samples = SAMPLES_PER_BIT * 2  # 2 bits of mark before data
        sync_start = max(0, n_lead + offset - sync_mark_samples)
        sync_end = n_lead + offset + SAMPLES_PER_BIT * 8  # 8 bits of data
        sync_buf = np.concatenate([
            silence,
            all_samples[sync_start:sync_end]
        ])
        rx.sync_from_samples(sync_buf)

        # Feed remaining data signal for decoding
        remaining = all_samples[sync_end:]
        rx.feed_rx_samples(remaining)

        # Feed idle to flush
        rx.feed_rx_samples(np.zeros(SAMPLES_PER_BIT * 5, dtype=np.float64))

        result = bytes(received)
        status = "PASS" if result == test_data else "FAIL"
        results.append((offset, status, result))
        print(f"  offset {offset:3d}: {status}  recv={result}")

    all_pass = all(s == "PASS" for _, s, _ in results)
    if all_pass:
        print("All offsets PASSED")
    else:
        print("Some offsets FAILED")
    return all_pass


if __name__ == '__main__':
    selftest()
    print()
    selftest_offset()
