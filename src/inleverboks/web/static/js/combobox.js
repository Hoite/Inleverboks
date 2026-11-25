// Searchable select (ARIA 1.2 combobox with a listbox), as progressive enhancement.
//
// The server renders a normal <select data-combobox data-config="…">. This module hides it
// and puts a text input with a filtered list in front of it. The select keeps its name and
// value, so the form posts exactly what it would without JavaScript. Every change sets the
// select's value and fires a "change" event on it, so other scripts keep listening to the
// select. Matching ignores case and accents and looks at the option text (name and e-mail)
// plus an optional data-search attribute.

const normalize = (text) =>
  text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();

let counter = 0;

const enhance = (select) => {
  const texts = JSON.parse(select.dataset.config || "{}");
  const id = select.id || `combobox-${++counter}`;
  const listId = `${id}-listbox`;
  const options = Array.from(select.options).map((option, index) => ({
    option,
    index,
    text: option.text.trim(),
    search: normalize(`${option.text} ${option.dataset.search || ""}`),
  }));
  const emptyOption = options.find((o) => o.option.value === "");

  const wrapper = document.createElement("div");
  wrapper.className = "combobox";
  const input = document.createElement("input");
  input.type = "text";
  input.id = id;
  input.className = "combobox-input";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);
  input.autocomplete = "off";
  input.spellcheck = false;
  for (const name of ["aria-describedby", "aria-invalid", "required"]) {
    if (select.hasAttribute(name)) input.setAttribute(name, select.getAttribute(name));
  }
  if (emptyOption) input.placeholder = emptyOption.text;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "combobox-button";
  button.tabIndex = -1;
  button.setAttribute("aria-label", texts.showOptions || "");
  button.setAttribute("aria-controls", listId);
  const list = document.createElement("ul");
  list.id = listId;
  list.className = "combobox-list";
  list.setAttribute("role", "listbox");
  const labels = select.labels ? Array.from(select.labels) : [];
  if (labels.length) {
    labels.forEach((label, n) => {
      label.id ||= `${id}-label-${n}`;
    });
    list.setAttribute("aria-labelledby", labels.map((label) => label.id).join(" "));
  }
  list.hidden = true;
  const empty = document.createElement("p");
  empty.className = "combobox-empty";
  empty.textContent = texts.noResults || "";
  empty.hidden = true;
  const status = document.createElement("p");
  status.className = "visually-hidden";
  status.setAttribute("role", "status");

  // The label now belongs to the input; the select keeps working but is out of sight.
  select.id = `${id}-select`;
  select.hidden = true;
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  select.before(wrapper);
  wrapper.append(input, button, list, empty, status, select);

  const items = options.map((entry) => {
    const li = document.createElement("li");
    li.id = `${id}-option-${entry.index}`;
    li.setAttribute("role", "option");
    li.textContent = entry.text;
    li.className = "combobox-option";
    li.addEventListener("mousedown", (event) => {
      event.preventDefault(); // keep the focus in the input
      choose(entry);
    });
    list.append(li);
    return li;
  });

  let visible = [];
  let active = -1;

  const current = () => options[select.selectedIndex];
  const showValue = () => {
    const entry = current();
    input.value = entry && entry.option.value !== "" ? entry.text : "";
  };

  const setActive = (position) => {
    items.forEach((li) => li.classList.remove("is-active"));
    active = position;
    if (position < 0 || position >= visible.length) {
      input.removeAttribute("aria-activedescendant");
      return;
    }
    const li = items[visible[position].index];
    li.classList.add("is-active");
    input.setAttribute("aria-activedescendant", li.id);
    li.scrollIntoView({ block: "nearest" });
  };

  const filter = (query) => {
    const words = normalize(query).split(/\s+/).filter(Boolean);
    visible = options.filter((o) => words.every((word) => o.search.includes(word)));
    items.forEach((li, index) => {
      const shown = visible.some((o) => o.index === index);
      li.hidden = !shown;
      li.setAttribute("aria-selected", String(index === select.selectedIndex));
    });
    empty.hidden = visible.length > 0;
    const template = visible.length === 1 ? texts.oneResult : texts.results;
    status.textContent = visible.length
      ? (template || "").replace("%s", String(visible.length))
      : texts.noResults || "";
  };

  const open = (query) => {
    filter(query);
    list.hidden = visible.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
    wrapper.classList.add("is-open");
    const selected = visible.findIndex((o) => o.index === select.selectedIndex);
    setActive(selected >= 0 ? selected : visible.length ? 0 : -1);
  };

  const close = () => {
    list.hidden = true;
    empty.hidden = true;
    wrapper.classList.remove("is-open");
    input.setAttribute("aria-expanded", "false");
    setActive(-1);
  };

  const choose = (entry) => {
    const changed = select.selectedIndex !== entry.index;
    select.selectedIndex = entry.index;
    showValue();
    close();
    if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const isOpen = () => wrapper.classList.contains("is-open");

  input.addEventListener("input", () => open(input.value));
  input.addEventListener("click", () => {
    if (!isOpen()) open("");
  });
  input.addEventListener("keydown", (event) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!isOpen() || event.altKey) open(isOpen() ? input.value : "");
        else setActive(Math.min(active + 1, visible.length - 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!isOpen()) open("");
        else setActive(Math.max(active - 1, 0));
        break;
      case "Home":
      case "End":
        if (isOpen() && visible.length && event.ctrlKey) {
          event.preventDefault();
          setActive(event.key === "Home" ? 0 : visible.length - 1);
        }
        break;
      case "Enter":
        if (isOpen()) {
          event.preventDefault(); // choose, do not submit the form
          if (active >= 0) choose(visible[active]);
        }
        break;
      case "Escape":
        if (isOpen()) {
          event.preventDefault();
          event.stopPropagation();
          close();
          showValue();
        } else if (input.value) {
          event.preventDefault();
          input.value = "";
        }
        break;
      case "Tab":
        close();
        break;
      default:
    }
  });
  input.addEventListener("blur", () => {
    close();
    // Only a choice from the list counts; anything else typed goes back to the value.
    // An emptied field means "none" when the select has an empty option.
    if (input.value.trim() === "" && emptyOption) {
      if (select.selectedIndex !== emptyOption.index) choose(emptyOption);
      return;
    }
    showValue();
  });
  button.addEventListener("mousedown", (event) => {
    event.preventDefault();
    input.focus();
    if (isOpen()) close();
    else open("");
  });
  // Someone else (a script, the browser restoring the form) changed the select.
  select.addEventListener("change", showValue);

  showValue();
};

document.querySelectorAll("select[data-combobox]").forEach(enhance);
