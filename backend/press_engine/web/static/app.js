// Copy buttons in the DM queue: <button data-copy="#textarea-id">Copy</button>
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.value || el.textContent);
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = old), 1200);
  } catch {
    el.select?.();
  }
});

// "Are you sure?" for destructive actions: data-confirm="..." on a <form> or its submit button.
// Uses a small in-page dialog because the Mac app's window has no native confirm().
function askConfirm(message) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML =
      '<div class="modal" role="alertdialog" aria-modal="true"><p></p>' +
      '<div class="row"><button type="button" class="primary" data-ok>Confirm</button>' +
      '<button type="button" data-cancel>Cancel</button></div></div>';
    backdrop.querySelector("p").textContent = message;
    const finish = (ok) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      resolve(ok);
    };
    const onKey = (e) => {
      if (e.key === "Escape") finish(false);
    };
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) finish(false);
    });
    backdrop.querySelector("[data-ok]").addEventListener("click", () => finish(true));
    backdrop.querySelector("[data-cancel]").addEventListener("click", () => finish(false));
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
    backdrop.querySelector("[data-ok]").focus();
  });
}

document.addEventListener("submit", async (e) => {
  const form = e.target;
  const submitter = e.submitter || null;
  const message = (submitter && submitter.dataset.confirm) || form.dataset.confirm;
  if (!message) return;
  if (form.dataset.confirmed === "1") {
    delete form.dataset.confirmed;
    return;
  }
  e.preventDefault();
  if (!(await askConfirm(message))) return;
  form.dataset.confirmed = "1";
  if (form.requestSubmit) {
    form.requestSubmit(submitter || undefined);
    return;
  }
  // Older WebKit: submit by hand, keeping the clicked button's name/value and formaction.
  if (submitter && submitter.name) {
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = submitter.name;
    hidden.value = submitter.value;
    form.appendChild(hidden);
  }
  if (submitter && submitter.hasAttribute("formaction")) form.action = submitter.formAction;
  form.submit();
});

// Paste screenshots straight into a text box: <textarea data-paste-images="file-input-id">.
// Pasted images are added to that file input, so they're sent with the form.
document.addEventListener("paste", (e) => {
  const box = e.target.closest?.("[data-paste-images]");
  if (!box) return;
  const input = document.getElementById(box.dataset.pasteImages);
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!input || !files.length) return;
  e.preventDefault();
  const dt = new DataTransfer();
  [...input.files, ...files].forEach((f, i) =>
    dt.items.add(f.name && f.name !== "image.png" ? f : new File([f], `pasted-${Date.now()}-${i}.png`, { type: f.type })));
  input.files = dt.files;
  let note = input.nextElementSibling;
  if (!note || !note.classList.contains("paste-note")) {
    note = document.createElement("div");
    note.className = "small muted paste-note";
    input.after(note);
  }
  note.textContent = `${input.files.length} image${input.files.length === 1 ? "" : "s"} attached`;
});

// Discovery vetting without reloading the page (which jumped back to the top):
// Approve / Reject / Back to To vet on a row, and the "ticked" buttons, are sent in the
// background; the rows fade out, the counts update and an Undo appears for a few seconds.
function showToast(text, undo) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast";
  t.setAttribute("role", "status");
  const span = document.createElement("span");
  span.textContent = text;
  t.appendChild(span);
  if (undo) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "Undo";
    b.addEventListener("click", () => { t.remove(); undo(); });
    t.appendChild(b);
  }
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

