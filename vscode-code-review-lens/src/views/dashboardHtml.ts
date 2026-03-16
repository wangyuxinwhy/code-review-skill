import type { Check, ReviewResult } from "../types/review.js";
import type { StoreState } from "../store/reviewStore.js";

interface FindingCard {
  file: string;
  symbol?: string;
  line?: number;
  checks: Check[];
}

export function generateDashboardHtml(state: StoreState, nonce: string, cspSource: string): string {
  if (!state.result) {
    return wrapHtml(nonce, cspSource, `<div class="empty">No review results loaded.</div>`);
  }

  const result = state.result;
  const staleness = state.staleness;
  const isStale = staleness && staleness.staleFiles.size > 0;

  const summary = buildSummaryHtml(result);
  const stalenessWarning = isStale
    ? `<div class="staleness-banner">\u26A0\uFE0F ${staleness.staleFiles.size}/${staleness.totalFiles} files modified since review — results may be outdated</div>`
    : "";
  const findings = buildFindingsHtml(result);

  const body = `
    ${summary}
    ${stalenessWarning}
    <div class="filter-bar">
      <label>Severity:
        <select id="severityFilter">
          <option value="all">All</option>
          <option value="blocking">Blocking</option>
          <option value="advisory">Advisory</option>
          <option value="passed">Passed</option>
        </select>
      </label>
      <label>Category:
        <select id="categoryFilter">
          <option value="all">All</option>
          ${getCategories(result).map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("")}
        </select>
      </label>
    </div>
    ${findings}
  `;

  return wrapHtml(nonce, cspSource, body);
}

function buildSummaryHtml(result: ReviewResult): string {
  const s = result.summary;
  return `
    <div class="summary">
      <div class="counter blocking">${s.blocking_failures}<span>Blocking</span></div>
      <div class="counter advisory">${s.advisory_failures}<span>Advisory</span></div>
      <div class="counter passed">${s.passed}<span>Passed</span></div>
      <div class="counter blocked">${s.blocked}<span>Blocked</span></div>
    </div>
  `;
}

function buildFindingsHtml(result: ReviewResult): string {
  const cards: FindingCard[] = [];

  for (const entry of result.targets) {
    const target = entry.target;
    const nonPassed = entry.checks.filter((c) => c.status !== "passed");
    if (nonPassed.length === 0) continue;

    if (target.type === "changeset") {
      cards.push({ file: "Changeset", checks: nonPassed });
    } else if (target.type === "file") {
      cards.push({ file: target.file, checks: nonPassed });
    } else {
      cards.push({
        file: target.file,
        symbol: target.symbol,
        line: target.lines[0],
        checks: nonPassed,
      });
    }
  }

  if (cards.length === 0) {
    return `<div class="empty">All checks passed!</div>`;
  }

  return `
    <div class="findings">
      ${cards.map((card) => buildCardHtml(card)).join("")}
    </div>
  `;
}

function buildCardHtml(card: FindingCard): string {
  const fileDisplay = card.file === "Changeset" ? "Changeset" : shortenPath(card.file);
  const symbolDisplay = card.symbol ? ` \u2192 <strong>${escapeHtml(card.symbol)}</strong>` : "";
  const lineDisplay = card.line ? ` (L${card.line})` : "";

  const checksHtml = card.checks
    .map((check) => {
      const severityClass = check.status === "failed" ? check.level : "blocked";
      const dataAttrs = card.file !== "Changeset"
        ? `data-file="${escapeHtml(card.file)}" data-line="${card.line ?? 1}"`
        : "";
      return `
        <div class="check ${severityClass}" data-category="${escapeHtml(check.category)}" ${dataAttrs}>
          <span class="check-badge ${severityClass}">${check.status === "failed" ? check.level.toUpperCase() : "BLOCKED"}</span>
          <span class="check-id">[${escapeHtml(check.id)}]</span>
          <span class="check-desc">${escapeHtml(check.description)}</span>
          ${check.note ? `<div class="check-note">${escapeHtml(check.note)}</div>` : ""}
        </div>
      `;
    })
    .join("");

  return `
    <div class="card" data-file="${escapeHtml(card.file)}">
      <div class="card-header clickable" ${card.file !== "Changeset" ? `data-file="${escapeHtml(card.file)}" data-line="${card.line ?? 1}"` : ""}>
        <span class="file-name">${escapeHtml(fileDisplay)}</span>${symbolDisplay}${lineDisplay}
      </div>
      <div class="card-body">${checksHtml}</div>
    </div>
  `;
}

function getCategories(result: ReviewResult): string[] {
  const categories = new Set<string>();
  for (const entry of result.targets) {
    for (const check of entry.checks) {
      categories.add(check.category);
    }
  }
  return [...categories].sort();
}

