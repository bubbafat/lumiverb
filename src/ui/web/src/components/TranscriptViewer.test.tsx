import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import TranscriptViewer from "./TranscriptViewer";

const SRT = "1\n00:00:05,000 --> 00:00:07,000\nHello there\n\n2\n01:02:03,500 --> 01:02:05,000\nMuch later\n";

afterEach(cleanup);

describe("TranscriptViewer", () => {
  it("jumps the video to a line's time", () => {
    const onSeek = vi.fn();
    render(<TranscriptViewer srt={SRT} onSeek={onSeek} />);
    fireEvent.click(screen.getByRole("button", { name: /Much later/ }));
    expect(onSeek).toHaveBeenCalledWith(3723.5);
    fireEvent.click(screen.getByRole("button", { name: /Hello there/ }));
    expect(onSeek).toHaveBeenLastCalledWith(5);
  });

  it("only lines inside the cap jump", () => {
    const onSeek = vi.fn();
    render(<TranscriptViewer srt={SRT} onSeek={onSeek} seekableUntil={60} />);
    screen.getByRole("button", { name: /Hello there/ });
    expect(screen.queryByRole("button", { name: /Much later/ })).toBeNull();
    screen.getByText("Much later");
  });

  it("is plain text when nothing plays", () => {
    render(<TranscriptViewer srt={SRT} />);
    screen.getByText("Hello there");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
