/* ─────────────────────────────────────────────────────────
   Apply-Nav — history.js
   Application history table load, stats rendering, filters.
   ───────────────────────────────────────────────────────── */

async function loadHistory() {
    const statusFilter = document.getElementById('history-status-filter')?.value || '';

    // Load statistics
    await loadStats();

    // Load history
    const tbody = document.getElementById('history-table-body');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:var(--text-muted);">Loading...</td></tr>`;

    try {
        let url = `/api/history?limit=100`;
        if (statusFilter) url += `&status=${encodeURIComponent(statusFilter)}`;

        const res = await fetch(url);
        if (!res.ok) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:var(--error);">Failed to load history.</td></tr>`;
            return;
        }

        const data = await res.json();
        const apps = data.applications || data || [];

        if (apps.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:var(--text-muted);">No applications found${statusFilter ? ` with status "${statusFilter}"` : ''}.</td></tr>`;
            return;
        }

        tbody.innerHTML = apps.map(a => {
            const score = a.score ?? '—';
            const scoreCls = a.score >= 75 ? 'var(--success)' : a.score >= 50 ? 'var(--warning)' : 'var(--error)';
            const statusInfo = statusDisplay(a.status);
            const atsLabel = atsDisplay(a.ats_type);

            const date = a.applied_at || a.created_at || '';
            let dateStr = '—';
            if (date) {
                try { dateStr = new Date(date).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }); }
                catch (e) { dateStr = date.slice(0, 10); }
            }

            return `
                <tr>
                    <td title="${escapeHtml(a.title || '')}">${escapeHtml(truncate(a.title || '—', 40))}</td>
                    <td title="${escapeHtml(a.company || '')}">${escapeHtml(truncate(a.company || '—', 25))}</td>
                    <td>${atsLabel}</td>
                    <td style="color:${typeof a.score === 'number' ? scoreCls : 'var(--text-muted)'}; font-weight:700;">${typeof a.score === 'number' ? a.score : '—'}</td>
                    <td>${statusInfo}</td>
                    <td style="color:var(--text-muted); font-size:0.8rem;">${dateStr}</td>
                </tr>
            `;
        }).join('');

    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:var(--error);">Error: ${e.message}</td></tr>`;
    }
}

async function loadStats() {
    try {
        const res = await fetch('/api/statistics');
        if (!res.ok) return;
        const stats = await res.json();

        const set = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val ?? '0';
        };

        set('stat-total', stats.total ?? stats.total_tracked ?? 0);
        set('stat-applied', stats.applied_total ?? stats.total_applied ?? 0);
        set('stat-today', stats.applied_today ?? stats.today_applied ?? 0);
        set('stat-week', stats.applied_this_week ?? 0);
        set('stat-month', stats.applied_this_month ?? 0);
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

function statusDisplay(status) {
    const map = {
        'applied':     '<span class="badge badge-success">Applied</span>',
        'review':      '<span class="badge badge-warning">In Review</span>',
        'applying':    '<span class="badge badge-accent">Applying</span>',
        'scored':      '<span class="badge">Scored</span>',
        'discovered':  '<span class="badge">Discovered</span>',
        'failed':      '<span class="badge badge-error">Failed</span>',
        'skipped':     '<span style="font-size:0.78rem; color:var(--text-muted);">Skipped</span>',
        'queued':      '<span class="badge badge-accent">Queued</span>',
        'manual_needed': '<span class="badge badge-warning">Manual</span>',
    };
    return map[status] || `<span class="badge">${status || 'Unknown'}</span>`;
}

function atsDisplay(ats) {
    const map = {
        'easy_apply':      '<span class="ats-badge easy-apply">Easy Apply</span>',
        'workday':         '<span class="ats-badge workday">Workday</span>',
        'greenhouse':      '<span class="ats-badge greenhouse">Greenhouse</span>',
        'lever':           '<span class="ats-badge lever">Lever</span>',
        'hitl_fallback':   '<span class="ats-badge external">External</span>',
        'unknown':         '<span class="ats-badge external">Unknown</span>',
    };
    return map[ats] || `<span class="ats-badge external">${ats || '—'}</span>`;
}

function truncate(str, n) {
    return str && str.length > n ? str.substring(0, n) + '…' : str;
}

function escapeHtml(str) {
    return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