async function sendVet(url, data) {
  const r = await fetch(url, { method: "POST", body: data, credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function updateCounts(counts) {
  for (const [state, n] of Object.entries(counts || {})) {
    document.querySelectorAll(`[data-count="${state}"]`).forEach((el) => (el.textContent = n));
  }
}

const UNDO_DECISION = { pending: "pending", rejected: "reject", approved: "approve", filtered: "filtered" };

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (!form.classList?.contains("vet-form")) return;
  const submitter = e.submitter;
  if (!submitter || submitter.dataset.confirm) return; // "Approve all" etc: normal submit after the confirm dialog
  const url = submitter.hasAttribute("formaction") ? submitter.formAction : form.action;
  if (url.includes("everything=")) return;
  e.preventDefault();
  const data = new FormData(form);
  data.set("decision", submitter.value);
  const one = new URL(url, location.href).searchParams.get("one");
  if (!one) {
    if (!data.getAll("ids").length) { showToast("Tick some contacts first"); return; }
  } else {
    data.delete("ids");
  }
  submitter.disabled = true;
  try {
    const res = await sendVet(url, data);
    const rows = res.ids.map((id) => document.getElementById(`person-${id}`)).filter(Boolean);
    rows.forEach((row) => row.classList.add("leaving"));
    setTimeout(() => rows.forEach((row) => row.remove()), 260);
    updateCounts(res.counts);
    showToast(res.message, async () => {
      // Put each one back where it was.
      const byState = {};
      for (const [id, state] of Object.entries(res.before)) (byState[state] ||= []).push(id);
      for (const [state, ids] of Object.entries(byState)) {
        const undo = new FormData();
        undo.set("decision", UNDO_DECISION[state] || "pending");
        undo.set("show", data.get("show") || "pending");
        ids.forEach((id) => undo.append("ids", id));
        await sendVet(form.action, undo);
      }
      location.reload(); // simplest way to put the rows back in order
    });
  } catch (err) {
    submitter.disabled = false;
    showToast("That didn't go through. Try again, or refresh the page.");
  }
});

// Update banner: progress while an update installs, a countdown before the restart
// (Restart now / Later), "Restarting…" while the app switches over, and "Updated to X".
(function updateBanner() {
  const el = document.getElementById("update-banner");
  if (!el) return;
  const owner = el.dataset.owner === "1";
  let restarting = false;

  function render(s) {
    el.innerHTML = "";
    const add = (html) => el.insertAdjacentHTML("beforeend", html);
    const text = (t) => { const span = document.createElement("span"); span.textContent = t; el.appendChild(span); };
    if (restarting) {
      add('<span class="spinner" aria-hidden="true"></span>');
      text("Restarting Press Engine to finish the update… this page reloads by itself.");
    } else if (s.phase === "installing") {
      add('<span class="spinner" aria-hidden="true"></span> <b>Updating Press Engine:</b> ');
      text(`${s.step || "Getting the new version"}…`);
    } else if (s.phase === "ready") {
      add(`<b>Version ${s.version} is installed.</b> `);
      text(s.postponed ? "It will be applied overnight, or next time you open Press Engine."
                       : `Press Engine restarts in ${s.restart_in ?? 0} seconds to switch over.`);
      if (owner) {
        const box = document.createElement("span");
        box.className = "actions";
        box.innerHTML = '<button type="button" class="primary" data-now>Restart now</button>' +
          (s.postponed ? "" : '<button type="button" data-later>Later</button>');
        el.appendChild(box);
        box.querySelector("[data-now]").onclick = () => post("/app/restart-now");
        const later = box.querySelector("[data-later]");
        if (later) later.onclick = () => post("/app/restart-later");
      }
    } else if (s.phase === "error") {
      add("<b>The update didn't install.</b> ");
      text(`${s.error || ""} Press Engine keeps running the current version and tries again later.`);
    } else if (s.announce) {
      add(`<b>Updated to version ${s.announce}.</b> <a href="https://github.com/dylancleverdon/Press-engine/releases/tag/v${s.announce}" target="_blank" rel="noopener">What's new</a>`);
      const box = document.createElement("span");
      box.className = "actions";
      box.innerHTML = '<button type="button">Dismiss</button>';
      box.querySelector("button").onclick = async () => { el.hidden = true; await post("/app/update-dismiss", false); };
      el.appendChild(box);
    }
    el.hidden = !(restarting || ["installing", "ready", "error"].includes(s.phase) || s.announce);
  }

  async function post(url, refresh = true) {
    try { await fetch(url, { method: "POST", credentials: "same-origin" }); } catch (e) { /* ignore */ }
    if (refresh) poll();
  }

  let timer = null;
  async function poll() {
    clearTimeout(timer);
    let active = false;
    try {
      const r = await fetch("/app/update-status", { credentials: "same-origin" });
      if (!r.ok) throw new Error();
      const s = await r.json();
      if (restarting) { location.reload(); return; } // back up on the new version
      if (s.phase === "ready" && !s.postponed && s.restart_in === 0) restarting = true;
      render(s);
      el.dataset.phase = s.phase || "idle";
      active = restarting || s.phase === "installing" || (s.phase === "ready" && !s.postponed);
    } catch (e) {
      // While an update is underway, a failed request means the app is restarting.
      if (["installing", "ready"].includes(el.dataset.phase) || restarting) { restarting = true; render({}); active = true; }
    }
    timer = setTimeout(poll, active ? 1500 : 60000); // otherwise, check once a minute
  }

  const phase = el.dataset.phase;
  timer = setTimeout(poll, phase === "installing" || phase === "ready" ? 300 : 60000);
})();
