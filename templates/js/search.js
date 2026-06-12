/* ─────────────────────────────────────────────────────────
   Apply-Nav — Job Search & Display Module
   ───────────────────────────────────────────────────────── */

let activeJobs = [];
let selectedJob = null;

function initSearch() {
    document.getElementById('search-jobs-btn').addEventListener('click', startJobSearch);
    
    // Saved Search Profiles controls
    document.getElementById('profile-selector')?.addEventListener('change', onProfileSelect);
    document.getElementById('run-profile-btn')?.addEventListener('click', runSearchProfile);
    document.getElementById('delete-profile-btn')?.addEventListener('click', deleteSearchProfile);
    document.getElementById('save-profile-btn')?.addEventListener('click', saveSearchProfile);

    onWsMessage(msg => {
        if (msg.type === 'search_results') displayJobs(msg.jobs);
        else if (msg.type === 'score_result') updateJobScore(msg.job_id, msg.analysis);
    });

    loadSearchProfiles();
    pollSessionHealth();
    setInterval(pollSessionHealth, 15000); // Poll every 15s
}

async function startJobSearch() {
    const keywords = document.getElementById('search-keywords').value;
    const location = document.getElementById('search-location').value;
    const pages = parseInt(document.getElementById('search-pages').value) || 2;
    const apiKey = document.getElementById('gemini-key').value;
    const easyApply = document.getElementById('search-easy-apply')?.checked !== false;

    document.getElementById('search-jobs-btn').disabled = true;
    document.getElementById('search-jobs-btn').innerText = 'Searching...';

    document.getElementById('jobs-container').innerHTML = `
        <div class="placeholder-text">
            <svg width="32" height="32" stroke="var(--primary)" viewBox="0 0 24 24" style="animation: blink 1s infinite alternate; fill:none; stroke-width: 2;"><circle cx="12" cy="12" r="10"></circle><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10M12 2a15.3 15.3 0 00-4 10 15.3 15.3 0 004 10M2 12h20"></path></svg>
            <span>Navigating LinkedIn and extracting job postings...</span>
        </div>
    `;

    try {
        const res = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keywords, location, max_pages: pages, gemini_key: apiKey, easy_apply: easyApply })
        });
        if (!res.ok) {
            const err = await res.json();
            alert(`Search failed: ${err.detail || 'Unknown error'}`);
            resetSearchBtn();
        }
    } catch (err) {
        alert(`Search failed: ${err.message}`);
        resetSearchBtn();
    }
}

function resetSearchBtn() {
    document.getElementById('search-jobs-btn').disabled = false;
    document.getElementById('search-jobs-btn').innerHTML = `
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
        Search LinkedIn
    `;
}

function displayJobs(jobs) {
    resetSearchBtn();
    activeJobs = jobs;
    document.getElementById('job-count-badge').innerText = `${jobs.length} Found`;

    const container = document.getElementById('jobs-container');
    if (jobs.length === 0) {
        container.innerHTML = `<div class="placeholder-text">No Easy Apply jobs found matching criteria. Try different keywords.</div>`;
        return;
    }

    container.innerHTML = '';
    jobs.forEach(job => {
        const card = document.createElement('div');
        card.className = `job-card${job.already_applied ? ' applied' : ''}`;
        card.id = `job-card-${job.job_id}`;
        card.onclick = () => selectJob(job.job_id);

        let atsClass = (job.ats_type || 'easy_apply').replace('_', '-');
        let atsLabel = job.ats_type === 'easy_apply' ? 'Easy Apply' : (job.ats_type || 'External');
        if (atsClass === 'unknown') {
            atsClass = 'external';
            atsLabel = 'External';
        } else {
            atsLabel = atsLabel.charAt(0).toUpperCase() + atsLabel.slice(1);
        }
        const appliedBadge = job.already_applied ? '<span class="ats-badge applied-badge">Applied</span>' : '';

        card.innerHTML = `
            <div class="job-info">
                <span class="job-title" title="${job.title}">${job.title}</span>
                <span class="job-company">${job.company}</span>
                <span class="job-meta">
                    ${job.location}
                    <span class="ats-badge ${atsClass}">${atsLabel}</span>
                    ${appliedBadge}
                </span>
            </div>
            <div class="score-badge" id="score-badge-${job.job_id}" style="color: var(--text-muted); border-color: rgba(255,255,255,0.1)">--</div>
        `;
        container.appendChild(card);
    });

    if (jobs.length > 0) selectJob(jobs[0].job_id);
}

