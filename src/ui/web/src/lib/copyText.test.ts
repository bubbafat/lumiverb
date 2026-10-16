import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./copyText";

/** What the copy command copied: the selected textarea's text, as a browser would. */
function copyCommand(works = true) {
  const copied: string[] = [];
  const exec = vi.fn((command: string) => {
    const area = document.activeElement as HTMLTextAreaElement | null;
    if (command === "copy" && works && area instanceof HTMLTextAreaElement) copied.push(area.value);
    return works;
  });
  Object.defineProperty(document, "execCommand", { value: exec, configurable: true });
  return { copied, exec };
}

function clipboard(writeText: ((text: string) => Promise<void>) | undefined) {
  Object.defineProperty(navigator, "clipboard", {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  });
}

afterEach(() => {
  clipboard(undefined);
  document.body.innerHTML = "";
});

describe("copyText", () => {
  it("uses the Clipboard API where the page may (https, or this machine)", async () => {
    const writeText = vi.fn(async () => undefined);
    clipboard(writeText);
    const { exec } = copyCommand();
    expect(await copyText("lv_key")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("lv_key");
    expect(exec).not.toHaveBeenCalled();
  });

  it("copies with the copy command where there's no Clipboard API (plain http on the network)", async () => {
    clipboard(undefined);
    const { copied } = copyCommand();
    expect(await copyText("lv_key")).toBe(true);
    expect(copied).toEqual(["lv_key"]);
    expect(document.querySelector("textarea")).toBeNull(); // nothing left on the page
  });

  it("falls back to the copy command when the Clipboard API refuses", async () => {
    clipboard(async () => {
      throw new DOMException("Document is not focused.", "NotAllowedError");
    });
    const { copied } = copyCommand();
    expect(await copyText("lv_key")).toBe(true);
    expect(copied).toEqual(["lv_key"]);
  });

  it("says when neither could, so the caller selects it to copy by hand", async () => {
    clipboard(undefined);
    copyCommand(false);
    expect(await copyText("lv_key")).toBe(false);
  });

  it("gives focus back to what had it", async () => {
    clipboard(undefined);
    copyCommand();
    const button = document.createElement("button");
    document.body.appendChild(button);
    button.focus();
    await copyText("lv_key");
    expect(document.activeElement).toBe(button);
  });
});
