import { describe, expect, it } from "vitest";
import { slugify } from "./NewProject";

describe("slugify", () => {
  // Must match surplus.projects.repo.slugify, which decides the real project id.
  it("matches the backend's slug rules", () => {
    expect(slugify("My Cool Idea")).toBe("my-cool-idea");
    expect(slugify("  Ünïcode & stuff!! ")).toBe("n-code-stuff");
    expect(slugify("***")).toBe("project");
    expect(slugify("a".repeat(70)).length).toBe(63);
  });
});
