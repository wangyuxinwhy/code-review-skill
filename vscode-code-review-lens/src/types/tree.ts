import type { Check, TargetDescriptor } from "./review.js";

export type ReviewTreeNodeType =
  | "severityGroup"
  | "file"
  | "symbol"
  | "check";

export interface SeverityGroupNode {
  type: "severityGroup";
  label: string;
  severity: "blocking" | "advisory" | "passed";
  count: number;
}

export interface FileNode {
  type: "file";
  filePath: string;
  label: string;
  severity: "blocking" | "advisory" | "passed";
}

export interface SymbolNode {
  type: "symbol";
  filePath: string;
  symbolName: string;
  lines: [number, number];
  label: string;
  severity: "blocking" | "advisory" | "passed";
}

export interface CheckNode {
  type: "check";
  check: Check;
  target: TargetDescriptor;
  filePath?: string;
  line?: number;
}

export type ReviewTreeNode = SeverityGroupNode | FileNode | SymbolNode | CheckNode;
