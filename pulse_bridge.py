#!/usr/bin/env python3
"""
PulseAudio Virtual Audio Bridge
================================
Routes audio between a browser-based calling service (Call2Friends, etc.)
and the Bell 103 modem using PipeWire virtual null sinks.

Architecture:
    ┌──────────────┐   call_sink.monitor   ┌──────────────────┐
    │   Chrome     │───────────────────────▶│  pulse_bridge.py │
    │ (Call2Friends)│                       │                   │
    │              │◀───────────────────────│  (modem engine)   │
    └──────────────┘   mic_sink             └──────────────────┘

    call_sink  : null sink — Chrome plays call audio here
    mic_sink   : null sink — Chrome reads this as its microphone

Setup (once per boot):
    python3 pulse_bridge.py --setup

Launch Chrome for calling:
    PULSE_SINK=call_sink PULSE_SOURCE=mic_sink google-chrome

Start the bridge:
    python3 pulse_bridge.py

IMPORTANT: The virtual sinks are destroyed when the bridge exits.
To keep them around (e.g. while you re-launch Chrome), use:
    python3 pulse_bridge.py --setup     # create sinks without running bridge
    python3 pulse_bridge.py             # bridge uses existing sinks
    python3 pulse_bridge.py --teardown  # destroy sinks when done
"""

import sys
import os
import time
import threading
import argparse
import subprocess
import numpy as np

from modem.bell103 import Bell103Modem, SAMPLE_RATE

BLOCKSIZE = 480  # 10 ms

CALL_SINK = "call_sink"
MIC_SINK = "mic_sink"

# ---------------------------------------------------------------------------
# PipeWire / PulseAudio helpers
# ---------------------------------------------------------------------------

