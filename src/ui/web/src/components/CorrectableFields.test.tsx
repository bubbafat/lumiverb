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
    fireEvent.click(screen.getByRole("button", { name: "Use the AI's" }));
    expect(onSave).toHaveBeenCalledWith(null);
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

  it("shows the AI's under edited tags and can go back to them", () => {
    const onSave = vi.fn();
    render(<CorrectableTags tags={["dog", "Rex"]} machineTags={["dog", "ocean"]} corrected canEdit onSave={onSave} renderTag={renderTag} />);
    expect(screen.getByText("Edited")).toBeTruthy();
    expect(screen.getByText("The AI's: dog, ocean")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use the AI's" }));
    expect(onSave).toHaveBeenCalledWith(null);
  });

  it("says when there are none", () => {
    render(<CorrectableTags tags={[]} corrected={false} canEdit={false} onSave={vi.fn()} renderTag={renderTag} />);
    expect(screen.getByText("No tags yet")).toBeTruthy();
  });
});
