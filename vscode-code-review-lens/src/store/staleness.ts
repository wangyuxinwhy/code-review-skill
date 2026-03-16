import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as vscode from "vscode";
import type { ReviewResult } from "../types/review.js";

export interface StalenessInfo {
  staleFiles: Set<string>;
  totalFiles: number;
}

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
    const data = fs.readFileSync(filePath);
    return "sha256:" + createHash("sha256").update(data).digest("hex");
  } catch {
    return null;
  }
}

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
