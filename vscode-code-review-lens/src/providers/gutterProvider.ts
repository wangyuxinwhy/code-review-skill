import * as vscode from "vscode";
import * as path from "node:path";
import type { StoreState } from "../store/reviewStore.js";
import type { AnnotationEntry } from "../utils/dataTransform.js";

export interface GutterDecorationTypes {
  blocking: vscode.TextEditorDecorationType;
  advisory: vscode.TextEditorDecorationType;
  passed: vscode.TextEditorDecorationType;
}

export function createGutterDecorationTypes(extensionPath: string): GutterDecorationTypes {
  return {
    blocking: vscode.window.createTextEditorDecorationType({
      gutterIconPath: path.join(extensionPath, "media", "icons", "blocking.svg"),
      gutterIconSize: "contain",
    }),
    advisory: vscode.window.createTextEditorDecorationType({
      gutterIconPath: path.join(extensionPath, "media", "icons", "advisory.svg"),
      gutterIconSize: "contain",
    }),
    passed: vscode.window.createTextEditorDecorationType({
      gutterIconPath: path.join(extensionPath, "media", "icons", "passed.svg"),
      gutterIconSize: "contain",
    }),
  };
}

export function applyGutterDecorations(
  editor: vscode.TextEditor,
  types: GutterDecorationTypes,
  state: StoreState | null,
): void {
  const clearAll = () => {
    editor.setDecorations(types.blocking, []);
    editor.setDecorations(types.advisory, []);
    editor.setDecorations(types.passed, []);
  };

  if (!state?.result) {
    clearAll();
    return;
  }

  const filePath = editor.document.uri.fsPath;

  // Determine per-line highest severity
  const lineSeverity = new Map<number, "blocking" | "advisory" | "passed">();

  // From annotation index
  const fileAnnotations = state.annotationIndex.get(filePath);
  if (fileAnnotations) {
    for (const [line, entries] of fileAnnotations) {
      const severity = getHighestSeverity(entries);
      upgradeLine(lineSeverity, line, severity);
    }
  }

  // From targets for symbol def lines
  for (const entry of state.result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;
    if (target.file !== filePath) continue;

    if (target.type === "symbol") {
      const line = target.lines[0] - 1;
      let hasFailed = false;
      let hasBlocking = false;

      for (const check of entry.checks) {
        if (check.status === "failed") {
          hasFailed = true;
          if (check.level === "blocking") hasBlocking = true;
        }
      }

      if (hasBlocking) {
        upgradeLine(lineSeverity, line, "blocking");
      } else if (hasFailed) {
        upgradeLine(lineSeverity, line, "advisory");
      }
    }
  }

  if (lineSeverity.size === 0) {
    clearAll();
    return;
  }

  const blockingRanges: vscode.Range[] = [];
  const advisoryRanges: vscode.Range[] = [];
  const passedRanges: vscode.Range[] = [];

  for (const [line, severity] of lineSeverity) {
    if (line >= editor.document.lineCount) continue;
    const range = new vscode.Range(line, 0, line, 0);
    switch (severity) {
      case "blocking":
        blockingRanges.push(range);
        break;
      case "advisory":
        advisoryRanges.push(range);
        break;
      case "passed":
        passedRanges.push(range);
        break;
    }
  }

  editor.setDecorations(types.blocking, blockingRanges);
  editor.setDecorations(types.advisory, advisoryRanges);
  editor.setDecorations(types.passed, passedRanges);
}

function getHighestSeverity(entries: AnnotationEntry[]): "blocking" | "advisory" {
  for (const entry of entries) {
    if (entry.check.level === "blocking" && entry.check.status === "failed") {
      return "blocking";
    }
  }
  return "advisory";
}

const SEVERITY_RANK: Record<string, number> = { blocking: 2, advisory: 1, passed: 0 };

function upgradeLine(
  map: Map<number, "blocking" | "advisory" | "passed">,
  line: number,
  severity: "blocking" | "advisory" | "passed",
): void {
  const current = map.get(line);
  if (!current || SEVERITY_RANK[severity] > SEVERITY_RANK[current]) {
    map.set(line, severity);
  }
}
