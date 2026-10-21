// Loaded in <head>, not deferred: the class is set before the page is drawn, so with script the install
// section shows as tabs from the start, and without it both ways to install are shown.
document.documentElement.classList.add("js");

document.addEventListener("DOMContentLoaded", () => {
  copyButtons();
  installTabs();
});

// A Copy button on each command, copying it as it would be typed.
function copyButtons() {
  for (const cmd of document.querySelectorAll(".cmd")) {
    const code = cmd.querySelector("code");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy this command");
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent.trim());
        button.textContent = "Copied";
      } catch {
        // No clipboard here (plain HTTP, or refused): select it, for the person to copy.
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = "Selected";
      }
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1800);
    });
    cmd.appendChild(button);
  }
}

// Mac or Linux: the one this browser is on first (a Linux desktop's; anything else, the Mac's).
function installTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  if (!tabs.length) return;
  const show = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
  };
  const ua = navigator.userAgent;
  const linux = /Linux/.test(ua) && !/Android/.test(ua);
  show(linux ? document.getElementById("tab-linux") : tabs[0], false);
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => show(tab, false));
    tab.addEventListener("keydown", (e) => {
      const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (step) {
        e.preventDefault();
        show(tabs[(i + step + tabs.length) % tabs.length], true);
      }
    });
  });
}
