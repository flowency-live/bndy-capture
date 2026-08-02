import { describe, expect, it } from "vitest";
import { firstUrl } from "./url.js";

describe("firstUrl", () => {
  it("extracts a URL from shared Facebook text", () => {
    expect(firstUrl("Fuzzy Duck https://facebook.com/profile.php?id=123"))
      .toBe("https://facebook.com/profile.php?id=123");
  });

  it("returns null for plain text", () => {
    expect(firstUrl("Fuzzy Duck")).toBeNull();
  });
});