function updateJobScore(jobId, analysis) {
    const jobIndex = activeJobs.findIndex(j => j.job_id === jobId);
    if (jobIndex !== -1) activeJobs[jobIndex].analysis = analysis;

    const badge = document.getElementById(`score-badge-${jobId}`);
    if (badge) {
        const score = analysis.score;
        badge.innerText = score;
        badge.className = 'score-badge';
        if (score >= 75) badge.classList.add('score-high');
        else if (score >= 50) badge.classList.add('score-mid');
        else badge.classList.add('score-low');
    }

    if (selectedJob && selectedJob.job_id === jobId) renderJobDetail(selectedJob);
}

function selectJob(jobId) {
    document.querySelectorAll('.job-card').forEach(card => card.classList.remove('selected'));
    const card = document.getElementById(`job-card-${jobId}`);
    if (card) card.classList.add('selected');
    selectedJob = activeJobs.find(j => j.job_id === jobId);
    renderJobDetail(selectedJob);
}

function renderJobDetail(job) {
    const container = document.getElementById('detail-container');
    if (!job) return;

    const analysis = job.analysis;
    let analysisHTML = '';

    if (analysis) {
        const matchedTags = analysis.matched_skills.map(s => `<span class="skill-tag match">${s}</span>`).join('');
        const missingTags = analysis.missing_skills.map(s => `<span class="skill-tag gap">${s}</span>`).join('');

        analysisHTML = `
            <div class="glass-card" style="background: rgba(0,0,0,0.15); border: 1px solid rgba(255,255,255,0.02); margin-top: 1rem;">
                <h4 style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.5rem;">MATCH RATIONALE</h4>
                <p style="font-size: 0.9rem; line-height: 1.4; color: var(--text-secondary); margin-bottom: 1rem;">${analysis.rationale}</p>
                <h4 style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.25rem;">SKILLS IDENTIFIED</h4>
                <div class="skills-grid" style="margin-bottom: 1rem;">${matchedTags || '<span style="font-size: 0.8rem; color: var(--text-muted)">None parsed</span>'}</div>
                <h4 style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.25rem;">SKILL GAPS</h4>
                <div class="skills-grid" style="margin-bottom: 1.25rem;">${missingTags || '<span style="font-size: 0.8rem; color: var(--success)">Zero skills missing!</span>'}</div>
                <h4 style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.5rem;">AI PERSONALIZED OUTREACH</h4>
                <div class="outreach-box">
                    <button class="copy-btn" onclick="copyOutreach()">Copy Note</button>
                    <div class="outreach-text" id="outreach-note-text">${analysis.outreach_note || 'N/A'}</div>
                </div>
            </div>
        `;
    } else {
        analysisHTML = `
            <div style="text-align: center; padding: 2rem 0; color: var(--text-muted); font-size: 0.85rem;">
                <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" style="margin: 0 auto 0.5rem; animation: blink 1.5s infinite alternate;"><path d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"></path></svg>
                Calculating match score against your resume...
            </div>
        `;
    }

    const applyBtnDisabled = job.already_applied ? 'disabled' : '';
    let applyBtnText = 'Already Applied';
    if (!job.already_applied) {
        if (job.ats_type === 'easy_apply') {
            applyBtnText = 'Apply Easy-Apply';
        } else {
            const platform = job.ats_type === 'unknown' ? 'External' : (job.ats_type.charAt(0).toUpperCase() + job.ats_type.slice(1));
            applyBtnText = `Apply ${platform}`;
        }
    }

    container.innerHTML = `
        <div class="detail-header">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;">
                <div>
                    <div class="detail-title" title="${job.title}">${job.title}</div>
                    <div class="detail-company">${job.company}</div>
                </div>
                <button class="btn btn-success" id="apply-job-btn" onclick="startEasyApply('${job.job_id}')" style="flex-shrink: 0;" ${applyBtnDisabled}>
                    ${applyBtnText}
                </button>
            </div>
            <div class="detail-meta">
                <span>${job.location}</span>
                <span>Job ID: ${job.job_id}</span>
            </div>
        </div>
        ${analysisHTML}
        <div>
            <h4 style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 0.5rem;">DESCRIPTION SNIPPET</h4>
            <p style="font-size: 0.85rem; line-height: 1.5; color: var(--text-secondary); white-space: pre-line; max-height: 250px; overflow-y: auto; background: rgba(0,0,0,0.1); padding: 0.75rem; border-radius: 0.5rem;">
                ${job.description || 'N/A'}
            </p>
        </div>
    `;
}

