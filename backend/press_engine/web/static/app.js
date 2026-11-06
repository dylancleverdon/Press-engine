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
