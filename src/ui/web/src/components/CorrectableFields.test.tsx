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

  it("says it was edited, shows the AI's, and can go back to it", () => {
    const onSave = vi.fn();
    render(
      <CorrectableText label="Description" value="Mittens" machine="a cat" corrected canEdit
        emptyText="No description yet" onSave={onSave} />,
    );
    expect(screen.getByText("Edited")).toBeTruthy();
    expect(screen.getByText("a cat")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use the AI's description" }));
    expect(onSave).toHaveBeenCalledWith(null);
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

  it("says when going back to the AI's fails", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Network down"));
    render(
      <CorrectableText label="Text in Image" value="EXIT 9" machine="EXIT" corrected canEdit
        emptyText="No text found" onSave={onSave} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Use the AI's text in image" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Network down");
    expect(screen.getByText("EXIT 9")).toBeTruthy();
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

  it("shows the AI's under edited tags and can go back to them", () => {
    const onSave = vi.fn();
    render(<CorrectableTags tags={["dog", "Rex"]} machineTags={["dog", "ocean"]} corrected canEdit onSave={onSave} renderTag={renderTag} />);
    expect(screen.getByText("Edited")).toBeTruthy();
    expect(screen.getByText("The AI's: dog, ocean")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use the AI's tags" }));
    expect(onSave).toHaveBeenCalledWith(null);
  });

  it("says when there are none", () => {
    render(<CorrectableTags tags={[]} corrected={false} canEdit={false} onSave={vi.fn()} renderTag={renderTag} />);
    expect(screen.getByText("No tags yet")).toBeTruthy();
  });
});
