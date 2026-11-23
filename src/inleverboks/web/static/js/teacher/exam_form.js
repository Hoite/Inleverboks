// Exam form: without a class list every student is "not on the list", so the option to
// allow them is always on. The server enforces the same rule; this only shows it.
const list = document.getElementById("class_list_id");
const allow = document.getElementById("allow_unlisted");

if (list && allow) {
  const sync = () => {
    const none = list.value === "";
    if (none) allow.checked = true;
    allow.disabled = none;
  };
  list.addEventListener("change", sync);
  sync();
}
