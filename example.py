"""
Phone-BBS Modem — Usage Examples
=================================
Run with:  python example.py <command>

Commands:
  selftest         Run the modem loopback self-test
  demo             Local ping-pong demo (no VoIP, no audio hardware)
  coupler          Acoustic coupler mode (use your phone — no provider needed)
  dial             Interactive BBS dialer (requires SIP credentials)
  terminal         Interactive terminal with audio (requires SIP + audio)
  scripted         Show programmatic API template
  list-providers   Show available VoIP providers
"""

from __future__ import annotations

import sys
import time
import threading

from modem import Bell103Modem, selftest


# =========================================================================
# EXAMPLE 1: Standalone modem loopback (no audio hardware, no VoIP)
# =========================================================================
def run_selftest():
    """Run the basic modem self-test (aligned blocks)."""
    print("=" * 60)
    print("Modem Self-Test")
    print("=" * 60)
    result = selftest()
    sys.exit(0 if result else 1)


# =========================================================================
# EXAMPLE 2: List VoIP providers
# =========================================================================
def run_list_providers():
    from sip import list_providers
    list_providers()


# =========================================================================
# EXAMPLE 3: Interactive BBS dialer (text-mode only, no audio hardware)
# =========================================================================
def run_dial():
    """
    Interactive dialer — connects to a BBS via VoIP, displays received text,
    and lets you type responses.

    You need a SIP account from one of the providers listed by
    ``python example.py list-providers``.

    Usage:
        1. Set the environment variables (or edit the script):
               export SIP_SERVER="sip.simonics.com"
               export SIP_USER="your_username"
               export SIP_PASS="your_password"
               export BBS_NUMBER="+15551234567"
        2. Run:  python example.py dial
    """
    import os
    from sip import VoIPClient, PROVIDERS

    server   = os.environ.get("SIP_SERVER")
    username = os.environ.get("SIP_USER")
    password = os.environ.get("SIP_PASS")
    number   = os.environ.get("BBS_NUMBER")

    if not all([server, username, password, number]):
        print("Missing credentials. Set these environment variables:")
        print("  SIP_SERVER   e.g. sip.simonics.com")
        print("  SIP_USER     your SIP username")
        print("  SIP_PASS     your SIP password")
        print("  BBS_NUMBER   e.g. +15551234567")
        sys.exit(1)

    # Find matching provider preset, or create a generic one
    from sip import PROVIDERS, ProviderConfig
    provider = None
    for cfg in PROVIDERS.values():
        if cfg.server == server:
            provider = cfg
            break
    if provider is None:
        provider = ProviderConfig(
            name="Custom", server=server, port=int(os.environ.get("SIP_PORT", "5060"))
        )

    # Create modem in originate mode (calling a BBS)
    modem = Bell103Modem(mode="originate")

    # We'll collect received bytes and print them
    received_data = bytearray()

    def on_data(data: bytes):
        received_data.extend(data)
        # Print received text (strip non-printable, show ANSI escapes raw)
        text = data.decode("latin-1", errors="replace")
        print(text, end="", flush=True)

    modem.on_byte_received(lambda b: None)  # handled via callback below

    print(f"\nConnecting to {number} via {provider.name} ...")
    print("(this may take 30-60 seconds for the call to connect)")
    print()

    # Create and start the VoIP client
    client = VoIPClient(provider, modem, username, password)
    try:
        client.start()
        print("Registered with SIP provider.")
        client.call(number)
        print("Call connected!  Audio bridge is live.")
        print("Type text and press Enter to send.  Type /quit to hang up.")
        print("=" * 60)

        # Register data callback on the modem (via client)
        client.on_data_received(on_data)

        # Main loop: read user input and send to modem
        while client.is_in_call:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break

            if line.strip() == "/quit":
                break

            # Send user text as modem data
            if line:
                data = (line + "\r").encode("latin-1")
                modem.send_data(data)

    except Exception as e:
        print(f"\nError: {e}")
    finally:
        client.stop()
        print("\nCall ended.")


