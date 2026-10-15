/**
 * Modem Bridge — Content Script
 *
 * 1. Injects the WebRTC interceptor into the page context
 * 2. Manages WebSocket connection to the Python server
 * 3. Relays PCM audio between the page and the server
 */

// ── Inject the page-context interceptor script ──
const injectScript = document.createElement('script');
injectScript.src = chrome.runtime.getURL('inject.js');
injectScript.onload = () => injectScript.remove();
(document.head || document.documentElement).appendChild(injectScript);

// ── WebSocket connection ──
let ws = null;
let reconnectTimer = null;
const WS_URL = 'ws://localhost:9090/';

function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  try {
    ws = new WebSocket(WS_URL);
    ws.binaryType = 'arraybuffer';
  } catch (e) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log('[Modem Bridge] WebSocket connected');
    chrome.runtime.sendMessage({ type: 'status', connected: true });
    maybeStartCapture();
  };

  ws.onmessage = (event) => {
    // Binary message = PCM16 audio from server → relay to page
    if (event.data instanceof ArrayBuffer) {
      document.dispatchEvent(new CustomEvent('__gv_bridge_from_content', {
        detail: { type: 'audio', data: event.data }
      }));
    } else {
      // JSON control message
      try {
        const msg = JSON.parse(event.data);
        document.dispatchEvent(new CustomEvent('__gv_bridge_from_content', {
          detail: msg
        }));
      } catch (e) {
        console.warn('[Modem Bridge] Invalid message:', event.data);
      }
    }
  };

  ws.onclose = (event) => {
    console.log('[Modem Bridge] WebSocket disconnected, code:', event.code, 'reason:', event.reason);
    chrome.runtime.sendMessage({ type: 'status', connected: false });
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = (event) => {
    console.error('[Modem Bridge] WebSocket error:', event);
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 3000);
}

function sendAudioToServer(audioData) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(audioData);
  }
}

// ── Page ↔ Content relay ──
document.addEventListener('__gv_bridge_from_page', (event) => {
  const detail = event.detail;
  if (detail.type === 'audio' && detail.data) {
    // Forward PCM16 audio data to the server
    sendAudioToServer(detail.data);
  } else if (detail.type === 'ready') {
    // Page script is ready to bridge
    console.log('[Modem Bridge] Page script ready');
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ready' }));
    }
  } else if (detail.type === 'log') {
    console.log('[Modem Bridge:Page]', detail.message);
  }
});

// ── Connection lifecycle ──
function maybeStartCapture() {
  // Tell the page script we're connected
  document.dispatchEvent(new CustomEvent('__gv_bridge_from_content', {
    detail: { type: 'server_connected' }
  }));
}

// Start connecting immediately
connect();

console.log('[Modem Bridge] Content script loaded');
