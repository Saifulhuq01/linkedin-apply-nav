/* ─────────────────────────────────────────────────────────
   Apply-Nav — User Configuration Panel Module
   ───────────────────────────────────────────────────────── */

function initConfig() {
    loadConfig();
    document.getElementById('save-config-btn')?.addEventListener('click', saveConfig);

    // API key local storage sync
    const geminiKeyEl = document.getElementById('gemini-key');
    if (geminiKeyEl) {
        const savedKey = localStorage.getItem('gemini_api_key');
        if (savedKey) geminiKeyEl.value = savedKey;
        geminiKeyEl.addEventListener('change', (e) => {
            localStorage.setItem('gemini_api_key', e.target.value);
        });
    }
}

async function loadConfig() {
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();

        // Fill user profile fields
        const user = cfg.user || {};
        setVal('cfg-first-name', user.first_name);
        setVal('cfg-last-name', user.last_name);
        setVal('cfg-email', user.email);
        setVal('cfg-phone', user.phone);
        setVal('cfg-city', user.city);
        setVal('cfg-work-auth', user.work_authorization);
        setVal('cfg-experience', user.years_of_experience);

        // Fill search defaults
        const search = cfg.search || {};
        setVal('search-keywords', search.default_keywords || '');
        setVal('search-location', search.default_location || '');

        // Show setup banner if profile is incomplete
        if (!user.first_name || !user.email) {
            const banner = document.getElementById('setup-banner');
            if (banner) banner.style.display = 'flex';
        }

    } catch (err) {
        console.error("Failed to load config:", err);
    }
}

async function saveConfig() {
    const btn = document.getElementById('save-config-btn');
    btn.disabled = true;
    btn.innerText = 'Saving...';

    const userData = {
        first_name: getVal('cfg-first-name'),
        last_name: getVal('cfg-last-name'),
        email: getVal('cfg-email'),
        phone: getVal('cfg-phone'),
        city: getVal('cfg-city'),
        work_authorization: getVal('cfg-work-auth'),
        years_of_experience: getVal('cfg-experience'),
    };

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user: userData })
        });

        if (res.ok) {
            btn.innerText = '✓ Saved!';
            btn.style.background = 'var(--success)';
            // Hide setup banner
            const banner = document.getElementById('setup-banner');
            if (banner) banner.style.display = 'none';
            setTimeout(() => {
                btn.innerText = 'Save Profile';
                btn.style.background = '';
                btn.disabled = false;
            }, 2000);
        } else {
            const err = await res.json();
            alert(`Save failed: ${err.detail || 'Unknown error'}`);
            btn.innerText = 'Save Profile';
            btn.disabled = false;
        }
    } catch (err) {
        alert(`Save failed: ${err.message}`);
        btn.innerText = 'Save Profile';
        btn.disabled = false;
    }
}

function setVal(id, val) {
    const el = document.getElementById(id);
    if (el && val) el.value = val;
}

function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
}
