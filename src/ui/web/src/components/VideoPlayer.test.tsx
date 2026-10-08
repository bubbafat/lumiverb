import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import VideoPlayer from "./VideoPlayer";

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function frame() {
  return screen.getByTestId("video-frame");
}

describe("VideoPlayer", () => {
  it("frames the short preview in amber and says so", () => {
    render(<VideoPlayer src="/v1/stream/a" source="preview" maxSeconds={null} />);
    expect(frame().dataset.playback).toBe("preview");
    expect(frame().className).toMatch(/amber/);
    const note = screen.getByText(/Short preview: first 10 seconds/);
    expect(note.className).toMatch(/amber/);
  });

  it("frames the whole video in green and labels it", () => {
    render(<VideoPlayer src="/v1/stream/b" source="analysis_proxy" maxSeconds={null} />);
    expect(frame().dataset.playback).toBe("full");
    expect(frame().className).toMatch(/emerald/);
    expect(screen.getByText("Full video").className).toMatch(/emerald/);
  });

  it("names the cap when the account has one", () => {
    render(<VideoPlayer src="/v1/stream/c" source="analysis_proxy" maxSeconds={30} />);
    expect(frame().className).toMatch(/emerald/);
    screen.getByText("Plays the first 30 seconds");
  });

  it("a cap under 10 seconds shortens the preview's note", () => {
    render(<VideoPlayer src="/v1/stream/d" source="preview" maxSeconds={5} />);
    screen.getByText(/first 5 seconds/);
    expect(screen.queryByText(/once it's processed/)).toBeNull();
  });

  it("carries on from the same moment when the whole video arrives", () => {
    const { rerender, container } = render(<VideoPlayer src="/v1/stream/short" source="preview" maxSeconds={null} />);
    const video = container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "currentTime", { value: 6.5, writable: true, configurable: true });
    fireEvent.play(video);
    fireEvent.timeUpdate(video);

    rerender(<VideoPlayer src="/v1/stream/full" source="analysis_proxy" maxSeconds={null} />);
    expect(video.getAttribute("src")).toBe("/v1/stream/full");
    // Loading a new source resets the position to 0 and says so.
    Object.defineProperty(video, "currentTime", { value: 0, writable: true, configurable: true });
    fireEvent.timeUpdate(video);
    Object.defineProperty(video, "duration", { value: 25, configurable: true });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(6.5);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
    expect(frame().dataset.playback).toBe("full");
  });

  it("doesn't start playing a paused video when it switches", () => {
    const { rerender, container } = render(<VideoPlayer src="/v1/stream/short" source="preview" maxSeconds={null} />);
    const video = container.querySelector("video") as HTMLVideoElement;
    Object.defineProperty(video, "currentTime", { value: 3, writable: true, configurable: true });
    fireEvent.timeUpdate(video);
    rerender(<VideoPlayer src="/v1/stream/full" source="analysis_proxy" maxSeconds={null} />);
    Object.defineProperty(video, "duration", { value: 25, configurable: true });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(3);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
});
