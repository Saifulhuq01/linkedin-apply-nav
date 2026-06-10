/* ─────────────────────────────────────────────────────────
   Apply-Nav — WebSocket Connection Manager
   ───────────────────────────────────────────────────────── */

let ws;
const wsListeners = [];

function connectWS() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${window.location.host}/ws`);

    ws.onopen = () => {
        document.getElementById('connection-status').className = 'status-dot online';
        document.getElementById('connection-text').innerText = 'Dashboard Connected';
        addLog('Connected to backend application server.', 'success');
    };

    ws.onclose = () => {
        document.getElementById('connection-status').className = 'status-dot';
        document.getElementById('connection-text').innerText = 'Offline (Reconnecting...)';
        setTimeout(connectWS, 2000);
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        // Dispatch to all registered listeners
        wsListeners.forEach(fn => fn(msg));
    };
}

function onWsMessage(callback) {
    wsListeners.push(callback);
}

function addLog(text, level = 'info') {
    const terminal = document.getElementById('terminal-log');
    if (!terminal) return;
    const entry = document.createElement('div');
    entry.className = `log-entry ${level}`;
    entry.innerText = `[${new Date().toLocaleTimeString()}] ${text}`;
    terminal.appendChild(entry);
    terminal.scrollTop = terminal.scrollHeight;
}
