export function chatSocketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws/chat`;
}

export function isSocketOpen(socket) {
  return socket && socket.readyState === WebSocket.OPEN;
}

export function sendJson(socket, payload) {
  if (!isSocketOpen(socket)) {
    return false;
  }

  socket.send(JSON.stringify(payload));
  return true;
}
