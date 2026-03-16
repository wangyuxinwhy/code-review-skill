import * as vscode from "vscode";
import type { Check } from "../types/review.js";
import type { LineFinding } from "./dataTransform.js";

/**
 * Build a hover MarkdownString for a line finding.
 */
export function buildHoverMessage(finding: LineFinding, isStale: boolean): vscode.MarkdownString {
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

  md.appendMarkdown("---\n*Code Review Lens*");
  return md;
}

/**
 * Build a diagnostic message from a check and optional symbol name.
 */
export function formatDiagnosticMessage(check: Check, symbolName?: string): string {
  const prefix = symbolName ? `[${check.id}] ${symbolName}: ` : `[${check.id}] `;
  const lines = [`${prefix}${check.description}`];
  if (check.note) {
    lines.push("", check.note);
  }
  return lines.join("\n");
}

/**
 * Map check status+level to VSCode DiagnosticSeverity.
 */
export function mapSeverity(check: Check): vscode.DiagnosticSeverity | null {
  if (check.status === "passed") return null;
  if (check.status === "blocked") return vscode.DiagnosticSeverity.Information;
  if (check.level === "blocking") return vscode.DiagnosticSeverity.Error;
  return vscode.DiagnosticSeverity.Warning;
}

/**
 * Build a CodeLens title string from check counts.
 */
export function buildCodeLensTitle(blockingCount: number, advisoryCount: number): string {
  const parts: string[] = [];
  if (blockingCount > 0) {
    parts.push(`${blockingCount} blocking`);
  }
  if (advisoryCount > 0) {
    parts.push(`${advisoryCount} advisory`);
  }
  if (parts.length === 0) {
    return "All checks passed";
  }
  return parts.join(" \u00B7 ");
}

/**
 * Get severity label for a check.
 */
export function getSeverityLabel(check: Check): string {
  if (check.status === "passed") return "passed";
  if (check.status === "blocked") return "blocked";
  return check.level;
}

/**
 * Get icon ID for tree view nodes.
 */
export function getCheckIcon(check: Check): vscode.ThemeIcon {
  if (check.status === "passed") {
    return new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed"));
  }
  if (check.status === "blocked") {
    return new vscode.ThemeIcon("circle-slash", new vscode.ThemeColor("testing.iconSkipped"));
  }
  if (check.level === "blocking") {
    return new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
  }
  return new vscode.ThemeIcon("warning", new vscode.ThemeColor("list.warningForeground"));
}
