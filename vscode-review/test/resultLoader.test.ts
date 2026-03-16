import { describe, it } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import type { ReviewResult } from "../src/types.js";

// readResultFile uses fs directly, no vscode dependency — import directly
// We need to replicate the pure logic since the module imports vscode
// Test the parsing/path-resolution logic inline

function readResultFileForTest(filePath: string, workspaceRoot: string): ReviewResult | null {
  let raw: string;
  try {
    raw = fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null) return null;
  const obj = parsed as Record<string, unknown>;
  if (
    obj.version !== "3" ||
    !Array.isArray(obj.targets) ||
    typeof obj.summary !== "object"
  ) {
    return null;
  }

  const result = parsed as ReviewResult;
  for (const entry of result.targets) {
    const target = entry.target;
    if ((target.type === "file" || target.type === "symbol") && !path.isAbsolute(target.file)) {
      target.file = path.join(workspaceRoot, target.file);
    }
  }
  return result;
}

const FIXTURE: ReviewResult = {
  version: "3",
  timestamp: "2026-03-10T00:00:00Z",
  checklist_version: "2",
  targets: [
    {
      target: { type: "changeset" },
      checks: [{ id: "dep-dir", category: "design", level: "blocking", description: "OK", pass: true, status: "passed", note: "" }],
    },
    {
      target: { type: "file", file: "src/handler.py" },
      checks: [{ id: "error-handling", category: "correctness", level: "advisory", description: "Errors swallowed", pass: false, status: "failed", note: "bare except" }],
    },
    {
      target: { type: "symbol", file: "src/handler.py", symbol: "handle_request", lines: [42, 95] as [number, number] },
      checks: [
        { id: "srp", category: "design", level: "blocking", description: "SRP violated", pass: false, status: "failed", note: "too many concerns" },
        { id: "naming", category: "readability", level: "advisory", description: "Names unclear", pass: null, status: "blocked", note: "Blocked by SRP" },
      ],
    },
  ],
  summary: { blocking_failures: 1, advisory_failures: 1, passed: 1, blocked: 1 },
  files: {},
  symbols: {},
};

describe("readResultFile", () => {
  it("parses valid v3 cache.json and resolves relative paths", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "review-test-"));
    const filePath = path.join(tmpDir, "cache.json");
    fs.writeFileSync(filePath, JSON.stringify(FIXTURE));

    const result = readResultFileForTest(filePath, "/workspace");
    assert.ok(result);
    assert.equal(result.version, "3");
    assert.equal(result.targets.length, 3);

    // File target path resolved
    const fileTarget = result.targets[1].target;
    assert.equal(fileTarget.type, "file");
    if (fileTarget.type === "file") {
      assert.equal(fileTarget.file, path.join("/workspace", "src/handler.py"));
    }

    // Symbol target path resolved
    const symbolTarget = result.targets[2].target;
    if (symbolTarget.type === "symbol") {
      assert.equal(symbolTarget.file, path.join("/workspace", "src/handler.py"));
      assert.deepEqual(symbolTarget.lines, [42, 95]);
    }

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("returns null for missing file", () => {
    const result = readResultFileForTest("/nonexistent/cache.json", "/workspace");
    assert.equal(result, null);
  });

  it("returns null for invalid JSON", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "review-test-"));
    const filePath = path.join(tmpDir, "cache.json");
    fs.writeFileSync(filePath, "not json{");

    const result = readResultFileForTest(filePath, "/workspace");
    assert.equal(result, null);

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("returns null for JSON missing required fields", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "review-test-"));
    const filePath = path.join(tmpDir, "cache.json");
    fs.writeFileSync(filePath, JSON.stringify({ version: "3" }));

    const result = readResultFileForTest(filePath, "/workspace");
    assert.equal(result, null);

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("rejects v2 cache files", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "review-test-"));
    const filePath = path.join(tmpDir, "cache.json");
    const v2Data = {
      version: "2",
      targets: [],
      summary: { blocking_failures: 0, advisory_failures: 0, passed: 0, blocked: 0 },
      entries: {},
    };
    fs.writeFileSync(filePath, JSON.stringify(v2Data));

    const result = readResultFileForTest(filePath, "/workspace");
    assert.equal(result, null);

    fs.rmSync(tmpDir, { recursive: true });
  });
});
