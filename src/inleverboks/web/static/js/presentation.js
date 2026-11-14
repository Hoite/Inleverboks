// Countdown for presentation mode. Uses the server time from the page to correct the
// clock of the presenting computer; the server stays leading.
const root = document.querySelector(".present");
const target = document.getElementById("countdown");

if (root && target) {
  const offset = Number(root.dataset.serverNow) - Date.now();
  const start = Number(root.dataset.start);
  const end = Number(root.dataset.end);

  const format = (ms) => {
    const total = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  };

  const tick = () => {
    const now = Date.now() + offset;
    if (now < start) {
      target.textContent = `${target.dataset.before} ${format(start - now)}`;
    } else if (now < end) {
      target.textContent = format(end - now);
    } else {
      target.textContent = target.dataset.closed;
      return;
    }
    window.setTimeout(tick, 1000 - (now % 1000));
  };
  tick();
}
