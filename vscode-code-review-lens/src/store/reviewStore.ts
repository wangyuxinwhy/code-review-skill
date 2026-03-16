import * as vscode from "vscode";
import type { Check, ReviewResult } from "../types/review.js";
import { ResultLoader } from "./resultLoader.js";
import { checkStaleness, type StalenessInfo } from "./staleness.js";
import { groupTargetsByFile, buildAnnotationIndex, type FileGroup, type AnnotationEntry } from "../utils/dataTransform.js";

export interface FilterState {
  severity: Set<string>;  // "blocking" | "advisory" | "passed"
  category: Set<string>;  // empty = all
}

export interface StoreState {
  result: ReviewResult | null;
  fileMap: Map<string, FileGroup>;
  changesetChecks: Check[];
  annotationIndex: Map<string, Map<number, AnnotationEntry[]>>;
  staleness: StalenessInfo | null;
  filter: FilterState;
}

export class ReviewStore implements vscode.Disposable {
  private readonly _onStateChanged = new vscode.EventEmitter<StoreState>();
  readonly onStateChanged = this._onStateChanged.event;

  private readonly loader: ResultLoader;
  private readonly disposables: vscode.Disposable[] = [];
  private _state: StoreState;

  constructor(workspaceRoot: string) {
    this.loader = new ResultLoader(workspaceRoot);
    this.disposables.push(this.loader, this._onStateChanged);

    this._state = {
      result: null,
      fileMap: new Map(),
      changesetChecks: [],
      annotationIndex: new Map(),
      staleness: null,
      filter: {
        severity: new Set(["blocking", "advisory", "passed"]),
        category: new Set(),
      },
    };

    this.loader.onResultChanged((result) => {
      this.computeState(result);
    }, undefined, this.disposables);
  }

  get state(): StoreState {
    return this._state;
  }

  get resultPath(): string {
    return this.loader.resultPath;
  }

  load(): void {
    this.loader.load();
  }

  clear(): void {
    this.computeState(null);
  }

  setFilter(filter: Partial<FilterState>): void {
    if (filter.severity !== undefined) {
      this._state.filter.severity = filter.severity;
    }
    if (filter.category !== undefined) {
      this._state.filter.category = filter.category;
    }
    // Re-emit with same result but new filter
    this._onStateChanged.fire(this._state);
  }

  private computeState(result: ReviewResult | null): void {
    if (!result) {
      this._state = {
        ...this._state,
        result: null,
        fileMap: new Map(),
        changesetChecks: [],
        annotationIndex: new Map(),
        staleness: null,
      };
      this._onStateChanged.fire(this._state);
      return;
    }

    const fileMap = groupTargetsByFile(result);
    const changesetChecks: Check[] = [];
    for (const entry of result.targets) {
      if (entry.target.type === "changeset") {
        changesetChecks.push(...entry.checks);
      }
    }
    const annotationIndex = buildAnnotationIndex(result);
    const staleness = checkStaleness(result);

    this._state = {
      ...this._state,
      result,
      fileMap,
      changesetChecks,
      annotationIndex,
      staleness,
    };
    this._onStateChanged.fire(this._state);
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}
