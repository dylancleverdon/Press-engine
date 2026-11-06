// Service worker: talks to the Press Engine backend on behalf of the content script.
const DEFAULT_URL = "http://localhost:8000";

async function getSettings() {
  const { backendUrl = DEFAULT_URL, token = "" } = await chrome.storage.local.get(["backendUrl", "token"]);
  return { backendUrl: (backendUrl || DEFAULT_URL).replace(/\/+$/, ""), token };
}

async function call(path, options = {}) {
  const { backendUrl, token } = await getSettings();
  if (!token) return { ok: false, error: "Open the extension's options and paste your Press Engine token." };
  try {
    const res = await fetch(`${backendUrl}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(options.headers || {}) },
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) return { ok: false, error: "Press Engine rejected the token. Check the extension options." };
    if (!res.ok) return { ok: false, error: body.detail ? JSON.stringify(body.detail) : `HTTP ${res.status}` };
    return { ok: true, result: body };
  } catch (e) {
    return { ok: false, error: `Can't reach ${backendUrl}. Is press-engine serve running? (${e.message})` };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "capture") {
    call("/api/capture", { method: "POST", body: JSON.stringify({ ...msg.payload, source: "extension" }) }).then(sendResponse);
    return true; // async response
  }
  if (msg && msg.type === "test") {
    call("/api/me").then(sendResponse);
    return true;
  }
  return false;
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
