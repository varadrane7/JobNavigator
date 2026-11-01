// Hiring Cafe results capture, page side (world: MAIN). Runs on hiring.cafe pages.
// hiring.cafe loads its search results as JSON from its own API. This wraps the page's
// fetch and XMLHttpRequest, reads a copy of every JSON answer that carries a list of job
// results, and hands it to content_hiringcafe.js (the extension side) by window message.
// It makes no request of its own: what is read is exactly what the page already loaded.
(() => {
  if (window.__jobnavHiringCafe) return;
  window.__jobnavHiringCafe = true;

  const looksLikeJob = (o) => o && typeof o === 'object' &&
    (o.job_information || o.v5_processed_job_data || (o.apply_url && (o.title || o.company_name)));

  // the first array of job-like objects in an answer: results, hits, jobs, data.results ...
  function jobsIn(body, depth = 0) {
    if (!body || typeof body !== 'object' || depth > 3) return null;
    if (Array.isArray(body)) return body.length && body.some(looksLikeJob) ? body.filter(looksLikeJob) : null;
    for (const key of ['results', 'hits', 'jobs', 'data']) {
      const found = jobsIn(body[key], depth + 1);
      if (found) return found;
    }
    return null;
  }

  function hand(body) {
    const jobs = jobsIn(body);
    if (jobs && jobs.length) window.postMessage({ source: 'jobnav-hiringcafe', jobs }, window.location.origin);
  }

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const type = resp.headers.get('content-type') || '';
      if (type.includes('json')) resp.clone().json().then(hand).catch(() => {});
    } catch (_) { /* never break the page */ }
    return resp;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (...args) { this.__jobnavUrl = args[1]; return origOpen.apply(this, args); };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener('load', () => {
      try {
        const type = this.getResponseHeader('content-type') || '';
        if (!type.includes('json')) return;
        hand(this.responseType === 'json' ? this.response : JSON.parse(this.responseText));
      } catch (_) { /* not JSON */ }
    });
    return origSend.apply(this, args);
  };
})();
