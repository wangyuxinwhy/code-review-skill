import * as vscode from "vscode";
import type { StoreState } from "../store/reviewStore.js";

export class StatusBarProvider implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.item.command = "codeReviewLens.statusBarMenu";
  }

  refresh(state: StoreState): void {
    if (!state.result) {
      this.item.hide();
      return;
    }

    const summary = state.result.summary;
    const parts: string[] = [];
    if (summary.blocking_failures > 0) parts.push(`${summary.blocking_failures}\u274C`);
    if (summary.advisory_failures > 0) parts.push(`${summary.advisory_failures}\u26A0\uFE0F`);
    parts.push(`${summary.passed}\u2705`);

    this.item.text = `$(checklist) Review: ${parts.join(" ")}`;

    // Build tooltip
    const tooltipLines: string[] = [];

    if (state.changesetChecks.length > 0) {
      tooltipLines.push("Changeset checks:");
      for (const check of state.changesetChecks) {
        const icon = check.status === "passed" ? "\u2705" : "\u274C";
        tooltipLines.push(`  ${icon} [${check.id}] ${check.description}`);
      }
      tooltipLines.push("");
    }

    if (state.staleness && state.staleness.staleFiles.size > 0) {
      tooltipLines.push(
        `\u26A0\uFE0F ${state.staleness.staleFiles.size}/${state.staleness.totalFiles} files modified since review`,
      );
    }

    tooltipLines.push("", "Click for options");

    this.item.tooltip = tooltipLines.join("\n");
    this.item.show();
  }

  hide(): void {
    this.item.hide();
  }

  dispose(): void {
    this.item.dispose();
  }
}
