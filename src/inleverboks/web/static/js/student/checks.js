// Mirror of src/inleverboks/services/checks.py for immediate feedback in the browser.
// The server recomputes everything; keep both in sync (tests compare the rules).

export const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const TEMP_RE = /^(~\$.*|.*\.tmp|\.~lock\..*#)$/i;

export function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function matchesPattern(name, pattern) {
  const source = pattern
    .split("*")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${source}$`, "is").test(name);
}

export function isTempFile(name) {
  return TEMP_RE.test(name);
}

/**
 * files: [{name, size, sha256, slotId|null}]
 * config: {slots: [{id, label, pattern, extensions}], extensions, allowExtra,
 *          maxFileBytes, maxSubmissionBytes, maxFiles, templateHashes}
 */
export function checkFiles(files, config) {
  const result = { errors: [], fileErrors: new Map(), fileWarnings: new Map(), fileNotes: new Map(), warnings: [] };
  if (files.length === 0) {
    result.errors.push("no_files");
    return result;
  }
  if (files.length > config.maxFiles) result.errors.push("too_many_files");
  if (files.reduce((sum, f) => sum + f.size, 0) > config.maxSubmissionBytes) {
    result.errors.push("submission_too_large");
  }
  const slots = new Map(config.slots.map((s) => [s.id, s]));
  const templates = new Set(config.templateHashes);
  const hashCount = new Map();
  for (const f of files) {
    if (f.sha256 && f.size > 0) hashCount.set(f.sha256, (hashCount.get(f.sha256) || 0) + 1);
  }
  const used = new Map();
  files.forEach((f, index) => {
    const errors = [];
    const warnings = [];
    const slot = f.slotId ? slots.get(f.slotId) : null;
    if (f.slotId === null && !config.allowExtra) errors.push("extra_not_allowed");
    if (slot) {
      used.set(slot.id, (used.get(slot.id) || 0) + 1);
      if (used.get(slot.id) > 1) errors.push("slot_taken");
    }
    const allowed = (slot && slot.extensions && slot.extensions.length ? slot.extensions : null) || config.extensions;
    if (!allowed.includes(extensionOf(f.name))) errors.push("extension_not_allowed");
    if (f.size > config.maxFileBytes) errors.push("file_too_large");
    if (f.sha256 && templates.has(f.sha256)) warnings.push("empty_template");
    if (isTempFile(f.name)) warnings.push("temp_file");
    if (f.size === 0) warnings.push("empty_file");
    if (f.sha256 && f.size > 0 && hashCount.get(f.sha256) > 1) warnings.push("duplicate_file");
    if (slot && slot.pattern && !matchesPattern(f.name, slot.pattern)) warnings.push("name_mismatch");
    if (extensionOf(f.name) === "zip") result.fileNotes.set(index, ["zip_unchecked"]);
    if (errors.length) result.fileErrors.set(index, errors);
    if (warnings.length) result.fileWarnings.set(index, warnings);
  });
  for (const slot of config.slots) {
    if (!used.get(slot.id)) result.warnings.push({ code: "empty_slot", slot: slot.label });
  }
  const expected = config.slots.length;
  const slotted = [...used.values()].reduce((a, b) => a + b, 0);
  if (expected && (slotted !== expected || files.length !== expected)) {
    result.warnings.push({ code: "count_mismatch", expected, actual: files.length });
  }
  result.blocked = result.errors.length > 0 || [...result.fileErrors.values()].some((e) => e.length);
  result.hasWarnings = result.warnings.length > 0 || [...result.fileWarnings.values()].some((w) => w.length);
  return result;
}

/** Slot for a newly added file: first free slot whose expected name matches. */
export function autoSlot(name, config, taken) {
  for (const slot of config.slots) {
    if (!taken.has(slot.id) && slot.pattern && matchesPattern(name, slot.pattern)) return slot.id;
  }
  if (config.allowExtra) return null;
  const free = config.slots.find((slot) => !taken.has(slot.id));
  return free ? free.id : null;
}
