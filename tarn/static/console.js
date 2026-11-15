// In-browser VM console: noVNC talking to Tarn's WebSocket-to-VNC bridge.
import RFB from "./vendor/novnc/core/rfb.js";

const params = new URLSearchParams(location.search);
const uuid = params.get("uuid");
if (params.get("embed")) document.body.classList.add("embed");

const $ = (id) => document.getElementById(id);
const status = $("status");
const overlay = $("overlay");
let rfb = null;
let scale = true;

function setStatus(text, bad = false) {
  status.textContent = text;
  status.classList.toggle("bad", bad);
}

async function loadName() {
  try {
    const r = await fetch(`/api/vms/${encodeURIComponent(uuid)}`, { credentials: "same-origin" });
    if (r.status === 401) {
      overlay.textContent = "Signed out. Sign in to Tarn again, then reload this page.";
      return false;
    }
    const vm = await r.json();
    $("vmname").textContent = vm.name;
    document.title = `${vm.name} console`;
    return true;
  } catch {
    return true;
  }
}

function connect() {
  $("reconnect").hidden = true;
  overlay.textContent = "";
  setStatus("Connecting…");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/api/vms/${encodeURIComponent(uuid)}/vnc`;
  rfb = new RFB($("screen"), url, { wsProtocols: ["binary"] });
  rfb.scaleViewport = scale;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;

  rfb.addEventListener("connect", () => {
    setStatus("Connected. Click the screen to type.");
    $("cad").disabled = false;
    $("paste").disabled = !navigator.clipboard;
    rfb.focus();
  });
  rfb.addEventListener("disconnect", (e) => {
    $("cad").disabled = true;
    $("paste").disabled = true;
    $("reconnect").hidden = false;
    if (e.detail.clean) {
      setStatus("Disconnected");
      overlay.textContent = "The console closed. If the VM was shut down, start it again from Tarn.";
    } else {
      setStatus("Connection lost", true);
      overlay.textContent = "Couldn't reach the VM's display. Is the VM running?";
    }
    rfb = null;
  });
  rfb.addEventListener("desktopname", (e) => {
    if (e.detail.name && $("vmname").textContent === "Console") $("vmname").textContent = e.detail.name;
  });
}

$("cad").addEventListener("click", () => { if (rfb) { rfb.sendCtrlAltDel(); rfb.focus(); } });
$("scale").addEventListener("click", (e) => {
  scale = !scale;
  e.currentTarget.setAttribute("aria-pressed", String(scale));
  e.currentTarget.textContent = scale ? "Fit to window" : "Actual size";
  if (rfb) rfb.scaleViewport = scale;
});
$("full").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
});
$("reconnect").addEventListener("click", connect);

// Types clipboard text as key presses: handy for long passwords and commands
// at installers, where there is no guest clipboard integration.
$("paste").addEventListener("click", async () => {
  if (!rfb) return;
  let text;
  try { text = await navigator.clipboard.readText(); } catch { setStatus("Clipboard access was blocked by the browser", true); return; }
  if (text.length > 2000) { setStatus("Clipboard text is too long to type (2000 characters max)", true); return; }
  for (const ch of text) {
    const code = ch === "\n" ? 0xff0d : ch.codePointAt(0);
    const keysym = code < 0x100 ? code : 0x01000000 + code;
    rfb.sendKey(keysym, null, true);
    rfb.sendKey(keysym, null, false);
  }
  rfb.focus();
  setStatus(`Typed ${text.length} characters`);
});

if (!uuid) {
  overlay.textContent = "No VM selected.";
  setStatus("No VM", true);
} else {
  loadName().then((ok) => { if (ok) connect(); });
}
