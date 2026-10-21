// A Copy button on each command, copying it as it would be typed (as on lumiverb.io).
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
