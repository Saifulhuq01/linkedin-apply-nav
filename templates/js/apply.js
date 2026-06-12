/* ─────────────────────────────────────────────────────────
   Apply-Nav — apply.js
   Handles EasyApply/external apply flow, modal, HITL UI,
   confirm-submit, cancel, and WS event routing.
   ───────────────────────────────────────────────────────── */

let _activeJobId = null;
let _questionHash = null;
let _selectedOption = null;

function initApply() {
    // Route WS events to apply handlers
    onWsMessage(msg => {
        // Structured events (new format)
        if (msg.event) {
            switch (msg.event) {
                case 'apply_started':      onApplyStarted(msg.data); break;
                case 'apply_complete':     onApplyComplete(msg.data); break;
                case 'apply_failed':       onApplyFailed(msg.data); break;
                case 'paused_for_question': onQuestion(msg.data); break;
                case 'paused_for_review':  onReadyForReview(msg.data); break;
                case 'hitl_active':        onHITLActive(msg.data); break;
                case 'status':             onStatusChange(msg.data?.status); break;
            }
        }
        // Legacy format
        if (msg.type) {
            if (msg.type === 'status') onStatusChange(msg.status);
            if (msg.type === 'question') onQuestion(msg.question);
        }
    });
}

// Called from search.js renderJobDetail
async function startEasyApply(jobId) {
    _activeJobId = jobId;
    _questionHash = null;
    _selectedOption = null;

    // Open modal
    openConsole();
    clearLog();
    addLog('[SYSTEM] Initiating application workflow...', 'system');
    setConsoleBadge('Starting...', 'badge-accent');
    document.getElementById('confirm-submit-btn').disabled = true;
    document.getElementById('question-overlay').classList.remove('visible');
    document.getElementById('review-prompt').classList.remove('visible');

    const apiKey = document.getElementById('gemini-key')?.value || '';

    try {
        const res = await fetch('/api/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_id: jobId, gemini_key: apiKey || undefined }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            addLog(`[ERROR] ${err.detail || 'Failed to start apply'}`, 'error');
            setConsoleBadge('Error', 'badge-error');
        } else {
            addLog('[SYSTEM] Apply task started. Browser will open...', 'system');
            setConsoleBadge('Running', 'badge-accent');
        }
    } catch (e) {
        addLog(`[ERROR] ${e.message}`, 'error');
        setConsoleBadge('Error', 'badge-error');
    }
}

function onApplyStarted(data) {
    addLog(`[SYSTEM] Apply started for job ${data?.job_id || ''}`, 'system');
    setConsoleBadge('Applying', 'badge-accent');
    document.getElementById('confirm-submit-btn').disabled = true;
    document.getElementById('question-overlay').classList.remove('visible');
    document.getElementById('review-prompt').classList.remove('visible');
}

function onApplyComplete(data) {
    addLog('[SUCCESS] Application submitted!', 'success');
    setConsoleBadge('Done ✓', 'badge-success');
    document.getElementById('confirm-submit-btn').disabled = true;
    document.getElementById('review-prompt').classList.remove('visible');
    document.getElementById('question-overlay').classList.remove('visible');
    // Mark job as applied in list
    if (data?.job_id) markJobApplied(data.job_id);
}

function onApplyFailed(data) {
    addLog(`[ERROR] Apply failed: ${data?.message || 'Unknown error'}`, 'error');
    setConsoleBadge('Failed', 'badge-error');
    document.getElementById('confirm-submit-btn').disabled = true;
}

function onStatusChange(status) {
    if (!status) return;
    switch (status) {
        case 'idle':
            setConsoleBadge('Idle', 'badge-accent');
            break;
        case 'applying':
            setConsoleBadge('Applying', 'badge-accent');
            break;
        case 'paused_for_question':
            setConsoleBadge('⏸ Awaiting Answer', 'badge-warning');
            break;
        case 'paused_for_review':
            setConsoleBadge('⏸ Awaiting Review', 'badge-warning');
            document.getElementById('confirm-submit-btn').disabled = false;
            document.getElementById('review-prompt').classList.add('visible');
            break;
        case 'searching':
            setConsoleBadge('Searching', 'badge-accent');
            break;
    }
}