def pw_cli(*args):
    """Run pw-cli and return (returncode, stdout, stderr)."""
    r = subprocess.run(["pw-cli"] + list(args),
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def pactl(*args):
    """Run pactl and return stdout (None on failure)."""
    r = subprocess.run(["pactl"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def sink_exists(name):
    out = pactl("list", "sinks", "short")
    if out is None:
        return False
    return any(name in line for line in out.splitlines())


def source_exists(name):
    out = pactl("list", "sources", "short")
    if out is None:
        return False
    return any(name in line for line in out.splitlines())


def pw_destroy_by_name(name):
    """Destroy all PipeWire global objects whose name matches."""
    rc, out, _ = pw_cli("list-objects")
    if rc != 0:
        return
    # Find object IDs that reference the name
    current_id = None
    for line in out.splitlines():
        if line.strip().startswith("id ") and ", type" in line:
            current_id = line.strip().split()[1].rstrip(",")
        if f'"{name}"' in line and current_id is not None:
            pw_cli("destroy", current_id)
            current_id = None


def find_pulse_device_index():
    """Find the 'pulse' ALSA index for sounddevice."""
    import sounddevice as sd
    for i, dev in enumerate(sd.query_devices()):
        if dev["name"].startswith("pulse"):
            return i
    return None


# ---------------------------------------------------------------------------
# Virtual sink management
# ---------------------------------------------------------------------------

def create_sink(name, description):
    """Create a PipeWire null-audio-sink node."""
    rc, out, err = pw_cli(
        "create-node", "adapter",
        "{"
        f' factory.name=support.null-audio-sink'
        f' node.name={name}'
        f' node.description="{description}"'
        f' media.class=Audio/Sink'
        f' object.linger=true'
        " }"
    )
    if rc != 0:
        print(f"  FAILED: {err}")
        return False
    # pw-cli prints the sink info line on creation
    print(f"  Created '{name}'  ({description})")
    return True


def setup_sinks():
    """Create call_sink and mic_sink."""
    print("=" * 60)
    print("PulseAudio Bridge — Setup")
    print("=" * 60)
    print()

    # Check PipeWire is running
    rc, _, _ = pw_cli("info")
    if rc != 0:
        print("ERROR: PipeWire/pw-cli not available.")
        sys.exit(1)

    for name, desc in [
        (CALL_SINK, "Call Sink — Chrome plays call audio here"),
        (MIC_SINK, "Mic Feed Sink — modem output → Chrome mic"),
    ]:
        if sink_exists(name):
            print(f"  ✓ '{name}' already exists")
        else:
            create_sink(name, desc)

    print()
    print("Sinks ready.")
    print()
    print("Launch Chrome (only Chrome uses these sinks):")
    print(f"  PULSE_SINK={CALL_SINK} PULSE_SOURCE={MIC_SINK} google-chrome")
    print()
    print("Then start the bridge:")
    print(f"  python3 pulse_bridge.py")
    print()


def teardown_sinks():
    """Destroy call_sink and mic_sink."""
    print("Tearing down virtual sinks...")
    for name in [CALL_SINK, MIC_SINK]:
        pw_destroy_by_name(name)
    # Give PipeWire a moment
    time.sleep(0.3)
    remaining = [n for n in [CALL_SINK, MIC_SINK] if sink_exists(n)]
    if remaining:
        print(f"  Still present: {', '.join(remaining)} (may need manual cleanup)")
    else:
        print("  Done — sinks removed.")


def list_devices():
    """List sinks and sources."""
    print("Sinks:")
    for line in (pactl("list", "sinks", "short") or "").splitlines():
        parts = line.split()
        name = parts[1] if len(parts) >= 2 else line
        marker = "  ◀" if name in (CALL_SINK, MIC_SINK) else ""
        print(f"  {name}{marker}")
    print()
    print("Sources (including monitors):")
    for line in (pactl("list", "sources", "short") or "").splitlines():
        parts = line.split()
        name = parts[1] if len(parts) >= 2 else line
        marker = ""
        if CALL_SINK in name or MIC_SINK in name:
            marker = "  ◀"
        print(f"  {name}{marker}")


# ---------------------------------------------------------------------------
# Stream routing helpers
# ---------------------------------------------------------------------------

def move_our_streams(pid, target_sink=None, target_source=None):
    """
    Move our process's PulseAudio streams to the given sink/source.
    Uses the 'pulse' ALSA device so streams register in PulseAudio.
    """
    if target_sink:
        out = pactl("list", "sink-inputs")
        if out:
            _move_by_pid(out, "Sink Input", "sink-input", pid, target_sink)

    if target_source:
        out = pactl("list", "source-outputs")
        if out:
            _move_by_pid(out, "Source Output", "source-output", pid, target_source)


def _move_by_pid(listing, obj_type, cmd_type, pid, target):
    """Internal: parse pactl list, find streams by PID, move them."""
    current_id = None
    for line in listing.splitlines():
        if line.startswith(f"{obj_type} #"):
            current_id = line.split("#")[1].strip()
        if f'application.process.id = "{pid}"' in line and current_id is not None:
            print(f"  → move {cmd_type} #{current_id} to '{target}'")
            pactl(f"move-{cmd_type.replace('_', '-')}", current_id, target)
            current_id = None


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class PulseBridge:
    """
    Routes audio between PulseAudio virtual sinks and the Bell 103 modem.
    """

    def __init__(self):
        self.modem = Bell103Modem(mode="originate")
        self.running = False

        # Register byte callback for terminal output
        self.modem.on_byte_received(self._on_byte)

    def _on_byte(self, b: int):
        """Print decoded bytes to terminal."""
        sys.stdout.buffer.write(bytes([b]))
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Audio callbacks
    # ------------------------------------------------------------------

    def _bridge_callback(self, indata, outdata, frames, time_info, status):
        """Full-duplex audio callback: call audio ↔ modem ↔ mic feed."""
        if status:
            print(f"[AUDIO] {status}")

        # Capture: call_sink.monitor → modem RX
        samples = indata[:, 0].astype(np.float64)
        self.modem.feed_rx_samples(samples)

        # Playback: modem TX → mic_sink (Chrome's virtual mic)
        tx = self.modem.generate_tx_samples(frames)
        outdata[:, 0] = tx.astype(np.float32)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        """Start the bridge."""
        import sounddevice as sd

        print("=" * 60)
        print("PulseAudio Bell 103 Bridge")
        print("=" * 60)
        print()

        # Verify sinks exist
        if not sink_exists(CALL_SINK):
            print(f"ERROR: '{CALL_SINK}' not found. Run --setup first.")
            sys.exit(1)
        if not sink_exists(MIC_SINK):
            print(f"ERROR: '{MIC_SINK}' not found. Run --setup first.")
            sys.exit(1)

        # Find the PulseAudio ALSA device index
        pulse_idx = find_pulse_device_index()
        if pulse_idx is None:
            print("ERROR: 'pulse' ALSA device not found in sounddevice.")
            sys.exit(1)
        print(f"  Using sounddevice device [{pulse_idx}] (pulse)")

        # Open full-duplex stream on the pulse ALSA device
        # This ensures our streams appear in PulseAudio for routing
        pid = os.getpid()
        stream = sd.Stream(
            device=pulse_idx,
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=BLOCKSIZE,
            callback=self._bridge_callback,
            dtype='float32',
            latency='low',
        )
        stream.start()

        # Move our streams to the correct virtual sinks
        print("  Routing streams...")
        move_our_streams(
            pid,
            target_sink=MIC_SINK,
            target_source=f"{CALL_SINK}.monitor",
        )
        print()

        print("  call_sink.monitor  →  modem RX  (capture call audio)")
        print("  modem TX           →  mic_sink  (Chrome's virtual mic)")
        print()
        print("  Launch Chrome with:")
        print(f"    PULSE_SINK={CALL_SINK} PULSE_SOURCE={MIC_SINK} google-chrome")
        print()
        print("  Commands:")
        print("    type text  — send through modem")
        print("    /quit      — exit")
        print("    /carrier   — show carrier status")
        print("    /stats     — show stats")
        print("    /reroute   — re-route streams (if you restarted Chrome)")
        print("    /reset     — reset modem")
        print()

        self.running = True
        print("[BRIDGE] Live.")

        try:
            while self.running:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break

                if not line.strip():
                    continue

                cmd = line.strip().split()[0].lower()

                if cmd == "/quit":
                    break
                elif cmd == "/carrier":
                    print(f"  Carrier: {'DETECTED' if self.modem.is_carrier_present() else 'LOST'}")
                elif cmd == "/stats":
                    print(f"  Carrier: {'PRESENT' if self.modem.is_carrier_present() else 'LOST'}")
                    print(f"  TX busy: {self.modem.is_busy()}")
                elif cmd == "/reroute":
                    print("  Re-routing streams...")
                    move_our_streams(
                        pid,
                        target_sink=MIC_SINK,
                        target_source=f"{CALL_SINK}.monitor",
                    )
                elif cmd == "/reset":
                    self.modem.reset()
                    print("  Modem reset.")
                else:
                    # Send to modem
                    self.modem.send_data((line + "\r\n").encode("latin-1"))
                    while self.modem.is_busy():
                        time.sleep(0.02)

        except Exception as e:
            print(f"\n[BRIDGE] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False
            stream.stop()
            print()
            print("[BRIDGE] Stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="PulseAudio Bell 103 Bridge"
    )
    parser.add_argument("--setup", action="store_true",
                        help="Create virtual sinks")
    parser.add_argument("--teardown", action="store_true",
                        help="Destroy virtual sinks")
    parser.add_argument("--list", action="store_true",
                        help="List PulseAudio devices")
    args = parser.parse_args()

    if args.setup:
        setup_sinks()
    elif args.teardown:
        teardown_sinks()
    elif args.list:
        list_devices()
    else:
        bridge = PulseBridge()
        bridge.run()


if __name__ == "__main__":
    main()
