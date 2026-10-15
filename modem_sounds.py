#!/usr/bin/env python3
"""
Modem Sounds Demo
=================
Generates classic modem handshake sounds and Bell 103 FSK data
through your speakers. No hardware modem needed.

Press Ctrl-C to stop.
"""

import sys
import math
import time
import json
import argparse
import numpy as np

SAMPLE_RATE = 48000

# Bell 103 frequencies
ORIG_MARK  = 1270.0
ORIG_SPACE = 1070.0
ANSW_MARK  = 2225.0
ANSW_SPACE = 2025.0

BAUD = 300
SAMPLES_PER_BIT = SAMPLE_RATE // BAUD  # 160

# ---------------------------------------------------------------------------
# Tone / waveform generators
# ---------------------------------------------------------------------------

def sine(freq, duration, sr=SAMPLE_RATE):
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    return np.sin(2.0 * math.pi * freq * t)

def dtmf_digit(digit, duration=0.1, sr=SAMPLE_RATE):
    """Generate a DTMF tone for a single digit."""
    freqs = {
        '1': (697, 1209), '2': (697, 1336), '3': (697, 1477),
        '4': (770, 1209), '5': (770, 1336), '6': (770, 1477),
        '7': (852, 1209), '8': (852, 1336), '9': (852, 1477),
        '0': (941, 1336),
        '*': (941, 1209), '#': (941, 1477),
    }
    if digit not in freqs:
        return np.zeros(int(duration * sr))
    f1, f2 = freqs[digit]
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    return np.sin(2.0 * math.pi * f1 * t) + np.sin(2.0 * math.pi * f2 * t)

def dial_tone(duration=3.0, sr=SAMPLE_RATE):
    """North American dial tone: 350 Hz + 440 Hz."""
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    return np.sin(2.0 * math.pi * 350 * t) + np.sin(2.0 * math.pi * 440 * t)

def busy_tone(duration=2.0, sr=SAMPLE_RATE):
    """Busy signal: 480 Hz + 620 Hz, 0.5s on / 0.5s off."""
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    cycle = int(0.5 * sr)
    sig = np.sin(2.0 * math.pi * 480 * t) + np.sin(2.0 * math.pi * 620 * t)
    mask = np.zeros(n)
    for start in range(0, n, cycle * 2):
        mask[start:min(start + cycle, n)] = 1.0
    return sig * mask

def ringback_tone(duration=4.0, sr=SAMPLE_RATE):
    """Ringback: 440 Hz + 480 Hz, 2s on / 4s off, repeated."""
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    sig = np.sin(2.0 * math.pi * 440 * t) + np.sin(2.0 * math.pi * 480 * t)
    cycle_on = int(2.0 * sr)
    cycle_off = int(4.0 * sr)
    mask = np.zeros(n)
    for start in range(0, n, cycle_on + cycle_off):
        mask[start:min(start + cycle_on, n)] = 1.0
    return sig * mask

# ---------------------------------------------------------------------------
# Bell 103 modulator
# ---------------------------------------------------------------------------

class Bell103Modulator:
    """Simple Bell 103 modulator: generates FSK audio from bytes."""

    def __init__(self, mode='originate'):
        self.mode = mode
        self.phase = 0.0
        self.bits = []
        self.bit_n = 0
        if mode == 'originate':
            self.mark_freq = ORIG_MARK
            self.space_freq = ORIG_SPACE
        else:
            self.mark_freq = ANSW_MARK
            self.space_freq = ANSW_SPACE

    def send_data(self, data):
        """Encode bytes: start bit (0) + 8 data bits LSB first + stop bit (1)."""
        for b in data:
            self.bits.append(0)  # start
            for i in range(8):
                self.bits.append((b >> i) & 1)
            self.bits.append(1)  # stop

    def generate(self, n_samples):
        out = np.zeros(n_samples, dtype=np.float64)
        for i in range(n_samples):
            bit = self.bits[0] if self.bits else 1
            freq = self.mark_freq if bit else self.space_freq
            out[i] = math.sin(self.phase)
            self.phase += 2.0 * math.pi * freq / SAMPLE_RATE
            if self.phase > 2.0 * math.pi:
                self.phase -= 2.0 * math.pi
            self.bit_n += 1
            if self.bit_n >= SAMPLES_PER_BIT:
                self.bit_n = 0
                if self.bits:
                    self.bits.pop(0)
        return out

    def is_busy(self):
        return len(self.bits) > 0

