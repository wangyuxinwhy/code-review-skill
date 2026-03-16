/**
 * Minimal vscode API mock for unit testing outside the extension host.
 */

export enum DiagnosticSeverity {
  Error = 0,
  Warning = 1,
  Information = 2,
  Hint = 3,
}

export enum DiagnosticTag {
  Unnecessary = 1,
  Deprecated = 2,
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
  tags?: DiagnosticTag[];
  relatedInformation?: DiagnosticRelatedInformation[];
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

export class Location {
  constructor(
    public readonly uri: Uri,
    public readonly range: Range,
  ) {}
}

export class DiagnosticRelatedInformation {
  constructor(
    public readonly location: Location,
    public readonly message: string,
  ) {}
}
