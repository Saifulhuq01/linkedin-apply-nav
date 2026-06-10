/* ─────────────────────────────────────────────────────────
   Apply-Nav — Apply Modal & Screening Questions Module
   ───────────────────────────────────────────────────────── */

let currentQuestionId = null;

function initApply() {
    document.getElementById('confirm-submit-btn').addEventListener('click', confirmSubmitApplication);

    onWsMessage(msg => {
        if (msg.type === 'status') {
            const badge = document.getElementById('console-status-badge');
            if (badge) badge.innerText = msg.status.toUpperCase();
            if (msg.status === 'paused_for_review') {
                document.getElementById('confirm-submit-btn').removeAttribute('disabled');
            } else {
                document.getElementById('confirm-submit-btn').setAttribute('disabled', 'true');
            }
        } else if (msg.type === 'question') {
            showQuestion(msg.question);
        }
    });
}

async function startEasyApply(jobId) {
    document.getElementById('applier-modal').classList.add('active');

    const terminal = document.getElementById('terminal-log');
    terminal.innerHTML = '<div class="log-entry system">[SYSTEM] Starting automation script session...</div>';
    document.getElementById('confirm-submit-btn').setAttribute('disabled', 'true');
    document.getElementById('question-overlay').classList.remove('active');

    const apiKey = document.getElementById('gemini-key').value;

    try {
        const res = await fetch('/api/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_id: jobId, gemini_key: apiKey })
        });
        if (!res.ok) {
            const err = await res.json();
            addLog(`Automation launch failed: ${err.detail || 'Unknown error'}`, 'error');
        }
    } catch (err) {
        addLog(`Connection error: ${err.message}`, 'error');
    }
}

function closeConsole() {
    document.getElementById('applier-modal').classList.remove('active');
    fetch('/api/cancel-apply', { method: 'POST' });
}

async function confirmSubmitApplication() {
    document.getElementById('confirm-submit-btn').setAttribute('disabled', 'true');
    addLog('Final submission confirmed by user. Completing application...', 'info');

    try {
        const res = await fetch('/api/confirm-submit', { method: 'POST' });
        if (!res.ok) {
            const err = await res.json();
            addLog(`Submit request failed: ${err.detail}`, 'error');
        }
    } catch (err) {
        addLog(`Submit request error: ${err.message}`, 'error');
    }
}

function showQuestion(q) {
    currentQuestionId = q.id;
    document.getElementById('question-text').innerText = q.text;

    const container = document.getElementById('question-options-container');
    container.innerHTML = '';

    if (q.type === 'radio' && q.options) {
        q.options.forEach(opt => {
            const label = document.createElement('label');
            label.className = 'radio-option';
            label.innerHTML = `<input type="radio" name="form-radio-q" value="${opt.id}"><span>${opt.text}</span>`;
            container.appendChild(label);
        });
    } else if (q.type === 'select' && q.options) {
        const select = document.createElement('select');
        select.className = 'form-control';
        select.id = 'form-select-q';
        q.options.forEach(opt => {
            const o = document.createElement('option');
            o.value = opt.text;
            o.innerText = opt.text;
            select.appendChild(o);
        });
        container.appendChild(select);
    } else {
        const input = document.createElement('input');
        input.type = q.type === 'number' ? 'number' : 'text';
        input.className = 'form-control';
        input.id = 'form-text-q';
        input.value = q.suggested || '';
        container.appendChild(input);
    }

    document.getElementById('question-overlay').classList.add('active');
    addLog('Script paused. Custom screening question requires your confirmation.', 'warning');
}

async function submitAnswer() {
    let answer = '';
    const radios = document.getElementsByName('form-radio-q');

    if (radios.length > 0) {
        let selectedId = '';
        for (const r of radios) { if (r.checked) { selectedId = r.value; break; } }
        if (!selectedId) { alert('Please select an option.'); return; }
        answer = selectedId;
    } else if (document.getElementById('form-select-q')) {
        answer = document.getElementById('form-select-q').value;
    } else if (document.getElementById('form-text-q')) {
        answer = document.getElementById('form-text-q').value;
        if (!answer) { alert('Please type an answer.'); return; }
    }

    document.getElementById('question-overlay').classList.remove('active');
    addLog(`Submitting answer: "${answer}"`, 'info');

    await fetch('/api/submit-answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: answer })
    });
}

function skipQuestion() {
    document.getElementById('question-overlay').classList.remove('active');
    addLog('Skipping question, attempting defaults...', 'info');
    fetch('/api/submit-answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer: "__SKIP__" })
    });
}
