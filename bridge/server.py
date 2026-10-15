#!/usr/bin/env python3
"""
Modem Bridge — WebSocket + Modem Server
========================================
Connects the Chrome Extension (PopTox/TextNow) to the Bell103Modem over WebSocket.

Audio flow:
  Extension (PCM16) ← WebSocket → Server (PCM16)
                                        ↓↑
                                   float64 conversion
                                        ↓↑
                                  Bell103Modem
                                        ↓↑
                                   Terminal UI

Usage:
  python -m bridge.server
"""

from __future__ import annotations

import sys
import os
import struct
import hashlib
import base64
import socket
import threading
import time
import json
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modem import Bell103Modem


# =========================================================================
# Minimal WebSocket Server
# =========================================================================

WEBSOCKET_MAGIC = b"258EAFA5-E914-47DA-95CA-5AB9FB256E7B"

class WebSocketError(Exception):
    pass


def _create_accept_key(key: str) -> str:
    sha1 = hashlib.sha1(key.encode() + WEBSOCKET_MAGIC).digest()
    return base64.b64encode(sha1).decode()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)


class WebSocketClient:
    """A single WebSocket connection."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.sock.settimeout(30)
        self._buffer = bytearray()
        self._closed = False

    def close(self, code: int = 1000, reason: str = ""):
        if not self._closed:
            self._closed = True
            try:
                # Send a close frame back before closing
                payload = struct.pack("!H", code) + reason.encode("utf-8")
                self._send_frame(0x8, payload)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass

    def _send_frame(self, opcode: int, data: bytes):
        """Send a WebSocket frame (unmasked, from server to client)."""
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode

        length = len(data)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", length))

        self.sock.sendall(bytes(frame) + data)

    def send_binary(self, data: bytes):
        """Send a binary message (opcode 0x2)."""
        self._send_frame(0x2, data)

    def send_text(self, text: str):
        """Send a text message (opcode 0x1)."""
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> bytes | str | None:
        """
        Receive one WebSocket message.
        Returns bytes for binary, str for text, None on close.
        """
        try:
            first = _recv_exact(self.sock, 2)
        except (WebSocketError, OSError):
            return None

        fin = (first[0] & 0x80) != 0
        opcode = first[0] & 0x0F
        masked = (first[1] & 0x80) != 0
        length = first[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]

        mask_key = _recv_exact(self.sock, 4) if masked else None
        payload = _recv_exact(self.sock, length)

        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # Close
            # Extract close code and reason (first 2 bytes are code)
            if len(payload) >= 2:
                code = struct.unpack("!H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", errors="replace") if len(payload) > 2 else ""
            else:
                code = 1000
                reason = ""
            print(f"  [i] WebSocket close frame: code={code} reason='{reason}'", file=sys.stderr)
            self.close(code, reason)
            return None
        elif opcode == 0x9:  # Ping
            self._send_frame(0xA, payload)  # Pong
            return self.recv()  # Wait for next frame
        elif opcode == 0x1:  # Text
            return payload.decode("utf-8")
        elif opcode == 0x2:  # Binary
            return payload
        else:
            return self.recv()


class WebSocketServer:
    """Minimal WebSocket server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9090):
        self.host = host
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._running = False
        self._on_connect = None

    def on_connect(self, handler):
        """Set the callback for new connections: handler(WebSocketClient)."""
        self._on_connect = handler

    def start(self):
        self._server.bind((self.host, self.port))
        self._server.listen(5)
        self._server.settimeout(1)
        self._running = True
        print(f"  [i] WebSocket server: ws://{self.host}:{self.port}", file=sys.stderr)

        while self._running:
            try:
                client_sock, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # Handle the HTTP upgrade handshake
            try:
                self._handle_handshake(client_sock, addr)
            except Exception as e:
                print(f"  [!] Handshake failed: {e}")
                client_sock.close()

    def stop(self):
        self._running = False
        try:
            self._server.close()
        except Exception:
            pass

    def _handle_handshake(self, sock: socket.socket, addr):
        """Perform WebSocket upgrade handshake."""
        # Read HTTP headers until blank line (1 byte at a time to avoid
        # consuming any WebSocket frames that may follow immediately)
        data = b""
        while True:
            chunk = sock.recv(1)
            if not chunk:
                raise WebSocketError("Connection closed during handshake")
            data += chunk
            if data.endswith(b"\r\n\r\n"):
                break
        data = data.decode("utf-8", errors="replace")
        lines = data.split("\r\n")

        # Parse key
        key = None
        for line in lines:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break

        if not key:
            raise WebSocketError("Missing Sec-WebSocket-Key")

        accept = _create_accept_key(key)

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        sock.sendall(response.encode())

        client = WebSocketClient(sock)
        if self._on_connect:
            self._on_connect(client)


# =========================================================================
# Bridge Logic
# =========================================================================

