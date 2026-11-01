// Hiring Cafe results capture, extension side. Runs on hiring.cafe pages.
// content_hiringcafe_main.js (page side) posts each batch of job results the page loaded;
// when "Hiring Cafe capture" is on in the popup, the batch goes to the background worker,
// which sends it to POST /api/jobs/hiring-cafe-import.
(() => {
  let enabled = true;   // on until switched off in the popup
  chrome.storage.sync.get('hiringCafeCapture', (d) => { enabled = d.hiringCafeCapture !== false; });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'sync' && changes.hiringCafeCapture) enabled = changes.hiringCafeCapture.newValue !== false;
  });

  window.addEventListener('message', (e) => {
    if (e.source !== window || e.origin !== window.location.origin) return;
    const d = e.data;
    if (!enabled || !d || d.source !== 'jobnav-hiringcafe' || !Array.isArray(d.jobs) || !d.jobs.length) return;
    try { chrome.runtime.sendMessage({ type: 'hiringcafe_jobs', jobs: d.jobs.slice(0, 200) }); } catch (_) { /* extension reloaded */ }
  });
})();
