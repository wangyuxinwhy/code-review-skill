import * as vscode from "vscode";
import type { Check, ReviewResult } from "../types/review.js";
import type { StoreState } from "../store/reviewStore.js";
import { buildCodeLensTitle } from "../utils/formatting.js";

interface SymbolCheckGroup {
  symbolName: string;
  line: number;
  checks: Check[];
  blockingCount: number;
  advisoryCount: number;
}

export class ReviewCodeLensProvider implements vscode.CodeLensProvider {
  private readonly _onDidChangeCodeLenses = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this._onDidChangeCodeLenses.event;

  private state: StoreState | null = null;

  refresh(state: StoreState): void {
    this.state = state;
    this._onDidChangeCodeLenses.fire();
  }

  clear(): void {
    this.state = null;
    this._onDidChangeCodeLenses.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!this.state?.result) return [];

    const filePath = document.uri.fsPath;
    const groups = this.getSymbolGroups(this.state.result, filePath);

    if (groups.length === 0) return [];

    const lenses: vscode.CodeLens[] = [];

    for (const group of groups) {
      const line = Math.max(0, Math.min(group.line, document.lineCount - 1));
      const range = new vscode.Range(line, 0, line, 0);
      const title = buildCodeLensTitle(group.blockingCount, group.advisoryCount);

      const lens = new vscode.CodeLens(range, {
        title,
        command: "codeReviewLens.showChecksQuickPick",
        arguments: [group.checks, group.symbolName],
      });
      lenses.push(lens);
    }

    return lenses;
  }

  private getSymbolGroups(result: ReviewResult, filePath: string): SymbolCheckGroup[] {
    const groups: SymbolCheckGroup[] = [];
    const fileGroupChecks: Check[] = [];

    for (const entry of result.targets) {
      const target = entry.target;
      if (target.type === "changeset") continue;
      if (target.file !== filePath) continue;

      if (target.type === "symbol") {
        let blockingCount = 0;
        let advisoryCount = 0;
        const nonPassed: Check[] = [];

        for (const check of entry.checks) {
          if (check.status === "passed") continue;
          nonPassed.push(check);
          if (check.status === "failed" && check.level === "blocking") blockingCount++;
          else if (check.status === "failed" && check.level === "advisory") advisoryCount++;
        }

        if (nonPassed.length > 0) {
          groups.push({
            symbolName: target.symbol,
            line: target.lines[0] - 1,
            checks: nonPassed,
            blockingCount,
            advisoryCount,
          });
        }
      } else {
        for (const check of entry.checks) {
          if (check.status !== "passed") {
            fileGroupChecks.push(check);
          }
        }
      }
    }

    // File-level checks as CodeLens at line 0
    if (fileGroupChecks.length > 0) {
      let blockingCount = 0;
      let advisoryCount = 0;
      for (const check of fileGroupChecks) {
        if (check.status === "failed" && check.level === "blocking") blockingCount++;
        else if (check.status === "failed" && check.level === "advisory") advisoryCount++;
      }
      groups.unshift({
        symbolName: "(file)",
        line: 0,
        checks: fileGroupChecks,
        blockingCount,
        advisoryCount,
      });
    }

    return groups;
  }

  dispose(): void {
    this._onDidChangeCodeLenses.dispose();
  }
}