# =========================================================================
# EXAMPLE 4: Interactive ANSI terminal with audio I/O
# =========================================================================
def run_terminal():
    """
    Like 'dial' but also plays the modem audio through your speakers and
    listens on your microphone.  Requires a working sounddevice setup.

    Use this to actually HEAR the modem handshake and ANSI art rendering.
    """
    import os
    import numpy as np
    from sip import VoIPClient, PROVIDERS
    from modem.audio import AudioStream

    server   = os.environ.get("SIP_SERVER")
    username = os.environ.get("SIP_USER")
    password = os.environ.get("SIP_PASS")
    number   = os.environ.get("BBS_NUMBER")

    if not all([server, username, password, number]):
        print("Missing credentials. Set these environment variables:")
        print("  SIP_SERVER, SIP_USER, SIP_PASS, BBS_NUMBER")
        sys.exit(1)

    provider = None
    for cfg in PROVIDERS.values():
        if cfg.server == server:
            provider = cfg
            break
    if provider is None:
        provider = ProviderConfig(
            name="Custom", server=server,
            port=int(os.environ.get("SIP_PORT", "5060"))
        )

    # Modem + audio stream for local monitoring
    modem = Bell103Modem(mode="originate")
    audio = AudioStream(modem, device_id=None)
    audio.start()

    received_data = bytearray()

    def on_data(data: bytes):
        received_data.extend(data)
        text = data.decode("latin-1", errors="replace")
        print(text, end="", flush=True)

    print(f"\nCalling {number} via {provider.name} ...")
    print("(audio is playing through your speakers)")
    print()

    client = VoIPClient(provider, modem, username, password)
    try:
        client.start()
        client.call(number)
        client.on_data_received(on_data)

        print("Connected!  Type to send, /quit to exit.")
        print("=" * 60)

        while client.is_in_call:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() == "/quit":
                break
            if line:
                data = (line + "\r").encode("latin-1")
                modem.send_data(data)

    except Exception as e:
        print(f"\nError: {e}")
    finally:
        client.stop()
        audio.stop()
        print("\nDisconnected.")


# =========================================================================
# EXAMPLE 5: Acoustic coupler — free, uses your phone + computer mic
# =========================================================================
def run_coupler():
    """
    Acoustic coupler mode — the ORIGINAL way people used modems!

    1. Call a BBS from your phone
    2. Hold the phone speaker near your computer's microphone
    3. Our modem decodes the BBS audio in real-time
    4. Type responses — they get encoded and played through your speaker
    5. Hold the phone mic near the speaker to transmit

    No SIP provider, no VoIP account, no phone line needed — just your phone
    with any cellular or landline plan (most have unlimited evenings/weekends).
    This is how 1970s acoustic coupler modems worked!

    Make sure your microphone and speaker volume are set appropriately.
    You may need to experiment with phone placement.
    """
    import time
    import numpy as np
    from modem.audio import AudioStream

    modem = Bell103Modem(mode="originate")

    # Callback prints decoded bytes to terminal
    def on_byte(b: int):
        sys.stdout.buffer.write(bytes([b]))
        sys.stdout.flush()

    modem.on_byte_received(on_byte)

    # Start the audio stream (mic → modem, modem → speaker)
    print("=" * 60)
    print("Acoustic Coupler Mode")
    print("=" * 60)
    print()
    print("1. Call a BBS number from your phone")
    print("2. Hold the phone SPEAKER near the computer MICROPHONE")
    print("3. You'll see the BBS text appear here as it's decoded")
    print("4. Type text here — it plays through your SPEAKER")
    print("5. Hold the phone MIC near the speaker to transmit")
    print()
    print("Tip: start with the phone right next to the mic and adjust")
    print("     volume until the text appears cleanly.")
    print()
    print("Type /quit to exit.")
    print()

    try:
        audio = AudioStream(modem, device_id=None)
        audio.start()
        print("Audio stream started!")

        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break

            if line.strip() == "/quit":
                break

            if line:
                modem.send_data((line + "\r\n").encode("latin-1"))

    except KeyboardInterrupt:
        pass
    finally:
        audio.stop()
        print("\nCoupler ended.")


