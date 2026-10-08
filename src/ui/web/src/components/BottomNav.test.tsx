import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { BottomNav } from "./BottomNav";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/libraries/:libraryId/*" element={<BottomNav />} />
        <Route path="*" element={<BottomNav />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("BottomNav", () => {
  it("links to projects, since the sidebar is hidden on phones", () => {
    renderAt("/");
    expect(screen.getByRole("link", { name: "Projects" }).getAttribute("href")).toBe("/projects");
  });

  it("marks Projects active on a project's page", () => {
    renderAt("/projects/prj_1");
    expect(screen.getByRole("link", { name: "Projects" }).className).toContain("text-indigo-400");
  });

  it("keeps Browse and Settings for the open library", () => {
    renderAt("/libraries/lib_1/browse");
    expect(screen.getByRole("link", { name: "Browse" }).getAttribute("href")).toBe("/libraries/lib_1/browse");
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/libraries/lib_1/settings");
    expect(screen.getByRole("link", { name: "Projects" }).className).not.toContain("text-indigo-400");
  });
});
