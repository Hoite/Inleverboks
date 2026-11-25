// Exam form helpers. The form works without JavaScript; the server enforces every rule.

// Without a class list every student is "not on the list", so the option to allow them
// is always on. The server enforces the same rule; this only shows it.
// The select itself: js/combobox.js gives its id to the search field in front of it.
const list = document.querySelector('select[name="class_list_id"]');
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

// Start date and time drive the end (same duration) and, on a new exam, the date in a
// suggested link name (ddmmYYYY, see services/exams.py). A link name the teacher typed is
// left alone.
const field = (id) => document.getElementById(id);
const startDate = field("starts_at_date");
const startTime = field("starts_at_time");
const endDate = field("ends_at_date");
const endTime = field("ends_at_time");
const form = document.querySelector("form[data-exam-form]");

// Mirror of timeutil.normalize_time: "9:00", "09.00", "0900" → minutes after midnight.
const minutes = (value) => {
  const match = /^([01]?\d|2[0-3])[:.]?([0-5]\d)$/.exec(value.trim());
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
};

// Wall-clock moment as a number (minutes); Date.UTC avoids daylight saving surprises.
const moment = (date, time) => {
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.value);
  const mins = minutes(time.value);
  if (!day || mins === null) return null;
  return Date.UTC(Number(day[1]), Number(day[2]) - 1, Number(day[3])) / 60000 + mins;
};

const pad = (n) => String(n).padStart(2, "0");

const write = (value, date, time) => {
  const d = new Date(value * 60000);
  date.value = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  time.value = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
};

const slugDate = (date) => {
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.value);
  return day ? `${day[3]}${day[2]}${day[1]}` : null;
};

if (form && startDate && startTime && endDate && endTime) {
  let previousStart = moment(startDate, startTime);
  const slug = field("slug");
  const suggested = form.querySelector('input[name="suggested_slug"]');
  // Auto: the link name is still the server's suggestion (or still empty).
  let slugAuto = Boolean(
    form.hasAttribute("data-new") && slug && (slug.value === "" || slug.value === suggested?.value),
  );
  slug?.addEventListener("input", () => {
    slugAuto = false;
  });

  const startChanged = () => {
    const start = moment(startDate, startTime);
    const end = moment(endDate, endTime);
    if (start === null) return;
    if (previousStart !== null && end !== null) {
      write(start + (end - previousStart), endDate, endTime);
    }
    previousStart = start;
    const day = slugDate(startDate);
    if (slugAuto && slug && day) {
      // The same rule as services/exams.redate_slug: the date at the end, without "-2".
      const next = slug.value.replace(/(?<![0-9])[0-9]{8}(?:-[0-9]+)?$/, day);
      if (next !== slug.value) {
        slug.value = next;
        if (suggested) suggested.value = next;
      }
    }
  };
  startDate.addEventListener("change", startChanged);
  startTime.addEventListener("change", startChanged);
}

// Base URL before the link name: the full one when it fits, else without https://.
const prefix = document.querySelector(".input-prefix");
const full = prefix?.querySelector(".prefix-full");
const short = prefix?.querySelector(".prefix-short");
if (prefix && full && short) {
  const fit = () => {
    full.hidden = false;
    short.hidden = true;
    if (full.scrollWidth > full.clientWidth + 1) {
      full.hidden = true;
      short.hidden = false;
    }
  };
  new ResizeObserver(fit).observe(prefix);
  fit();
}
