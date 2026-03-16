import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as vscode from "vscode";
import type { ReviewResult } from "./types.js";

export interface StalenessInfo {
  /** Set of absolute file paths whose content hash is missing from cache. */
  staleFiles: Set<string>;
  /** Total number of unique files in the review. */
  totalFiles: number;
}

/**
 * Compare each reviewed file's content hash against the cache's files section.
 * A file is stale if its current hash is not present in result.files.
 *
 * Uses raw bytes (Buffer) for hashing — matches Python's path.read_bytes().
 */
export function checkStaleness(result: ReviewResult): StalenessInfo {
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

function computeFileHash(filePath: string): string | null {
  try {
    const data = fs.readFileSync(filePath); // raw bytes Buffer
    return "sha256:" + createHash("sha256").update(data).digest("hex");
  } catch {
    return null;
  }
}

/**
 * Downgrade a diagnostic severity by one level for stale results.
 */
export function downgradeSeverity(severity: vscode.DiagnosticSeverity): vscode.DiagnosticSeverity {
  switch (severity) {
    case vscode.DiagnosticSeverity.Error:
      return vscode.DiagnosticSeverity.Warning;
    case vscode.DiagnosticSeverity.Warning:
      return vscode.DiagnosticSeverity.Information;
    case vscode.DiagnosticSeverity.Information:
      return vscode.DiagnosticSeverity.Hint;
    case vscode.DiagnosticSeverity.Hint:
      return vscode.DiagnosticSeverity.Hint;
  }
}
