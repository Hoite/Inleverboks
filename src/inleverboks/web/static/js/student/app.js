// Student hand-in page: choose files, hash (SHA-256), check, preview, confirm and upload.
// Nothing is stored in the browser (no localStorage/sessionStorage/IndexedDB) and nothing
// leaves the browser before the student confirms.
import { autoSlot, checkFiles, extensionOf } from "./checks.js";

const root = document.getElementById("student-app");
if (root) start(JSON.parse(root.dataset.config));

function start(config) {
  const t = config.text;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const offset = config.serverNow - Date.now();
  const idempotencyKey = randomKey();
  let entries = []; // {id, file, name, size, sha256, slotId}
  let nextId = 1;
  let busy = false;
  let finished = false;

  const $ = (id) => document.getElementById(id);
  const slotsEl = $("slots");
  const statusEl = $("status");

  // Elements ------------------------------------------------------------------------
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1).replace(".", ",")} kB`;
    return `${(bytes / 1048576).toFixed(1).replace(".", ",")} MB`;
  }

  function groupedHash(hash) {
    return hash ? hash.match(/.{1,8}/g).join(" ") : t.hashing;
  }

  function format(template, ...values) {
    let i = 0;
    return template.replace(/%s/g, () => String(values[i++]));
  }

  // Hashing -------------------------------------------------------------------------
  async function sha256(file) {
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function addFiles(fileList, slotId) {
    const taken = new Set(entries.map((e) => e.slotId).filter(Boolean));
    for (const file of fileList) {
      let target = slotId;
      if (target === undefined) target = autoSlot(file.name, config, taken);
      if (target) {
        // A slot holds one file: the previous one moves to the extra files (or is replaced).
        for (const other of entries.filter((e) => e.slotId === target)) {
          if (config.allowExtra) other.slotId = null;
          else entries = entries.filter((e) => e !== other);
        }
        taken.add(target);
      }
      const entry = { id: nextId++, file, name: file.name.normalize("NFC"), size: file.size, sha256: null, slotId: target ?? null };
      entries.push(entry);
      sha256(file).then((hash) => {
        entry.sha256 = hash;
        render();
      });
    }
    touch();
    render();
  }

  // Rendering -----------------------------------------------------------------------
  function fileRow(entry, result, index) {
    const errors = result.fileErrors.get(index) || [];
    const warnings = result.fileWarnings.get(index) || [];
    const notes = result.fileNotes.get(index) || [];
    const move = el("select", { "aria-label": `${t.slot}: ${entry.name}`, onchange: (ev) => {
      const value = ev.target.value || null;
      for (const other of entries) if (value && other.slotId === value && other !== entry) other.slotId = null;
      entry.slotId = value;
      touch();
      render();
    } });
    for (const slot of config.slots) {
      move.append(el("option", { value: slot.id, selected: entry.slotId === slot.id, text: slot.label }));
    }
    if (config.allowExtra || entry.slotId === null) {
      move.append(el("option", { value: "", selected: entry.slotId === null, text: t.extra }));
    }
    const copy = el("button", { type: "button", class: "button button-small button-secondary", text: t.copy, disabled: !entry.sha256,
      onclick: async () => {
        try { await navigator.clipboard.writeText(entry.sha256); copy.textContent = t.copied; } catch { /* ignore */ }
      } });
    const preview = el("button", { type: "button", class: "button button-small button-secondary", text: t.preview,
      onclick: () => openPreview(entry) });
    const remove = el("button", { type: "button", class: "button button-small button-danger", text: t.remove,
      onclick: () => { entries = entries.filter((e) => e !== entry); touch(); render(); } });
    const messages = el("ul", { class: "file-messages" });
    for (const code of errors) messages.append(el("li", { class: "msg-error" }, el("span", { "aria-hidden": "true", text: "✕ " }), t[`e_${code}`] || code));
    for (const code of warnings) messages.append(el("li", { class: "msg-warning" }, el("span", { "aria-hidden": "true", text: "! " }), t[`w_${code}`] || code));
    for (const code of notes) messages.append(el("li", { class: "msg-note" }, el("span", { "aria-hidden": "true", text: "i " }), code === "zip_unchecked" ? t.zipNote : code));
    return el("li", { class: `file-row${errors.length ? " has-error" : warnings.length ? " has-warning" : ""}` },
      el("div", { class: "file-name", text: entry.name }),
      el("div", { class: "file-meta" },
        el("span", { text: `${t.size}: ${formatSize(entry.size)}` }),
        el("label", { class: "file-move" }, el("span", { class: "visually-hidden", text: t.slot }), move)),
      el("div", { class: "file-hash" },
        el("span", { class: "mono", text: `SHA-256: ${groupedHash(entry.sha256)}` }), copy),
      messages,
      el("div", { class: "button-row" }, preview, remove));
  }

  function slotPicker(slot) {
    const input = el("input", { type: "file", class: "visually-hidden", id: `pick-${slot.id}`,
      onchange: (ev) => { addFiles([...ev.target.files].slice(0, 1), slot.id); ev.target.value = ""; } });
    return el("div", { class: "slot-pick" },
      el("label", { class: "button button-small button-secondary", for: `pick-${slot.id}`, text: t.choose }), input);
  }

  function render() {
    const files = entries.map((e) => ({ name: e.name, size: e.size, sha256: e.sha256, slotId: e.slotId }));
    const result = checkFiles(files, config);
    slotsEl.replaceChildren();
    for (const slot of config.slots) {
      const mine = entries.filter((e) => e.slotId === slot.id);
      const list = el("ul", { class: "file-list" });
      for (const entry of mine) list.append(fileRow(entry, result, entries.indexOf(entry)));
      slotsEl.append(el("section", { class: `slot-card${mine.length ? " filled" : ""}` },
        el("h3", {}, el("span", { "aria-hidden": "true", text: mine.length ? "✓ " : "○ " }), slot.label),
        slot.pattern ? el("p", { class: "hint" }, `${t.expectedName}: `, el("span", { class: "mono", text: slot.pattern })) : null,
        mine.length ? list : slotPicker(slot)));
    }
    const extra = entries.filter((e) => e.slotId === null);
    if (config.allowExtra || extra.length) {
      const list = el("ul", { class: "file-list" });
      for (const entry of extra) list.append(fileRow(entry, result, entries.indexOf(entry)));
      const input = el("input", { type: "file", multiple: true, class: "visually-hidden", id: "pick-extra",
        onchange: (ev) => { addFiles([...ev.target.files], null); ev.target.value = ""; } });
      slotsEl.append(el("section", { class: "slot-card extra-card" },
        el("h3", { text: t.extra }), extra.length ? list : null,
        config.allowExtra ? el("div", { class: "slot-pick" }, el("label", { class: "button button-small button-secondary", for: "pick-extra", text: t.chooseMany }), input) : null));
    }
    const blocking = $("blocking");
    const blockingList = $("blocking-list");
    blockingList.replaceChildren();
    for (const code of result.errors) {
      if (code === "no_files" && entries.length === 0) continue;
      blockingList.append(el("li", { text: t[`e_${code}`] || t.noFiles }));
    }
    blocking.hidden = blockingList.children.length === 0;
    return result;
  }

  // Preview (sandboxed iframe) --------------------------------------------------------
  const previewDialog = $("preview-dialog");
  const frame = $("preview-frame");
  let frameReady = null;
  $("preview-close").textContent = t.close;
  $("preview-close").addEventListener("click", () => previewDialog.close());
  $("preview-note").textContent = t.previewNote;

  function ensureFrame() {
    if (!frameReady) {
      frameReady = new Promise((resolve) => {
        const onMessage = (ev) => {
          if (ev.source === frame.contentWindow && ev.data && ev.data.type === "ready") {
            window.removeEventListener("message", onMessage);
            resolve();
          }
        };
        window.addEventListener("message", onMessage);
        frame.src = config.previewFrame;
      });
    }
    return frameReady;
  }

  async function openPreview(entry) {
    touch();
    $("preview-title").textContent = entry.name;
    previewDialog.showModal();
    await ensureFrame();
    const buffer = await entry.file.arrayBuffer();
    frame.contentWindow.postMessage(
      { type: "render", name: entry.name, ext: extensionOf(entry.name), mime: entry.file.type, buffer,
        text: { noPreview: t.noPreview, zipNote: t.zipNote } },
      "*",
      [buffer],
    );
  }

  // Confirmation and upload -------------------------------------------------------------
  function deviations(result) {
    const items = [];
    for (const w of result.warnings) {
      if (w.code === "count_mismatch") items.push(format(t.w_count_mismatch, w.actual, w.expected));
      else if (w.code === "empty_slot") items.push(format(t.w_empty_slot, w.slot));
    }
    result.fileWarnings.forEach((codes, index) => {
      for (const code of codes) items.push(`${entries[index].name}: ${t[`w_${code}`] || code}`);
    });
    return items;
  }

  function confirmDeviations(items) {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = t.confirmTitle;
    $("confirm-intro").textContent = t.confirmIntro;
    $("confirm-list").replaceChildren(...items.map((text) => el("li", { text })));
    $("confirm-yes").textContent = t.confirmYes;
    $("confirm-no").textContent = t.confirmNo;
    return new Promise((resolve) => {
      const done = (value) => {
        dialog.close();
        $("confirm-yes").onclick = null;
        $("confirm-no").onclick = null;
        resolve(value);
      };
      $("confirm-yes").onclick = () => done(true);
      $("confirm-no").onclick = () => done(false);
      dialog.addEventListener("cancel", () => resolve(false), { once: true });
      dialog.showModal();
      $("confirm-no").focus();
    });
  }

  $("submit-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (busy || finished) return;
    touch();
    statusEl.textContent = "";
    const result = render();
    if (entries.length === 0) { statusEl.textContent = t.noFiles; return; }
    if (entries.some((e) => !e.sha256)) { statusEl.textContent = t.hashing; return; }
    if (result.blocked) { $("blocking").scrollIntoView({ block: "center" }); return; }
    if (!$("declaration").checked) { statusEl.textContent = t.declarationRequired; $("declaration").focus(); return; }
    const items = deviations(result);
    if (items.length && !(await confirmDeviations(items))) return;
    upload(items.length > 0);
  });

  function upload(warningsConfirmed) {
    busy = true;
    const button = $("submit-button");
    button.disabled = true;
    $("progress-wrap").hidden = false;
    statusEl.textContent = t.uploading;
    const form = new FormData();
    form.append("manifest", JSON.stringify(entries.map((e) => ({ slot_id: e.slotId, size: e.size, sha256: e.sha256 }))));
    form.append("idempotency_key", idempotencyKey);
    form.append("declaration", "accepted");
    form.append("warnings_confirmed", warningsConfirmed ? "true" : "false");
    for (const entry of entries) form.append("files", entry.file, entry.name);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", config.uploadUrl);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) $("progress").value = Math.round((e.loaded / e.total) * 100);
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 200 && data && data.ok) showReceipt(data.html);
      else fail(data && data.message ? data.message : t.uploadFailed, data && data.error === "session");
    });
    xhr.addEventListener("error", () => fail(t.uploadFailed, false));
    xhr.send(form);
  }

  function fail(message, sessionGone) {
    busy = false;
    $("submit-button").disabled = false;
    $("progress-wrap").hidden = true;
    statusEl.textContent = message;
    if (sessionGone) window.setTimeout(() => window.location.assign(config.resetUrl), 4000);
  }

  function showReceipt(html) {
    finished = true;
    entries = []; // release the files
    const template = document.createElement("template");
    template.innerHTML = html; // rendered and escaped by the server
    document.querySelectorAll("dialog[open]").forEach((d) => d.close());
    root.replaceChildren(template.content);
    const receipt = $("receipt");
    if (receipt) receipt.focus();
    root.querySelector('[data-action="print"]')?.addEventListener("click", () => window.print());
    const pdfForm = $("receipt-pdf");
    pdfForm?.addEventListener("submit", () => {
      window.setTimeout(() => pdfForm.querySelector("button").setAttribute("disabled", ""), 0);
    });
    window.setTimeout(() => window.location.assign(config.resetUrl), config.receiptResetSeconds * 1000);
  }

  // Drag and drop -----------------------------------------------------------------------
  const dropzone = $("dropzone");
  $("pick-any").addEventListener("change", (ev) => { addFiles([...ev.target.files]); ev.target.value = ""; });
  for (const name of ["dragenter", "dragover"]) {
    dropzone.addEventListener(name, (ev) => { ev.preventDefault(); dropzone.classList.add("drag"); });
  }
  for (const name of ["dragleave", "drop"]) {
    dropzone.addEventListener(name, () => dropzone.classList.remove("drag"));
  }
  dropzone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    if (ev.dataTransfer && ev.dataTransfer.files.length) addFiles([...ev.dataTransfer.files]);
  });
  // Dropping outside the drop zone must not navigate away from the page.
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("drop", (ev) => ev.preventDefault());

  // Remaining time --------------------------------------------------------------------
  const timeLeft = $("time-left");
  function tick() {
    const left = config.end - (Date.now() + offset);
    if (left <= 0) {
      timeLeft.textContent = t.closed;
      if (!finished && !busy) $("submit-button").disabled = true;
      return;
    }
    const total = Math.floor(left / 1000);
    const h = Math.floor(total / 3600);
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const s = String(total % 60).padStart(2, "0");
    timeLeft.textContent = `${h}:${m}:${s}`;
    window.setTimeout(tick, 1000);
  }
  tick();

  // Inactivity on shared computers -------------------------------------------------------
  let lastActivity = Date.now();
  let warningShown = false;
  function touch() { lastActivity = Date.now(); }
  for (const name of ["pointerdown", "keydown", "scroll"]) window.addEventListener(name, touch, { passive: true });
  const inactive = $("inactive-dialog");
  $("inactive-title").textContent = t.inactiveTitle;
  $("inactive-stay").textContent = t.inactiveStay;
  $("inactive-stay").addEventListener("click", () => { warningShown = false; touch(); inactive.close(); });
  window.setInterval(() => {
    if (finished || busy) return;
    const idle = (Date.now() - lastActivity) / 1000;
    if (!warningShown && idle >= config.inactivitySeconds) {
      warningShown = true;
      inactive.showModal();
    }
    if (warningShown) {
      const left = Math.max(0, Math.ceil(config.inactivitySeconds + 60 - idle));
      $("inactive-body").textContent = format(t.inactiveBody, left);
      if (left === 0) window.location.assign(config.resetUrl);
    }
  }, 1000);

  render();
}

function randomKey() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}