# =========================================================================
# EXAMPLE 6: Local modem ping-pong demo (no VoIP, no audio hardware)
# =========================================================================
def run_demo():
    """
    Local modem ping-pong demo with real audio playback.

    Connects two modems back-to-back in memory (one originate, one answer)
    so the echo works without needing your mic to pick up the speakers.
    At the same time, the modem tones are played through your speakers
    so you can hear the Bell 103 frequencies.

    Type text → hear the 1070/1270/2025/2225 Hz tones → see it echoed back.
    """
    import time
    import numpy as np
    import sounddevice as sd
    from collections import deque

    SAMPLE_RATE = 48000
    CHUNK = 960  # 20ms

    modem_a = Bell103Modem(mode="originate")
    modem_b = Bell103Modem(mode="answer")

    # Filter for clean terminal display: skip bare CR (use only \n for newlines)
    def display_byte(b: int):
        # Skip carriage return to avoid line overwriting
        if b == 0x0D:  # \r
            return
        sys.stdout.buffer.write(bytes([b]))
        sys.stdout.flush()

    modem_a.on_byte_received(display_byte)

    print("=" * 60)
    print("Local Modem Ping-Pong Demo (with audio!)")
    print("=" * 60)
    print()
    print("Two modems connected back-to-back in memory.")
    print("Modem B echoes everything Modem A sends.")
    print("Modem tones play through your speakers so you can hear them.")
    print()
    print("Type text and press Enter to send.  Type /quit to exit.")
    print()

    # BBS thread: reads data from modem_b and echoes it back
    done = False

    def bbs_echo():
        nonlocal done
        while not done:
            data = modem_b.get_rx_bytes()
            if data:
                modem_b.send_data(data)  # echo back
            time.sleep(0.02)

    bbs_thread = threading.Thread(target=bbs_echo, daemon=True)
    bbs_thread.start()

    # Open speaker output stream for blocking writes
    speaker_stream = sd.OutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='float32',
    )
    speaker_stream.start()

    # Seed: send an initial newline so the echo test begins
    modem_a.send_data(b"\r\n")

    # Warm up: pump a few chunks to fill the audio buffer
    for _ in range(4):
        tx_a = modem_a.generate_tx_samples(CHUNK)
        modem_b.feed_rx_samples(tx_a)
        tx_b = modem_b.generate_tx_samples(CHUNK)
        modem_a.feed_rx_samples(tx_b)
        speaker_stream.write(tx_a.astype(np.float32))

    try:
        while not done:
            # Pump one chunk between modems
            tx_a = modem_a.generate_tx_samples(CHUNK)
            modem_b.feed_rx_samples(tx_a)

            tx_b = modem_b.generate_tx_samples(CHUNK)
            modem_a.feed_rx_samples(tx_b)

            # Play modem A's output through speakers (blocking write)
            speaker_stream.write(tx_a.astype(np.float32))

            # Check for user input (non-blocking)
            # We poll for input between chunks
            import select
            import sys as _sys
            if select.select([_sys.stdin], [], [], 0.0)[0]:
                line = _sys.stdin.readline().rstrip('\n\r')
                if line == "/quit":
                    break
                if line:
                    modem_a.send_data((line + "\r\n").encode("latin-1"))
    except (EOFError, KeyboardInterrupt):
        pass
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        done = True
        # Flush remaining audio
        for _ in range(50):
            if not modem_a.is_busy() and not modem_b.is_busy():
                break
            tx_a = modem_a.generate_tx_samples(CHUNK)
            modem_b.feed_rx_samples(tx_a)
            tx_b = modem_b.generate_tx_samples(CHUNK)
            modem_a.feed_rx_samples(tx_b)
            speaker_stream.write(tx_a.astype(np.float32))
        speaker_stream.stop()
        print()
        print("Demo ended.")


# =========================================================================
# EXAMPLE 6: Programmatic usage (for scripting)
# =========================================================================
def run_scripted():
    """
    Example of scripted usage: connect, log in, capture a file, hang up.
    (This does NOT actually run — it's a template.)
    """
    print("""
    # ---- Template for scripted BBS access ----
    from modem import Bell103Modem
    from sip import VoIPClient, PROVIDERS

    modem = Bell103Modem(mode='originate')
    client = VoIPClient(PROVIDERS["google_voice_simonics"],
                        modem, "user", "pass")

    client.start()
    client.call("+15551234567")

    # Wait for carrier / banner
    import time
    time.sleep(5)

    # Send commands
    modem.send_data(b"guest\\r")
    time.sleep(2)
    modem.send_data(b"\\r")
    time.sleep(2)

    # Read received data
    data = modem.get_rx_bytes()
    print(data.decode("latin-1", errors="replace"))

    # Download a file (if the BBS supports X/Y/Z modem,
    # you'd need a file transfer protocol handler)

    client.hangup()
    client.stop()
    """)


# =========================================================================
# EXAMPLE 7: PulseAudio virtual audio bridge
# =========================================================================
def run_pulse():
    """Start the PulseAudio Bell 103 bridge (virtual sink routing)."""
    from pulse_bridge import PulseBridge, setup_sinks, list_devices
    import sys
    import sounddevice as sd

    # Quick sanity-check: do our sinks exist?
    try:
        import subprocess
        subprocess.run(["pactl", "info"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: PulseAudio not running. Install it or use a different command.")
        sys.exit(1)

    if "--setup" in sys.argv:
        setup_sinks()
    elif "--list" in sys.argv:
        list_devices()
        print("\nSounddevice devices:")
        print(sd.query_devices())
    else:
        monitor = "--monitor" in sys.argv
        bridge = PulseBridge(monitor_speakers=monitor)
        bridge.run()


# =========================================================================
# Main
# =========================================================================
if __name__ == "__main__":
    commands = {
        "selftest":      run_selftest,
        "demo":          run_demo,
        "coupler":       run_coupler,
        "dial":          run_dial,
        "terminal":      run_terminal,
        "scripted":      run_scripted,
        "list-providers": run_list_providers,
        "pulse":         run_pulse,
    }

    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(__doc__.strip())
        print(f"\nAvailable commands: {', '.join(commands)}")
        sys.exit(1)

    commands[sys.argv[1]]()
