/**
 * Modem Bridge — Page-Context WebRTC Interceptor
 *
 * Injected directly into the page to intercept RTCPeerConnection.
 * Captures incoming audio (BBS → modem), injects outgoing audio (modem → BBS).
 *
 * Communication with content script via CustomEvent.
 */

(function () {
  'use strict';

  // ── State ──
  let audioCtx = null;
  const peerConnections = new Set();
  let wsConnected = false;

  // ── Logging ──
  function log(msg) {
    document.dispatchEvent(new CustomEvent('__gv_bridge_from_page', {
      detail: { type: 'log', message: msg }
    }));
  }

  // ── Audio context (lazy, user-gesture required for resume) ──
  function getAudioContext() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 48000,
      });
    }
    if (audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
    return audioCtx;
  }

  // ── PCM16 conversion helpers ──
  function float32ToPcm16(float32) {
    const len = float32.length;
    const pcm16 = new Int16Array(len);
    for (let i = 0; i < len; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      pcm16[i] = s < 0 ? s * 32768 : s * 32767;
    }
    return pcm16;
  }

  function pcm16ToFloat32(pcm16) {
    const int16 = new Int16Array(pcm16);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768.0;
    }
    return float32;
  }

  // ── Per-peer-connection audio capture logic ──
  function setupRxCapture(pc, audioTrack) {
    const ctx = getAudioContext();
    const stream = new MediaStream([audioTrack]);

    // Capture incoming audio (BBS → modem)
    const source = ctx.createMediaStreamSource(stream);
    const captureNode = ctx.createScriptProcessor(2048, 1, 1);

    source.connect(captureNode);
    // Do NOT connect to destination — audio stays digital, no speakers

    captureNode.onaudioprocess = (e) => {
      if (!wsConnected) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm16 = float32ToPcm16(input);
      document.dispatchEvent(new CustomEvent('__gv_bridge_from_page', {
        detail: { type: 'audio', data: pcm16.buffer }
      }));
    };

    log('Rx capture started');
  }

  function setupTxOutput(pc, sender) {
    const ctx = getAudioContext();
    const dest = ctx.createMediaStreamDestination();

    // Replace the WebRTC sender's track with our custom stream
    sender.replaceTrack(dest.stream.getAudioTracks()[0])
      .then(() => log('Tx track replaced'))
      .catch((err) => log('Tx replaceTrack error: ' + err.message));

    // ScriptProcessorNode that outputs silence when no data, or queued audio
    const outputNode = ctx.createScriptProcessor(2048, 0, 1);
    outputNode.connect(dest);

    // Per-peer audio queue
    const queue = [];
    pc._gvAudioQueue = queue;
    pc._gvAudioDest = dest;

    outputNode.onaudioprocess = (e) => {
      const output = e.outputBuffer.getChannelData(0);
      if (queue.length > 0) {
        const chunk = queue.shift();
        const n = Math.min(output.length, chunk.length);
        for (let i = 0; i < n; i++) output[i] = chunk[i];
        for (let i = n; i < output.length; i++) output[i] = 0;
      } else {
        output.fill(0); // silence while idle
      }
    };

    log('Tx output ready');
  }

  // ── RTCPeerConnection monkey-patch ──
  const OriginalRTCPeerConnection = window.RTCPeerConnection;

  window.RTCPeerConnection = function PatchedRTCPeerConnection(...args) {
    const pc = new OriginalRTCPeerConnection(...args);
    peerConnections.add(pc);
    log('RTCPeerConnection created');

    // --- Intercept incoming audio tracks ---
    pc.addEventListener('track', (event) => {
      if (event.track.kind === 'audio') {
        log('Remote audio track received');
        setupRxCapture(pc, event.track);
      }
    });

    // --- Intercept addTrack to find audio sender ---
    const origAddTrack = pc.addTrack.bind(pc);
    pc.addTrack = function (track, stream) {
      const sender = origAddTrack(track, stream);
      if (track.kind === 'audio') {
        log('Audio sender detected (addTrack)');
        setupTxOutput(pc, sender);
      }
      return sender;
    };

    // --- Intercept addTransceiver to find audio sender ---
    const origAddTransceiver = pc.addTransceiver.bind(pc);
    pc.addTransceiver = function (trackOrKind, init) {
      const result = origAddTransceiver(trackOrKind, init);
      const kind = typeof trackOrKind === 'string' ? trackOrKind : trackOrKind.kind;
      if (kind === 'audio') {
        log('Audio sender detected (addTransceiver)');
        setupTxOutput(pc, result.sender);
      }
      return result;
    };

    // --- Cleanup on connection close ---
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'closed' || pc.connectionState === 'disconnected') {
        peerConnections.delete(pc);
        log('RTCPeerConnection closed');
      }
    });

    return pc;
  };

  // Copy static methods
  window.RTCPeerConnection.prototype = OriginalRTCPeerConnection.prototype;

  // ── Listen for content script messages ──
  document.addEventListener('__gv_bridge_from_content', (event) => {
    const detail = event.detail;

    if (detail.type === 'server_connected') {
      wsConnected = true;
      log('Server connected');
      document.dispatchEvent(new CustomEvent('__gv_bridge_from_page', {
        detail: { type: 'ready' }
      }));
    } else if (detail.type === 'server_disconnected') {
      wsConnected = false;
      log('Server disconnected');
    } else if (detail.type === 'audio' && detail.data) {
      // Incoming PCM16 audio from server → play to all peers
      const float32 = pcm16ToFloat32(detail.data);
      for (const pc of peerConnections) {
        if (pc._gvAudioQueue) {
          pc._gvAudioQueue.push(float32);
        }
      }
    }
  });

  log('WebRTC interceptor installed');

})();
