"""
Audio I/O Layer
===============
Wraps sounddevice to capture and play 48 kHz audio for the Bell 103 modem.

Provides a real-time audio callback that connects a sounddevice stream
to a Bell103Modem instance for both transmit and receive.
"""

import time
import threading
import numpy as np
import sounddevice as sd

from .bell103 import SAMPLE_RATE, SAMPLES_PER_BIT, Bell103Modem


class AudioStream:
    """
    Bidirectional audio stream at 48 kHz that connects to a Bell103Modem.

    Usage::

        modem = Bell103Modem(mode='originate')
        stream = AudioStream(modem, device_id=None)  # None = default
        stream.start()

        # ... call a BBS, send/receive data ...

        stream.stop()
    """

    def __init__(self, modem, device_id=None, channels=1, blocksize=1920):
        """
        Parameters
        ----------
        modem : Bell103Modem
            The modem instance to connect.
        device_id : int or None
            Sounddevice device index.  None = default input+output device.
        channels : int
            Number of audio channels (1 = mono).
        blocksize : int
            Frames per buffer.  480 = 10 ms at 48 kHz.
        """
        self.modem = modem
        self.device_id = device_id
        self.channels = channels
        self.blocksize = blocksize
        self.stream = None
        self.running = False

        # For timing sync on first carrier detection
        self._sync_done = False
        self._sync_buf = []

        # Statistics
        self.bytes_sent = 0
        self.bytes_received = 0
        self.start_time = 0.0

        # Callbacks for received bytes (separate from modem's own callbacks)
        self._rx_byte_callbacks = []

    # ------------------------------------------------------------------
    def start(self):
        """Open and start the audio stream."""
        if self.running:
            return

        if self.device_id is None:
            # Find default device
            default_in = sd.query_devices(kind='input')
            default_out = sd.query_devices(kind='output')
            self.device_id = default_out['index']
            device_info = sd.query_devices(self.device_id)
        else:
            device_info = sd.query_devices(self.device_id)

        self.stream = sd.Stream(
            device=self.device_id,
            samplerate=SAMPLE_RATE,
            channels=self.channels,
            blocksize=self.blocksize,
            callback=self._callback,
            dtype='float32',
        )
        self.stream.start()
        self.running = True
        self.start_time = time.time()
        print(f"[AUDIO] Stream started on device {self.device_id} "
              f"({device_info['name']}), {SAMPLE_RATE} Hz, {self.blocksize} samples/block")

    def stop(self):
        """Stop and close the audio stream."""
        if not self.running or self.stream is None:
            return
        self.stream.stop()
        self.stream.close()
        self.stream = None
        self.running = False
        elapsed = time.time() - self.start_time
        print(f"[AUDIO] Stream stopped  ({elapsed:.1f}s, "
              f"{self.bytes_sent}B sent, {self.bytes_received}B received)")

    # ------------------------------------------------------------------
    def _callback(self, indata, outdata, frames, time_info, status):
        """
        Called by sounddevice on each audio buffer.
        - `indata`: microphone input → feed to modem.
        - `outdata`: modem transmit samples → speaker output.
        """
        if status:
            print(f"[AUDIO] {status}")

        # --- Process input (mic → modem) ---
        input_samples = indata[:, 0].astype(np.float64)  # mono

        if not self._sync_done:
            # Accumulate for initial timing sync
            self._sync_buf.extend(input_samples)
            # If carrier detected, try syncing
            if len(self._sync_buf) >= SAMPLES_PER_BIT * 20:
                sync_samples = np.array(self._sync_buf, dtype=np.float64)
                if np.max(np.abs(sync_samples)) > 0.01:  # rough energy check
                    self.modem.sync_from_samples(sync_samples)
                    self._sync_done = True
                    self._sync_buf = []
                    print("[AUDIO] Sync complete")
                else:
                    # No carrier yet, keep accumulating
                    # Limit buffer size to avoid unbounded growth
                    if len(self._sync_buf) > SAMPLE_RATE * 5:  # 5 seconds max
                        self._sync_buf = self._sync_buf[-SAMPLE_RATE:]
        else:
            self.modem.feed_rx_samples(input_samples)

        # --- Generate output (modem → speaker) ---
        out_float = self.modem.generate_tx_samples(frames)
        outdata[:, 0] = out_float.astype(np.float32)

        # Collect received bytes
        rx_data = self.modem.get_rx_bytes()
        if rx_data:
            self.bytes_received += len(rx_data)
            for cb in self._rx_byte_callbacks:
                try:
                    cb(rx_data)
                except Exception as e:
                    print(f"[AUDIO] byte cb: {e}")

    # ------------------------------------------------------------------
    def send_data(self, data):
        """Queue data for transmission."""
        self.modem.send_data(data)
        self.bytes_sent += len(data)

    def send_byte(self, byte):
        """Queue a single byte."""
        self.modem.send_byte(byte)
        self.bytes_sent += 1

    def is_busy(self):
        """True if there is data remaining to be transmitted."""
        return self.modem.is_busy()

    def on_bytes_received(self, callback):
        """
        Register a callback for received data.
        Callback receives a `bytes` object.
        """
        self._rx_byte_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def list_devices():
        """Print available audio devices."""
        print(sd.query_devices())

    @staticmethod
    def list_devices_api():
        """Print devices grouped by API."""
        for api in sd.query_hostapis():
            print(f"\n--- {api['name']} ---")
            for idx in api['devices']:
                dev = sd.query_devices(idx)
                print(f"  {idx}: {dev['name']}  "
                      f"in={dev['max_input_channels']}  "
                      f"out={dev['max_output_channels']}")


def audio_loopback_test(duration=5.0):
    """
    Simple test: transmit a test string through the audio path
    and receive it back (requires a physical loopback cable or
    virtual audio loopback device).
    """
    print("Audio Loopback Test")
    print("===================")
    print(f"Test will run for {duration} seconds.")
    print("Connect an audio cable from speaker-out to mic-in,")
    print("or use a virtual loopback device (e.g. PulseAudio monitor).")
    print()

    modem = Bell103Modem(mode='originate')
    stream = AudioStream(modem)

    test_data = b"Hello, BBS World! 12345"
    modem.send_data(test_data)

    received = bytearray()
    def on_rx(data):
        received.extend(data)

    stream.on_bytes_received(on_rx)

    print(f"Sending: {test_data}")
    stream.start()

    # Wait for transmission to complete + some margin
    import time
    timeout = time.time() + duration
    while (modem.is_busy() or time.time() < timeout - 2.0) and time.time() < timeout:
        time.sleep(0.1)

    stream.stop()

    result = bytes(received)
    print(f"\nReceived: {result}")
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


if __name__ == '__main__':
    import sys
    if '--list' in sys.argv:
        AudioStream.list_devices()
    elif '--loopback' in sys.argv:
        audio_loopback_test()
    else:
        print("Usage: python -m modem.audio --list  (list audio devices)")
        print("       python -m modem.audio --loopback  (run loopback test)")
