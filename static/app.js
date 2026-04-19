/**
 * ShopWave AI Support Agent — Frontend Logic
 * ────────────────────────────────────────────
 * SSE streaming, ticket card rendering, modal, audit trail.
 */

// ── State ──────────────────────────────────────────
const state = {
  tickets: [],         // raw ticket data (loaded from initial render)
  results: {},         // ticket_id → result object
  processing: false,
  evaluating: false,
  stats: { total: 20, pending: 20, resolved: 0, escalated: 0, failed: 0, avgTime: 0 },
};

// ── DOM refs ───────────────────────────────────────
const $grid       = document.getElementById('tickets-grid');
const $btnRun     = document.getElementById('btn-run');
const $btnEval    = document.getElementById('btn-eval');
const $statusChip = document.getElementById('status-chip');
const $modal      = document.getElementById('modal-overlay');
const $modalBody  = document.getElementById('modal-body');
const $modalTitle = document.getElementById('modal-title');
const $statTotal     = document.getElementById('stat-total');
const $statPending   = document.getElementById('stat-pending');
const $statResolved  = document.getElementById('stat-resolved');
const $statEscalated = document.getElementById('stat-escalated');
const $statFailed    = document.getElementById('stat-failed');
const $statTime      = document.getElementById('stat-time');
const $archToggle = document.getElementById('arch-toggle');
const $archContent= document.getElementById('arch-content');
const $scorecard  = document.getElementById('scorecard-container');

// ── Initialize ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Tickets are embedded in the page via template
  const raw = document.getElementById('tickets-data');
  if (raw) {
    try {
      state.tickets = JSON.parse(raw.textContent);
    } catch (e) {
      state.tickets = [];
    }
  }
  state.stats.total = state.tickets.length;
  state.stats.pending = state.tickets.length;
  renderGrid();
  updateStats();

  // Architecture toggle
  $archToggle.addEventListener('click', () => {
    $archToggle.classList.toggle('open');
    $archContent.classList.toggle('open');
  });

  // Run button
  $btnRun.addEventListener('click', startProcessing);
  $btnEval.addEventListener('click', startEvaluation);

  // Modal close
  document.getElementById('modal-close').addEventListener('click', closeModal);
  $modal.addEventListener('click', (e) => {
    if (e.target === $modal) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
});

// ── Render ticket grid ─────────────────────────────
function renderGrid() {
  $grid.innerHTML = '';
  state.tickets.forEach((t, i) => {
    const card = document.createElement('div');
    card.className = 'ticket-card';
    card.id = `card-${t.ticket_id}`;
    card.style.animationDelay = `${i * 0.04}s`;

    const result  = state.results[t.ticket_id];
    const status  = result ? result.status : 'pending';
    const cat     = result?.classification?.category || '—';
    const pri     = result?.classification?.priority || '—';
    const tools   = result?.resolution?.tool_calls || 0;

    card.innerHTML = `
      <div class="card-header">
        <span class="ticket-id">${t.ticket_id}</span>
        <span class="status-badge ${status}">${status}</span>
      </div>
      <div class="card-subject">${escHtml(t.subject)}</div>
      <div class="card-body">${escHtml(t.body)}</div>
      <div class="card-footer">
        ${cat !== '—' ? `<span class="tag category">${cat}</span>` : ''}
        ${pri !== '—' ? `<span class="tag priority-${pri}">${pri}</span>` : ''}
        ${tools > 0 ? `<span class="tag tools">🔧 ${tools} tools</span>` : ''}
      </div>
    `;
    card.addEventListener('click', () => openModal(t.ticket_id));
    $grid.appendChild(card);
  });
}

// ── Update stats ───────────────────────────────────
function updateStats() {
  const s = state.stats;
  $statTotal.textContent     = s.total;
  $statPending.textContent   = s.pending;
  $statResolved.textContent  = s.resolved;
  $statEscalated.textContent = s.escalated;
  $statFailed.textContent    = s.failed;
  $statTime.textContent      = s.avgTime > 0 ? `${s.avgTime}s` : '—';
}

