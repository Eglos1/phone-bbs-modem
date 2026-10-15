"""
SIP VoIP Client
===============
Bridges G.711 (PCMU) audio between a SIP VoIP provider and the
Bell 103 modem engine.

Uses pyVoIP for SIP signalling and RTP streaming.  The audio
bridge runs in a background daemon thread that:

  1. Reads unsigned 8-bit PCM @ 8 kHz from the VoIP call,
  2. Converts to float64 and resamples to 48 kHz,
  3. Feeds the samples to `Bell103Modem.feed_rx_samples()`,
  4. Generates transmit samples from the modem,
  5. Resamples back to 8 kHz and writes unsigned 8-bit PCM
     to the VoIP call.

Usage::

    from modem import Bell103Modem
    from sip import VoIPClient, PROVIDERS

    modem = Bell103Modem(mode='originate')
    client = VoIPClient(
        PROVIDERS["google_voice_simonics"],
        modem,
        username="your_sip_user",
        password="your_sip_pass",
    )
    client.start()               # register with SIP provider
    client.call("+15551234567")  # dial a BBS
    # ... data flows automatically ...
    client.hangup()
    client.stop()
"""

from __future__ import annotations

import audioop
import math
import threading
import time
from typing import Callable, Optional

import numpy as np
import scipy.signal

from pyVoIP.VoIP.VoIP import VoIPPhone, VoIPCall, CallState

from modem.bell103 import (
    Bell103Modem,
    SAMPLE_RATE,
    SAMPLES_PER_BIT,
    GOERTZEL_ENERGY_THRESHOLD,
)

from .provider import ProviderConfig

# ---------------------------------------------------------------------------
# DTMF tone frequencies (Hz)
# ---------------------------------------------------------------------------
_DTMF_ROW_FREQS = [697, 770, 852, 941]
_DTMF_COL_FREQS = [1209, 1336, 1477]
_DTMF_MAP: dict[str, tuple[int, int]] = {
    "1": (0, 0),
    "2": (0, 1),
    "3": (0, 2),
    "4": (1, 0),
    "5": (1, 1),
    "6": (1, 2),
    "7": (2, 0),
    "8": (2, 1),
    "9": (2, 2),
    "0": (3, 1),
    "*": (3, 0),
    "#": (3, 2),
}

# Telephony sample rate used by pyVoIP (G.711 PCMU)
TELEPHONY_RATE = 8000
SAMPLES_20MS = TELEPHONY_RATE // 50  # 160 samples @ 8 kHz


