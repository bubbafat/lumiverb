import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, deleteTranscript: vi.fn(), uploadTranscript: vi.fn() };
});

import { ApiError, deleteTranscript, uploadTranscript } from "../api/client";
import { NoTranscript, TranscriptActions } from "./TranscriptActions";

const remove = vi.mocked(deleteTranscript);
const upload = vi.mocked(uploadTranscript);

beforeEach(() => {
  remove.mockReset();
  upload.mockReset();
});
afterEach(cleanup);

function actions(props: Partial<Parameters<typeof TranscriptActions>[0]> = {}) {
  const onChanged = vi.fn();
  render(
    <TranscriptActions assetId="ast_1" source="whisper" machineUnderneath={false} canEdit
      onDownload={vi.fn()} onChanged={onChanged} {...props} />,
  );
  return onChanged;
}

describe("TranscriptActions", () => {
  it("removes the machine's, saying it's the machine's", async () => {
    remove.mockResolvedValue(undefined);
    const onChanged = actions();
    fireEvent.click(screen.getByRole("button", { name: "Remove the machine's transcript" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("ast_1", "machine"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("removing a person's says the machine's comes back", async () => {
    remove.mockResolvedValue(undefined);
    actions({ source: "manual", machineUnderneath: true });
    const button = screen.getByRole("button", { name: "Remove your transcript and use the machine's" });
    expect(button.textContent).toBe("Use the machine's");
    fireEvent.click(button);
    await waitFor(() => expect(remove).toHaveBeenCalledWith("ast_1", "manual"));
  });

  it("a person's alone is just removed", () => {
    actions({ source: "manual" });
    expect(screen.getByRole("button", { name: "Remove your transcript" }).textContent).toBe("Remove");
  });

  it("can't be clicked twice while removing", async () => {
    let finish: () => void = () => {};
    remove.mockReturnValue(new Promise<void>((resolve) => (finish = resolve)));
    actions({ source: "manual", machineUnderneath: true });
    const button = screen.getByRole("button", { name: /Remove your transcript/ });
    fireEvent.click(button);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(remove).toHaveBeenCalledTimes(1);
    finish();
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  });

  it("when it changed meanwhile, says nothing was removed and shows the new one", async () => {
    remove.mockRejectedValue(new ApiError(409, "The clip now shows the machine's transcript", "transcript_changed"));
    const onChanged = actions({ source: "manual" });
    fireEvent.click(screen.getByRole("button", { name: "Remove your transcript" }));
    expect((await screen.findByRole("alert")).textContent).toContain("nothing was removed");
    expect(onChanged).toHaveBeenCalled();
  });

  it("says when removing fails", async () => {
    remove.mockRejectedValue(new ApiError(403, "Editor access required"));
    const onChanged = actions();
    fireEvent.click(screen.getByRole("button", { name: "Remove the machine's transcript" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Editor access required");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("offers only Download to someone who can't edit", () => {
    actions({ canEdit: false });
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(screen.queryByLabelText("Replace")).toBeNull();
  });

  it("says when replacing fails", async () => {
    upload.mockRejectedValue(new ApiError(400, "Invalid SRT format"));
    actions();
    const file = new File(["not srt"], "x.srt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Replace"), { target: { files: [file] } });
    expect((await screen.findByRole("alert")).textContent).toContain("Invalid SRT format");
  });
});

describe("NoTranscript", () => {
  it("lets an editor upload one", async () => {
    upload.mockResolvedValue(undefined as never);
    const onChanged = vi.fn();
    render(<NoTranscript assetId="ast_1" canEdit onChanged={onChanged} />);
    const file = new File(["1\n00:00:00,000 --> 00:00:01,000\nhi\n"], "x.srt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Upload SRT"), { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith("ast_1", expect.stringContaining("hi")));
    expect(onChanged).toHaveBeenCalled();
  });

  it("just says there's none to someone who can't edit", () => {
    render(<NoTranscript assetId="ast_1" canEdit={false} onChanged={vi.fn()} />);
    expect(screen.getByText("No transcript")).toBeTruthy();
    expect(screen.queryByLabelText("Upload SRT")).toBeNull();
  });
});
