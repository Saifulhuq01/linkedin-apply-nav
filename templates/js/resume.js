/* ─────────────────────────────────────────────────────────
   Apply-Nav — resume.js
   Resume upload (drag-drop + click), text preview,
   version list, active resume management.
   ───────────────────────────────────────────────────────── */

function initResume() {
    const zone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('resume-file-input');

    if (!zone || !fileInput) return;

    // Drag-and-drop
    zone.addEventListener('dragover', e => {
        e.preventDefault();
        zone.classList.add('drag-over');
    });
    zone.addEventListener('dragleave', () => {
        zone.classList.remove('drag-over');
    });
    zone.addEventListener('drop', e => {
        e.preventDefault();
        zone.classList.remove('drag-over');
        const file = e.dataTransfer?.files?.[0];
        if (file) handleResumeFile(file);
    });

    // Click-to-browse
    fileInput.addEventListener('change', () => {
        const file = fileInput.files?.[0];
        if (file) handleResumeFile(file);
        fileInput.value = '';
    });

    // Initial load
    loadActiveResumePreview();
}

async function handleResumeFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
        showUploadError('Only PDF files are supported.');
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        showUploadError('File too large. Maximum 10MB.');
        return;
    }

    const zone = document.getElementById('upload-zone');
    zone.innerHTML = `
        <div class="upload-icon">⏳</div>
        <div class="upload-text">Uploading <strong>${file.name}</strong>...</div>
        <div class="upload-hint">Extracting text...</div>
    `;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const res = await fetch('/api/resume/upload', {
            method: 'POST',
            body: formData,
        });
        if (res.ok) {
            const data = await res.json();
            const resume = data.resume;
            zone.innerHTML = `
                <div class="upload-icon">✅</div>
                <div class="upload-text" style="color:var(--success);">${resume.original_name} uploaded!</div>
                <div class="upload-hint">Resume is now active.</div>
            `;
            await loadActiveResumePreview();
            await loadResumeList();
            addLog(`Resume uploaded: ${resume.original_name}`, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            showUploadError(err.detail || 'Upload failed.');
        }
    } catch (e) {
        showUploadError(`Upload error: ${e.message}`);
    }
}

function showUploadError(msg) {
    const zone = document.getElementById('upload-zone');
    zone.innerHTML = `
        <div class="upload-icon">❌</div>
        <div class="upload-text" style="color:var(--error);">${msg}</div>
        <div class="upload-hint" style="cursor:pointer; color:var(--accent);" onclick="resetUploadZone()">Click to try again</div>
    `;
}

function resetUploadZone() {
    const zone = document.getElementById('upload-zone');
    zone.innerHTML = `
        <div class="upload-icon">📄</div>
        <div class="upload-text">Drop resume PDF or click to browse</div>
        <div class="upload-hint">PDF only, max 10MB</div>
    `;
}

async function loadActiveResumePreview() {
    try {
        const res = await fetch('/api/resume');
        if (res.ok) {
            const data = await res.json();
            const preview = document.getElementById('resume-content');
            if (preview) {
                preview.value = data.has_resume
                    ? (data.text || '').substring(0, 500) + (data.text?.length > 500 ? '...' : '')
                    : 'No resume uploaded yet.';
            }
        }
    } catch (e) {
        console.error('Failed to load resume preview:', e);
    }
}

async function loadResumeList() {
    const container = document.getElementById('resume-list');
    if (!container) return;

    try {
        const res = await fetch('/api/resume/list');
        if (res.ok) {
            const resumes = await res.json();
            if (resumes.length === 0) {
                container.innerHTML = '';
                return;
            }
            container.innerHTML = `
                <p style="font-size:0.72rem; color:var(--text-muted); margin-bottom:0.5rem; text-transform:uppercase; letter-spacing:0.06em;">Resume History</p>
                <div style="display:flex; flex-direction:column; gap:0.375rem;">
                    ${resumes.map(r => `
                        <div style="display:flex; align-items:center; justify-content:space-between; gap:0.5rem; padding:0.5rem 0.625rem; background:rgba(255,255,255,0.03); border:1px solid ${r.is_active ? 'rgba(99,102,241,0.4)' : 'rgba(255,255,255,0.06)'}; border-radius:6px;">
                            <div style="min-width:0; flex:1;">
                                <div style="font-size:0.78rem; font-weight:600; color:${r.is_active ? 'var(--accent)' : 'var(--text-secondary)'}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${r.original_name}">${r.original_name}</div>
                                <div style="font-size:0.65rem; color:var(--text-muted);">${r.is_active ? '● Active' : new Date(r.uploaded_at).toLocaleDateString()}</div>
                            </div>
                            ${!r.is_active ? `<button class="btn btn-secondary" style="padding:0.2rem 0.5rem; font-size:0.7rem; flex-shrink:0;" onclick="activateResume('${r.filename}')">Activate</button>` : ''}
                        </div>
                    `).join('')}
                </div>
            `;
        }
    } catch (e) {
        console.error('Failed to load resume list:', e);
    }
}

async function activateResume(filename) {
    try {
        const res = await fetch('/api/resume/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename }),
        });
        if (res.ok) {
            await loadActiveResumePreview();
            await loadResumeList();
            addLog(`Activated resume: ${filename}`, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            addLog(`Failed to activate resume: ${err.detail}`, 'error');
        }
    } catch (e) {
        addLog(`Error: ${e.message}`, 'error');
    }
}
