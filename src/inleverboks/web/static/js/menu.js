// Header menus built with <details>: they work without JavaScript. This only closes an
// open menu with Escape (focus back on its button) or with a click or focus elsewhere.
const menus = Array.from(document.querySelectorAll("details[data-menu]"));

const close = (menu, focus) => {
  if (!menu.open) return;
  menu.open = false;
  if (focus) menu.querySelector("summary")?.focus();
};

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const menu of menus) {
    if (menu.open) close(menu, menu.contains(document.activeElement));
  }
});

for (const type of ["click", "focusin"]) {
  document.addEventListener(type, (event) => {
    for (const menu of menus) {
      if (!menu.contains(event.target)) close(menu, false);
    }
  });
}
