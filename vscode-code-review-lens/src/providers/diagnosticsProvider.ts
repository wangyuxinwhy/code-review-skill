import * as vscode from "vscode";
import type { Check, ReviewResult, ReviewTarget } from "../types/review.js";
import type { StoreState } from "../store/reviewStore.js";
import { formatDiagnosticMessage, mapSeverity } from "../utils/formatting.js";
import { downgradeSeverity } from "../store/staleness.js";
import { resolveAnnotationLine } from "../utils/dataTransform.js";

export interface DiagnosticsResult {
  fileDiagnostics: Map<string, vscode.Diagnostic[]>;
  changesetChecks: Check[];
}

function targetToRange(entry: ReviewTarget): vscode.Range {
  const target = entry.target;
  if (target.type === "symbol" && target.lines) {
    const startLine = Math.max(0, target.lines[0] - 1);
    const endLine = Math.max(0, target.lines[1] - 1);
    return new vscode.Range(startLine, 0, endLine, Number.MAX_SAFE_INTEGER);
  }
  return new vscode.Range(0, 0, 0, 0);
}

export function buildDiagnostics(state: StoreState): DiagnosticsResult {
  const fileDiagnostics = new Map<string, vscode.Diagnostic[]>();
  const changesetChecks: Check[] = [];

  if (!state.result) {
    return { fileDiagnostics, changesetChecks };
  }

  const staleFiles = state.staleness?.staleFiles ?? new Set<string>();

  for (const entry of state.result.targets) {
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
      const message = formatDiagnosticMessage(check, symbolName);
      const displayMessage = isStale ? `(stale) ${message}` : message;

      const diagnostic = new vscode.Diagnostic(range, displayMessage, severity);
      diagnostic.source = "Code Review Lens";

      // Add related information for annotations
      if (check.annotations?.length) {
        diagnostic.relatedInformation = check.annotations.map((annotation) => {
          const annotationLine = resolveAnnotationLine(target, annotation);
          const loc = new vscode.Location(
            vscode.Uri.file(filePath),
            new vscode.Range(annotationLine, 0, annotationLine, 0),
          );
          return new vscode.DiagnosticRelatedInformation(loc, annotation.message);
        });
      }

      // Mark stale diagnostics as unnecessary
      if (isStale) {
        diagnostic.tags = [vscode.DiagnosticTag.Unnecessary];
      }

      const uri = filePath;
      if (!fileDiagnostics.has(uri)) {
        fileDiagnostics.set(uri, []);
      }
      fileDiagnostics.get(uri)!.push(diagnostic);
    }
  }

  return { fileDiagnostics, changesetChecks };
}

export function applyDiagnostics(
  collection: vscode.DiagnosticCollection,
  diagnosticsResult: DiagnosticsResult,
): void {
  collection.clear();
  for (const [filePath, diagnostics] of diagnosticsResult.fileDiagnostics) {
    collection.set(vscode.Uri.file(filePath), diagnostics);
  }
}
