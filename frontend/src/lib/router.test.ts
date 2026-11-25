import { describe, expect, it } from "vitest";
import { match } from "./router";

describe("match", () => {
  it("matches static and param segments", () => {
    expect(match("/projects", "/projects")).toEqual({});
    expect(match("/projects/:id/:tab?", "/projects/my-idea")).toEqual({ id: "my-idea" });
    expect(match("/projects/:id/:tab?", "/projects/my-idea/backlog")).toEqual({ id: "my-idea", tab: "backlog" });
    expect(match("/projects/new", "/projects/my-idea")).toBeNull();
    expect(match("/projects", "/projects/x/y")).toBeNull();
    expect(match("/", "/")).toEqual({});
  });
});
