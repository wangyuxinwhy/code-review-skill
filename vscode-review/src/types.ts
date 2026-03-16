/**
 * cache.json v3 schema — keep in sync with .claude/skills/code-review/cache.py
 */

export interface ReviewSummary {
  blocking_failures: number;
  advisory_failures: number;
  passed: number;
  blocked: number;
  symbols_reviewed?: number;
}

export type CheckStatus = "passed" | "failed" | "blocked";
export type CheckLevel = "blocking" | "advisory";

export interface Annotation {
  offset: number;
  message: string;
}

export interface Check {
  id: string;
  category: string;
  level: CheckLevel;
  description: string;
  pass: boolean | null;
  status: CheckStatus;
  note: string;
  annotations?: Annotation[];
}

export interface ChangesetTarget {
  type: "changeset";
}

export interface FileTarget {
  type: "file";
  file: string;
}

export interface SymbolTarget {
  type: "symbol";
  file: string;
  symbol: string;
  lines: [number, number];
}

export type TargetDescriptor = ChangesetTarget | FileTarget | SymbolTarget;

export interface ReviewTarget {
  target: TargetDescriptor;
  checks: Check[];
}

export interface CacheChecks {
  checks: Check[];
}

export interface ReviewResult {
  version: string;
  timestamp?: string;
  checklist_version?: string;
  targets: ReviewTarget[];
  summary: ReviewSummary;
  files?: Record<string, CacheChecks>;
  symbols?: Record<string, CacheChecks>;
}
