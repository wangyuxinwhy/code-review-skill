import * as vscode from "vscode";
import type { ReviewResult } from "../types/review.js";
import type { StoreState } from "../store/reviewStore.js";
import { aggregateFindings, aggregateAnnotations } from "../utils/dataTransform.js";
import { buildHoverMessage } from "../utils/formatting.js";

export interface DecorationTypes {
  blocking: vscode.TextEditorDecorationType;
  advisory: vscode.TextEditorDecorationType;
  annotation: vscode.TextEditorDecorationType;
}

const BADGE_CSS = "none; border-radius: 3px; padding: 0.1em 0.4em;";

export function createDecorationTypes(): DecorationTypes {
  return {
    blocking: vscode.window.createTextEditorDecorationType({
      dark: {
        after: {
          margin: "0 0.5em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(166, 51, 51, 0.6)",
          color: "#FFFFFF",
        },
      },
      light: {
        after: {
          margin: "0 0.5em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(248, 215, 218, 0.8)",
          color: "#721C24",
        },
      },
    }),
    advisory: vscode.window.createTextEditorDecorationType({
      dark: {
        after: {
          margin: "0 0.5em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(180, 150, 50, 0.5)",
          color: "#FFFFFF",
        },
      },
      light: {
        after: {
          margin: "0 0.5em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(240, 220, 170, 0.8)",
          color: "#5c4d00",
        },
      },
    }),
    annotation: vscode.window.createTextEditorDecorationType({
      dark: {
        after: {
          margin: "0 0.8em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(100, 100, 100, 0.3)",
          color: "rgba(180, 180, 180, 0.8)",
        },
      },
      light: {
        after: {
          margin: "0 0.8em",
          textDecoration: BADGE_CSS,
          backgroundColor: "rgba(200, 200, 200, 0.4)",
          color: "rgba(80, 80, 80, 0.8)",
        },
      },
    }),
  };
}

export function applyDecorations(
  editor: vscode.TextEditor,
  types: DecorationTypes,
  state: StoreState | null,
): void {
  const clearAll = () => {
    editor.setDecorations(types.blocking, []);
    editor.setDecorations(types.advisory, []);
    editor.setDecorations(types.annotation, []);
  };

  if (!state?.result) {
    clearAll();
    return;
  }

  const filePath = editor.document.uri.fsPath;
  const findings = aggregateFindings(state.result, filePath);
  const annotations = aggregateAnnotations(state.result, filePath);

  if (findings.length === 0 && annotations.length === 0) {
    clearAll();
    return;
  }

  const isStale = state.staleness?.staleFiles.has(filePath) ?? false;

  const blockingDecos: vscode.DecorationOptions[] = [];
  const advisoryDecos: vscode.DecorationOptions[] = [];

  for (const finding of findings) {
    if (finding.line >= editor.document.lineCount) continue;

    const lineText = editor.document.lineAt(finding.line).text;
    const range = new vscode.Range(finding.line, lineText.length, finding.line, lineText.length);
    const hoverMessage = buildHoverMessage(finding, isStale);

    if (finding.blockingCount > 0) {
      const stalePrefix = isStale ? "(stale) " : "";
      blockingDecos.push({
        range,
        hoverMessage,
        renderOptions: {
          after: { contentText: `${stalePrefix}BLOCKING ${finding.blockingCount}` },
        },
      });
    }

    if (finding.advisoryCount > 0) {
      const stalePrefix = isStale ? "(stale) " : "";
      advisoryDecos.push({
        range,
        hoverMessage: finding.blockingCount > 0 ? undefined : hoverMessage,
        renderOptions: {
          after: { contentText: `${stalePrefix}advisory ${finding.advisoryCount}` },
        },
      });
    }
  }

  editor.setDecorations(types.blocking, blockingDecos);
  editor.setDecorations(types.advisory, advisoryDecos);

  // Line-level annotations
  const annotationDecos: vscode.DecorationOptions[] = [];

  for (const annotation of annotations) {
    if (annotation.line >= editor.document.lineCount) continue;

    const lineText = editor.document.lineAt(annotation.line).text;
    const range = new vscode.Range(annotation.line, lineText.length, annotation.line, lineText.length);
    const contentText = annotation.entries.map((entry) => entry.message).join(" \u00B7 ");

    const hoverMd = new vscode.MarkdownString();
    hoverMd.isTrusted = true;
    for (const entry of annotation.entries) {
      hoverMd.appendMarkdown(`**[${entry.checkId}]** ${entry.message}\n\n`);
    }
    hoverMd.appendMarkdown("---\n*Code Review Lens*");

    annotationDecos.push({
      range,
      hoverMessage: hoverMd,
      renderOptions: { after: { contentText } },
    });
  }

  editor.setDecorations(types.annotation, annotationDecos);
}
