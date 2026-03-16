import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import type { ReviewResult } from "../../src/types/review.js";

interface StalenessInfo {
  staleFiles: Set<string>;
  totalFiles: number;
}

function computeFileHash(filePath: string): string | null {
  try {
    const data = fs.readFileSync(filePath);
    return "sha256:" + createHash("sha256").update(data).digest("hex");
  } catch {
    return null;
  }
}

function checkStaleness(result: ReviewResult): StalenessInfo {
  const staleFiles = new Set<string>();
  const allFiles = new Set<string>();

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "file" || target.type === "symbol") {
      allFiles.add(target.file);
    }
  }

  const fileHashes = result.files ?? {};
  for (const filePath of allFiles) {
    const hash = computeFileHash(filePath);
    if (!hash || !(hash in fileHashes)) {
      staleFiles.add(filePath);
    }
  }

  return { staleFiles, totalFiles: allFiles.size };
}

function makeResult(filePath: string, fileHash?: string): ReviewResult {
  const files: Record<string, { checks: [] }> = {};
  if (fileHash) {
    files[fileHash] = { checks: [] };
  }
  return {
    version: "3",
    targets: [
      { target: { type: "file", file: filePath }, checks: [] },
    ],
    summary: { blocking_failures: 0, advisory_failures: 0, passed: 0, blocked: 0 },
    files,
  };
}

describe("checkStaleness (hash-based)", () => {
  it("reports not stale when hash matches", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "stale-test-"));
    const filePath = path.join(tmpDir, "a.py");
    fs.writeFileSync(filePath, "x = 1\n");
    const hash = computeFileHash(filePath)!;

    const result = makeResult(filePath, hash);
    const info = checkStaleness(result);

    assert.equal(info.staleFiles.size, 0);
    assert.equal(info.totalFiles, 1);

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("reports stale when hash does not match", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "stale-test-"));
    const filePath = path.join(tmpDir, "a.py");
    fs.writeFileSync(filePath, "x = 1\n");

    const result = makeResult(filePath, "sha256:old_hash");
    const info = checkStaleness(result);

    assert.equal(info.staleFiles.size, 1);
    assert.ok(info.staleFiles.has(filePath));

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("reports stale when file is missing", () => {
    const result = makeResult("/nonexistent/file.py", "sha256:abc");
    const info = checkStaleness(result);
    assert.equal(info.staleFiles.size, 1);
  });

  it("reports stale when no files section", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "stale-test-"));
    const filePath = path.join(tmpDir, "a.py");
    fs.writeFileSync(filePath, "x = 1\n");

    const result = makeResult(filePath);
    delete result.files;
    const info = checkStaleness(result);

    assert.equal(info.staleFiles.size, 1);

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("deduplicates files across targets", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "stale-test-"));
    const filePath = path.join(tmpDir, "a.py");
    fs.writeFileSync(filePath, "def foo(): pass\n");
    const hash = computeFileHash(filePath)!;

    const result: ReviewResult = {
      version: "3",
      targets: [
        { target: { type: "file", file: filePath }, checks: [] },
        { target: { type: "symbol", file: filePath, symbol: "foo", lines: [1, 1] }, checks: [] },
      ],
      summary: { blocking_failures: 0, advisory_failures: 0, passed: 0, blocked: 0 },
      files: { [hash]: { checks: [] } },
    };

    const info = checkStaleness(result);
    assert.equal(info.totalFiles, 1);
    assert.equal(info.staleFiles.size, 0);

    fs.rmSync(tmpDir, { recursive: true });
  });
});

describe("computeFileHash", () => {
  it("produces sha256-prefixed hash", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hash-test-"));
    const filePath = path.join(tmpDir, "test.py");
    fs.writeFileSync(filePath, "hello\n");

    const hash = computeFileHash(filePath);
    assert.ok(hash);
    assert.ok(hash.startsWith("sha256:"));
    assert.equal(hash.length, "sha256:".length + 64);

    fs.rmSync(tmpDir, { recursive: true });
  });

  it("returns null for missing file", () => {
    assert.equal(computeFileHash("/nonexistent/file.py"), null);
  });
});
