import * as vscode from "vscode";
import { ResultLoader } from "./resultLoader.js";
import {
  applyDecorations,
  createDecorationTypes,
  type DecorationTypes,
} from "./decorationsProvider.js";
import type { Check, ReviewResult } from "./types.js";
import { checkStaleness } from "./staleness.js";

let loader: ResultLoader | undefined;
let statusBarItem: vscode.StatusBarItem | undefined;
let decorationTypes: DecorationTypes | undefined;
let currentResult: ReviewResult | null = null;

export function activate(context: vscode.ExtensionContext): void {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!workspaceRoot) return;

  decorationTypes = createDecorationTypes();
  context.subscriptions.push(decorationTypes.blocking, decorationTypes.advisory, decorationTypes.annotation);

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusBarItem.command = "claudeReview.openResult";
  context.subscriptions.push(statusBarItem);

  loader = new ResultLoader(workspaceRoot);
  context.subscriptions.push(loader);

  // Wire: result changes → update decorations + status bar
  loader.onResultChanged((result) => {
    currentResult = result;
    if (result) {
      refreshAllEditors();
      updateStatusBar(result);
    } else {
      clearAll();
    }
  });

  // Update decorations when visible editors change (e.g. opening a new tab)
  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => refreshAllEditors()),
  );

  // Re-check staleness on file save
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(() => loader?.load()),
  );

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand("claudeReview.refresh", () => loader?.load()),
    vscode.commands.registerCommand("claudeReview.clear", () => clearAll()),
    vscode.commands.registerCommand("claudeReview.openResult", () => {
      if (loader) {
        vscode.window.showTextDocument(vscode.Uri.file(loader.resultPath));
      }
    }),
  );

  // Initial load
  loader.load();
}

function refreshAllEditors(): void {
  if (!decorationTypes) return;
  for (const editor of vscode.window.visibleTextEditors) {
    applyDecorations(editor, decorationTypes, currentResult);
  }
}

function clearAll(): void {
  currentResult = null;
  if (decorationTypes) {
    for (const editor of vscode.window.visibleTextEditors) {
      editor.setDecorations(decorationTypes.blocking, []);
      editor.setDecorations(decorationTypes.advisory, []);
      editor.setDecorations(decorationTypes.annotation, []);
    }
  }
  statusBarItem?.hide();
}

function updateStatusBar(result: ReviewResult): void {
  if (!statusBarItem) return;

  const summary = result.summary;
  const parts: string[] = [];
  if (summary.blocking_failures > 0) parts.push(`${summary.blocking_failures}\u274C`);
  if (summary.advisory_failures > 0) parts.push(`${summary.advisory_failures}\u26A0\uFE0F`);
  parts.push(`${summary.passed}\u2705`);

  statusBarItem.text = `Claude Review: ${parts.join(" ")}`;

  // Collect changeset checks for tooltip
  const changesetChecks: Check[] = [];
  for (const entry of result.targets) {
    if (entry.target.type === "changeset") {
      changesetChecks.push(...entry.checks);
    }
  }

  // Build tooltip
  const tooltipLines: string[] = [];
  if (changesetChecks.length > 0) {
    tooltipLines.push("Changeset checks:");
    for (const check of changesetChecks) {
      const icon = check.status === "passed" ? "\u2705" : "\u274C";
      tooltipLines.push(`  ${icon} [${check.id}] ${check.description}`);
    }
    tooltipLines.push("");
  }

  const { staleFiles, totalFiles } = checkStaleness(result);
  if (staleFiles.size > 0) {
    tooltipLines.push(`\u26A0\uFE0F ${staleFiles.size}/${totalFiles} files modified since review`);
  }

  statusBarItem.tooltip = tooltipLines.join("\n");
  statusBarItem.show();
}

export function deactivate(): void {
  // Cleanup handled by context.subscriptions
}
