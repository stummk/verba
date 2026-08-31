// WebSocket client with auto-reconnect; dispatches events by type.

const listeners = new Map(); // type -> Set<fn>
// close code the backend uses when the socket carries no valid session
const SESSION_REQUIRED = 4401;
let socket = null;
let reconnectDelay = 1000;

export function on(type, handler) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(handler);
  return () => listeners.get(type).delete(handler);
}

function dispatch(type, data) {
  for (const handler of listeners.get(type) ?? []) handler(data);
  for (const handler of listeners.get("*") ?? []) handler(type, data);
}

export function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.onopen = () => {
    reconnectDelay = 1000;
    dispatch("connection", { online: true });
  };
  socket.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      dispatch(message.type, message.data);
    } catch { /* ignore malformed frames */ }
  };
  socket.onclose = (event) => {
    dispatch("connection", { online: false });
    // 4401: the server has a session requirement this socket cannot meet.
    // Reconnecting would only produce the same rejection every second — the
    // shell takes over and shows the login instead.
    if (event.code === SESSION_REQUIRED) {
      window.dispatchEvent(new CustomEvent("auth:required"));
      return;
    }
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  };
}

// keepalive so proxies don't drop the connection (single global timer)
setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
}, 25000);
