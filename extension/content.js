// Floating "Save to CRM" button. Lives in a shadow root so the page's CSS can't
// break it; positioned bottom-right so it survives the sites' frequent redesigns.
(() => {
  if (window.__pressEngine) return;
  window.__pressEngine = true;

  const host = document.createElement("div");
  host.id = "press-engine-capture";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .wrap, .toast { font: 14px/1.3 system-ui, -apple-system, "Segoe UI", sans-serif; }
      .wrap { position: fixed; right: 18px; bottom: 18px; z-index: 2147483647; display: flex; gap: 6px; align-items: center; }
      .wrap[hidden] { display: none; }
      button { border: 0; border-radius: 999px; padding: 10px 16px; cursor: pointer; font: inherit; font-weight: 600;
               background: #2f5fd0; color: #fff; box-shadow: 0 4px 14px rgba(0,0,0,.25); }
      button.small { padding: 10px 12px; background: #fff; color: #2f5fd0; }
      button:disabled { opacity: .7; cursor: default; }
      .toast { position: fixed; z-index: 2147483647; right: 18px; bottom: 70px; max-width: 320px; background: #1d1d1f; color: #fff; padding: 10px 14px;
               border-radius: 10px; box-shadow: 0 4px 14px rgba(0,0,0,.3); display: none; }
      .toast.show { display: block; }
      .toast.error { background: #b3261e; }
    </style>
    <div class="wrap" hidden>
      <button class="small" id="note" title="Save with a note or a business email">✎</button>
      <button id="save">Save to CRM</button>
    </div>
    <div class="toast" id="toast" role="status"></div>`;
  document.documentElement.appendChild(host);

  const wrap = shadow.querySelector(".wrap");
  const save = shadow.getElementById("save");
  const noteBtn = shadow.getElementById("note");
  const toast = shadow.getElementById("toast");
  let toastTimer;

  function showToast(message, isError) {
    toast.textContent = message;
    toast.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast.className = "toast"), isError ? 7000 : 3500);
  }

  function refresh() {
    wrap.hidden = !window.PressEngineCollect.collect(location, document);
  }

  async function send(extra) {
    const data = window.PressEngineCollect.collect(location, document);
    if (!data) return showToast("Open a profile or a video first.", true);
    save.disabled = noteBtn.disabled = true;
    save.textContent = "Saving…";
    try {
      const res = await chrome.runtime.sendMessage({ type: "capture", payload: { ...data, ...extra } });
      if (res && res.ok) {
        const r = res.result;
        showToast(`${r.created ? "Saved" : "Already saved"}: ${r.name}${r.needs_review ? " (needs a handle)" : ""}`);
        save.textContent = "Saved ✓";
      } else {
        showToast((res && res.error) || "Couldn't save.", true);
        save.textContent = "Save to CRM";
      }
    } catch (e) {
      showToast(`Extension error: ${e.message}. Try reloading the page.`, true);
      save.textContent = "Save to CRM";
    } finally {
      save.disabled = noteBtn.disabled = false;
      setTimeout(() => (save.textContent = "Save to CRM"), 2500);
    }
  }

  save.addEventListener("click", () => send({}));
  noteBtn.addEventListener("click", () => {
    const input = window.prompt("Note for the CRM (optional). Paste a business email here too if you can see one:");
    if (input === null) return;
    const email = (input.match(/[\w.+-]+@[\w-]+(\.[\w-]+)+/) || [])[0];
    send({ note: input.trim() || undefined, email });
  });

  // These sites are single-page apps: re-check when the URL changes.
  let last = "";
  setInterval(() => {
    if (location.href !== last) {
      last = location.href;
      refresh();
    }
  }, 800);
})();
