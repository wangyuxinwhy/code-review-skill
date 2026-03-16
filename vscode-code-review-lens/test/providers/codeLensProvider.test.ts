import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { Check, ReviewResult } from "../../src/types/review.js";

// Test the CodeLens title building logic without vscode dependency

function buildCodeLensTitle(blockingCount: number, advisoryCount: number): string {
  const parts: string[] = [];
  if (blockingCount > 0) {
    parts.push(`${blockingCount} blocking`);
  }
  if (advisoryCount > 0) {
    parts.push(`${advisoryCount} advisory`);
  }
  if (parts.length === 0) {
    return "All checks passed";
  }
  return parts.join(" \u00B7 ");
}

function getSymbolGroups(result: ReviewResult, filePath: string) {
  const groups: { symbolName: string; line: number; blockingCount: number; advisoryCount: number; checks: Check[] }[] = [];

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;
    if (target.file !== filePath) continue;

    if (target.type === "symbol") {
      let blockingCount = 0;
      let advisoryCount = 0;
      const nonPassed: Check[] = [];

      for (const check of entry.checks) {
        if (check.status === "passed") continue;
        nonPassed.push(check);
        if (check.status === "failed" && check.level === "blocking") blockingCount++;
        else if (check.status === "failed" && check.level === "advisory") advisoryCount++;
      }

      if (nonPassed.length > 0) {
        groups.push({
          symbolName: target.symbol,
          line: target.lines[0] - 1,
          blockingCount,
          advisoryCount,
          checks: nonPassed,
        });
      }
    }
  }

  return groups;
}

const RESULT: ReviewResult = {
  version: "3",
  targets: [
    {
      target: { type: "symbol", file: "/ws/main.py", symbol: "handle", lines: [10, 30] },
      checks: [
        { id: "srp", category: "design", level: "blocking", description: "SRP fail", pass: false, status: "failed", note: "" },
        { id: "naming", category: "readability", level: "advisory", description: "Names", pass: false, status: "failed", note: "" },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/main.py", symbol: "parse", lines: [35, 45] },
      checks: [
        { id: "type-ann", category: "correctness", level: "advisory", description: "OK", pass: true, status: "passed", note: "" },
      ],
    },
  ],
  summary: { blocking_failures: 1, advisory_failures: 1, passed: 1, blocked: 0 },
};

describe("buildCodeLensTitle", () => {
  it("shows blocking and advisory counts", () => {
    assert.equal(buildCodeLensTitle(2, 3), "2 blocking \u00B7 3 advisory");
  });

  it("shows only blocking when no advisory", () => {
    assert.equal(buildCodeLensTitle(1, 0), "1 blocking");
  });

  it("shows only advisory when no blocking", () => {
    assert.equal(buildCodeLensTitle(0, 2), "2 advisory");
  });

  it("shows all passed when both zero", () => {
    assert.equal(buildCodeLensTitle(0, 0), "All checks passed");
  });
});

describe("getSymbolGroups", () => {
  it("returns groups with non-passed checks", () => {
    const groups = getSymbolGroups(RESULT, "/ws/main.py");
    assert.equal(groups.length, 1); // parse has only passed, excluded
    assert.equal(groups[0].symbolName, "handle");
    assert.equal(groups[0].blockingCount, 1);
    assert.equal(groups[0].advisoryCount, 1);
  });

  it("returns empty for unrelated files", () => {
    const groups = getSymbolGroups(RESULT, "/ws/other.py");
    assert.equal(groups.length, 0);
  });
});
