import * as vscode from "vscode";
import type { Check, ReviewResult, ReviewTarget } from "./types.js";
import { checkStaleness, downgradeSeverity } from "./staleness.js";

/**
 * Map check status+level to VSCode DiagnosticSeverity.
 *
 * | status + level         | Severity    |
 * |------------------------|-------------|
 * | failed + blocking      | Error       |
 * | failed + advisory      | Warning     |
 * | blocked                | Information |
 * | passed                 | (skipped)   |
 */
export function mapSeverity(check: Check): vscode.DiagnosticSeverity | null {
  if (check.status === "passed") return null;
  if (check.status === "blocked") return vscode.DiagnosticSeverity.Information;
  // status === "failed"
  if (check.level === "blocking") return vscode.DiagnosticSeverity.Error;
  return vscode.DiagnosticSeverity.Warning;
}

/**
 * Build a diagnostic message from a check and optional symbol name.
 */
export function formatMessage(check: Check, symbolName?: string): string {
  const prefix = symbolName ? `[${check.id}] ${symbolName}: ` : `[${check.id}] `;
  const lines = [`${prefix}${check.description}`];
  if (check.note) {
    lines.push("", check.note);
  }
  return lines.join("\n");
}

/**
 * Convert a ReviewTarget to a vscode.Range.
 */
export function targetToRange(entry: ReviewTarget): vscode.Range {
  const target = entry.target;
  if (target.type === "symbol" && target.lines) {
    const startLine = Math.max(0, target.lines[0] - 1);
    const endLine = Math.max(0, target.lines[1] - 1);
    return new vscode.Range(startLine, 0, endLine, Number.MAX_SAFE_INTEGER);
  }
  // File-level diagnostic — position at the top of the file
  return new vscode.Range(0, 0, 0, 0);
}

export interface DiagnosticsResult {
  /** Diagnostics grouped by file URI. */
  fileDiagnostics: Map<string, vscode.Diagnostic[]>;
  /** Targets with type "changeset" — displayed in status bar tooltip. */
  changesetChecks: Check[];
}

/**
 * Process a ReviewResult into VSCode diagnostics.
 * Handles staleness detection: stale diagnostics get "(stale)" prefix and downgraded severity.
 */
export function buildDiagnostics(result: ReviewResult): DiagnosticsResult {
  const fileDiagnostics = new Map<string, vscode.Diagnostic[]>();
  const changesetChecks: Check[] = [];
  const { staleFiles } = checkStaleness(result);

  for (const entry of result.targets) {
    const target = entry.target;

    if (target.type === "changeset") {
      changesetChecks.push(...entry.checks);
      continue;
    }

    const filePath = target.file;
    const isStale = staleFiles.has(filePath);
    const range = targetToRange(entry);
    const symbolName = target.type === "symbol" ? target.symbol : undefined;

    for (const check of entry.checks) {
      const baseSeverity = mapSeverity(check);
      if (baseSeverity === null) continue;

      const severity = isStale ? downgradeSeverity(baseSeverity) : baseSeverity;
      const message = formatMessage(check, symbolName);
      const displayMessage = isStale ? `(stale) ${message}` : message;

      const diagnostic = new vscode.Diagnostic(range, displayMessage, severity);
      diagnostic.source = "Claude Review";

      const uri = filePath;
      if (!fileDiagnostics.has(uri)) {
        fileDiagnostics.set(uri, []);
      }
      fileDiagnostics.get(uri)!.push(diagnostic);
    }
  }

  return { fileDiagnostics, changesetChecks };
}

/**
 * Push diagnostics into a DiagnosticCollection.
 * Clears all existing diagnostics first, then sets new ones grouped by file.
 */
export function applyDiagnostics(
  collection: vscode.DiagnosticCollection,
  diagnosticsResult: DiagnosticsResult,
): void {
  collection.clear();
  for (const [filePath, diagnostics] of diagnosticsResult.fileDiagnostics) {
    collection.set(vscode.Uri.file(filePath), diagnostics);
  }
}
