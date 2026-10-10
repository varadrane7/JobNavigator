// backend/autoapply/submitters/js/aa_page.js — injected by the AutoApply submitters (Playwright
// page.evaluate) after autofill_match.js and ats_combobox.js. Field discovery and labelling are
// ported from extension/content_autofill_fill.js; the difference is that every control gets a
// data-aa-idx so Python can address it, file inputs are included (they are usually hidden behind
// an "Attach" button), and each field reports its human question text and whether it is required.
(function () {
  'use strict';
  const IDX = 'data-aa-idx';
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // Rendered text only: hidden helper/error text inside a label ("No location found") stays out.
  const txt = (n) => (n ? (typeof n.innerText === 'string' && n.isConnected ? n.innerText : n.textContent) : '');
  // Required markers: Greenhouse/Ashby '*', Lever '✱'.
  const stripStar = (s) => clean(s).replace(/\s*[*\u2731]\s*$/, '').replace(/^[*\u2731]\s*/, '');

  function isVisible(el) {
    if (typeof el.checkVisibility === 'function' && !el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return r.right > 0 && r.bottom > 0;
  }

  const CONTROLS = 'input:not([type="hidden"]), select, textarea';
  const LABELISH = 'label, legend, .application-label, [class*="label" i], [class*="question" i], [class*="title" i]';
  // Walk up from a control while the ancestor still holds no more than `maxControls` controls
  // (so a page-wide wrapper never lends its first label to every field) and return the first
  // label-ish text that is not itself the control's own wrapping label.
  function nearestQuestion(el, maxControls) {
    for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
      if (n.querySelectorAll(CONTROLS).length > maxControls) break;
      const lab = Array.from(n.querySelectorAll(LABELISH)).find((l) => !l.contains(el) && clean(txt(l)));
      if (lab) return txt(lab);
    }
    return '';
  }

  function labelParts(el) {
    const parts = [];
    if (el.id) {
      try { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) parts.push(['for', txt(l)]); } catch (e) { /* bad id */ }
    }
    if (el.getAttribute('aria-label')) parts.push(['aria', el.getAttribute('aria-label')]);
    const alby = el.getAttribute('aria-labelledby');
    if (alby) alby.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) parts.push(['alby', txt(n)]); });
    const anc = el.closest('label');
    if (anc) parts.push(['anc', txt(anc)]);
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) parts.push(['legend', txt(lg)]); }
    const ff = el.closest('[data-automation-id^="formField-"]');
    if (ff) { const l = ff.querySelector('label, legend'); if (l) parts.push(['wd', txt(l)]); }
    const dfp = el.closest('[data-field-path]');
    if (dfp) {
      const q = dfp.querySelector('label:not([for]), legend, [class*="title" i], [class*="label" i], [class*="question" i], h1, h2, h3, h4');
      if (q) parts.push(['ashby', txt(q)]);
    }
    // Greenhouse/Lever put the question in a label-ish sibling inside the field's own wrapper.
    const near = nearestQuestion(el, 1);
    if (near) parts.push(['near', near]);
    if (el.placeholder) parts.push(['ph', el.placeholder]);
    return parts;
  }

  function question(el) {
    for (const [, t] of labelParts(el)) { const s = stripStar(t); if (s && s.length < 600) return s; }
    return clean(el.name || el.id || '');
  }

  function signature(el) {
    const bits = labelParts(el).map(([, t]) => t);
    if (el.name) bits.push(el.name);
    if (el.id) bits.push(el.id);
    if (el.getAttribute('data-automation-id')) bits.push(el.getAttribute('data-automation-id'));
    const dfp = el.closest('[data-field-path]');
    if (dfp) bits.push(dfp.getAttribute('data-field-path') || '');
    return clean(bits.join(' '));
  }

  function isRequired(el, q) {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    for (const [, t] of labelParts(el)) if (/[*\u2731]\s*$/.test(clean(t))) return true;
    return /\(required\)/i.test(q || '');
  }

  function optionLabel(el) {
    if (el.id) { try { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(txt(l)); } catch (e) { /* bad id */ } }
    const anc = el.closest('label');
    if (anc) return clean(txt(anc));
    return clean(el.getAttribute('aria-label') || el.value);
  }

  function groupQuestion(container, el) {
    const cand = Array.from(container.querySelectorAll('legend, label:not([for]), .application-label, [class*="question" i], [class*="title" i], [class*="label" i], h3, h4'))
      .find((n) => clean(txt(n)) && !n.querySelector('input'));
    if (cand) return stripStar(txt(cand));
    const up = nearestQuestion(container, container.querySelectorAll(CONTROLS).length);
    if (up) return stripStar(up);
    return question(el);
  }

  let fields = [];

  // Whether the site already put a value there (LinkedIn prefills from the profile).
  function isFilled(el, f) {
    const els = f._els || [el];
    switch (f.kind) {
      case 'text': case 'textarea': return !!(el.value || '').trim();
      case 'select': return el.selectedIndex > 0 && el.value !== '';
      case 'radioGroup': case 'checkboxGroup': return els.some((e) => e.checked);
      case 'checkbox': return !!el.checked;
      case 'combobox': {
        const C = window.__autofillCombobox;
        return !!(C && C.displayedValues(el).length);
      }
      case 'file': return !!(el.files && el.files.length);
      default: return false;
    }
  }

  function discover(root) {
    if (typeof root === 'string') root = document.querySelector(root);
    root = root || document;
    document.querySelectorAll(`[${IDX}]`).forEach((n) => n.removeAttribute(IDX));
    fields = [];
    const push = (el, f) => {
      el.setAttribute(IDX, String(fields.length));
      f.idx = fields.length;
      try { f.filled = isFilled(el, f); } catch (e) { f.filled = false; }
      fields.push({ el, ...f });
    };
    const combo = window.__autofillCombobox;
    const seenRadio = new Set();
    const cbGroups = new Map();
    const cbContainer = (cb) => {
      const explicit = cb.closest('[data-field-path], fieldset, [role="group"]');
      if (explicit) return explicit;
      // Lever: <li class="application-question"> <ul> <li><label><input type=checkbox>…
      for (let n = cb.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
        const boxes = n.querySelectorAll('input[type="checkbox"]').length;
        const others = n.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]), select, textarea').length;
        if (boxes >= 2 && others <= 2) {
          // climb to the widest such ancestor that still adds no new checkboxes (to reach the question label)
          let best = n;
          for (let m = n.parentElement, e = 0; m && e < 3; m = m.parentElement, e++) {
            if (m.querySelectorAll('input[type="checkbox"]').length !== boxes) break;
            best = m;
          }
          return best;
        }
        if (boxes >= 2) break;
      }
      return cb;
    };
    root.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      if (cb.disabled || !isVisible(cb) && !isVisible(cb.closest('label') || cb)) return;
      const c = cbContainer(cb);
      if (!cbGroups.has(c)) cbGroups.set(c, []);
      cbGroups.get(c).push(cb);
    });
    const doneGroups = new Set();
    root.querySelectorAll('input, textarea, select').forEach((el) => {
      if (el.disabled || el.readOnly) return;
      const type = (el.type || '').toLowerCase();
      if (type === 'file') {
        const q = question(el);
        push(el, { kind: 'file', question: q, signature: signature(el), required: isRequired(el, q), accept: el.accept || '' });
        return;
      }
      if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
      const lbl = el.closest('label');
      if (!isVisible(el) && !(lbl && isVisible(lbl))) return;
      const q = question(el);
      const base = { question: q, signature: signature(el), required: isRequired(el, q), name: el.name || '', type };
      if (type === 'password') { push(el, { ...base, kind: 'password' }); return; }
      if (el.tagName === 'TEXTAREA') { push(el, { ...base, kind: 'textarea', maxlength: el.maxLength > 0 ? el.maxLength : null }); return; }
      if (el.tagName === 'SELECT') {
        const opts = Array.from(el.options);
        push(el, { ...base, kind: 'select', options: opts.map((o) => clean(o.textContent)) });
        return;
      }
      if (type === 'checkbox') {
        const c = cbContainer(el);
        const g = cbGroups.get(c) || [el];
        if (g.length >= 2) {
          if (doneGroups.has(c)) return;
          doneGroups.add(c);
          const gq = groupQuestion(c, el);
          push(el, { ...base, kind: 'checkboxGroup', question: gq, signature: clean(gq + ' ' + signature(el)),
                     required: base.required || isRequired(c, gq), options: g.map(optionLabel), _els: g });
          return;
        }
        push(el, { ...base, kind: 'checkbox', question: stripStar(optionLabel(el)) || q });
        return;
      }
      if (type === 'radio') {
        if (!el.name || seenRadio.has(el.name)) return;
        seenRadio.add(el.name);
        const g = Array.from(root.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`));
        const c = el.closest('fieldset, [data-field-path], [role="radiogroup"], .application-question, .field') || el.parentElement;
        const gq = groupQuestion(c, el);
        push(el, { ...base, kind: 'radioGroup', question: gq, signature: clean(gq + ' ' + signature(el)),
                   required: base.required || g.some((r) => r.required), options: g.map(optionLabel), _els: g });
        return;
      }
      if (['text', 'email', 'tel', 'url', 'search', 'number', 'date', ''].includes(type)) {
        if (combo && combo.isComboWidget(el)) push(el, { ...base, kind: 'combobox' });
        else push(el, { ...base, kind: 'text', maxlength: el.maxLength > 0 ? el.maxLength : null });
      }
    });
    if (combo) {
      root.querySelectorAll(combo.COMBO_WIDGET).forEach((el) => {
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) || el.hasAttribute(IDX) || !isVisible(el)) return;
        const q = question(el);
        push(el, { kind: 'combobox', question: q, signature: signature(el), required: isRequired(el, q) });
      });
    }
    return fields.map(({ el, _els, ...f }) => f);
  }

  const get = (i) => fields[i] && fields[i].el;

  function setNative(el, value) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
      : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  }

  function labelFor(el) {
    if (el.id) { try { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l; } catch (e) { /* */ } }
    return el.closest('label');
  }
  function setChecked(el, want) {
    if (el.checked === want) return true;
    const l = labelFor(el);
    if (l) l.click();
    if (el.checked !== want) { try { el.click(); } catch (e) { /* */ } }
    if (el.checked !== want) {
      const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
      d.set.call(el, want);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return el.checked === want;
  }

  function apply(i, action) {
    const f = fields[i];
    if (!f) return 'missing';
    if (action.text !== undefined) { setNative(f.el, action.text); return 'ok'; }
    if (action.selectIndex !== undefined) { setNative(f.el, f.el.options[action.selectIndex].value); return 'ok'; }
    if (action.radioIndex !== undefined) return setChecked(f._els[action.radioIndex], true) ? 'ok' : 'did-not-stick';
    if (action.checked !== undefined) return setChecked(f.el, !!action.checked) ? 'ok' : 'did-not-stick';
    if (action.checkIndices) return action.checkIndices.every((k) => setChecked(f._els[k], true)) ? 'ok' : 'did-not-stick';
    return 'no-action';
  }

  async function comboOptions(i, search) {
    const C = window.__autofillCombobox;
    const w = get(i);
    if (!w) return [];
    const input = w.tagName === 'INPUT' ? w : w.querySelector('input');
    if (input) try { input.focus({ preventScroll: true }); } catch (e) { /* */ }
    if (!C.isOpen(w)) { C.press(w); await C.settle(() => C.isOpen(w)); }
    if (search && input) {
      setNative(input, search);
      await C.settle(() => C.comboOptionNodes(w).length > 0);
    }
    let nodes = C.comboOptionNodes(w);
    if (!nodes.length) nodes = Array.from(document.querySelectorAll('[role="option"]')).filter(C.isOnScreen);
    return nodes.map((n) => clean(n.textContent));
  }

  async function comboPick(i, wanted, search) {
    const C = window.__autofillCombobox;
    const w = get(i);
    if (!w) return 'missing';
    return C.fillCombobox(w, (texts) => {
      const t = texts.findIndex((x) => clean(x) === wanted);
      return t >= 0 ? t : null;
    }, search || null);
  }

  function pageState() {
    const html = document.documentElement.innerHTML.toLowerCase();
    const frames = Array.from(document.querySelectorAll('iframe')).map((f) => (f.src || '').toLowerCase());
    // A visible challenge, not just the invisible reCAPTCHA badge most forms load.
    const challenge = frames.some((s) => /recaptcha\/api2\/bframe|hcaptcha\.com\/.*challenge|challenges\.cloudflare\.com/.test(s) &&
      Array.from(document.querySelectorAll('iframe')).some((f) => f.src.toLowerCase() === s && isVisible(f)));
    const checkbox = frames.some((s) => /recaptcha\/api2\/anchor|hcaptcha\.com.*checkbox|challenges\.cloudflare\.com/.test(s));
    const password = Array.from(document.querySelectorAll('input[type="password"]')).some(isVisible);
    const text = clean(document.body ? document.body.innerText : '').slice(0, 4000);
    return { url: location.href, challenge, captchaWidget: checkbox, password, text, invisibleRecaptcha: html.includes('grecaptcha') };
  }

  function errorsShown() {
    const sel = '[aria-invalid="true"], .error, .field-error, [class*="error" i][role="alert"], [role="alert"]';
    return Array.from(document.querySelectorAll(sel)).filter(isVisible).map((n) => clean(n.textContent)).filter(Boolean).slice(0, 20);
  }

  window.__aa = { discover, apply, comboOptions, comboPick, pageState, errorsShown, IDX };
})();
