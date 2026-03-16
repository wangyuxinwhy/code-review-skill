import * as vscode from "vscode";
import type { Annotation, Check, ReviewResult, TargetDescriptor } from "./types.js";
import { checkStaleness } from "./staleness.js";

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

interface LineFinding {
  line: number;
  blockingCount: number;
  advisoryCount: number;
  checks: { check: Check; symbolName?: string }[];
}

/**
 * Resolve an annotation's offset to a 0-indexed absolute line number.
 *
 * Symbol target: absoluteLine = target.lines[0] - 1 + offset
 * File target:   absoluteLine = offset
 */
export function resolveAnnotationLine(target: TargetDescriptor, annotation: Annotation): number {
  if (target.type === "symbol") {
    return target.lines[0] - 1 + annotation.offset;
  }
  // File target: offset is already 0-indexed from file start
  return annotation.offset;
}

/**
 * Collect symbol line ranges for a given file.
 * Used to attribute file-level checks to symbols when their annotations
 * fall within a symbol's body.
 */
function collectSymbolRanges(
  result: ReviewResult,
  filePath: string,
): { defLine: number; start: number; end: number }[] {
  const ranges: { defLine: number; start: number; end: number }[] = [];
  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "symbol" && target.file === filePath) {
      ranges.push({ defLine: target.lines[0] - 1, start: target.lines[0], end: target.lines[1] });
    }
  }
  return ranges;
}

/**
 * Find which symbol (def line, 0-indexed) a 1-indexed annotation line belongs to.
 * Returns -1 if no symbol contains it.
 */
function findOwningSymbol(
  annotationLine: number,
  symbolRanges: { defLine: number; start: number; end: number }[],
): number {
  for (const range of symbolRanges) {
    if (annotationLine >= range.start && annotationLine <= range.end) {
      return range.defLine;
    }
  }
  return -1;
}

function addToFinding(lineMap: Map<number, LineFinding>, line: number, check: Check): void {
  if (!lineMap.has(line)) {
    lineMap.set(line, { line, blockingCount: 0, advisoryCount: 0, checks: [] });
  }
  const finding = lineMap.get(line)!;

  if (check.status === "failed" && check.level === "blocking") {
    finding.blockingCount++;
  } else if (check.status === "failed" && check.level === "advisory") {
    finding.advisoryCount++;
  }

  finding.checks.push({ check });
}

/**
 * Aggregate non-passing checks per symbol def line for a given file.
 *
 * File-level checks with annotations inside a symbol's body are attributed
 * to that symbol's badge. This way the badge count matches the number of
 * distinct failing checks the developer sees within the function.
 */
function aggregateFindings(result: ReviewResult, filePath: string): LineFinding[] {
  const lineMap = new Map<number, LineFinding>();
  const symbolRanges = collectSymbolRanges(result, filePath);

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;
    if (target.file !== filePath) continue;

    if (target.type === "symbol") {
      const line = target.lines[0] - 1;
      for (const check of entry.checks) {
        if (check.status === "passed") continue;
        addToFinding(lineMap, line, check);
      }
    } else {
      // File-level checks: attribute to a symbol if annotations fall inside it,
      // otherwise badge at line 0.
      for (const check of entry.checks) {
        if (check.status === "passed") continue;

        let attributed = false;
        if (check.annotations?.length) {
          // Group annotations by which symbol they fall in
          const symbolHits = new Set<number>();
          for (const annotation of check.annotations) {
            // Convert offset to 1-indexed line for findOwningSymbol
            const absoluteLine = annotation.offset + 1;
            const ownerLine = findOwningSymbol(absoluteLine, symbolRanges);
            if (ownerLine >= 0) {
              symbolHits.add(ownerLine);
            }
          }
          if (symbolHits.size > 0) {
            for (const defLine of symbolHits) {
              addToFinding(lineMap, defLine, check);
            }
            attributed = true;
          }
        }

        if (!attributed) {
          addToFinding(lineMap, 0, check);
        }
      }
    }
  }

  return [...lineMap.values()].filter(
    (finding) => finding.blockingCount > 0 || finding.advisoryCount > 0 || finding.checks.length > 0,
  );
}

interface AnnotationFinding {
  line: number;
  entries: { checkId: string; message: string }[];
}

