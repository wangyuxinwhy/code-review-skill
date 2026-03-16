/**
 * Minimal vscode API mock for unit testing outside the extension host.
 * Only covers types used by diagnosticsProvider and staleness.
 */

export enum DiagnosticSeverity {
  Error = 0,
  Warning = 1,
  Information = 2,
  Hint = 3,
}

export class Range {
  constructor(
    public readonly startLine: number,
    public readonly startCharacter: number,
    public readonly endLine: number,
    public readonly endCharacter: number,
  ) {}
}

export class Diagnostic {
  source?: string;
  constructor(
    public readonly range: Range,
    public readonly message: string,
    public readonly severity?: DiagnosticSeverity,
  ) {}
}

export class Uri {
  private constructor(public readonly fsPath: string) {}
  static file(path: string): Uri {
    return new Uri(path);
  }
}