function copyOutreach() {
    const text = document.getElementById('outreach-note-text').innerText;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.copy-btn');
        btn.innerText = 'Copied!';
        btn.style.background = 'var(--success)';
        setTimeout(() => { btn.innerText = 'Copy Note'; btn.style.background = 'rgba(255, 255, 255, 0.05)'; }, 2000);
    });
}

async function loadSearchProfiles() {
    try {
        const res = await fetch('/api/search-profiles');
        if (res.ok) {
            const profiles = await res.json();
            const selector = document.getElementById('profile-selector');
            if (selector) {
                selector.innerHTML = '<option value="">-- Select a Profile --</option>';
                profiles.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.name;
                    opt.innerText = p.name;
                    opt.dataset.keywords = p.keywords;
                    opt.dataset.location = p.location;
                    opt.dataset.maxPages = p.max_pages;
                    opt.dataset.easyApply = p.easy_apply !== false;
                    selector.appendChild(opt);
                });
            }
            onProfileSelect();
        }
    } catch (err) {
        console.error("Failed to load search profiles:", err);
    }
}

function onProfileSelect() {
    const selector = document.getElementById('profile-selector');
    const runBtn = document.getElementById('run-profile-btn');
    const deleteBtn = document.getElementById('delete-profile-btn');
    
    if (selector && selector.value) {
        if (runBtn) runBtn.removeAttribute('disabled');
        if (deleteBtn) deleteBtn.removeAttribute('disabled');
        
        const opt = selector.options[selector.selectedIndex];
        document.getElementById('search-keywords').value = opt.dataset.keywords || '';
        document.getElementById('search-location').value = opt.dataset.location || '';
        document.getElementById('search-pages').value = opt.dataset.maxPages || '2';
        const easyApplyInput = document.getElementById('search-easy-apply');
        if (easyApplyInput) {
            easyApplyInput.checked = opt.dataset.easyApply !== 'false';
        }
    } else {
        if (runBtn) runBtn.setAttribute('disabled', 'true');
        if (deleteBtn) deleteBtn.setAttribute('disabled', 'true');
    }
}

async function saveSearchProfile() {
    const nameInput = document.getElementById('new-profile-name');
    const name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
        alert("Please enter a profile name.");
        return;
    }
    
    const keywords = document.getElementById('search-keywords').value.trim();
    const location = document.getElementById('search-location').value.trim();
    const max_pages = parseInt(document.getElementById('search-pages').value) || 2;
    const easy_apply = document.getElementById('search-easy-apply')?.checked !== false;
    
    try {
        const res = await fetch('/api/search-profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, keywords, location, max_pages, easy_apply })
        });
        if (res.ok) {
            nameInput.value = '';
            await loadSearchProfiles();
            const selector = document.getElementById('profile-selector');
            if (selector) {
                selector.value = name;
                onProfileSelect();
            }
        } else {
            alert("Failed to save search profile.");
        }
    } catch (err) {
        alert("Error saving search profile: " + err.message);
    }
}

