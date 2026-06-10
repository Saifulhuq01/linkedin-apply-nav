/* ─────────────────────────────────────────────────────────
   Apply-Nav — Resume Upload & Management Module
   ───────────────────────────────────────────────────────── */

function initResume() {
    loadResume();
    setupUploadZone();
}

async function loadResume() {
    try {
        const res = await fetch('/api/resume');
        const data = await res.json();
        const el = document.getElementById('resume-content');
        if (el) el.value = data.text;

        // Update upload zone status
        if (data.has_resume) {
            const zone = document.getElementById('upload-zone');
            if (zone) {
                zone.innerHTML = `
                    <div class="upload-success">✓ Resume uploaded</div>
                    <div class="upload-text">Drop a new PDF to replace</div>
                    <div class="upload-hint">Current resume: ${data.text.length} characters extracted</div>
                `;
            }
        }
    } catch (err) {
        console.error("Error loading resume:", err);
    }
}

function setupUploadZone() {
    const zone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('resume-file-input');
    if (!zone || !fileInput) return;

    zone.addEventListener('click', () => fileInput.click());

    zone.addEventListener('dragover', (e) => {
        e.preventDefault();
        zone.classList.add('drag-over');
    });

    zone.addEventListener('dragleave', () => {
        zone.classList.remove('drag-over');
    });

    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('drag-over');
        const files = e.dataTransfer.files;
        if (files.length > 0) uploadResumeFile(files[0]);
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) uploadResumeFile(e.target.files[0]);
    });
}

async function uploadResumeFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
        alert('Only PDF files are supported.');
        return;
    }

    const zone = document.getElementById('upload-zone');
    zone.innerHTML = `
        <div class="upload-text" style="color: var(--primary);">
            <svg width="24" height="24" stroke="currentColor" viewBox="0 0 24 24" style="animation: blink 0.8s infinite alternate; fill:none; stroke-width: 2;"><path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
            Uploading ${file.name}...
        </div>
    `;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const res = await fetch('/api/resume/upload', {
            method: 'POST',
            body: formData,
        });

        if (!res.ok) {
            const err = await res.json();
            alert(`Upload failed: ${err.detail || 'Unknown error'}`);
            zone.innerHTML = `
                <div class="upload-icon">📄</div>
                <div class="upload-text">Drop your resume PDF here or click to browse</div>
                <div class="upload-hint">PDF only, max 10MB</div>
            `;
            return;
        }

        const data = await res.json();

        zone.innerHTML = `
            <div class="upload-success">✓ Resume uploaded successfully</div>
            <div class="upload-text">${data.chars_extracted} characters extracted</div>
            <div class="upload-hint">Drop a new PDF to replace</div>
        `;

        // Update resume text preview
        const el = document.getElementById('resume-content');
        if (el) el.value = data.text;

    } catch (err) {
        alert(`Upload failed: ${err.message}`);
    }
}