# ---------------------------------------------------------------------------
# Classic handshake sequences
# ---------------------------------------------------------------------------

def classic_handshake():
    """
    Classic US Robotics / Hayes modem handshake sequence:
    Dial → Ringback → Answer → Training → Data
    """
    parts = []

    # 0.5s silence
    parts.append(np.zeros(int(0.5 * SAMPLE_RATE)))

    # DTMF dialing: 951-652-1690
    dial_num = "9516521690"
    for d in dial_num:
        parts.append(dtmf_digit(d, 0.1))
        parts.append(np.zeros(int(0.08 * SAMPLE_RATE)))
    # pause after dialing
    parts.append(np.zeros(int(0.5 * SAMPLE_RATE)))

    # Ringback (simulating 1 ring)
    parts.append(ringback_tone(3.0))

    # --- Answer sequence (what the remote modem sends) ---
    # V.25 answer tone (2100 Hz) — 2 seconds
    ans_tone = sine(2100.0, 2.0)
    # Add 150 Hz amplitude modulation (ANSam) for the first part
    t = np.arange(len(ans_tone), dtype=np.float64) / SAMPLE_RATE
    ans_tone[:int(1.5*SAMPLE_RATE)] *= (1.0 + 0.5 * np.sin(2.0 * math.pi * 150 * t[:int(1.5*SAMPLE_RATE)]))
    parts.append(ans_tone)
    parts.append(np.zeros(int(0.2 * SAMPLE_RATE)))

    # Bell 103 answer tone (2225 Hz) — 1 second
    parts.append(sine(ANSW_MARK, 1.0))
    parts.append(np.zeros(int(0.1 * SAMPLE_RATE)))

    # --- Originating modem sends its carrier ---
    parts.append(sine(ORIG_MARK, 1.5))
    parts.append(np.zeros(int(0.2 * SAMPLE_RATE)))

    # --- Training / scrambling sequence ---
    # Alternating marks and spaces at 300 baud
    scram = Bell103Modulator(mode='originate')
    # Send a pattern: 0x55 = 01010101 = alternating bits = training pattern
    scram.send_data(b'\x55' * 20)
    buf = []
    while scram.is_busy():
        buf.append(scram.generate(SAMPLES_PER_BIT))
    parts.append(np.concatenate(buf))

    # 0x00 bytes = all spaces
    scram2 = Bell103Modulator(mode='originate')
    scram2.send_data(b'\x00' * 10)
    buf = []
    while scram2.is_busy():
        buf.append(scram2.generate(SAMPLES_PER_BIT))
    parts.append(np.concatenate(buf))

    parts.append(np.zeros(int(0.3 * SAMPLE_RATE)))

    # --- Data transmission: Hello message ---
    msg = b"WELCOME TO THE BELL 103 MODEM\r\nRUSTY & EDIE'S BBS LINE\r\nCONNECT 300\r\n\r\n> "
    mod = Bell103Modulator(mode='originate')
    mod.send_data(msg)
    buf = []
    while mod.is_busy():
        buf.append(mod.generate(SAMPLES_PER_BIT))
    parts.append(np.concatenate(buf))

    # Silence
    parts.append(np.zeros(int(0.5 * SAMPLE_RATE)))

    # Disconnect (carrier drop)
    # Short warble before hangup
    parts.append(sine(1400, 0.15))
    parts.append(sine(1800, 0.15))
    parts.append(sine(1400, 0.15))

    parts.append(np.zeros(int(1.0 * SAMPLE_RATE)))

    return np.concatenate(parts)


