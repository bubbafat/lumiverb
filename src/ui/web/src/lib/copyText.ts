/**
 * Copy text to the clipboard from a button.
 *
 * The Clipboard API is only there for a page over https or on this machine
 * (localhost). A Mac's Lumiverb opened from another device, or by its
 * network name (http://<mac>.local:6464), is neither, so the browser's
 * older copy command does it instead: from a click, every browser still
 * allows that. False only when neither could; the caller then selects the
 * text for copying by hand (never a dialog to copy from).
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Refused (no permission, the page not focused): the copy command below.
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "0";
  area.style.opacity = "0";
  const focused = document.activeElement as HTMLElement | null;
  document.body.appendChild(area);
  area.focus();
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    focused?.focus?.();
  }
}

/** Select an element's text, for copying by hand when copyText couldn't. */
export function selectText(element: HTMLElement | null): void {
  if (!element) return;
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}
