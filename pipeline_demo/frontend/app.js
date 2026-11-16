/**
 * app.js
 * ======
 * Vanilla JavaScript Single Page Application with hash routing (#/, #/pod/:id, #/pod/:id/compare/:run_id).
 * Handles API calls, real subprocess log streaming via polling, interactive rule filtering,
 * side drawer inspection, and side-by-side cross-payer comparison.
 */

(function () {
  "use strict";

  const POD_NAMES = {
    uhc: "United Healthcare",
    humana: "Humana",
    devoted: "Devoted Health",
  };

  const appView = document.getElementById("app-view");
  const drawerOverlay = document.getElementById("side-drawer-overlay");
  const drawer = document.getElementById("side-drawer");
  const drawerTitle = document.getElementById("drawer-title");
  const drawerBody = document.getElementById("drawer-body");
  const drawerCloseBtn = document.getElementById("drawer-close-btn");

  // Close drawer helpers
  function closeDrawer() {
    drawerOverlay.classList.remove("active");
    drawer.classList.remove("active");
  }

  drawerOverlay.addEventListener("click", closeDrawer);
  drawerCloseBtn.addEventListener("click", closeDrawer);

  // Router dispatcher
  window.addEventListener("hashchange", handleRoute);
  window.addEventListener("DOMContentLoaded", handleRoute);

  function handleRoute() {
    const hash = window.location.hash || "#/";
    closeDrawer();

    if (hash === "#/" || hash === "#") {
      renderHome();
    } else if (hash.startsWith("#/pod/") && hash.includes("/compare/")) {
      const parts = hash.split("/");
      const podId = parts[2];
      const runId = parts[4];
      renderCompare(podId, runId);
    } else if (hash.startsWith("#/pod/")) {
      const parts = hash.split("/");
      const podId = parts[2];
      renderPod(podId);
    } else {
      window.location.hash = "#/";
    }
  }

  // Helper: Format number with commas
  function fmtNum(n) {
    return (n || 0).toLocaleString();
  }

  // --------------------------------------------------------------------------
  // PAGE 1: HOME
  // --------------------------------------------------------------------------
  async function renderHome() {
    appView.innerHTML = `
      <div style="margin-bottom: 24px;">
        <h1 style="font-size: 24px; font-weight: 700; margin-bottom: 6px;">Payer Policy → Billing Rules</h1>
        <p style="color: var(--text-secondary); font-size: 15px;">
          Deterministic pipeline converting unstructured healthcare policy PDFs into structured, explainable billing rules and cross-payer intelligence.
        </p>
      </div>

      <!-- 3 KPI Cards -->
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Total Rules Extracted</div>
          <div class="kpi-value" id="kpi-rules">—</div>
          <div class="kpi-subtext">Summed across all baseline workbooks</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Total CPT Codes Covered</div>
          <div class="kpi-value" id="kpi-cpt">—</div>
          <div class="kpi-subtext">Distinct CPT/HCPCS codes (ranges expanded)</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Total Pods</div>
          <div class="kpi-value" id="kpi-pods">3</div>
          <div class="kpi-subtext">Active payer ingestion pipelines</div>
        </div>
      </div>

      <!-- 3 Payer Pod Cards -->
      <div style="margin-bottom: 12px;">
        <h2 style="font-size: 16px; font-weight: 600; color: var(--text-primary);">Select a Payer Pod to Run Extraction</h2>
      </div>
      <div class="pod-grid" id="pod-cards-container">
        <!-- Rendered dynamically -->
        <div class="pod-card"><div style="padding: 20px; color: var(--text-muted);">Loading pods...</div></div>
      </div>
    `;

    try {
      const res = await fetch("/api/summary");
      if (!res.ok) throw new Error("Failed to load summary");
      const data = await res.json();

      document.getElementById("kpi-rules").textContent = fmtNum(data.total_rules);
      document.getElementById("kpi-cpt").textContent = fmtNum(data.total_cpt_codes);
      document.getElementById("kpi-pods").textContent = data.total_pods;

      const container = document.getElementById("pod-cards-container");
      container.innerHTML = data.pods.map(pod => `
        <a href="#/pod/${pod.id}" class="pod-card">
          <div>
            <div class="pod-header">
              <div class="pod-name">${pod.name}</div>
              <div class="pod-badge">Active Pod</div>
            </div>
            <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 12px;">
              Ingests and compiles payment policies into deterministic billing rules targeting standard claim lines.
            </p>
          </div>
          <div class="pod-stats">
            <div class="pod-stat-item">
              <strong>${fmtNum(pod.rules_count)}</strong> rules
            </div>
            <div class="pod-stat-item">
              <strong>${fmtNum(pod.cpt_count)}</strong> CPT codes
            </div>
          </div>
        </a>
      `).join("");
    } catch (err) {
      console.error(err);
    }
  }

  // --------------------------------------------------------------------------
  // PAGE 2: POD WORKSPACE & PDF UPLOAD
  // --------------------------------------------------------------------------
  function renderPod(podId) {
    if (!POD_NAMES[podId]) {
      window.location.hash = "#/";
      return;
    }

    const podName = POD_NAMES[podId];

    appView.innerHTML = `
      <div class="breadcrumb">
        <a href="#/">Home</a>
        <span>/</span>
        <span>${podName}</span>
      </div>

      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px;">
          <div>
            <h1 style="font-size: 20px; font-weight: 700; margin-bottom: 4px;">${podName} Policy Extraction Workspace</h1>
            <p style="color: var(--text-secondary); font-size: 14px;">
              Upload an official ${podName} policy document in PDF format to run the deterministic extraction and rule generation pipeline.
            </p>
          </div>
          <a href="#/" class="btn btn-secondary btn-sm">← Back to Home</a>
        </div>

        <!-- Drag & Drop Upload Zone -->
        <div class="upload-zone" id="upload-zone">
          <div class="upload-icon">📄</div>
          <div class="upload-title">Drop policy PDF here or click to browse</div>
          <div class="upload-hint">Accepts valid PDF documents up to 25 MB</div>
          <input type="file" id="file-input" accept=".pdf" style="display: none;">
        </div>

        <div id="file-info-container"></div>

        <div style="margin-top: 18px; display: flex; gap: 12px; align-items: center;">
          <button type="button" id="run-btn" class="btn btn-primary" disabled>
            Run Extraction Pipeline
          </button>
          <span id="run-status-text" style="font-size: 13px; color: var(--text-secondary);">Select a PDF file to begin</span>
        </div>
      </div>

      <!-- Stepper Container (hidden until run started) -->
      <div id="stepper-section" class="hidden">
        <div class="card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <div class="card-title" style="margin-bottom: 0;">Pipeline Execution Progress</div>
            <div id="compare-btn-container"></div>
          </div>
          <div class="stepper" id="stepper-container"></div>
        </div>
      </div>

      <!-- Generated Rules & Queries Section (hidden until rules ready) -->
      <div id="rules-section" class="hidden">
        <!-- Prominent Generated Claim Audit Query Card -->
        <div class="card" style="margin-bottom: 20px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div>
              <div class="card-title" style="margin-bottom: 2px;">Generated Claim Audit SQL Query</div>
              <div style="font-size: 13px; color: var(--text-secondary);">
                Executable SQL logic generated from the extracted policy rules for automated claims auditing
              </div>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
              <label for="query-rule-select" style="font-size: 12px; font-weight: 600; color: var(--text-secondary); margin: 0;">Rule:</label>
              <select id="query-rule-select" class="form-control" style="width: auto; min-width: 220px; font-weight: 500;"></select>
              <button type="button" id="copy-active-query-btn" class="btn btn-secondary btn-sm" onclick="window.copyActiveQuery()">
                Copy Query
              </button>
            </div>
          </div>
          <div id="query-meta-strip" style="margin-bottom: 12px; font-size: 12px; color: var(--text-secondary); display: flex; gap: 10px; align-items: center; flex-wrap: wrap;"></div>
          <pre class="sql-block" style="max-height: 280px; overflow-y: auto; margin-bottom: 0;"><code id="active-sql-code">-- Query will appear here</code></pre>
        </div>

        <div class="card">
          <div class="table-toolbar">
            <div class="table-toolbar-left">
              <input type="text" id="rule-search" class="form-control" placeholder="Search rules, codes, titles..." style="width: 280px;">
              <select id="category-filter" class="form-control" style="width: 220px;">
                <option value="">All Categories</option>
              </select>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
              <span id="rules-count-badge" style="font-size: 13px; color: var(--text-secondary);"></span>
              <a id="download-workbook-btn" href="#" class="btn btn-secondary btn-sm" download>Download Workbook (.xlsx)</a>
              <button id="compare-action-btn" class="btn btn-primary btn-sm">Compare Rules →</button>
            </div>
          </div>

          <div class="table-wrapper">
            <table class="rules-table">
              <thead>
                <tr>
                  <th style="width: 140px;">Rule ID</th>
                  <th style="width: 150px;">Category</th>
                  <th>Rule Text</th>
                  <th style="width: 150px;">Target Codes</th>
                  <th style="width: 130px;">Audit Action</th>
                  <th style="width: 70px;">Page</th>
                  <th style="width: 90px; text-align: right;">Details</th>
                </tr>
              </thead>
              <tbody id="rules-table-body">
                <tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;">No rules loaded.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;

    // Wire Upload interactions
    const uploadZone = document.getElementById("upload-zone");
    const fileInput = document.getElementById("file-input");
    const fileInfoContainer = document.getElementById("file-info-container");
    const runBtn = document.getElementById("run-btn");
    const runStatusText = document.getElementById("run-status-text");

    let selectedFile = null;

    uploadZone.addEventListener("click", () => fileInput.click());

    uploadZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      uploadZone.classList.add("dragover");
    });

    uploadZone.addEventListener("dragleave", () => {
      uploadZone.classList.remove("dragover");
    });

    uploadZone.addEventListener("drop", (e) => {
      e.preventDefault();
      uploadZone.classList.remove("dragover");
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        handleFileSelect(e.dataTransfer.files[0]);
      }
    });

    fileInput.addEventListener("change", (e) => {
      if (e.target.files && e.target.files.length > 0) {
        handleFileSelect(e.target.files[0]);
      }
    });

    function handleFileSelect(file) {
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        alert("Please upload a valid PDF document.");
        return;
      }
      if (file.size > 25 * 1024 * 1024) {
        alert("File size exceeds 25 MB limit.");
        return;
      }
      selectedFile = file;
      const sizeKb = (file.size / 1024).toFixed(1);
      fileInfoContainer.innerHTML = `
        <div class="file-info-card">
          <div class="file-name">📄 ${file.name}</div>
          <div class="file-size">${sizeKb} KB</div>
        </div>
      `;
      runBtn.disabled = false;
      runStatusText.textContent = "Ready to start pipeline execution";
    }

    runBtn.addEventListener("click", async () => {
      if (!selectedFile) return;

      runBtn.disabled = true;
      runStatusText.textContent = "Uploading policy PDF...";

      const formData = new FormData();
      formData.append("file", selectedFile);

      try {
        const res = await fetch(`/api/pods/${podId}/runs`, {
          method: "POST",
          body: formData,
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "Failed to start run");
        }

        const data = await res.json();
        const runId = data.run_id;

        document.getElementById("stepper-section").classList.remove("hidden");
        pollRunProgress(podId, runId);
      } catch (err) {
        alert("Error starting run: " + err.message);
        runBtn.disabled = false;
        runStatusText.textContent = "Run failed: " + err.message;
      }
    });
  }

  // --------------------------------------------------------------------------
  // Polling Pipeline Progress & Stepper
  // --------------------------------------------------------------------------
  let currentPollingTimer = null;

  function pollRunProgress(podId, runId) {
    if (currentPollingTimer) {
      clearInterval(currentPollingTimer);
    }

    const stepperContainer = document.getElementById("stepper-container");

    async function checkStatus() {
      try {
        const res = await fetch(`/api/runs/${runId}`);
        if (!res.ok) throw new Error("Failed to fetch run status");
        const run = await res.json();

        renderStepper(stepperContainer, run);

        if (run.status === "done") {
          clearInterval(currentPollingTimer);
          onRunComplete(podId, runId, run);
        } else if (run.status === "failed") {
          clearInterval(currentPollingTimer);
          const statusText = document.getElementById("run-status-text");
          if (statusText) {
            statusText.textContent = `Pipeline failed: ${run.error || "Subprocess returned an error"}`;
            statusText.style.color = "var(--color-error)";
          }
        }
      } catch (err) {
        console.error(err);
      }
    }

    checkStatus();
    currentPollingTimer = setInterval(checkStatus, 1000);
  }

  function renderStepper(container, run) {
    if (!container) return;

    container.innerHTML = run.steps.map((step, idx) => {
      const statusClass = `step-${step.status}`;
      let statusIcon = "•";
      if (step.status === "done") statusIcon = "✓";
      else if (step.status === "running") statusIcon = "⟳";
      else if (step.status === "failed") statusIcon = "✕";

      let linkHtml = "";
      if (idx === 1 && run.outputs.extraction_json && step.status === "done") {
        linkHtml = `
          <a href="/api/runs/${run.run_id}/files/extraction.json" target="_blank" class="step-link" download>
            View extraction.json
          </a>
        `;
      } else if (idx === 2 && run.outputs.rules_workbook && step.status === "done") {
        linkHtml = `
          <a href="/api/runs/${run.run_id}/files/${run.outputs.rules_workbook}" class="step-link" download>
            Download ${run.outputs.rules_workbook}
          </a>
        `;
      }

      const logText = (step.log || []).join("\n");
      const hasLog = logText.trim().length > 0;

      return `
        <div class="step-item ${statusClass}" id="step-item-${idx}">
          <div class="step-header">
            <div class="step-info">
              <div class="step-status-icon">${statusIcon}</div>
              <div class="step-title">${idx + 1}. ${step.name}</div>
              <div class="step-details">${step.details || ""}</div>
            </div>
            <div class="step-meta">
              ${step.elapsed ? `<span>${step.elapsed}s</span>` : ""}
              ${linkHtml}
              ${hasLog ? `
                <button type="button" class="btn btn-secondary btn-sm" onclick="window.toggleStepLog(${idx})">
                  Toggle Log
                </button>
              ` : ""}
            </div>
          </div>
          ${hasLog ? `
            <div id="step-log-${idx}" class="log-container ${step.status === 'running' ? '' : 'hidden'}">${escapeHtml(logText)}</div>
          ` : ""}
        </div>
      `;
    }).join("");
  }

  window.toggleStepLog = function (idx) {
    const el = document.getElementById(`step-log-${idx}`);
    if (el) el.classList.toggle("hidden");
  };

  async function onRunComplete(podId, runId, run) {
    const statusText = document.getElementById("run-status-text");
    if (statusText) {
      statusText.textContent = `Completed in ${((run.steps[1].elapsed || 0) + (run.steps[2].elapsed || 0)).toFixed(1)}s`;
      statusText.style.color = "var(--color-success)";
    }

    // Enable compare button in header
    const compareBtnContainer = document.getElementById("compare-btn-container");
    if (compareBtnContainer) {
      compareBtnContainer.innerHTML = `
        <a href="#/pod/${podId}/compare/${runId}" class="btn btn-primary btn-sm">
          Compare Rules Across Payers →
        </a>
      `;
    }

    const rulesSection = document.getElementById("rules-section");
    if (rulesSection) rulesSection.classList.remove("hidden");

    const compareActionBtn = document.getElementById("compare-action-btn");
    if (compareActionBtn) {
      compareActionBtn.onclick = () => {
        window.location.hash = `#/pod/${podId}/compare/${runId}`;
      };
    }

    const downloadBtn = document.getElementById("download-workbook-btn");
    if (downloadBtn && run.outputs.rules_workbook) {
      downloadBtn.href = `/api/runs/${runId}/files/${run.outputs.rules_workbook}`;
    }

    // Load rules table
    loadRunRulesTable(runId);
  }

  let currentRunRules = [];

  async function loadRunRulesTable(runId) {
    try {
      const res = await fetch(`/api/runs/${runId}/rules`);
      if (!res.ok) throw new Error("Failed to load rules");
      const data = await res.json();
      currentRunRules = data.rules || [];

      // Populate category filter
      const categoryFilter = document.getElementById("category-filter");
      if (categoryFilter) {
        categoryFilter.innerHTML = `<option value="">All Categories (${data.categories.length})</option>` +
          data.categories.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
        categoryFilter.onchange = renderFilteredRules;
      }

      const searchInput = document.getElementById("rule-search");
      if (searchInput) {
        searchInput.oninput = renderFilteredRules;
      }

      // Populate query rule selector
      const querySelect = document.getElementById("query-rule-select");
      if (querySelect) {
        querySelect.innerHTML = currentRunRules.map((r, i) => `
          <option value="${i}">${escapeHtml(r.rule_id)}: ${escapeHtml(r.audit_action)} (${escapeHtml(r.rule_category || "Unclassified")})</option>
        `).join("");
        querySelect.onchange = (e) => {
          window.displayActiveRuleQuery(parseInt(e.target.value, 10));
        };
      }

      renderFilteredRules();

      if (currentRunRules.length > 0) {
        window.displayActiveRuleQuery(0);
      }
    } catch (err) {
      console.error(err);
    }
  }

  function renderFilteredRules() {
    const searchVal = (document.getElementById("rule-search")?.value || "").toLowerCase().trim();
    const catVal = document.getElementById("category-filter")?.value || "";

    const filtered = currentRunRules.filter(r => {
      if (catVal && r.rule_category !== catVal) return false;
      if (searchVal) {
        const hay = `${r.rule_id} ${r.rule_category} ${r.rule_text} ${r.target_codes} ${r.policy_title}`.toLowerCase();
        if (!hay.includes(searchVal)) return false;
      }
      return true;
    });

    const badge = document.getElementById("rules-count-badge");
    if (badge) badge.textContent = `Showing ${filtered.length} of ${currentRunRules.length} rules`;

    const tbody = document.getElementById("rules-table-body");
    if (!tbody) return;

    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;">No matching rules found.</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered.map((r, i) => `
      <tr onclick="window.displayActiveRuleQuery(${i})" id="rule-row-${i}" style="cursor: pointer;">
        <td><strong>${escapeHtml(r.rule_id)}</strong></td>
        <td><span class="badge badge-unmatched">${escapeHtml(r.rule_category || "UNCLASSIFIED")}</span></td>
        <td>
          <div class="rule-text-cell" id="rule-cell-${i}">
            ${escapeHtml(r.rule_text || "")}
          </div>
        </td>
        <td><code>${escapeHtml(r.target_codes || "—")}</code></td>
        <td><span class="badge ${r.audit_action.startsWith('DENY') ? 'badge-conflict' : 'badge-same'}">${escapeHtml(r.audit_action)}</span></td>
        <td>${escapeHtml(r.source_page || "—")}</td>
        <td style="text-align: right; white-space: nowrap;">
          <button type="button" class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); window.openRuleDrawer(${i})">
            Details ↗
          </button>
        </td>
      </tr>
    `).join("");
  }

  window.displayActiveRuleQuery = function (index) {
    const r = currentRunRules[index];
    if (!r) return;

    const querySelect = document.getElementById("query-rule-select");
    if (querySelect) querySelect.value = index;

    const codeEl = document.getElementById("active-sql-code");
    if (codeEl) {
      codeEl.textContent = r.sql_query || "-- No executable SQL query generated for this rule";
    }

    const metaEl = document.getElementById("query-meta-strip");
    if (metaEl) {
      metaEl.innerHTML = `
        <span>Rule: <strong>${escapeHtml(r.rule_id)}</strong></span>
        <span>•</span>
        <span>Category: <span class="badge badge-unmatched">${escapeHtml(r.rule_category || "UNCLASSIFIED")}</span></span>
        <span>•</span>
        <span>Action: <span class="badge ${r.audit_action.startsWith('DENY') ? 'badge-conflict' : 'badge-same'}">${escapeHtml(r.audit_action)}</span></span>
        <span>•</span>
        <span>CPT/HCPCS: <code>${escapeHtml(r.target_codes || "None specified")}</code></span>
        ${r.policy_number ? `<span>•</span><span>Policy #: <strong>${escapeHtml(r.policy_number)}</strong></span>` : ""}
      `;
    }

    // Highlight row in table
    document.querySelectorAll(".rules-table tbody tr").forEach(tr => tr.classList.remove("selected"));
    const row = document.getElementById(`rule-row-${index}`);
    if (row) row.classList.add("selected");
  };

  window.copyActiveQuery = function () {
    const sql = document.getElementById("active-sql-code")?.textContent || "";
    if (!sql) return;
    navigator.clipboard.writeText(sql).then(() => {
      const btn = document.getElementById("copy-active-query-btn");
      if (btn) {
        const orig = btn.textContent;
        btn.textContent = "Copied!";
        btn.classList.add("btn-primary");
        btn.classList.remove("btn-secondary");
        setTimeout(() => {
          btn.textContent = orig;
          btn.classList.remove("btn-primary");
          btn.classList.add("btn-secondary");
        }, 1800);
      }
    });
  };

  window.openRuleDrawer = function (index) {
    const r = currentRunRules[index];
    if (!r) return;

    drawerTitle.textContent = `${r.rule_id} — ${r.policy_title}`;
    drawerBody.innerHTML = `
      <div class="drawer-section">
        <div class="drawer-section-label">Policy Information</div>
        <div class="drawer-section-value">
          <strong>Title:</strong> ${escapeHtml(r.policy_title)}<br>
          ${r.policy_number ? `<strong>Policy Number:</strong> ${escapeHtml(r.policy_number)}<br>` : ""}
          ${r.lob ? `<strong>Line of Business:</strong> ${escapeHtml(r.lob)}<br>` : ""}
          ${r.source_pdf ? `<strong>Source PDF:</strong> ${escapeHtml(r.source_pdf)} (Page ${r.source_page || "—"})` : ""}
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-label">Rule Classification</div>
        <div class="drawer-section-value">
          <span class="badge badge-unmatched" style="font-size: 13px;">${escapeHtml(r.rule_category)}</span>
          <span class="badge ${r.audit_action.startsWith('DENY') ? 'badge-conflict' : 'badge-same'}" style="font-size: 13px; margin-left: 8px;">
            ${escapeHtml(r.audit_action)}
          </span>
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-label">Verbatim Policy Rule Text</div>
        <div class="drawer-section-value" style="background-color: var(--bg-subtle); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border-color);">
          ${escapeHtml(r.rule_text)}
        </div>
      </div>

      ${r.exception_text ? `
        <div class="drawer-section">
          <div class="drawer-section-label">Exception & Clinical Notes</div>
          <div class="drawer-section-value" style="font-style: italic; color: var(--text-secondary);">
            ${escapeHtml(r.exception_text)}
          </div>
        </div>
      ` : ""}

      <div class="drawer-section">
        <div class="drawer-section-label">Medical Codes Scope</div>
        <div class="drawer-section-value">
          <strong>Target CPT/HCPCS:</strong> <code>${escapeHtml(r.target_codes || "None specified")}</code><br>
          ${r.target_modifiers ? `<strong>Modifiers:</strong> <code>${escapeHtml(r.target_modifiers)}</code><br>` : ""}
          ${r.possible_icd_codes ? `<strong>Associated ICD-10:</strong> <code>${escapeHtml(r.possible_icd_codes)}</code>` : ""}
        </div>
      </div>

      <div class="drawer-section">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
          <div class="drawer-section-label" style="margin-bottom: 0;">Executable Claim Audit SQL Query</div>
          <button type="button" class="btn btn-secondary btn-sm" onclick="window.copySql()">Copy SQL</button>
        </div>
        <pre class="sql-block"><code id="drawer-sql">${escapeHtml(r.sql_query || "-- No query generated")}</code></pre>
      </div>
    `;

    drawerOverlay.classList.add("active");
    drawer.classList.add("active");
  };

  window.copySql = function () {
    const el = document.getElementById("drawer-sql");
    if (el) {
      navigator.clipboard.writeText(el.textContent);
      alert("SQL query copied to clipboard!");
    }
  };

  // --------------------------------------------------------------------------
  // PAGE 3: COMPARE RULES
  // --------------------------------------------------------------------------
  async function renderCompare(podId, runId) {
    const podName = POD_NAMES[podId] || "Payer";

    appView.innerHTML = `
      <div class="breadcrumb">
        <a href="#/">Home</a>
        <span>/</span>
        <a href="#/pod/${podId}">${podName}</a>
        <span>/</span>
        <span>Cross-Payer Comparison</span>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
        <div>
          <h1 style="font-size: 20px; font-weight: 700; margin-bottom: 4px;">Cross-Payer Rule Alignment & Gap Analysis</h1>
          <p style="color: var(--text-secondary); font-size: 14px;">
            Comparing <strong>${escapeHtml(podName)}</strong> against the other 2 clients (<span id="other-payers-list">—</span>) by CPT/HCPCS code overlap. Self-comparison is excluded.
          </p>
        </div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <a href="#/pod/${podId}" class="btn btn-secondary btn-sm">← Back to Workspace</a>
          <a id="download-report-btn" href="#" class="btn btn-primary btn-sm" download>Download comparison_report.xlsx</a>
        </div>
      </div>

      <!-- Summary Strip -->
      <div class="compare-summary-strip" id="compare-summary-strip">
        <div class="compare-stats">
          <div class="compare-stat-item">
            <span class="badge badge-same">●</span>
            <span>Matched Pairs: <strong id="stat-matched">—</strong></span>
          </div>
          <div class="compare-stat-item">
            <span class="badge badge-differing">●</span>
            <span>Differing Details: <strong id="stat-differing">—</strong></span>
          </div>
          <div class="compare-stat-item">
            <span class="badge badge-conflict">●</span>
            <span>Possible Conflicts: <strong id="stat-conflicting">—</strong></span>
          </div>
          <div class="compare-stat-item">
            <span class="badge badge-unmatched">●</span>
            <span>Unmatched Rules: <strong id="stat-unmatched">—</strong></span>
          </div>
        </div>
        <div style="font-size: 13px; color: var(--text-secondary);">
          Comparing: <strong style="color: var(--text-primary);">${escapeHtml(podName)}</strong> vs other 2 clients: <strong id="other-payers-list">—</strong>
        </div>
      </div>

      <!-- Two-Column Layout -->
      <div class="compare-layout">
        <!-- Left Column: This Run's Rules -->
        <div>
          <div class="compare-col-header" id="left-col-header">This Policy's Rules</div>
          <div class="rule-list" id="left-rule-list">
            <div style="padding: 24px; text-align: center; color: var(--text-muted);">Running cross-payer comparison...</div>
          </div>
        </div>

        <!-- Right Column: Matched Rules grouped by Pod -->
        <div>
          <div class="compare-col-header">Matched Rules from Other 2 Clients</div>
          <div class="matches-panel" id="right-matches-panel">
            <div style="color: var(--text-muted); text-align: center; padding: 48px 0;">
              Select a rule on the left to inspect peer payer alignment and discrepancies.
            </div>
          </div>
        </div>
      </div>
    `;

    try {
      const res = await fetch(`/api/runs/${runId}/compare`);
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed to load comparison");
      }
      const data = await res.json();

      document.getElementById("stat-matched").textContent = fmtNum(data.summary.matched);
      document.getElementById("stat-differing").textContent = fmtNum(data.summary.differing);
      document.getElementById("stat-conflicting").textContent = fmtNum(data.summary.conflicting);
      document.getElementById("stat-unmatched").textContent = fmtNum(data.summary.unmatched);

      document.getElementById("other-payers-list").textContent = data.other_payers.join(" & ");
      document.getElementById("left-col-header").textContent = `${data.current_payer} Rules (${data.rules.length})`;

      const dlBtn = document.getElementById("download-report-btn");
      if (dlBtn) dlBtn.href = `/api/runs/${runId}/files/${data.report_filename}`;

      renderComparisonColumns(data);
    } catch (err) {
      alert("Error loading comparison report: " + err.message);
      document.getElementById("left-rule-list").innerHTML = `
        <div style="color: var(--color-error); padding: 16px;">Failed: ${escapeHtml(err.message)}</div>
      `;
    }
  }

  function renderComparisonColumns(data) {
    const leftList = document.getElementById("left-rule-list");
    const rightPanel = document.getElementById("right-matches-panel");

    if (!leftList || !rightPanel) return;

    if (!data.rules || data.rules.length === 0) {
      leftList.innerHTML = `<div style="padding: 24px; color: var(--text-muted);">No rules found in this policy run.</div>`;
      return;
    }

    leftList.innerHTML = data.rules.map((r, idx) => {
      // Badges for matches in each other payer
      const badgeHtml = data.other_payers.map(op => {
        const matches = r.matches_by_pod[op] || [];
        const count = matches.length;
        return `
          <span class="badge ${count > 0 ? 'badge-same' : 'badge-unmatched'}" style="font-size: 11px;">
            ${op}: ${count}
          </span>
        `;
      }).join("");

      return `
        <div class="rule-item-card ${idx === 0 ? 'active' : ''}" id="rule-item-${idx}" onclick="window.selectCompareRule(${idx})">
          <div class="rule-item-top">
            <div class="rule-item-id">${escapeHtml(r.rule_id)}</div>
            <div class="rule-item-category">${escapeHtml(r.rule_category)}</div>
          </div>
          <div class="rule-item-text">${escapeHtml(r.rule_text)}</div>
          <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;">
            Codes: <code>${escapeHtml(r.target_codes || "None")}</code>
          </div>
          <div class="rule-item-badges">${badgeHtml}</div>
        </div>
      `;
    }).join("");

    window._compareData = data;
    window.selectCompareRule(0);
  }

  window.selectCompareRule = function (index) {
    const data = window._compareData;
    if (!data || !data.rules[index]) return;

    const r = data.rules[index];

    // Highlight active card
    data.rules.forEach((_, i) => {
      const el = document.getElementById(`rule-item-${i}`);
      if (el) el.classList.toggle("active", i === index);
    });

    const rightPanel = document.getElementById("right-matches-panel");
    if (!rightPanel) return;

    const groupsHtml = data.other_payers.map(payerName => {
      if (!payerName || payerName.toLowerCase() === (data.current_payer || "").toLowerCase()) return "";
      const matches = r.matches_by_pod[payerName] || [];
      const unmatchedReason = r.unmatched_by_pod[payerName];

      let contentHtml = "";

      if (matches.length > 0) {
        contentHtml = matches.map(m => {
          let badgeClass = "badge-same";
          let labelText = m.result;
          if (m.result === "POSSIBLE_CONFLICT") {
            badgeClass = "badge-conflict";
          } else if (m.result.includes("DIFFERENT")) {
            badgeClass = "badge-differing";
          }

          return `
            <div class="match-card">
              <div class="match-card-top">
                <div class="match-card-id">${escapeHtml(m.c2_rule_id)} — ${escapeHtml(m.c2_policy_title)}</div>
                <span class="badge ${badgeClass}">${escapeHtml(labelText)}</span>
              </div>
              <div class="match-card-text">
                ${escapeHtml(m.c2_rule_text)}
              </div>
              <div class="match-card-meta">
                <strong>Shared Codes (${m.shared_code_count}):</strong> <code>${escapeHtml(m.shared_codes)}</code><br>
                ${m.differences && m.differences !== "No differences found" ? `
                  <div style="margin-top: 6px; color: var(--color-warning);">
                    <strong>Differences:</strong> ${escapeHtml(m.differences)}
                  </div>
                ` : ""}
              </div>
            </div>
          `;
        }).join("");
      } else {
        contentHtml = `
          <div class="no-match-alert">
            <strong>No matching rule in ${payerName}.</strong><br>
            <span style="font-size: 12px;">${escapeHtml(unmatchedReason || "None of the rule's codes appear in any peer policy rule.")}</span>
          </div>
        `;
      }

      return `
        <div class="pod-matches-group">
          <div class="pod-matches-title">${payerName} Comparison</div>
          ${contentHtml}
        </div>
      `;
    }).join("");

    rightPanel.innerHTML = `
      <div style="border-bottom: 1px solid var(--border-color); padding-bottom: 16px; margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
          <div style="font-size: 16px; font-weight: 700;">${escapeHtml(r.rule_id)}</div>
          <span class="badge badge-unmatched">${escapeHtml(r.rule_category)}</span>
        </div>
        <p style="font-size: 14px; color: var(--text-primary); margin-bottom: 8px;">
          ${escapeHtml(r.rule_text)}
        </p>
        <div style="font-size: 12px; color: var(--text-secondary);">
          <strong>Target Codes:</strong> <code>${escapeHtml(r.target_codes || "None")}</code>
        </div>
      </div>
      ${groupsHtml}
    `;
  };

  function escapeHtml(text) {
    if (!text) return "";
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

})();