async function deleteSearchProfile() {
    const selector = document.getElementById('profile-selector');
    const name = selector ? selector.value : '';
    if (!name) return;
    
    if (!confirm(`Are you sure you want to delete profile "${name}"?`)) return;
    
    try {
        const res = await fetch(`/api/search-profiles/${encodeURIComponent(name)}`, {
            method: 'DELETE'
        });
        if (res.ok) {
            await loadSearchProfiles();
        } else {
            alert("Failed to delete search profile.");
        }
    } catch (err) {
        alert("Error deleting search profile: " + err.message);
    }
}

async function runSearchProfile() {
    const selector = document.getElementById('profile-selector');
    const name = selector ? selector.value : '';
    if (!name) return;
    
    const apiKey = document.getElementById('gemini-key').value;
    const runBtn = document.getElementById('run-profile-btn');
    const originalText = runBtn.innerText;
    
    runBtn.disabled = true;
    runBtn.innerText = 'Starting...';
    
    try {
        const res = await fetch(`/api/search-profiles/${encodeURIComponent(name)}/run?gemini_key=${encodeURIComponent(apiKey)}`, {
            method: 'POST'
        });
        if (!res.ok) {
            const err = await res.json();
            alert(`Failed to run search profile: ${err.detail || 'Unknown error'}`);
        }
    } catch (err) {
        alert(`Error running search profile: ${err.message}`);
    } finally {
        runBtn.disabled = false;
        runBtn.innerText = originalText;
    }
}

async function pollSessionHealth() {
    const dot = document.getElementById('session-status-dot');
    const text = document.getElementById('session-status-text');
    try {
        const res = await fetch('/api/session/health');
        if (res.ok) {
            const data = await res.json();
            if (dot && text) {
                if (data.circuit_open) {
                    dot.style.background = '#f59e0b';
                    text.innerText = `Session: Cooldown (${data.remaining_cooldown_seconds}s)`;
                } else if (data.session_valid) {
                    dot.style.background = '#10b981';
                    text.innerText = 'Session: Active';
                } else {
                    dot.style.background = '#ef4444';
                    text.innerText = 'Session: Inactive / Expired';
                }
            }
        } else {
            if (dot && text) {
                dot.style.background = '#ef4444';
                text.innerText = 'Session: Server Error (' + res.status + ')';
            }
        }
    } catch (err) {
        console.error("Failed to poll session health:", err);
        if (dot && text) {
            dot.style.background = '#ef4444';
            text.innerText = 'Session: Offline (Retrying)';
        }
    }
}

async function verifySession() {
    const btn = document.getElementById('verify-session-btn');
    const dot = document.getElementById('session-status-dot');
    const text = document.getElementById('session-status-text');
    
    if (btn) {
        btn.disabled = true;
        btn.innerText = 'Verifying...';
    }
    if (dot) dot.style.background = '#f59e0b';
    if (text) text.innerText = 'Session: Verifying...';

    try {
        const res = await fetch('/api/session/verify', { method: 'POST' });
        if (res.ok) {
            const data = await res.json();
            if (data.session_valid) {
                if (dot) dot.style.background = '#10b981';
                if (text) text.innerText = 'Session: Active';
                alert("LinkedIn session is active and valid!");
            } else {
                if (dot) dot.style.background = '#ef4444';
                if (text) text.innerText = 'Session: Inactive / Expired';
                alert("LinkedIn session is expired or invalid. Please login via terminal first.");
            }
        } else {
            let detail = 'Server error';
            try {
                const err = await res.json();
                detail = err.detail || detail;
            } catch (e) {}
            alert(`Verification failed: ${detail}`);
            pollSessionHealth();
        }
    } catch (err) {
        alert(`Verification failed: ${err.message}`);
        pollSessionHealth();
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerText = 'Verify';
        }
    }
}
