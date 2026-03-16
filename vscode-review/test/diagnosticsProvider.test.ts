import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { DiagnosticSeverity, Range } from "./mock-vscode.js";
import type { Check, ReviewResult, ReviewTarget } from "../src/types.js";

// Since diagnosticsProvider.ts imports vscode, we replicate the pure logic here for testing.
// This tests the mapping functions in isolation.

function mapSeverity(check: Check): DiagnosticSeverity | null {
  if (check.status === "passed") return null;
  if (check.status === "blocked") return DiagnosticSeverity.Information;
  if (check.level === "blocking") return DiagnosticSeverity.Error;
  return DiagnosticSeverity.Warning;
}

function formatMessage(check: Check, symbolName?: string): string {
  const prefix = symbolName ? `[${check.id}] ${symbolName}: ` : `[${check.id}] `;
  const lines = [`${prefix}${check.description}`];
  if (check.note) {
    lines.push("", check.note);
  }
  return lines.join("\n");
}

function targetToRange(entry: ReviewTarget): Range {
  const target = entry.target;
  if (target.type === "symbol" && target.lines) {
    const startLine = Math.max(0, target.lines[0] - 1);
    const endLine = Math.max(0, target.lines[1] - 1);
    return new Range(startLine, 0, endLine, Number.MAX_SAFE_INTEGER);
  }
  return new Range(0, 0, 0, 0);
}

function downgradeSeverity(severity: DiagnosticSeverity): DiagnosticSeverity {
  switch (severity) {
    case DiagnosticSeverity.Error:
      return DiagnosticSeverity.Warning;
    case DiagnosticSeverity.Warning:
      return DiagnosticSeverity.Information;
    case DiagnosticSeverity.Information:
      return DiagnosticSeverity.Hint;
    case DiagnosticSeverity.Hint:
      return DiagnosticSeverity.Hint;
  }
}

const failedBlocking: Check = {
  id: "srp",
  category: "design",
  level: "blocking",
  description: "SRP violated",
  pass: false,
  status: "failed",
  note: "too many concerns",
};

const failedAdvisory: Check = {
  id: "naming",
  category: "readability",
  level: "advisory",
  description: "Names unclear",
  pass: false,
  status: "failed",
  note: "vague names",
};

const blocked: Check = {
  id: "param-breadth",
  category: "design",
  level: "advisory",
  description: "Params too broad",
  pass: null,
  status: "blocked",
  note: "Blocked by SRP",
};

const passed: Check = {
  id: "type-annotations",
  category: "correctness",
  level: "advisory",
  description: "Fully annotated",
  pass: true,
  status: "passed",
  note: "",
};

describe("mapSeverity", () => {
  it("maps failed+blocking to Error", () => {
    assert.equal(mapSeverity(failedBlocking), DiagnosticSeverity.Error);
  });

  it("maps failed+advisory to Warning", () => {
    assert.equal(mapSeverity(failedAdvisory), DiagnosticSeverity.Warning);
  });

  it("maps blocked to Information", () => {
    assert.equal(mapSeverity(blocked), DiagnosticSeverity.Information);
  });

  it("returns null for passed", () => {
    assert.equal(mapSeverity(passed), null);
  });
});

describe("formatMessage", () => {
  it("formats message without symbol name", () => {
    const msg = formatMessage(failedBlocking);
    assert.equal(msg, "[srp] SRP violated\n\ntoo many concerns");
  });

  it("formats message with symbol name", () => {
    const msg = formatMessage(failedBlocking, "handle_request");
    assert.equal(msg, "[srp] handle_request: SRP violated\n\ntoo many concerns");
  });

  it("omits note section when note is empty", () => {
    const msg = formatMessage(passed);
    assert.equal(msg, "[type-annotations] Fully annotated");
  });
});

describe("targetToRange", () => {
  it("converts symbol target to 0-indexed range", () => {
    const entry: ReviewTarget = {
      target: { type: "symbol", file: "f.py", symbol: "foo", lines: [42, 95] },
      checks: [],
    };
    const range = targetToRange(entry);
    assert.equal(range.startLine, 41);
    assert.equal(range.endLine, 94);
    assert.equal(range.startCharacter, 0);
  });

  it("returns file-level range for file targets", () => {
    const entry: ReviewTarget = {
      target: { type: "file", file: "f.py" },
      checks: [],
    };
    const range = targetToRange(entry);
    assert.equal(range.startLine, 0);
    assert.equal(range.endLine, 0);
  });
});

describe("downgradeSeverity", () => {
  it("downgrades Error to Warning", () => {
    assert.equal(downgradeSeverity(DiagnosticSeverity.Error), DiagnosticSeverity.Warning);
  });

  it("downgrades Warning to Information", () => {
    assert.equal(downgradeSeverity(DiagnosticSeverity.Warning), DiagnosticSeverity.Information);
  });

  it("downgrades Information to Hint", () => {
    assert.equal(downgradeSeverity(DiagnosticSeverity.Information), DiagnosticSeverity.Hint);
  });

  it("keeps Hint as Hint", () => {
    assert.equal(downgradeSeverity(DiagnosticSeverity.Hint), DiagnosticSeverity.Hint);
  });
});
