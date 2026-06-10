/* ─────────────────────────────────────────────────────────
   Apply-Nav — Application History Module
   ───────────────────────────────────────────────────────── */

function initHistory() {
    // Load on tab switch
}

async function loadHistory() {
    try {
        const [historyRes, statsRes] = await Promise.all([
            fetch('/api/history?limit=50'),
            fetch('/api/stats')
        ]);

        const historyData = await historyRes.json();
        const stats = await statsRes.json();

        renderStats(stats);
        renderHistory(historyData.applications || []);
    } catch (err) {
        console.error("Failed to load history:", err);
    }
}

function renderStats(stats) {
    const container = document.getElementById('stats-grid');
    if (!container) return;

    container.innerHTML = `
        <div class="stat-card success">
            <div class="stat-value">${stats.total_applied}</div>
            <div class="stat-label">Applied</div>
        </div>
        <div class="stat-card primary">
            <div class="stat-value">${stats.avg_score || 0}</div>
            <div class="stat-label">Avg Score</div>
        </div>
        <div class="stat-card warning">
            <div class="stat-value">${stats.today_applied}</div>
            <div class="stat-label">Today</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${stats.success_rate}%</div>
            <div class="stat-label">Success Rate</div>
        </div>
    `;
}

function renderHistory(applications) {
    const container = document.getElementById('history-table-body');
    if (!container) return;

    if (applications.length === 0) {
        container.innerHTML = `
            <tr><td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-muted);">
                No applications yet. Search and apply to see history here.
            </td></tr>
        `;
        return;
    }

    container.innerHTML = applications.map(app => {
        const date = app.applied_at
            ? new Date(app.applied_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
            : new Date(app.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

        const statusClass = app.status || 'pending';
        const atsClass = (app.ats_type || 'easy_apply').replace('_', '-');
        const atsLabel = app.ats_type === 'easy_apply' ? 'Easy Apply' : (app.ats_type || 'Unknown');

        return `
            <tr>
                <td style="color: var(--text-primary); font-weight: 500;">${app.title || 'Unknown'}</td>
                <td>${app.company || 'Unknown'}</td>
                <td><span class="ats-badge ${atsClass}">${atsLabel}</span></td>
                <td>${app.score !== null ? app.score : '—'}</td>
                <td><span class="status-pill ${statusClass}">${statusClass}</span></td>
                <td style="color: var(--text-muted); font-size: 0.8rem;">${date}</td>
            </tr>
        `;
    }).join('');
}