class ModemBridge:
    """Bridges WebSocket PCM16 audio to/from the Bell103Modem."""

    SAMPLE_RATE = 48000
    PCM16_MAX = 32767.0

    def __init__(self):
        self.modem = Bell103Modem(mode="originate")
        self._ws: WebSocketClient | None = None
        self._running = False
        self._lock = threading.Lock()

        # Decoded output buffer for terminal display
        self._rx_decoded = bytearray()

        # Register modem callback
        self.modem.on_byte_received(self._on_modem_byte)

    def _on_modem_byte(self, b: int):
        """Called when the modem decodes a byte."""
        with self._lock:
            self._rx_decoded.append(b)

    def _pcm16_to_float64(self, data: bytes) -> np.ndarray:
        """Convert PCM16 bytes to float64 samples."""
        int16 = np.frombuffer(data, dtype=np.int16).astype(np.float64)
        return int16 / self.PCM16_MAX

    def _float64_to_pcm16(self, samples: np.ndarray) -> bytes:
        """Convert float64 samples to PCM16 bytes."""
        samples = np.clip(samples, -1.0, 1.0)
        int16 = (samples * self.PCM16_MAX).astype(np.int16)
        return int16.tobytes()

    def _log(self, msg: str):
        """Print a log message (to stderr to avoid mixing with BBS output)."""
        print(msg, file=sys.stderr)

    def on_ws_connect(self, ws: WebSocketClient):
        """Called when a WebSocket client connects."""
        import uuid
        self._session_id = uuid.uuid4().hex[:8]

        # Stop any previous session
        self._running = False
        time.sleep(0.05)
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._ws = ws
        self._running = True
        self.modem.reset()
        self._rx_decoded.clear()
        self._log(f"  [+] Client connected [{self._session_id}]")
        ws.send_text(json.dumps({"type": "connected"}))

        # Start receive loop in a daemon thread
        threading.Thread(target=self._recv_loop, args=(ws,), daemon=True).start()
        # Start transmit loop in a daemon thread
        threading.Thread(target=self._tx_loop, daemon=True).start()

    def _recv_loop(self, ws: WebSocketClient, _session_id: str | None = None):
        """Receive audio from WebSocket and feed to modem."""
        sid = _session_id or getattr(self, '_session_id', '??')
        try:
            while self._running:
                msg = ws.recv()
                if msg is None:
                    break

                if isinstance(msg, bytes):
                    float64 = self._pcm16_to_float64(msg)
                    self.modem.feed_rx_samples(float64)
                elif isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "ready":
                            self._log(f"  [+] Extension ready [{sid}]")
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            import traceback
            self._log(f"  [!] {sid}: Receive error: {e}")
            self._log(f"  [!] {sid}: Traceback:\n{traceback.format_exc()}")
        finally:
            self._running = False
            self._log(f"  [i] Client disconnected [{sid}]")

    def _tx_loop(self):
        """Generate modem output and send to WebSocket."""
        CHUNK = 960  # 20ms at 48kHz
        try:
            while self._running:
                samples = self.modem.generate_tx_samples(CHUNK)
                pcm16 = self._float64_to_pcm16(samples)
                if self._ws:
                    try:
                        self._ws.send_binary(pcm16)
                    except Exception:
                        break
                time.sleep(0.005)
        except Exception:
            pass

    def read_decoded(self) -> bytes:
        """Read and clear decoded bytes."""
        with self._lock:
            data = bytes(self._rx_decoded)
            self._rx_decoded.clear()
            return data

    def send_text(self, text: str):
        """Send text through the modem."""
        self.modem.send_data(text.encode("latin-1"))

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()
            self._ws = None


# =========================================================================
# Terminal UI
# =========================================================================

def eprint(*args, **kwargs):
    """Print to stderr (status messages, not BBS data)."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)

def run_terminal(bridge: ModemBridge):
    """Interactive terminal for the BBS."""
    eprint()
    eprint("=" * 60)
    eprint("Modem Bridge — Connected")
    eprint("=" * 60)
    eprint()
    eprint("Type text and press Enter to send.")
    eprint("Type /quit to exit.")
    eprint("Received BBS data appears below:")
    eprint("-" * 60)
    sys.stderr.flush()

    try:
        while bridge._running:
            # Print any decoded bytes (stdout = BBS data only)
            data = bridge.read_decoded()
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()

            # Check for user input (non-blocking)
            try:
                import select
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    line = line.rstrip("\n")
                    if line.strip() == "/quit":
                        break
                    if line:
                        bridge.send_text(line + "\r\n")
            except (EOFError, KeyboardInterrupt):
                break

    except Exception as e:
        eprint(f"\n[!] Error: {e}")
    finally:
        eprint("\nDisconnected.")


# =========================================================================
# Main
# =========================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Modem Bridge — WebSocket + Modem Server"
    )
    parser.add_argument(
        "--port", type=int, default=9090,
        help="WebSocket server port (default: 9090)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="WebSocket server host (default: 127.0.0.1)"
    )

    args = parser.parse_args()

    eprint("=" * 60)
    eprint("Modem Bridge Server")
    eprint("=" * 60)
    eprint()

    bridge = ModemBridge()
    ws_server = WebSocketServer(host=args.host, port=args.port)

    # Wire up the WebSocket connect handler
    ws_server.on_connect(bridge.on_ws_connect)

    # Start WebSocket server in a daemon thread
    ws_thread = threading.Thread(target=ws_server.start, daemon=True)
    ws_thread.start()

    eprint("  [i] Server ready — load extension in Chrome, open call2friends.com")
    eprint("  [i] Press Ctrl+C to stop")
    eprint()

    running = True
    while running:
        # Wait for a connection
        eprint("  [i] Waiting for extension connection...")
        timeout = 120
        start = time.time()
        while not bridge._running and (time.time() - start) < timeout:
            time.sleep(0.1)

        if not bridge._running:
            eprint("  [!] Timed out waiting for extension connection.")
            eprint("  [!] Restart the server to try again.")
            break

        # Run the terminal UI for this connection session
        try:
            run_terminal(bridge)
        except KeyboardInterrupt:
            running = False
            break

        # If we get here, the terminal session ended.
        if not running:
            break

        eprint()
        eprint("  [i] Session ended. Waiting for next connection...  (Ctrl+C to quit)")
        eprint()

    bridge.stop()
    ws_server.stop()
    eprint("Server stopped.")


if __name__ == "__main__":
    main()
