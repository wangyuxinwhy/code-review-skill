import type { Annotation, Check, ReviewResult, ReviewTarget, TargetDescriptor } from "../types/review.js";

export interface SymbolInfo {
  symbolName: string;
  lines: [number, number];
  checks: Check[];
}

export interface FileGroup {
  fileChecks: Check[];
  symbols: SymbolInfo[];
  target?: ReviewTarget;
}

export interface AnnotationEntry {
  checkId: string;
  message: string;
  check: Check;
}

export interface LineFinding {
  line: number;
  blockingCount: number;
  advisoryCount: number;
  checks: { check: Check; symbolName?: string }[];
}

export interface AnnotationFinding {
  line: number;
  entries: { checkId: string; message: string }[];
}

/**
 * Resolve an annotation's offset to a 0-indexed absolute line number.
 */
export function resolveAnnotationLine(target: TargetDescriptor, annotation: Annotation): number {
  if (target.type === "symbol") {
    return target.lines[0] - 1 + annotation.offset;
  }
  return annotation.offset;
}

/**
 * Group targets by file path. Returns a map of filePath → { fileChecks, symbols }.
 */
export function groupTargetsByFile(result: ReviewResult): Map<string, FileGroup> {
  const fileMap = new Map<string, FileGroup>();

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;

    const filePath = target.file;
    if (!fileMap.has(filePath)) {
      fileMap.set(filePath, { fileChecks: [], symbols: [] });
    }
    const group = fileMap.get(filePath)!;

    if (target.type === "file") {
      group.fileChecks.push(...entry.checks);
      group.target = entry;
    } else if (target.type === "symbol") {
      group.symbols.push({
        symbolName: target.symbol,
        lines: target.lines,
        checks: entry.checks,
      });
    }
  }

  return fileMap;
}

/**
 * Build a per-file, per-line annotation index.
 * Returns Map<filePath, Map<lineNumber(0-indexed), AnnotationEntry[]>>
 */
export function buildAnnotationIndex(
  result: ReviewResult,
): Map<string, Map<number, AnnotationEntry[]>> {
  const index = new Map<string, Map<number, AnnotationEntry[]>>();

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;

    const filePath = target.file;

    for (const check of entry.checks) {
      if (check.status === "passed" || !check.annotations) continue;

      for (const annotation of check.annotations) {
        const line = resolveAnnotationLine(target, annotation);

        if (!index.has(filePath)) {
          index.set(filePath, new Map());
        }
        const fileIndex = index.get(filePath)!;
        if (!fileIndex.has(line)) {
          fileIndex.set(line, []);
        }
        fileIndex.get(line)!.push({
          checkId: check.id,
          message: annotation.message,
          check,
        });
      }
    }
  }

  return index;
}

/**
 * Collect symbol line ranges for a given file.
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
 */
export function aggregateFindings(result: ReviewResult, filePath: string): LineFinding[] {
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
      for (const check of entry.checks) {
        if (check.status === "passed") continue;

        let attributed = false;
        if (check.annotations?.length) {
          const symbolHits = new Set<number>();
          for (const annotation of check.annotations) {
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

/**
 * Collect structured annotations from non-passing checks for a given file.
 */
export function aggregateAnnotations(result: ReviewResult, filePath: string): AnnotationFinding[] {
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
