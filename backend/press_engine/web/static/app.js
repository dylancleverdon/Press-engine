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

// Confirm destructive actions: <form data-confirm="Sure?">
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});
