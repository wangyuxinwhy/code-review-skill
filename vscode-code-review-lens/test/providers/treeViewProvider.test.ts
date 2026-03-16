import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { ReviewResult } from "../../src/types/review.js";
import type { StoreState, FilterState } from "../../src/store/reviewStore.js";
import type { StalenessInfo } from "../../src/store/staleness.js";
import { groupTargetsByFile, buildAnnotationIndex } from "../../src/utils/dataTransform.js";

// Test the tree hierarchy logic without vscode dependency
// We test the data flow that feeds the tree provider

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
      target: { type: "symbol", file: "/ws/main.py", symbol: "parse", lines: [10, 20] },
      checks: [
        { id: "srp", category: "design", level: "blocking", description: "SRP violated", pass: false, status: "failed", note: "" },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/utils.py", symbol: "handle", lines: [42, 95] },
      checks: [
        { id: "naming", category: "readability", level: "advisory", description: "Names unclear", pass: false, status: "failed", note: "" },
      ],
    },
  ],
  summary: { blocking_failures: 1, advisory_failures: 1, passed: 1, blocked: 0 },
};

function makeState(result: ReviewResult): StoreState {
  return {
    result,
    fileMap: groupTargetsByFile(result),
    changesetChecks: result.targets
      .filter((t) => t.target.type === "changeset")
      .flatMap((t) => t.checks),
    annotationIndex: buildAnnotationIndex(result),
    staleness: { staleFiles: new Set(), totalFiles: 0 },
    filter: { severity: new Set(["blocking", "advisory", "passed"]), category: new Set() },
  };
}

describe("tree hierarchy data", () => {
  it("has correct summary counts", () => {
    const state = makeState(RESULT);
    assert.equal(state.result!.summary.blocking_failures, 1);
    assert.equal(state.result!.summary.advisory_failures, 1);
    assert.equal(state.result!.summary.passed, 1);
  });

  it("groups targets by file correctly", () => {
    const state = makeState(RESULT);
    assert.equal(state.fileMap.size, 2);
    assert.ok(state.fileMap.has("/ws/main.py"));
    assert.ok(state.fileMap.has("/ws/utils.py"));
  });

  it("identifies changeset checks", () => {
    const state = makeState(RESULT);
    assert.equal(state.changesetChecks.length, 1);
    assert.equal(state.changesetChecks[0].id, "dep");
  });

  it("filter severity can narrow to blocking only", () => {
    const state = makeState(RESULT);
    state.filter.severity = new Set(["blocking"]);

    // Verify filtering logic
    let blockingCount = 0;
    for (const entry of state.result!.targets) {
      for (const check of entry.checks) {
        if (check.status === "failed" && check.level === "blocking" && state.filter.severity.has("blocking")) {
          blockingCount++;
        }
      }
    }
    assert.equal(blockingCount, 1);
  });
});
