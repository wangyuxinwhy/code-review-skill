import * as vscode from "vscode";
import * as crypto from "node:crypto";
import type { StoreState } from "../store/reviewStore.js";
import { generateDashboardHtml } from "./dashboardHtml.js";

export class DashboardPanel implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private state: StoreState | null = null;
  private readonly disposables: vscode.Disposable[] = [];
  private readonly extensionUri: vscode.Uri;

  constructor(extensionUri: vscode.Uri) {
    this.extensionUri = extensionUri;
  }

  open(state: StoreState): void {
    this.state = state;

    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside);
      this.updateContent();
      return;
    }

    this.panel = vscode.window.createWebviewPanel(
      "codeReviewDashboard",
      "Code Review Dashboard",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [this.extensionUri],
      },
    );

    this.panel.onDidDispose(() => {
      this.panel = undefined;
    }, undefined, this.disposables);

    this.panel.webview.onDidReceiveMessage(
      (message) => {
        if (message.type === "navigate" && message.file && message.line) {
          const uri = vscode.Uri.file(message.file);
          const line = Math.max(0, message.line - 1);
          vscode.window.showTextDocument(uri, {
            selection: new vscode.Range(line, 0, line, 0),
            viewColumn: vscode.ViewColumn.One,
          });
        }
      },
      undefined,
      this.disposables,
    );

    this.updateContent();
  }

  refresh(state: StoreState): void {
    this.state = state;
    if (this.panel) {
      this.updateContent();
    }
  }

  private updateContent(): void {
    if (!this.panel || !this.state) return;

    const nonce = crypto.randomBytes(16).toString("hex");
    const cspSource = this.panel.webview.cspSource;
    this.panel.webview.html = generateDashboardHtml(this.state, nonce, cspSource);
  }

  dispose(): void {
    this.panel?.dispose();
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}