// ── Start processing ───────────────────────────────
async function startProcessing() {
  if (state.processing) return;
  state.processing = true;
  $btnRun.disabled = true;
  $btnRun.innerHTML = '<span class="loading-spinner"></span> Processing…';
  setStatus('processing');

  // Reset stats
  state.results = {};
  state.stats = { total: state.tickets.length, pending: state.tickets.length, resolved: 0, escalated: 0, failed: 0, avgTime: 0 };
  updateStats();
  renderGrid();

  // Hit API to start
  try {
    const resp = await fetch('/api/process', { method: 'POST' });
    if (!resp.ok) {
      const data = await resp.json();
      alert(data.error || 'Failed to start');
      resetBtn();
      return;
    }
  } catch (err) {
    alert('Network error: ' + err.message);
    resetBtn();
    return;
  }

  // Open SSE stream
  const evtSource = new EventSource('/api/stream');
  evtSource.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    handleEvent(data);
    if (data.type === 'complete') {
      evtSource.close();
      onComplete(data.stats);
    }
  };
  evtSource.onerror = () => {
    evtSource.close();
    // Try fetching results directly
    setTimeout(fetchFinalResults, 2000);
  };
}

function resetBtn() {
  state.processing = false;
  $btnRun.disabled = false;
  $btnRun.innerHTML = '▶ Process Tickets';
  setStatus('idle');
}

async function startEvaluation() {
  if (state.processing || state.evaluating) return;
  state.evaluating = true;
  $btnEval.disabled = true;
  $btnEval.innerHTML = '<span class="loading-spinner"></span> Evaluating…';
  setStatus('evaluating');

  try {
    const resp = await fetch('/api/evaluate', { method: 'POST' });
    if (!resp.ok) {
        alert('Failed to start evaluation');
        state.evaluating = false;
        $btnEval.disabled = false;
        $btnEval.innerHTML = '🔍 Run Analytics';
        return;
    }
  } catch (err) {
    alert('Network error');
    state.evaluating = false;
    $btnEval.disabled = false;
    $btnEval.innerHTML = '🔍 Run Analytics';
    return;
  }
}

const $analytics = document.getElementById('analytics-dashboard');

function updateScorecard(card) {
  $analytics.style.display = 'block';
  $analytics.scrollIntoView({ behavior: 'smooth' });
  
  document.getElementById('total-score').textContent = Math.round(card.total_score);
  
  const b = card.breakdown;
  updateScoreItem('prod', b.production_readiness, 30);
  updateScoreItem('agentic', b.agentic_design, 10);
  updateScoreItem('depth', b.engineering_depth, 30);
  updateScoreItem('eval', b.evaluation_self_awareness, 10);
  updateScoreItem('pres', b.presentation_deployment, 20);

  // Report 2: Paper Metrics
  const pm = card.paper_metrics;
  document.getElementById('metric-acc').textContent = Math.round(pm.accuracy * 100) + '%';
  document.getElementById('metric-pre').textContent = Math.round(pm.precision * 100) + '%';
  document.getElementById('metric-rec').textContent = Math.round(pm.recall * 100) + '%';
  document.getElementById('metric-f1').textContent  = pm.f1_score.toFixed(2);
  document.getElementById('metric-sim').textContent = pm.mean_semantic_similarity.toFixed(2);

  // Report 3: Component Health
  const s = card.stats;
  document.getElementById('stat-reader-hit').textContent = Math.round((s.reader_hit_rate || 0) * 100);
  document.getElementById('stat-class-conf').textContent = Math.round((s.avg_classifier_confidence || 0) * 100);
  document.getElementById('stat-res-chain').textContent = s.avg_tool_chain || 0;
  document.getElementById('stat-rel-success').textContent = Math.round((s.tool_success_rate || 0) * 100);
}

