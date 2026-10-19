import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AdminLogsPage from "./AdminLogsPage";
import type { ServiceLog } from "../api/client";

const fetchMock = vi.fn();
let urls: string[] = [];
let answer: (url: URL) => Response;

const FIRST: ServiceLog = {
  source: "file",
  where: "/opt/homebrew/var/log/lumiverb.log",
  unavailable: null,
  entries: [
    { id: "1:300", part: "scheduler", level: "error", at: null,
      text: "2026-10-11 21:54:00,000 ERROR src.x: faces failed\nTraceback (most recent call last):\nValueError: boom" },
    { id: "1:200", part: "api", level: "info", at: null, text: "2026-10-11 21:53:23,440 INFO src.y: Backup started" },
  ],
  next_cursor: "1:200",
};
const OLDER: ServiceLog = {
  ...FIRST,
  entries: [{ id: "1:0", part: "lumiverb-server", level: "info", at: null, text: "postgres started (pid 1)" }],
  next_cursor: null,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  urls = [];
  answer = (url) => json(url.searchParams.get("after") ? OLDER : FIRST);
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => {
    urls.push(url);
    return answer(new URL(url, "http://x"));
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminLogsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastQuery = () => new URL(urls[urls.length - 1], "http://x").searchParams;

describe("AdminLogsPage", () => {
  it("shows the log newest first, each record said by which part, a traceback with it", async () => {
    renderPage();
    const items = await screen.findAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringMatching(/^Processing.*faces failed.*ValueError: boom$/s),
      expect.stringMatching(/^API.*Backup started$/),
    ]);
    expect(screen.getByText("/opt/homebrew/var/log/lumiverb.log. Newest first.")).toBeTruthy();
  });

  it("offers the parts a Mac's log has; Linux's journal has its own", async () => {
    renderPage();
    const parts = await screen.findByRole("group", { name: "Part" });
    await waitFor(() => expect(parts.textContent).toBe("AllAPIProcessingPlug-insDatabaseWeb serverService"));
    cleanup();
    answer = () => json({ ...FIRST, source: "journal", where: "the journal (lumiverb-api.service)" });
    renderPage();
    const journal = await screen.findByRole("group", { name: "Part" });
    await waitFor(() => expect(journal.textContent).toBe("AllAPIProcessingPlug-insUpkeep"));
  });

  it("asks for one part, problems only, and the text searched", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Processing" }));
    await waitFor(() => expect(lastQuery().get("part")).toBe("scheduler"));
    fireEvent.click(screen.getByLabelText("Problems only"));
    await waitFor(() => expect(lastQuery().get("problems")).toBe("true"));
    fireEvent.change(screen.getByLabelText("Search the log"), { target: { value: " boom " } });
    await waitFor(() => expect(lastQuery().get("q")).toBe("boom"));
    expect(lastQuery().get("part")).toBe("scheduler");
  });

  it("looks further back from where the page ended", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Look further back" }));
    expect(await screen.findByText("postgres started (pid 1)")).toBeTruthy();
    expect(lastQuery().get("after")).toBe("1:200");
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Look further back" })).toBeNull();
  });

  it("says when a search found nothing in the stretch it looked through", async () => {
    answer = () => json({ ...FIRST, entries: [] });
    renderPage();
    expect(await screen.findByText("Nothing found in the stretch looked through.")).toBeTruthy();
  });

  it("says why there's no log here", async () => {
    answer = () => json({ source: null, where: null, unavailable: "Lumiverb was started from a terminal: its log is there.",
                          entries: [], next_cursor: null });
    renderPage();
    expect(await screen.findByText("Lumiverb was started from a terminal: its log is there.")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Part" })).toBeNull();
  });

  it("says a server with more than one account shows it only on its machine", async () => {
    const message = "This server has more than one account and its log names every account's files: it's shown only on the machine Lumiverb runs on.";
    answer = () => json({ error: { code: "logs_elsewhere", message, details: {} } }, 403);
    renderPage();
    expect(await screen.findByText(message)).toBeTruthy();
  });

  it("downloads the lines shown", async () => {
    let saved: Blob | null = null;
    const made = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = (blob: Blob) => { saved = blob; return "blob:x"; };
    URL.revokeObjectURL = () => {};
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    try {
      renderPage();
      await screen.findAllByRole("listitem");
      fireEvent.click(screen.getByRole("button", { name: "Download these lines" }));
    } finally {
      URL.createObjectURL = made.create;
      URL.revokeObjectURL = made.revoke;
    }
    expect(click).toHaveBeenCalled();
    expect(await (saved as Blob | null)!.text()).toBe(
      "scheduler | 2026-10-11 21:54:00,000 ERROR src.x: faces failed\nTraceback (most recent call last):\nValueError: boom\n"
      + "api | 2026-10-11 21:53:23,440 INFO src.y: Backup started\n");
  });
});