def answer_mode_demo():
    """
    Simulate being the answer modem (like a BBS):
    Answer tone → wait for originate → send banner
    """
    parts = []
    parts.append(np.zeros(int(0.3 * SAMPLE_RATE)))

    # Answer tone — 2225 Hz
    parts.append(sine(ANSW_MARK, 2.0))
    parts.append(np.zeros(int(0.2 * SAMPLE_RATE)))

    # Simulate receiving: originate carrier appears
    parts.append(sine(ORIG_MARK, 1.0))
    parts.append(np.zeros(int(0.1 * SAMPLE_RATE)))

    # Data exchange (answer mode)
    mod = Bell103Modulator(mode='answer')
    mod.send_data(b"HELLO FROM THE BBS!\r\nYOU ARE CONNECTED AT 300 BAUD.\r\n\r\n")
    buf = []
    while mod.is_busy():
        buf.append(mod.generate(SAMPLES_PER_BIT))
    parts.append(np.concatenate(buf))

    parts.append(np.zeros(int(0.5 * SAMPLE_RATE)))

    # Originate responds
    mod2 = Bell103Modulator(mode='originate')
    mod2.send_data(b"I LOVE BELL 103 MODEMS!\r\n")
    buf = []
    while mod2.is_busy():
        buf.append(mod2.generate(SAMPLES_PER_BIT))
    parts.append(np.concatenate(buf))

    parts.append(np.zeros(int(0.3 * SAMPLE_RATE)))
    return np.concatenate(parts)


def pure_bell103_carrier(duration=5.0):
    """Just a pure Bell 103 originate mark tone — the classic carrier."""
    return sine(ORIG_MARK, duration)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Modem Sounds Demo")
    parser.add_argument("--handshake", action="store_true", default=True,
                        help="Full handshake sequence (default)")
    parser.add_argument("--answer", action="store_true",
                        help="Answer-mode BBS demo")
    parser.add_argument("--carrier", type=float, default=None, metavar="SEC",
                        help="Pure Bell 103 carrier tone for N seconds")
    args = parser.parse_args()

    print("=" * 60)
    print("   MODEM SOUNDS — Classic Bell 103 Handshake")
    print("=" * 60)
    print()
    print("  Press Ctrl-C to stop.")
    print()

    try:
        import sounddevice as sd

        # Determine which demo
        if args.carrier:
            sig = pure_bell103_carrier(args.carrier)
            label = f"Bell 103 originate carrier ({ORIG_MARK} Hz) — {args.carrier}s"
        elif args.answer:
            sig = answer_mode_demo()
            label = "Answer-mode BBS demo (∼10s)"
        else:
            sig = classic_handshake()
            label = "Full classic handshake (∼25s)"

        # Normalize
        peak = np.max(np.abs(sig))
        if peak > 0:
            sig = sig / peak * 0.8

        print(f"  Playing: {label}")
        print(f"  Duration: {len(sig)/SAMPLE_RATE:.1f}s")
        print(f"  Sample rate: {SAMPLE_RATE} Hz")
        print()

        # Find a good output device
        pulse_idx = None
        default_idx = None
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_output_channels'] > 0:
                if 'pulse' in dev['name']:
                    pulse_idx = i
                if 'default' in dev['name']:
                    default_idx = i

        device = pulse_idx if pulse_idx is not None else default_idx
        if device is not None:
            print(f"  Output device [{device}]: {sd.query_devices()[device]['name']}")
        print()

        sd.play(sig, samplerate=SAMPLE_RATE, device=device)

        # Show a simple progress meter
        total_samples = len(sig)
        duration = total_samples / SAMPLE_RATE
        start = time.time()
        while sd.get_stream().active:
            elapsed = time.time() - start
            frac = min(elapsed / duration, 1.0)
            bar_len = 40
            filled = int(frac * bar_len)
            bar = '█' * filled + '░' * (bar_len - filled)
            sys.stdout.write(f'\r  [{bar}] {frac*100:4.0f}%')
            sys.stdout.flush()
            time.sleep(0.1)

        print()
        print()
        print("  ✓ Done!")

    except KeyboardInterrupt:
        sd.stop()
        print("\n  Stopped.")
    except ImportError:
        print("ERROR: sounddevice not installed.")
        print("  pip install sounddevice numpy")
        sys.exit(1)
    except Exception as e:
        print(f"\n  Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
