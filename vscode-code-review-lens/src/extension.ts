import * as vscode from "vscode";
import { ReviewStore, type StoreState } from "./store/reviewStore.js";
import { ReviewTreeViewProvider } from "./providers/treeViewProvider.js";
import { ReviewCodeLensProvider } from "./providers/codeLensProvider.js";
import {
  createGutterDecorationTypes,
  applyGutterDecorations,
  type GutterDecorationTypes,
} from "./providers/gutterProvider.js";
import {
  createDecorationTypes,
  applyDecorations,
  type DecorationTypes,
} from "./providers/decorationsProvider.js";
import { buildDiagnostics, applyDiagnostics } from "./providers/diagnosticsProvider.js";
import { StatusBarProvider } from "./providers/statusBarProvider.js";
import { DashboardPanel } from "./views/dashboardPanel.js";
import type { Check } from "./types/review.js";
import { getSeverityLabel } from "./utils/formatting.js";

let store: ReviewStore | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!workspaceRoot) return;

  // Central store
  store = new ReviewStore(workspaceRoot);
  context.subscriptions.push(store);

  // Decoration types
  const decorationTypes: DecorationTypes = createDecorationTypes();
  context.subscriptions.push(decorationTypes.blocking, decorationTypes.advisory, decorationTypes.annotation);

  const gutterTypes: GutterDecorationTypes = createGutterDecorationTypes(context.extensionPath);
  context.subscriptions.push(gutterTypes.blocking, gutterTypes.advisory, gutterTypes.passed);

  // Diagnostics collection
  const diagnosticsCollection = vscode.languages.createDiagnosticCollection("codeReviewLens");
  context.subscriptions.push(diagnosticsCollection);

  // Tree view
  const treeProvider = new ReviewTreeViewProvider();
  context.subscriptions.push(treeProvider);
  vscode.window.createTreeView("codeReviewFindings", {
    treeDataProvider: treeProvider,
    showCollapseAll: true,
  });

  // CodeLens
  const codeLensProvider = new ReviewCodeLensProvider();
  context.subscriptions.push(codeLensProvider);
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ scheme: "file" }, codeLensProvider),
  );

  // Status bar
  const statusBar = new StatusBarProvider();
  context.subscriptions.push(statusBar);

  // Dashboard
  const dashboard = new DashboardPanel(context.extensionUri);
  context.subscriptions.push(dashboard);

  // Wire store → all providers
  store.onStateChanged((state) => {
    treeProvider.refresh(state);
    codeLensProvider.refresh(state);
    statusBar.refresh(state);
    refreshAllEditors(decorationTypes, gutterTypes, state);

    if (state.result) {
      const diagResult = buildDiagnostics(state);
      applyDiagnostics(diagnosticsCollection, diagResult);
    } else {
      diagnosticsCollection.clear();
    }

    dashboard.refresh(state);
  });

  // Update decorations when visible editors change
  context.subscriptions.push(
    vscode.window.onDidChangeVisibleTextEditors(() => {
      if (store) {
        refreshAllEditors(decorationTypes, gutterTypes, store.state);
      }
    }),
  );

  // Re-check staleness on file save
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(() => store?.load()),
  );

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewLens.refresh", () => store?.load()),

    vscode.commands.registerCommand("codeReviewLens.clear", () => {
      store?.clear();
      statusBar.hide();
      diagnosticsCollection.clear();
      treeProvider.clear();
      codeLensProvider.clear();
      for (const editor of vscode.window.visibleTextEditors) {
        editor.setDecorations(decorationTypes.blocking, []);
        editor.setDecorations(decorationTypes.advisory, []);
        editor.setDecorations(decorationTypes.annotation, []);
        editor.setDecorations(gutterTypes.blocking, []);
        editor.setDecorations(gutterTypes.advisory, []);
        editor.setDecorations(gutterTypes.passed, []);
      }
    }),

    vscode.commands.registerCommand("codeReviewLens.openDashboard", () => {
      if (store) {
        dashboard.open(store.state);
      }
    }),

    vscode.commands.registerCommand("codeReviewLens.openResult", () => {
      if (store) {
        vscode.window.showTextDocument(vscode.Uri.file(store.resultPath));
      }
    }),

    vscode.commands.registerCommand("codeReviewLens.filterBySeverity", async () => {
      const pick = await vscode.window.showQuickPick(
        [
          { label: "Show All", value: "all" },
          { label: "Blocking Only", value: "blocking" },
          { label: "Advisory Only", value: "advisory" },
          { label: "Passed Only", value: "passed" },
        ],
        { placeHolder: "Filter by severity" },
      );
      if (!pick || !store) return;

      if (pick.value === "all") {
        store.setFilter({ severity: new Set(["blocking", "advisory", "passed"]) });
      } else {
        store.setFilter({ severity: new Set([pick.value]) });
      }
    }),

    vscode.commands.registerCommand("codeReviewLens.filterByCategory", async () => {
      if (!store?.state.result) return;

      const categories = new Set<string>();
      for (const entry of store.state.result.targets) {
        for (const check of entry.checks) {
          categories.add(check.category);
        }
      }

      const items = [
        { label: "Show All", value: "all" },
        ...[...categories].sort().map((c) => ({ label: c, value: c })),
      ];

      const pick = await vscode.window.showQuickPick(items, { placeHolder: "Filter by category" });
      if (!pick || !store) return;

      if (pick.value === "all") {
        store.setFilter({ category: new Set() });
      } else {
        store.setFilter({ category: new Set([pick.value]) });
      }
    }),

    vscode.commands.registerCommand(
      "codeReviewLens.showChecksQuickPick",
      async (checks: Check[], symbolName: string) => {
        const items = checks.map((check) => ({
          label: `$(${getQuickPickIcon(check)}) [${check.id}] ${check.description}`,
          description: getSeverityLabel(check),
          detail: check.note || undefined,
        }));

        await vscode.window.showQuickPick(items, {
          placeHolder: `Checks for ${symbolName}`,
        });
      },
    ),

    vscode.commands.registerCommand("codeReviewLens.statusBarMenu", async () => {
      const pick = await vscode.window.showQuickPick(
        [
          { label: "$(dashboard) Open Dashboard", command: "codeReviewLens.openDashboard" },
          { label: "$(refresh) Refresh", command: "codeReviewLens.refresh" },
          { label: "$(file-code) Open cache.json", command: "codeReviewLens.openResult" },
          { label: "$(clear-all) Clear", command: "codeReviewLens.clear" },
        ],
        { placeHolder: "Code Review Lens" },
      );
      if (pick) {
        vscode.commands.executeCommand(pick.command);
      }
    }),
  );

  // Initial load
  store.load();
}

function refreshAllEditors(
  decorationTypes: DecorationTypes,
  gutterTypes: GutterDecorationTypes,
  state: StoreState,
): void {
  for (const editor of vscode.window.visibleTextEditors) {
    applyDecorations(editor, decorationTypes, state);
    applyGutterDecorations(editor, gutterTypes, state);
  }
}

function getQuickPickIcon(check: Check): string {
  if (check.status === "passed") return "pass";
  if (check.status === "blocked") return "circle-slash";
  if (check.level === "blocking") return "error";
  return "warning";
}

export function deactivate(): void {
  // Cleanup handled by context.subscriptions
}