function updateScoreItem(id, val, max) {
  const pct = (val / max) * 100;
  document.getElementById(`score-${id}`).style.width = `${pct}%`;
  document.getElementById(`val-${id}`).textContent = Math.round(val);
}

async function fetchFinalResults() {
  try {
    const resp = await fetch('/api/results');
    const results = await resp.json();
    results.forEach(r => {
      state.results[r.ticket_id] = r;
    });
    recalcStats();
    renderGrid();
    updateStats();
  } catch (e) {}
  onComplete(null); 
}

// ── Handle SSE events ──────────────────────────────
function handleEvent(data) {
  switch (data.type) {
    case 'start':
      // already handled
      break;

    case 'agent_update': {
      const card = document.getElementById(`card-${data.ticket_id}`);
      if (!card) break;
      if (data.status === 'running') {
        card.classList.add('is-processing');
        const badge = card.querySelector('.status-badge');
        if (badge) {
          badge.className = 'status-badge processing';
          badge.textContent = data.agent;
        }
      }
      if (data.status === 'complete' && data.agent === 'classifier' && data.data) {
        // Store classification early
        if (!state.results[data.ticket_id]) {
          state.results[data.ticket_id] = { classification: data.data };
        } else {
          state.results[data.ticket_id].classification = data.data;
        }
      }
      break;
    }

    case 'tool_call':
      // Could animate tool chips here
      break;

    case 'ticket_done': {
      const card = document.getElementById(`card-${data.ticket_id}`);
      if (card) {
        card.classList.remove('is-processing');
        const badge = card.querySelector('.status-badge');
        if (badge) {
          badge.className = `status-badge ${data.status}`;
          badge.textContent = data.status;
        }
        // Update footer with classification and tool count
        const footer = card.querySelector('.card-footer');
        if (footer && data.classification) {
          const cat = data.classification.category || '';
          const pri = data.classification.priority || '';
          const tc  = data.tool_calls || 0;
          footer.innerHTML = `
            ${cat ? `<span class="tag category">${cat}</span>` : ''}
            ${pri ? `<span class="tag priority-${pri}">${pri}</span>` : ''}
            ${tc > 0 ? `<span class="tag tools">🔧 ${tc} tools</span>` : ''}
          `;
        }
      }

      // Merge into results
      if (!state.results[data.ticket_id]) state.results[data.ticket_id] = {};
      Object.assign(state.results[data.ticket_id], data);
      state.stats.pending = Math.max(0, state.stats.pending - 1);
      if (data.status === 'resolved')  state.stats.resolved++;
      if (data.status === 'escalated') state.stats.escalated++;
      if (data.status === 'failed')    state.stats.failed++;
      updateStats();
      break;
    }

    case 'complete':
      onComplete(data.stats);
      break;

    case 'eval_start':
      $btnEval.style.display = 'inline-block';
      $btnEval.disabled = true;
      $btnEval.innerHTML = '<span class="loading-spinner"></span> Judging…';
      break;

    case 'eval_done':
      if (!state.results[data.ticket_id]) state.results[data.ticket_id] = {};
      state.results[data.ticket_id].evaluation = data.data;
      break;

    case 'eval_complete':
      state.evaluating = false;
      $btnEval.disabled = false;
      $btnEval.innerHTML = '🔍 Run Analytics';
      updateScorecard(data.scorecard);
      break;

    case 'eval_error':
      state.evaluating = false;
      $btnEval.disabled = false;
      $btnEval.innerHTML = '🔍 Run Analytics';
      alert("❌ Evaluation Error: " + data.error);
      break;
  }
}

