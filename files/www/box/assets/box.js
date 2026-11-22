/* Shared helpers for EmergencyBox pages. */
window.BOX = (() => {
  // on the box itself (its name, its hotspot address, or any private address), not a preview
  const onBox = ['{{ADDRESS}}', '{{HOSTNAME}}', '{{HOTSPOT_IP}}', 'localhost'].includes(location.hostname)
    || /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/.test(location.hostname);
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const decode = s => { const t = document.createElement('textarea'); t.innerHTML = s ?? ''; return t.value; };
  // keep only <b> from a server snippet, escape everything else
  const snippet = s => esc(decode(s)).replace(/&lt;(\/?)b&gt;/g, '<$1b>');
  const xml = async url => {
    const r = await fetch(url, {cache: 'no-store'});
    const doc = new DOMParser().parseFromString(await r.text(), 'application/xml');
    if (!r.ok || doc.getElementsByTagName('parsererror').length) throw new Error('bad response ' + r.status);
    return doc;
  };
  const kids = (el, name) => [...el.children].filter(c => c.localName === name);
  const kid = (el, name) => kids(el, name)[0];
  const txt = (el, name) => (kid(el, name)?.textContent || '').trim();
  const params = () => new URLSearchParams(location.search);
  const go = (q, replace) => { const u = location.pathname + (q.toString() ? '?' + q : ''); history[replace ? 'replaceState' : 'pushState']({}, '', u); };
  const store = {
    get: k => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };
  return {onBox, $, esc, decode, snippet, xml, kids, kid, txt, params, go, store};
})();
