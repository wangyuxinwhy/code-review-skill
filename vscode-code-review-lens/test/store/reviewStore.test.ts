import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { ReviewResult } from "../../src/types/review.js";
import { groupTargetsByFile, buildAnnotationIndex } from "../../src/utils/dataTransform.js";

const RESULT: ReviewResult = {
  version: "3",
  targets: [
    {
      target: { type: "changeset" },
      checks: [
        { id: "dep", category: "design", level: "blocking", description: "OK", pass: true, status: "passed", note: "" },
      ],
    },
    {
      target: { type: "file", file: "/ws/handler.py" },
      checks: [
        { id: "error-handling", category: "correctness", level: "advisory", description: "Swallowed", pass: false, status: "failed", note: "bare except" },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/handler.py", symbol: "handle", lines: [10, 30] },
      checks: [
        {
          id: "srp", category: "design", level: "blocking", description: "SRP fail", pass: false, status: "failed", note: "split it",
          annotations: [{ offset: 5, message: "Too many responsibilities here" }],
        },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/utils.py", symbol: "parse", lines: [1, 20] },
      checks: [
        { id: "naming", category: "readability", level: "advisory", description: "Vague", pass: false, status: "failed", note: "rename" },
      ],
    },
  ],
  summary: { blocking_failures: 1, advisory_failures: 2, passed: 1, blocked: 0 },
};

describe("groupTargetsByFile", () => {
  it("groups targets by file path", () => {
    const fileMap = groupTargetsByFile(RESULT);
    assert.equal(fileMap.size, 2);
    assert.ok(fileMap.has("/ws/handler.py"));
    assert.ok(fileMap.has("/ws/utils.py"));
  });

  it("separates file checks and symbols", () => {
    const fileMap = groupTargetsByFile(RESULT);
    const handler = fileMap.get("/ws/handler.py")!;
    assert.equal(handler.fileChecks.length, 1);
    assert.equal(handler.symbols.length, 1);
    assert.equal(handler.symbols[0].symbolName, "handle");
  });

  it("skips changeset targets", () => {
    const fileMap = groupTargetsByFile(RESULT);
    for (const [key] of fileMap) {
      assert.notEqual(key, "__changeset__");
    }
  });
});

describe("buildAnnotationIndex", () => {
  it("builds per-file per-line annotation entries", () => {
    const index = buildAnnotationIndex(RESULT);
    assert.ok(index.has("/ws/handler.py"));

    const fileIndex = index.get("/ws/handler.py")!;
    // symbol target lines[0] = 10, offset = 5 → line = 10-1+5 = 14
    assert.ok(fileIndex.has(14));

    const entries = fileIndex.get(14)!;
    assert.equal(entries.length, 1);
    assert.equal(entries[0].checkId, "srp");
    assert.equal(entries[0].message, "Too many responsibilities here");
  });

  it("skips passed checks", () => {
    const index = buildAnnotationIndex(RESULT);
    // Changeset check is passed, should not appear
    assert.ok(!index.has("__changeset__"));
  });
});