function onComplete(stats) {
  console.log("Processing complete. Showing Analytics button.");
  state.processing = false;
  $btnRun.disabled = false;
  $btnRun.innerHTML = '▶ Process Tickets';
  setStatus('complete');
  
  // Force show the evaluation button
  if ($btnEval) {
    $btnEval.style.setProperty('display', 'inline-block', 'important');
    $btnEval.disabled = false;
    alert("✨ Processing Complete! You can now click 'Run Analytics' to generate the Judging Report.");
  }

  if (stats) {
    state.stats.resolved  = stats.resolved || 0;
    state.stats.escalated = stats.escalated || 0;
    state.stats.failed    = stats.failed || 0;
    state.stats.pending   = 0;
    state.stats.avgTime   = stats.avg_time || 0;
    updateStats();
  }

  // Final sync of results
  fetch('/api/results')
    .then(r => r.json())
    .then(results => {
      results.forEach(r => { state.results[r.ticket_id] = r; });
      renderGrid();
    })
    .catch(err => console.error("Final results fetch failed", err));
}

function recalcStats() {
  const vals = Object.values(state.results);
  state.stats.resolved  = vals.filter(r => r.status === 'resolved').length;
  state.stats.escalated = vals.filter(r => r.status === 'escalated').length;
  state.stats.failed    = vals.filter(r => r.status === 'failed').length;
  state.stats.pending   = state.stats.total - vals.length;
  const times = vals.map(r => r.elapsed_seconds || r.elapsed || 0).filter(Boolean);
  state.stats.avgTime   = times.length ? (times.reduce((a,b)=>a+b,0) / times.length).toFixed(2) : 0;
}

// ── Status chip ────────────────────────────────────
function setStatus(s) {
  $statusChip.className = `status-chip ${s}`;
  const label = $statusChip.querySelector('.status-label');
  if (label) label.textContent = s.charAt(0).toUpperCase() + s.slice(1);
}