class VoIPClient:
    """
    SIP client that bridges G.711 audio to a Bell103Modem.

    Manages SIP registration, outbound calling, DTMF sending,
    and a background audio bridge thread.
    """

    def __init__(
        self,
        provider: ProviderConfig,
        modem: Bell103Modem,
        username: str,
        password: str,
        my_ip: str = "0.0.0.0",
        sip_port: int = 5060,
        rtp_port_range: tuple[int, int] = (10000, 20000),
    ):
        self.provider = provider
        self.modem = modem
        self.username = username
        self.password = password
        self.my_ip = my_ip
        self.sip_port = sip_port
        self.rtp_port_low, self.rtp_port_high = rtp_port_range

        # Phone instance (created on start())
        self.phone: Optional[VoIPPhone] = None
        # Active call (created on call())
        self.call: Optional[VoIPCall] = None

        # Bridge thread control
        self._bridge_running = False
        self._bridge_thread: Optional[threading.Thread] = None

        # Callbacks
        self._rx_callback: Optional[Callable[[bytes], None]] = None
        self._state_callback: Optional[Callable[[CallState], None]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """
        Register with the SIP provider.

        Blocks until registration succeeds or fails (timeout ~10 s).
        Raises RuntimeError on failure.
        """
        if self.phone is not None:
            self.stop()  # ensure clean state

        self.phone = VoIPPhone(
            self.provider.server,
            self.provider.port,
            self.username,
            self.password,
            myIP=self.my_ip,
            sipPort=self.sip_port,
            rtpPortLow=self.rtp_port_low,
            rtpPortHigh=self.rtp_port_high,
        )
        self.phone.start()

        # Wait for registration to complete
        timeout = 10.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            from pyVoIP.VoIP.status import PhoneStatus
            status = self.phone.get_status()
            if status == PhoneStatus.REGISTERED:
                print(f"[VoIP] Registered with {self.provider.name}")
                return
            if status == PhoneStatus.FAILED:
                raise RuntimeError(
                    f"Registration with {self.provider.name} failed"
                )
            time.sleep(0.05)

        self.phone.stop()
        self.phone = None
        raise TimeoutError(
            f"Registration with {self.provider.name} timed out "
            f"after {timeout:.0f}s"
        )

    def stop(self) -> None:
        """Deregister from the SIP provider and clean up."""
        self.hangup()
        if self.phone is not None:
            try:
                self.phone.stop()
                print("[VoIP] Deregistered")
            except Exception:
                pass
            self.phone = None

    # ------------------------------------------------------------------
    # Calling
    # ------------------------------------------------------------------
    def call(self, number: str) -> None:
        """
        Place an outbound call to *number*.

        Blocks until the call is answered or fails (timeout ~60 s).
        Automatically starts the audio bridge once answered.
        """
        if self.phone is None:
            raise RuntimeError("Not registered. Call start() first.")

        print(f"[VoIP] Calling {number} ...")
        self.call = self.phone.call(number)

        # Wait for answer
        timeout = 60.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            state = self.call.state
            if state == CallState.ANSWERED:
                elapsed = time.monotonic() - t0
                print(f"[VoIP] Call answered ({elapsed:.1f}s)")
                if self._state_callback:
                    self._state_callback(state)
                break
            elif state == CallState.ENDED:
                raise RuntimeError("Call ended before being answered")
            time.sleep(0.05)
        else:
            self.call.hangup()
            self.call = None
            raise TimeoutError(
                f"Call to {number} not answered within {timeout:.0f}s"
            )

        # Start the audio bridge
        self._start_bridge()

    def hangup(self) -> None:
        """End the current call and stop the audio bridge."""
        self._stop_bridge()
        if self.call is not None:
            try:
                self.call.hangup()
            except Exception:
                pass
            self.call = None
            if self._state_callback:
                self._state_callback(CallState.ENDED)
            print("[VoIP] Call ended")

    # ------------------------------------------------------------------
    # Audio bridge
    # ------------------------------------------------------------------
    def _start_bridge(self) -> None:
        """Launch the background audio bridge thread."""
        if self._bridge_running:
            return
        self._bridge_running = True
        self._sync_done = False
        self._sync_buf: list[float] = []
        self._bridge_thread = threading.Thread(
            target=self._bridge_loop, daemon=True, name="VoIP-Bridge"
        )
        self._bridge_thread.start()
        print("[VoIP] Audio bridge started")

    def _stop_bridge(self) -> None:
        """Signal the bridge thread to stop and wait for it."""
        self._bridge_running = False
        if self._bridge_thread is not None:
            self._bridge_thread.join(timeout=3.0)
            self._bridge_thread = None
        self._sync_done = False
        self._sync_buf = []

    def _bridge_loop(self) -> None:
        """
        Main audio bridge loop.

        Runs in a daemon thread.  Reads audio from the VoIP call,
        feeds the modem, and writes modem output back to the call.
        """
        # Pre-allocate a reusable resampling buffer for performance
        while self._bridge_running:
            try:
                call = self.call
                if call is None:
                    break
                if call.state != CallState.ANSWERED:
                    # Call ended while we were running
                    break

                # --- Receive path: VoIP → Modem ---
                # Read unsigned 8-bit PCM @ 8 kHz (160 samples = 20 ms)
                data = call.read_audio(length=SAMPLES_20MS, blocking=True)
                if not data or len(data) != SAMPLES_20MS:
                    continue

                # Convert unsigned 8-bit PCM → float64 [-1, 1]
                pcm_u8 = np.frombuffer(data, dtype=np.uint8).astype(np.float64)
                pcm_f64 = (pcm_u8 - 128.0) / 128.0

                # --- Timing sync on first carrier ---
                if not self._sync_done:
                    self._sync_buf.extend(pcm_f64.tolist())
                    # Try sync once we have ~120 ms of audio
                    if len(self._sync_buf) >= SAMPLES_20MS * 6:
                        sync_array = np.array(
                            self._sync_buf[: SAMPLES_20MS * 12],
                            dtype=np.float64,
                        )
                        if np.max(np.abs(sync_array)) > 0.005:
                            self.modem.sync_from_samples(sync_array)
                            self._sync_done = True
                            self._sync_buf = []
                            print("[VoIP] Timing sync acquired")
                        else:
                            # Slide window: keep last few blocks
                            self._sync_buf = self._sync_buf[-SAMPLES_20MS * 2 :]
                    continue

                # --- Resample 8 kHz → 48 kHz ---
                pcm_48k = scipy.signal.resample_poly(
                    pcm_f64, up=6, down=1
                )

                # Feed to modem
                self.modem.feed_rx_samples(pcm_48k)

                # Collect any received bytes
                rx_data = self.modem.get_rx_bytes()
                if rx_data and self._rx_callback:
                    try:
                        self._rx_callback(rx_data)
                    except Exception as e:
                        print(f"[VoIP] RX callback error: {e}")

                # --- Transmit path: Modem → VoIP ---
                tx_48k = self.modem.generate_tx_samples(len(pcm_48k))

                # Resample 48 kHz → 8 kHz
                tx_8k = scipy.signal.resample_poly(tx_48k, up=1, down=6)

                # Convert float64 → unsigned 8-bit PCM
                tx_u8 = np.clip(
                    tx_8k * 128.0 + 128.0, 0.0, 255.0
                ).astype(np.uint8)

                # Write to VoIP call
                call.write_audio(tx_u8.tobytes())

            except Exception as e:
                print(f"[VoIP] Bridge error: {e}")
                break

        self._bridge_running = False
        self._sync_done = False
        self._sync_buf = []
        print("[VoIP] Audio bridge stopped")

    # ------------------------------------------------------------------
    # DTMF
    # ------------------------------------------------------------------
    def send_dtmf(self, digits: str, duration: float = 0.1) -> None:
        """
        Send DTMF tones in-band during an active call.

        Generates the dual-tone sine waves at 8 kHz and writes them
        directly to the VoIP call's audio output.  Each tone is
        *duration* seconds long, followed by a 50 ms gap.

        This temporarily pre-empts the modem's transmit signal.
        """
        if not self.call or self.call.state != CallState.ANSWERED:
            print("[VoIP] No active call — cannot send DTMF")
            return

        n_samples = int(TELEPHONY_RATE * duration)
        gap_samples = int(TELEPHONY_RATE * 0.05)
        t = np.arange(n_samples, dtype=np.float64) / TELEPHONY_RATE

        for ch in digits:
            if ch not in _DTMF_MAP:
                continue
            row, col = _DTMF_MAP[ch]
            f1, f2 = _DTMF_ROW_FREQS[row], _DTMF_COL_FREQS[col]

            # Generate dual-tone
            tone = (np.sin(2 * math.pi * f1 * t) + np.sin(2 * math.pi * f2 * t)) / 2.0
            tone_u8 = np.clip(tone * 128.0 + 128.0, 0.0, 255.0).astype(np.uint8)

            self.call.write_audio(tone_u8.tobytes())

            # Gap
            gap = np.full(gap_samples, 128, dtype=np.uint8)
            self.call.write_audio(gap.tobytes())

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_data_received(self, callback: Callable[[bytes], None]) -> None:
        """
        Register a callback for received data bytes from the modem.

        The callback is called from the bridge thread with ``bytes``
        objects containing decoded serial data from the remote end.
        """
        self._rx_callback = callback

    def on_call_state(self, callback: Callable[[CallState], None]) -> None:
        """Register a callback for call state changes."""
        self._state_callback = callback

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------
    @property
    def is_registered(self) -> bool:
        """True if the phone is currently registered."""
        if self.phone is None:
            return False
        try:
            from pyVoIP.VoIP.status import PhoneStatus
            return self.phone.get_status() == PhoneStatus.REGISTERED
        except Exception:
            return False

    @property
    def is_in_call(self) -> bool:
        """True if there is an active answered call."""
        return (
            self.call is not None
            and self.call.state == CallState.ANSWERED
        )

    @property
    def bridge_active(self) -> bool:
        """True if the audio bridge thread is running."""
        return self._bridge_running and (
            self._bridge_thread is not None and self._bridge_thread.is_alive()
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> "VoIPClient":
        return self

    def __exit__(self, *args) -> None:
        self.stop()
