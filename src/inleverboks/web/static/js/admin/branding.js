// Live preview and contrast check for the house style screen. The server computes the
// same check (services/branding.py) when the form is checked or saved; it stays leading.
const form = document.getElementById("branding-form");
const preview = document.querySelector("[data-brand-preview]");

const channel = (v) => {
  const c = v / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};
const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
};
const ratio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const setState = (el, ok) => {
  el.querySelector('[data-state="ok"]').hidden = !ok;
  el.querySelector('[data-state="fail"]').hidden = ok;
};

if (form && preview) {
  const inputs = [...form.querySelectorAll("[data-brand-var]")];
  const colour = (ref) =>
    ref.startsWith("#") ? ref : form.querySelector(`[data-brand-var="${ref}"]`).value.toLowerCase();

  const update = () => {
    for (const input of inputs) {
      const key = input.dataset.brandVar;
      preview.style.setProperty(`--color-${key.replace(/_/g, "-")}`, input.value);
      form.querySelector(`[data-brand-hex="${key}"]`).textContent = input.value.toLowerCase();
    }
    let allOk = true;
    for (const row of form.querySelectorAll("[data-contrast]")) {
      const value = ratio(colour(row.dataset.fg), colour(row.dataset.bg));
      const ok = Math.round(value * 100) / 100 >= Number(row.dataset.min);
      allOk &&= ok;
      row.querySelector("[data-ratio]").textContent = `${value.toFixed(2)}:1`;
      setState(row, ok);
    }
    setState(form.querySelector("[data-contrast-summary]"), allOk);
    preview.dataset.font = form.querySelector("[data-brand-font]").value;
    preview.querySelector("[data-brand-name-preview]").textContent =
      form.querySelector("[data-brand-name]").value;
  };

  form.addEventListener("input", update);
  form.addEventListener("change", update);
}
