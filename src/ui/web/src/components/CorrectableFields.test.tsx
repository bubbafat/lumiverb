import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CorrectableTags, CorrectableText } from "./CorrectableFields";

afterEach(cleanup);

describe("CorrectableText", () => {
  it("shows the value, and nothing to edit for someone who can't", () => {
    render(
      <CorrectableText label="Description" value="a cat on a sofa" corrected={false} canEdit={false}
        emptyText="No description yet" onSave={vi.fn()} />,
    );
    expect(screen.getByText("a cat on a sofa")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit description" })).toBeNull();
  });

  it("lets an editor correct it", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <CorrectableText label="Description" value="a cat on a sofa" corrected={false} canEdit
        emptyText="No description yet" onSave={onSave} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    const box = screen.getByLabelText("Description") as HTMLTextAreaElement;
    expect(box.value).toBe("a cat on a sofa");
    fireEvent.change(box, { target: { value: "Mittens on Grandma's sofa" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Mittens on Grandma's sofa"));
    await waitFor(() => expect(screen.queryByLabelText("Description")).toBeNull());
  });

  it("says a person wrote it, with no machine copy and no going back to one", () => {
    // Robert, Oct 9: edits keep only the latest.
    render(
      <CorrectableText label="Description" value="Mittens" corrected canEdit
        emptyText="No description yet" onSave={vi.fn()} />,
    );
    expect(screen.getByText("Edited")).toBeTruthy();
    expect(screen.queryByText(/The AI's/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Use the AI's/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit description" })).toBeTruthy();
  });

  it("says when a save fails, and keeps what was typed", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Editor access required"));
    render(
      <CorrectableText label="Description" value="a cat" corrected={false} canEdit
        emptyText="No description yet" onSave={onSave} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Mittens" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Couldn't save");
    expect(alert.textContent).toContain("Editor access required");
    expect((screen.getByLabelText("Description") as HTMLTextAreaElement).value).toBe("Mittens");

    // Cancelling drops the message with the edit.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Mittens" } });

    // Saved on a second try: the message goes.
    onSave.mockResolvedValue(undefined);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    await waitFor(() => expect(screen.queryByLabelText("Description")).toBeNull());
  });

  it("cancels without saving", () => {
    const onSave = vi.fn();
    render(<CorrectableText label="Text in Image" value="EXIT" corrected={false} canEdit emptyText="No text found" onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit text in image" }));
    fireEvent.change(screen.getByLabelText("Text in Image"), { target: { value: "EXIT 9" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("EXIT")).toBeTruthy();
  });
});

describe("CorrectableTags", () => {
  const renderTag = (tag: string) => <span key={tag}>{tag}</span>;

  it("lets an editor remove the AI's tags and add their own", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<CorrectableTags tags={["dog", "beach", "ocean"]} corrected={false} canEdit onSave={onSave} renderTag={renderTag} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove ocean" }));
    const input = screen.getByLabelText("Add a tag");
    fireEvent.change(input, { target: { value: " Rex " } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "dog" } }); // already there
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(["dog", "beach", "Rex"]));
  });

  it("saves a tag typed but not yet entered", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<CorrectableTags tags={["dog"]} corrected={false} canEdit onSave={onSave} renderTag={renderTag} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.change(screen.getByLabelText("Add a tag"), { target: { value: "Rex" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(["dog", "Rex"]));
  });

  it("says when saving tags fails, and keeps the edits", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Server error"));
    render(<CorrectableTags tags={["dog", "ocean"]} corrected={false} canEdit onSave={onSave} renderTag={renderTag} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove ocean" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Server error");
    expect(screen.queryByRole("button", { name: "Remove ocean" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove dog" })).toBeTruthy();
  });

  it("says a person's tags are theirs, with no machine list beside them", () => {
    render(<CorrectableTags tags={["dog", "Rex"]} corrected canEdit onSave={vi.fn()} renderTag={renderTag} />);
    expect(screen.getByText("Edited")).toBeTruthy();
    expect(screen.queryByText(/The AI's/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Use the AI's/ })).toBeNull();
  });

  it("says when there are none", () => {
    render(<CorrectableTags tags={[]} corrected={false} canEdit={false} onSave={vi.fn()} renderTag={renderTag} />);
    expect(screen.getByText("No tags yet")).toBeTruthy();
  });
});
