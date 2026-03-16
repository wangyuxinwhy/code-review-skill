import * as vscode from "vscode";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ReviewResult } from "./types.js";

const RESULT_REL_PATH = ".claude/review/cache.json";

export class ResultLoader implements vscode.Disposable {
  private readonly _onResultChanged = new vscode.EventEmitter<ReviewResult | null>();
  readonly onResultChanged = this._onResultChanged.event;

  private watcher: vscode.FileSystemWatcher | undefined;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(private readonly workspaceRoot: string) {
    this.disposables.push(this._onResultChanged);
    this.setupWatcher();
  }

  get resultPath(): string {
    return path.join(this.workspaceRoot, RESULT_REL_PATH);
  }

  private setupWatcher(): void {
    const pattern = new vscode.RelativePattern(this.workspaceRoot, RESULT_REL_PATH);
    this.watcher = vscode.workspace.createFileSystemWatcher(pattern);

    this.watcher.onDidChange(() => this.load(), undefined, this.disposables);
    this.watcher.onDidCreate(() => this.load(), undefined, this.disposables);
    this.watcher.onDidDelete(() => this._onResultChanged.fire(null), undefined, this.disposables);

    this.disposables.push(this.watcher);
  }

  load(): ReviewResult | null {
    const result = readResultFile(this.resultPath, this.workspaceRoot);
    this._onResultChanged.fire(result);
    return result;
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}

/**
 * Read and parse cache.json v3, resolving relative file paths to absolute.
 * Returns null if the file doesn't exist, is malformed, or is not v3.
 */
export function readResultFile(filePath: string, workspaceRoot: string): ReviewResult | null {
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

  if (!isReviewResult(parsed)) {
    return null;
  }

  resolveTargetPaths(parsed, workspaceRoot);
  return parsed;
}

function isReviewResult(value: unknown): value is ReviewResult {
  if (typeof value !== "object" || value === null) return false;
  const obj = value as Record<string, unknown>;
  return (
    obj.version === "3" &&
    Array.isArray(obj.targets) &&
    typeof obj.summary === "object"
  );
}

/**
 * Resolve relative file paths in targets to absolute paths rooted at workspaceRoot.
 */
function resolveTargetPaths(result: ReviewResult, workspaceRoot: string): void {
  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "file" || target.type === "symbol") {
      if (!path.isAbsolute(target.file)) {
        target.file = path.join(workspaceRoot, target.file);
      }
    }
  }
}
