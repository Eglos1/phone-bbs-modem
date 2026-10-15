/**
 * Modem Bridge — Background Service Worker
 * 
 * Manages extension state and provides UI feedback.
 * The actual audio bridging happens in content.js + the Python server.
 */

// Track connection status
let connected = false;

// Listen for messages from the content script
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type === 'status') {
    connected = msg.connected;
    const color = connected ? '#00aa00' : '#aa0000';
    const text = connected ? 'ON' : 'OFF';

    chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setBadgeText({ text });
  }
});

// When the extension icon is clicked, open Call2Friends
chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: 'https://call2friends.com' });
});