/**
 * Collect structured annotations from non-passing checks for a given file.
 * Groups annotations by resolved line number for rendering.
 */
function aggregateAnnotations(result: ReviewResult, filePath: string): AnnotationFinding[] {
  const lineMap = new Map<number, AnnotationFinding>();

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;
    if (target.file !== filePath) continue;

    for (const check of entry.checks) {
      if (check.status !== "failed" || !check.annotations) continue;

      for (const annotation of check.annotations) {
        const line = resolveAnnotationLine(target, annotation);
        if (!lineMap.has(line)) {
          lineMap.set(line, { line, entries: [] });
        }
        lineMap.get(line)!.entries.push({ checkId: check.id, message: annotation.message });
      }
    }
  }

  return [...lineMap.values()];
}

function buildHoverMessage(finding: LineFinding, isStale: boolean): vscode.MarkdownString {
  const md = new vscode.MarkdownString();
  md.isTrusted = true;

  if (isStale) {
    md.appendMarkdown("*(stale — file modified since review)*\n\n---\n\n");
  }

  for (const { check } of finding.checks) {
    const tag =
      check.status === "failed"
        ? check.level === "blocking"
          ? "BLOCKING"
          : "advisory"
        : "blocked";
    md.appendMarkdown(`**[${check.id}]** ${check.description} \`${tag}\`\n\n`);
    if (check.note) {
      md.appendMarkdown(`${check.note}\n\n`);
    }
  }

  md.appendMarkdown("---\n*Claude Review*");
  return md;
}

export function applyDecorations(
  editor: vscode.TextEditor,
  types: DecorationTypes,
  result: ReviewResult | null,
): void {
  const clearAll = () => {
    editor.setDecorations(types.blocking, []);
    editor.setDecorations(types.advisory, []);
    editor.setDecorations(types.annotation, []);
  };

  if (!result) {
    clearAll();
    return;
  }

  const filePath = editor.document.uri.fsPath;
  const findings = aggregateFindings(result, filePath);
  const annotations = aggregateAnnotations(result, filePath);

  if (findings.length === 0 && annotations.length === 0) {
    clearAll();
    return;
  }

  const { staleFiles } = checkStaleness(result);
  const isStale = staleFiles.has(filePath);

  const blockingDecos: vscode.DecorationOptions[] = [];
  const advisoryDecos: vscode.DecorationOptions[] = [];

  for (const finding of findings) {
    if (finding.line >= editor.document.lineCount) continue;

    const lineText = editor.document.lineAt(finding.line).text;
    const range = new vscode.Range(finding.line, lineText.length, finding.line, lineText.length);
    const hoverMessage = buildHoverMessage(finding, isStale);
    if (finding.blockingCount > 0) {
      blockingDecos.push({
        range,
        hoverMessage,
        renderOptions: {
          after: { contentText: `r ${finding.blockingCount}` },
        },
      });
    }

    if (finding.advisoryCount > 0) {
      advisoryDecos.push({
        range,
        // Avoid duplicate hover when both badges are on the same line
        hoverMessage: finding.blockingCount > 0 ? undefined : hoverMessage,
        renderOptions: {
          after: { contentText: `r ${finding.advisoryCount}` },
        },
      });
    }
  }

  editor.setDecorations(types.blocking, blockingDecos);
  editor.setDecorations(types.advisory, advisoryDecos);

  // --- Line-level annotations ---
  const annotationDecos: vscode.DecorationOptions[] = [];

  for (const annotation of annotations) {
    if (annotation.line >= editor.document.lineCount) continue;

    const lineText = editor.document.lineAt(annotation.line).text;
    const range = new vscode.Range(annotation.line, lineText.length, annotation.line, lineText.length);
    const contentText = annotation.entries.map((entry) => entry.message).join(" · ");

    const hoverMd = new vscode.MarkdownString();
    hoverMd.isTrusted = true;
    for (const entry of annotation.entries) {
      hoverMd.appendMarkdown(`**[${entry.checkId}]** ${entry.message}\n\n`);
    }
    hoverMd.appendMarkdown("---\n*Claude Review*");

    annotationDecos.push({
      range,
      hoverMessage: hoverMd,
      renderOptions: { after: { contentText } },
    });
  }

  editor.setDecorations(types.annotation, annotationDecos);
}