function shortenPath(filePath: string): string {
  const parts = filePath.split("/");
  return parts.length > 3 ? parts.slice(-3).join("/") : filePath;
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function wrapHtml(nonce: string, cspSource: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Code Review Dashboard</title>
  <style nonce="${nonce}">
    body {
      font-family: var(--vscode-font-family);
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      padding: 16px;
      margin: 0;
    }
    .summary {
      display: flex;
      gap: 16px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .counter {
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 12px 20px;
      border-radius: 8px;
      font-size: 28px;
      font-weight: bold;
      min-width: 80px;
    }
    .counter span {
      font-size: 12px;
      font-weight: normal;
      margin-top: 4px;
      opacity: 0.8;
    }
    .counter.blocking { background: rgba(166, 51, 51, 0.3); color: #ff6b6b; }
    .counter.advisory { background: rgba(180, 150, 50, 0.3); color: #ffd93d; }
    .counter.passed { background: rgba(56, 138, 52, 0.3); color: #6bcb77; }
    .counter.blocked { background: rgba(100, 100, 100, 0.3); color: #aaa; }
    .staleness-banner {
      background: rgba(180, 150, 50, 0.2);
      border: 1px solid rgba(180, 150, 50, 0.5);
      border-radius: 6px;
      padding: 8px 12px;
      margin-bottom: 16px;
      font-size: 13px;
    }
    .filter-bar {
      display: flex;
      gap: 12px;
      margin-bottom: 16px;
      font-size: 13px;
    }
    .filter-bar select {
      background: var(--vscode-dropdown-background);
      color: var(--vscode-dropdown-foreground);
      border: 1px solid var(--vscode-dropdown-border);
      padding: 4px 8px;
      border-radius: 4px;
    }
    .findings { display: flex; flex-direction: column; gap: 12px; }
    .card {
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      overflow: hidden;
    }
    .card-header {
      padding: 8px 12px;
      background: var(--vscode-sideBar-background);
      font-size: 13px;
      font-weight: 500;
    }
    .card-header.clickable { cursor: pointer; }
    .card-header.clickable:hover { background: var(--vscode-list-hoverBackground); }
    .card-body { padding: 8px 12px; }
    .check {
      padding: 6px 0;
      border-bottom: 1px solid var(--vscode-panel-border);
      font-size: 13px;
      cursor: pointer;
    }
    .check:last-child { border-bottom: none; }
    .check:hover { opacity: 0.8; }
    .check-badge {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: bold;
      margin-right: 6px;
    }
    .check-badge.blocking { background: rgba(166, 51, 51, 0.6); color: #fff; }
    .check-badge.advisory { background: rgba(180, 150, 50, 0.5); color: #fff; }
    .check-badge.blocked { background: rgba(100, 100, 100, 0.4); color: #ccc; }
    .check-id { opacity: 0.7; margin-right: 4px; }
    .check-note {
      margin-top: 4px;
      padding-left: 12px;
      opacity: 0.7;
      font-size: 12px;
    }
    .empty {
      text-align: center;
      padding: 40px;
      opacity: 0.6;
      font-size: 16px;
    }
    .file-name { opacity: 0.9; }
  </style>
</head>
<body>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();

    document.addEventListener('click', (e) => {
      const target = e.target.closest('[data-file]');
      if (target && target.dataset.file && target.dataset.file !== 'Changeset') {
        vscode.postMessage({
          type: 'navigate',
          file: target.dataset.file,
          line: parseInt(target.dataset.line || '1', 10)
        });
      }
    });

    const severityFilter = document.getElementById('severityFilter');
    const categoryFilter = document.getElementById('categoryFilter');

    function applyFilters() {
      const severity = severityFilter?.value || 'all';
      const category = categoryFilter?.value || 'all';

      document.querySelectorAll('.check').forEach(el => {
        const checkSeverity = el.classList.contains('blocking') ? 'blocking'
          : el.classList.contains('advisory') ? 'advisory'
          : el.classList.contains('blocked') ? 'blocked'
          : 'passed';
        const checkCategory = el.dataset.category || '';

        const severityMatch = severity === 'all' || checkSeverity === severity;
        const categoryMatch = category === 'all' || checkCategory === category;

        el.style.display = severityMatch && categoryMatch ? '' : 'none';
      });

      document.querySelectorAll('.card').forEach(card => {
        const visibleChecks = card.querySelectorAll('.check:not([style*="display: none"])');
        card.style.display = visibleChecks.length > 0 ? '' : 'none';
      });
    }

    severityFilter?.addEventListener('change', applyFilters);
    categoryFilter?.addEventListener('change', applyFilters);
  </script>
</body>
</html>`;
}
