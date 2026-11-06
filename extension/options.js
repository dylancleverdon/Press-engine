const $ = (id) => document.getElementById(id);

function setStatus(text, ok) {
  $("status").textContent = text;
  $("status").className = ok ? "ok" : "err";
}

async function load() {
  const { backendUrl = "http://localhost:8000", token = "" } = await chrome.storage.local.get(["backendUrl", "token"]);
  $("url").value = backendUrl;
  $("token").value = token;
}

async function save() {
  let url = $("url").value.trim().replace(/\/+$/, "") || "http://localhost:8000";
  if (!/^https?:\/\//.test(url)) url = "http://" + url;
  let origin;
  try {
    origin = new URL(url).origin;
  } catch {
    return setStatus("That address doesn't look right.", false);
  }
  // Remote backends (e.g. a tunnel to D's machine) need an explicit host permission.
  if (!/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) {
    const granted = await chrome.permissions.request({ origins: [origin + "/*"] });
    if (!granted) return setStatus(`Chrome didn't allow access to ${origin}.`, false);
  }
  await chrome.storage.local.set({ backendUrl: url, token: $("token").value.trim() });
  setStatus("Saved.", true);
}

async function test() {
  await save();
  const res = await chrome.runtime.sendMessage({ type: "test" });
  if (res && res.ok) setStatus(`Connected as ${res.result.name} (${res.result.role}).`, true);
  else setStatus((res && res.error) || "No response.", false);
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
