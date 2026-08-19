import { describe, expect, it } from "vitest";
import { renderRefs } from "./renderRefs";

describe("renderRefs", () => {
  it("returns a single text segment for plain text", () => {
    expect(renderRefs("hello world")).toEqual([{ type: "text", value: "hello world" }]);
  });

  it("returns an empty array for an empty string", () => {
    expect(renderRefs("")).toEqual([]);
  });

  it("splits a single ref out of surrounding text", () => {
    expect(renderRefs("Move [[ref:apple_1]] now")).toEqual([
      { type: "text", value: "Move " },
      { type: "ref", name: "apple_1" },
      { type: "text", value: " now" },
    ]);
  });

  it("handles multiple tags and preserves newlines", () => {
    const content = "For robot0:\n1. Move [[ref:apple_1]] to [[ref:fridge]]\n2. Open [[ref:fridge]]";
    expect(renderRefs(content)).toEqual([
      { type: "text", value: "For robot0:\n1. Move " },
      { type: "ref", name: "apple_1" },
      { type: "text", value: " to " },
      { type: "ref", name: "fridge" },
      { type: "text", value: "\n2. Open " },
      { type: "ref", name: "fridge" },
    ]);
  });

  it("trims whitespace inside the ref name", () => {
    expect(renderRefs("[[ref: apple_1 ]]")).toEqual([{ type: "ref", name: "apple_1" }]);
  });

  it("treats an unclosed tag as plain text", () => {
    const content = "Move [[ref:apple_1 to the fridge";
    expect(renderRefs(content)).toEqual([{ type: "text", value: content }]);
  });

  it("treats a malformed (empty name) tag as plain text", () => {
    const content = "nothing here [[ref:]] at all";
    expect(renderRefs(content)).toEqual([{ type: "text", value: content }]);
  });

  it("handles a ref at the very start and end of the string", () => {
    expect(renderRefs("[[ref:a]] middle [[ref:b]]")).toEqual([
      { type: "ref", name: "a" },
      { type: "text", value: " middle " },
      { type: "ref", name: "b" },
    ]);
  });

  it("handles back-to-back refs with no text between them", () => {
    expect(renderRefs("[[ref:a]][[ref:b]]")).toEqual([
      { type: "ref", name: "a" },
      { type: "ref", name: "b" },
    ]);
  });

  it("parses the action-id anchor after a pipe", () => {
    expect(renderRefs("Move [[ref:mug_1|move_mug_1_sink]] to [[ref:sink|move_mug_1_sink]]")).toEqual([
      { type: "text", value: "Move " },
      { type: "ref", name: "mug_1", actionId: "move_mug_1_sink" },
      { type: "text", value: " to " },
      { type: "ref", name: "sink", actionId: "move_mug_1_sink" },
    ]);
  });

  it("distinguishes the same name anchored to different actions", () => {
    expect(renderRefs("[[ref:sink|move_mug_1_sink]] vs [[ref:sink|move_mug_2_sink]]")).toEqual([
      { type: "ref", name: "sink", actionId: "move_mug_1_sink" },
      { type: "text", value: " vs " },
      { type: "ref", name: "sink", actionId: "move_mug_2_sink" },
    ]);
  });

  it("omits actionId for a plain tag and for an empty id after the pipe", () => {
    expect(renderRefs("[[ref:sink]] and [[ref:sink|]]")).toEqual([
      { type: "ref", name: "sink" },
      { type: "text", value: " and " },
      { type: "ref", name: "sink" },
    ]);
  });

  it("trims whitespace around both name and id", () => {
    expect(renderRefs("[[ref: sink | move_mug_1_sink ]]")).toEqual([
      { type: "ref", name: "sink", actionId: "move_mug_1_sink" },
    ]);
  });

  it("treats a pipe-only payload (empty name) as plain text", () => {
    const content = "broken [[ref:|move_mug_1_sink]] tag";
    expect(renderRefs(content)).toEqual([{ type: "text", value: content }]);
  });
});