function onQuestion(data) {
    if (!data) return;
    _questionHash = data.question_hash || null;
    _selectedOption = null;

    const overlay = document.getElementById('question-overlay');
    const questionText = document.getElementById('question-text');
    const optContainer = document.getElementById('question-options-container');
    const answerInput = document.getElementById('question-answer');

    questionText.textContent = data.question || data.text || 'Unknown question';
    optContainer.innerHTML = '';

    // Options
    const opts = data.options || (data.radios || []).map(r => typeof r === 'string' ? r : r.text);
    opts.forEach(opt => {
        if (!opt) return;
        const btn = document.createElement('button');
        btn.className = 'question-option-btn';
        btn.textContent = opt;
        btn.onclick = () => {
            document.querySelectorAll('.question-option-btn').forEach(b => b.classList.remove('selected'));
            btn.classList.add('selected');
            _selectedOption = opt;
            if (answerInput) answerInput.value = opt;
        };
        optContainer.appendChild(btn);
    });

    // Pre-fill suggested answer
    const suggested = data.suggested || data.ai_suggestion || '';
    if (answerInput) {
        answerInput.value = suggested;
    }

    // Auto-select the suggested option
    if (suggested && opts.length > 0) {
        const normSugg = suggested.toLowerCase().trim();
        document.querySelectorAll('.question-option-btn').forEach(btn => {
            if (btn.textContent.toLowerCase().trim() === normSugg) {
                btn.click();
            }
        });
    }

    overlay.classList.add('visible');
    document.getElementById('review-prompt').classList.remove('visible');
    setConsoleBadge('⏸ Awaiting Answer', 'badge-warning');
    addLog(`[QUESTION] ${data.question || data.text}`, 'system');
}

function onReadyForReview(data) {
    document.getElementById('question-overlay').classList.remove('visible');
    document.getElementById('review-prompt').classList.add('visible');
    document.getElementById('confirm-submit-btn').disabled = false;
    setConsoleBadge('⏸ Awaiting Review', 'badge-warning');
    addLog('[HITL] Application ready for review. Please confirm submission.', 'warning');
}

function onHITLActive(data) {
    document.getElementById('question-overlay').classList.remove('visible');
    document.getElementById('review-prompt').classList.add('visible');
    document.getElementById('confirm-submit-btn').disabled = false;
    const filled = data?.fields_filled ?? 0;
    const total = data?.fields_total ?? 0;
    addLog(`[HITL] Browser opened. Pre-filled ${filled}/${total} fields. Please complete manually.`, 'warning');
    setConsoleBadge('⏸ Manual Required', 'badge-warning');
}

async function submitAnswer() {
    const answerInput = document.getElementById('question-answer');
    const answer = _selectedOption || (answerInput ? answerInput.value.trim() : '');

    if (!answer) {
        answerInput.style.borderColor = 'var(--error)';
        setTimeout(() => { answerInput.style.borderColor = ''; }, 2000);
        return;
    }

    addLog(`[ANSWER] Submitting: "${answer}"`, 'info');
    document.getElementById('question-overlay').classList.remove('visible');

    try {
        await fetch('/api/submit-answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question_hash: _questionHash || 'unknown', answer }),
        });
    } catch (e) {
        addLog(`[ERROR] Failed to submit answer: ${e.message}`, 'error');
    }
    _selectedOption = null;
    _questionHash = null;
}

async function skipQuestion() {
    document.getElementById('question-overlay').classList.remove('visible');
    _selectedOption = null;
    try {
        await fetch('/api/submit-answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question_hash: _questionHash || 'unknown', answer: 'Yes' }),
        });
    } catch (e) {}
    addLog('[SKIP] Question skipped — default answer used.', 'warning');
    _questionHash = null;
}

async function confirmSubmit() {
    const btn = document.getElementById('confirm-submit-btn');
    const spinner = document.getElementById('submit-spinner');
    btn.disabled = true;
    if (spinner) spinner.style.display = 'inline-block';
    addLog('[HITL] User confirmed submission...', 'system');
    setConsoleBadge('Submitting...', 'badge-accent');

    try {
        await fetch('/api/confirm-submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        addLog('[SUCCESS] Submission confirmed. Waiting for backend...', 'success');
    } catch (e) {
        addLog(`[ERROR] ${e.message}`, 'error');
        btn.disabled = false;
    }
    if (spinner) spinner.style.display = 'none';
    document.getElementById('review-prompt').classList.remove('visible');
}

async function closeConsole() {
    if (_activeJobId) {
        try {
            await fetch('/api/cancel-apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
        } catch (e) {}
    }
    document.getElementById('applier-modal').classList.remove('visible');
    _activeJobId = null;
}

// ─── Modal Helpers ────────────────────────────────────────── 

function openConsole() {
    document.getElementById('applier-modal').classList.add('visible');
}

function clearLog() {
    document.getElementById('terminal-log').innerHTML = '';
    addLog('[SYSTEM] Console session initialized.', 'system');
}

function setConsoleBadge(text, cls) {
    const badge = document.getElementById('console-status-badge');
    badge.textContent = text;
    badge.className = `badge ${cls}`;
}

function markJobApplied(jobId) {
    const card = document.getElementById(`job-card-${jobId}`);
    if (card) {
        card.classList.add('applied');
        card.style.pointerEvents = 'none';
    }
    const applyBtn = document.getElementById('apply-job-btn');
    if (applyBtn && applyBtn.onclick?.toString().includes(jobId)) {
        applyBtn.disabled = true;
        applyBtn.textContent = 'Already Applied';
    }
}
