/* ─────────────────────────────────────────────────────────
   Apply-Nav — config.js
   User profile form load/save, LLM config, cache stats.
   ───────────────────────────────────────────────────────── */

function initConfig() {
    // Loaded lazily when Settings tab is opened
}

async function loadConfig() {
    try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = await res.json();

        const user = cfg.candidate || cfg.user || {};

        const set = (id, val) => {
            const el = document.getElementById(id);
            if (el && val !== undefined && val !== null) el.value = val;
        };

        set('cfg-first-name', user.first_name || user.name?.split(' ')[0] || '');
        set('cfg-last-name', user.last_name || (user.name?.split(' ').slice(1).join(' ') || ''));
        set('cfg-email', user.email || '');
        set('cfg-phone', user.phone || '');
        set('cfg-city', user.city || '');
        set('cfg-experience', user.years_of_experience || user.experience_years || '');
        set('cfg-notice', user.notice_period || '');
        set('cfg-current-ctc', user.current_ctc || '');
        set('cfg-expected-ctc', user.expected_ctc || '');
        set('cfg-linkedin', user.linkedin_url || '');
        set('cfg-github', user.github_url || '');

        const llm = cfg.llm || {};
        set('cfg-llm-provider', llm.provider || 'gemini');

        // Check if setup is needed
        const needsSetup = !user.first_name && !user.email;
        const banner = document.getElementById('setup-banner');
        if (banner) banner.style.display = needsSetup ? 'block' : 'none';

    } catch (e) {
        console.error('Failed to load config:', e);
    }
}

async function saveConfig() {
    const btn = document.getElementById('save-config-btn');
    const statusEl = document.getElementById('save-config-status');

    const firstName = document.getElementById('cfg-first-name')?.value.trim() || '';
    const lastName = document.getElementById('cfg-last-name')?.value.trim() || '';

    const userPayload = {
        first_name: firstName,
        last_name: lastName,
        name: `${firstName} ${lastName}`.trim(),
        email: document.getElementById('cfg-email')?.value.trim() || '',
        phone: document.getElementById('cfg-phone')?.value.trim() || '',
        city: document.getElementById('cfg-city')?.value.trim() || '',
        years_of_experience: document.getElementById('cfg-experience')?.value.trim() || '',
        notice_period: document.getElementById('cfg-notice')?.value.trim() || '',
        current_ctc: document.getElementById('cfg-current-ctc')?.value.trim() || '',
        expected_ctc: document.getElementById('cfg-expected-ctc')?.value.trim() || '',
        linkedin_url: document.getElementById('cfg-linkedin')?.value.trim() || '',
        github_url: document.getElementById('cfg-github')?.value.trim() || '',
    };

    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user: userPayload, candidate: userPayload }),
        });
        if (res.ok) {
            if (statusEl) {
                statusEl.textContent = '✓ Saved!';
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
            }
            // Hide setup banner
            const banner = document.getElementById('setup-banner');
            if (banner && userPayload.first_name) banner.style.display = 'none';
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Save failed: ${err.detail || 'Unknown error'}`);
        }
    } catch (e) {
        alert(`Save error: ${e.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Save Profile'; }
    }
}

async function saveLLMConfig() {
    const statusEl = document.getElementById('llm-save-status');
    const provider = document.getElementById('cfg-llm-provider')?.value || 'gemini';
    const geminiKey = document.getElementById('cfg-gemini-key')?.value.trim() || '';

    const payload = { llm: { provider } };
    if (geminiKey) {
        payload.llm.gemini_api_key = geminiKey;
        // Also update the sidebar key input
        const sidebarKey = document.getElementById('gemini-key');
        if (sidebarKey && !sidebarKey.value) sidebarKey.value = geminiKey;
    }

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (res.ok) {
            if (statusEl) {
                statusEl.textContent = '✓ Saved!';
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
            }
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Save failed: ${err.detail}`);
        }
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
}

async function refreshCacheStats() {
    try {
        const res = await fetch('/api/cache/stats');
        if (res.ok) {
            const data = await res.json();
            const stats = data.stats || {};
            document.getElementById('cache-entries-count').textContent = stats.total_entries ?? 0;
            document.getElementById('cache-hits-count').textContent = stats.total_hits ?? 0;
            document.getElementById('cache-hit-rate').textContent = (stats.hit_rate !== undefined) ? `${Math.round(stats.hit_rate * 100)}%` : '0%';
        }
    } catch (e) {
        console.error('Cache stats error:', e);
    }
}

async function clearCache() {
    if (!confirm('Clear the entire answer cache? This cannot be undone.')) return;
    try {
        const res = await fetch('/api/cache', { method: 'DELETE' });
        if (res.ok) {
            await refreshCacheStats();
            addLog('Answer cache cleared.', 'info');
        }
    } catch (e) {
        addLog(`Failed to clear cache: ${e.message}`, 'error');
    }
}
