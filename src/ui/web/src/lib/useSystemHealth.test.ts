import { describe, expect, it } from "vitest";
import type { SystemHealth } from "../api/client";
import { healthAlert } from "./useSystemHealth";

const at = (state: SystemHealth["state"]): SystemHealth => ({ state, rows: [], libraries: [] });

describe("healthAlert", () => {
  it("is the overall state unless it's green", () => {
    expect(healthAlert(at("green"), false)).toBeNull();
    expect(healthAlert(at("yellow"), false)).toBe("yellow");
    expect(healthAlert(at("red"), false)).toBe("red");
  });

  it("is red when the API doesn't answer, and nothing before it has", () => {
    expect(healthAlert(at("green"), true)).toBe("red");
    expect(healthAlert(undefined, false)).toBeNull();
  });
});