// ═══════════════════════════════════════════════════
//  MODAL
// ═══════════════════════════════════════════════════
function openModal(ticketId) {
  const ticket = state.tickets.find(t => t.ticket_id === ticketId);
  const result = state.results[ticketId];
  if (!ticket) return;

  $modalTitle.innerHTML = `<span style="color:var(--accent);font-family:var(--font-mono)">${ticketId}</span> ${escHtml(ticket.subject)}`;

  let html = '';

  // ── Ticket Info ──
  html += `<div class="modal-section">
    <h3>📋 Ticket Information</h3>
    <div class="info-grid">
      <div class="info-item">
        <div class="info-label">Customer Email</div>
        <div class="info-value">${escHtml(ticket.customer_email)}</div>
      </div>
      <div class="info-item">
        <div class="info-label">Source</div>
        <div class="info-value">${ticket.source || '—'}</div>
      </div>
      <div class="info-item full">
        <div class="info-label">Message</div>
        <div class="info-value">${escHtml(ticket.body)}</div>
      </div>
      <div class="info-item full">
        <div class="info-label">Expected Action</div>
        <div class="info-value" style="color:var(--text-muted);font-size:0.78rem">${escHtml(ticket.expected_action || '—')}</div>
      </div>
    </div>
  </div>`;

  if (!result || !result.classification) {
    html += `<div class="modal-section"><p style="color:var(--text-muted)">This ticket has not been processed yet. Click "Process All Tickets" to begin.</p></div>`;
    $modalBody.innerHTML = html;
    showModal();
    return;
  }

  // ── Classification ──
  const cls = result.classification;
  html += `<div class="modal-section">
    <h3>🏷️ Classification</h3>
    <div class="info-grid">
      <div class="info-item">
        <div class="info-label">Category</div>
        <div class="info-value"><span class="tag category">${cls.category || '—'}</span></div>
      </div>
      <div class="info-item">
        <div class="info-label">Priority</div>
        <div class="info-value"><span class="tag priority-${cls.priority}">${cls.priority || '—'}</span></div>
      </div>
      <div class="info-item">
        <div class="info-label">Confidence</div>
        <div class="info-value">${cls.confidence != null ? (cls.confidence * 100).toFixed(0) + '%' : '—'}</div>
      </div>
      <div class="info-item">
        <div class="info-label">Auto-resolvable</div>
        <div class="info-value">${cls.can_auto_resolve ? '✅ Yes' : '❌ No'}</div>
      </div>
      <div class="info-item full">
        <div class="info-label">Reasoning</div>
        <div class="info-value" style="font-size:0.82rem">${escHtml(cls.reasoning || '—')}</div>
      </div>
    </div>
  </div>`;

  // ── Resolution ──
  const res = result.resolution;
  if (res) {
    const statusColor = res.status === 'resolved' ? 'var(--success)' 
                      : res.status === 'escalated' ? 'var(--escalate)' 
                      : 'var(--error)';

    html += `<div class="modal-section">
      <h3>🔧 Resolution <span class="status-badge ${res.status}" style="margin-left:8px">${res.status}</span></h3>
      <div class="info-grid">
        <div class="info-item">
          <div class="info-label">Tools Called</div>
          <div class="info-value">${res.tool_calls || 0}</div>
        </div>
        <div class="info-item">
          <div class="info-label">Processing Time</div>
          <div class="info-value">${result.elapsed_seconds || result.elapsed || '—'}s</div>
        </div>
      </div>
    </div>`;

    // Find the send_reply or escalation message
    if (res.audit_trail && res.audit_trail.length) {
      const replyEntry = res.audit_trail.find(e => e.tool === 'send_reply');
      const escalateEntry = res.audit_trail.find(e => e.tool === 'escalate');

      if (replyEntry && replyEntry.arguments) {
        html += `<div class="modal-section">
          <h3>💬 Customer Reply</h3>
          <div class="reply-box">${escHtml(replyEntry.arguments.message || '')}</div>
        </div>`;
      }
      if (escalateEntry && escalateEntry.arguments) {
        html += `<div class="modal-section">
          <h3>🚨 Escalation Summary</h3>
          <div class="reply-box escalation">${escHtml(escalateEntry.arguments.summary || '')}</div>
          <div style="margin-top:8px">
            <span class="tag priority-${escalateEntry.arguments.priority}">${escalateEntry.arguments.priority} priority</span>
          </div>
        </div>`;
      }
    }

    // ── Audit Trail ──
    if (res.audit_trail && res.audit_trail.length) {
      html += `<div class="modal-section">
        <h3>📜 Audit Trail (${res.audit_trail.length} steps)</h3>
        <div class="audit-timeline">`;

      res.audit_trail.forEach(entry => {
        const toolClass = entry.tool ? `tool-${entry.tool}` : '';
        const hasError  = entry.result && (entry.result.error || entry.action === 'llm_error');

        html += `<div class="audit-entry ${toolClass}" style="animation-delay:${(entry.step||1) * 0.05}s">
          <div class="audit-entry-header">
            <span class="audit-step">#${entry.step || '?'}</span>
            <span class="audit-tool">${entry.tool || entry.action || '—'}</span>
            <span class="audit-time">${entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : ''}</span>
          </div>
          <div class="audit-args">${escHtml(JSON.stringify(entry.arguments || entry.error || {}, null, 2))}</div>
          <div class="audit-result ${hasError ? 'error' : ''}">${escHtml(truncateJson(entry.result || {}))}</div>
        </div>`;
      });

      html += `</div></div>`;
    }
  }

  // Error case
  if (result.error) {
    html += `<div class="modal-section">
      <h3>❌ Error</h3>
      <div class="reply-box" style="border-color:rgba(239,68,68,0.3);color:var(--error)">${escHtml(result.error)}</div>
    </div>`;
  }

  $modalBody.innerHTML = html;
  showModal();
}

function showModal() {
  $modal.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  $modal.classList.remove('open');
  document.body.style.overflow = '';
}

// ── Helpers ────────────────────────────────────────
function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function truncateJson(obj) {
  const s = JSON.stringify(obj, null, 2);
  if (s.length > 500) return s.slice(0, 497) + '…';
  return s;
}
