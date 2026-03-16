import * as vscode from "vscode";
import * as path from "node:path";
import type { ReviewTreeNode, SeverityGroupNode, FileNode, SymbolNode, CheckNode } from "../types/tree.js";
import type { Check } from "../types/review.js";
import type { StoreState } from "../store/reviewStore.js";
import { getCheckIcon } from "../utils/formatting.js";

export class ReviewTreeViewProvider implements vscode.TreeDataProvider<ReviewTreeNode> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<ReviewTreeNode | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private state: StoreState | null = null;

  refresh(state: StoreState): void {
    this.state = state;
    this._onDidChangeTreeData.fire();
  }

  clear(): void {
    this.state = null;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: ReviewTreeNode): vscode.TreeItem {
    switch (element.type) {
      case "severityGroup":
        return this.buildSeverityGroupItem(element);
      case "file":
        return this.buildFileItem(element);
      case "symbol":
        return this.buildSymbolItem(element);
      case "check":
        return this.buildCheckItem(element);
    }
  }

  getChildren(element?: ReviewTreeNode): ReviewTreeNode[] {
    if (!this.state?.result) return [];

    if (!element) {
      return this.getRootChildren();
    }

    switch (element.type) {
      case "severityGroup":
        return this.getSeverityGroupChildren(element);
      case "file":
        return this.getFileChildren(element);
      case "symbol":
        return this.getSymbolChildren(element);
      case "check":
        return [];
    }
  }

  private getRootChildren(): ReviewTreeNode[] {
    if (!this.state?.result) return [];
    const { result, filter } = this.state;

    const groups: SeverityGroupNode[] = [];

    if (filter.severity.has("blocking")) {
      const count = result.summary.blocking_failures;
      if (count > 0) {
        groups.push({ type: "severityGroup", label: `Blocking Issues (${count})`, severity: "blocking", count });
      }
    }
    if (filter.severity.has("advisory")) {
      const count = result.summary.advisory_failures;
      if (count > 0) {
        groups.push({ type: "severityGroup", label: `Advisory Issues (${count})`, severity: "advisory", count });
      }
    }
    if (filter.severity.has("passed")) {
      const count = result.summary.passed;
      if (count > 0) {
        groups.push({ type: "severityGroup", label: `Passed (${count})`, severity: "passed", count });
      }
    }

    return groups;
  }

  private getSeverityGroupChildren(group: SeverityGroupNode): ReviewTreeNode[] {
    if (!this.state?.result) return [];
    const { result, filter } = this.state;

    // Group checks by file, include changeset checks
    const fileNodes = new Map<string, FileNode>();
    const changesetChecks: CheckNode[] = [];

    for (const entry of result.targets) {
      const target = entry.target;

      for (const check of entry.checks) {
        if (!this.matchesSeverity(check, group.severity)) continue;
        if (filter.category.size > 0 && !filter.category.has(check.category)) continue;

        if (target.type === "changeset") {
          changesetChecks.push({
            type: "check",
            check,
            target,
          });
        } else {
          const filePath = target.file;
          if (!fileNodes.has(filePath)) {
            fileNodes.set(filePath, {
              type: "file",
              filePath,
              label: path.basename(filePath),
              severity: group.severity,
            });
          }
        }
      }
    }

    const nodes: ReviewTreeNode[] = [...fileNodes.values()];

    // Add changeset checks under a "Changeset" pseudo-file node
    if (changesetChecks.length > 0) {
      const changesetFileNode: FileNode = {
        type: "file",
        filePath: "__changeset__",
        label: "Changeset",
        severity: group.severity,
      };
      nodes.unshift(changesetFileNode);
    }

    return nodes;
  }

  private getFileChildren(fileNode: FileNode): ReviewTreeNode[] {
    if (!this.state?.result) return [];
    const { result, filter } = this.state;

    if (fileNode.filePath === "__changeset__") {
      // Return changeset checks directly
      const nodes: CheckNode[] = [];
      for (const entry of result.targets) {
        if (entry.target.type !== "changeset") continue;
        for (const check of entry.checks) {
          if (!this.matchesSeverity(check, fileNode.severity)) continue;
          if (filter.category.size > 0 && !filter.category.has(check.category)) continue;
          nodes.push({ type: "check", check, target: entry.target });
        }
      }
      return nodes;
    }

    // Group by symbol, then file-level checks
    const symbolNodes = new Map<string, SymbolNode>();
    const fileChecks: CheckNode[] = [];

    for (const entry of result.targets) {
      const target = entry.target;
      if (target.type === "changeset") continue;
      if (target.file !== fileNode.filePath) continue;

      for (const check of entry.checks) {
        if (!this.matchesSeverity(check, fileNode.severity)) continue;
        if (filter.category.size > 0 && !filter.category.has(check.category)) continue;

        if (target.type === "symbol") {
          const key = `${target.symbol}:${target.lines[0]}`;
          if (!symbolNodes.has(key)) {
            symbolNodes.set(key, {
              type: "symbol",
              filePath: target.file,
              symbolName: target.symbol,
              lines: target.lines,
              label: `${target.symbol} (L${target.lines[0]}-${target.lines[1]})`,
              severity: fileNode.severity,
            });
          }
        } else {
          fileChecks.push({
            type: "check",
            check,
            target,
            filePath: target.file,
            line: 0,
          });
        }
      }
    }

    return [...symbolNodes.values(), ...fileChecks];
  }

  private getSymbolChildren(symbolNode: SymbolNode): ReviewTreeNode[] {
    if (!this.state?.result) return [];
    const { result, filter } = this.state;

    const checks: CheckNode[] = [];
    for (const entry of result.targets) {
      const target = entry.target;
      if (target.type !== "symbol") continue;
      if (target.file !== symbolNode.filePath) continue;
      if (target.symbol !== symbolNode.symbolName) continue;
      if (target.lines[0] !== symbolNode.lines[0]) continue;

      for (const check of entry.checks) {
        if (!this.matchesSeverity(check, symbolNode.severity)) continue;
        if (filter.category.size > 0 && !filter.category.has(check.category)) continue;
        checks.push({
          type: "check",
          check,
          target,
          filePath: target.file,
          line: target.lines[0] - 1,
        });
      }
    }

    return checks;
  }

  private matchesSeverity(check: Check, severity: "blocking" | "advisory" | "passed"): boolean {
    switch (severity) {
      case "blocking":
        return check.status === "failed" && check.level === "blocking";
      case "advisory":
        return (check.status === "failed" && check.level === "advisory") || check.status === "blocked";
      case "passed":
        return check.status === "passed";
    }
  }

  private buildSeverityGroupItem(node: SeverityGroupNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.Expanded);
    switch (node.severity) {
      case "blocking":
        item.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
        break;
      case "advisory":
        item.iconPath = new vscode.ThemeIcon("warning", new vscode.ThemeColor("list.warningForeground"));
        break;
      case "passed":
        item.collapsibleState = vscode.TreeItemCollapsibleState.Collapsed;
        item.iconPath = new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed"));
        break;
    }
    return item;
  }

  private buildFileItem(node: FileNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.Expanded);
    if (node.filePath !== "__changeset__") {
      item.resourceUri = vscode.Uri.file(node.filePath);
      item.iconPath = vscode.ThemeIcon.File;
      item.description = path.dirname(node.filePath).split(path.sep).slice(-2).join(path.sep);
    } else {
      item.iconPath = new vscode.ThemeIcon("git-commit");
    }
    return item;
  }

  private buildSymbolItem(node: SymbolNode): vscode.TreeItem {
    const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.Expanded);
    item.iconPath = new vscode.ThemeIcon("symbol-function");
    item.command = {
      command: "vscode.open",
      title: "Go to Symbol",
      arguments: [
        vscode.Uri.file(node.filePath),
        { selection: new vscode.Range(node.lines[0] - 1, 0, node.lines[0] - 1, 0) },
      ],
    };
    return item;
  }

  private buildCheckItem(node: CheckNode): vscode.TreeItem {
    const item = new vscode.TreeItem(
      `[${node.check.id}] ${node.check.description}`,
      vscode.TreeItemCollapsibleState.None,
    );
    item.iconPath = getCheckIcon(node.check);
    item.tooltip = node.check.note || node.check.description;

    if (node.filePath && node.line !== undefined) {
      item.command = {
        command: "vscode.open",
        title: "Go to Finding",
        arguments: [
          vscode.Uri.file(node.filePath),
          { selection: new vscode.Range(node.line, 0, node.line, 0) },
        ],
      };
    }
    return item;
  }

  dispose(): void {
    this._onDidChangeTreeData.dispose();
  }
}
