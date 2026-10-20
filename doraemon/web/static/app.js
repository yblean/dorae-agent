// Chat behaviour. Without this script every button still works as a normal form.
(() => {
  const thread = document.getElementById('thread');           // the scrolling area
  const list = thread ? thread.querySelector('.thread-inner') || thread : null;  // the centred column messages go in
  const agent = thread ? thread.dataset.agent : null;
  const headMascot = document.querySelector('.chat-head [data-mascot]');
  let moodTimer;

  function setMood(mood) {
    if (!headMascot) return;
    headMascot.dataset.mood = mood;
    clearTimeout(moodTimer);
    if (mood === 'happy') moodTimer = setTimeout(() => setMood('idle'), 3500);
  }

  function scrollDown() {
    if (thread) thread.scrollTo({ top: thread.scrollHeight, behavior: 'smooth' });
  }

  // Server-rendered message HTML (email text is escaped on the server)
  function appendHtml(html) {
    if (!thread || !html) return;
    const tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    tpl.content.querySelectorAll('[data-msg]').forEach((m) => {  // already shown (background refresh got it first)
      if (list.querySelector(`[data-msg="${m.dataset.msg}"]`)) m.remove();
    });
    list.append(tpl.content);
    scrollDown();
  }

  function appendUser(text) {
    const msg = document.createElement('div');
    msg.className = 'msg from-you';
    const body = document.createElement('div');
    body.className = 'bubble';
    body.textContent = text;
    msg.append(body);
    list.append(msg);
    scrollDown();
    return msg;
  }

  function typing() {
    if (!thread) return () => {};
    const dots = document.createElement('div');
    dots.className = 'typing';
    dots.innerHTML = '<span class="dot-anim"></span><span class="dot-anim dot2"></span><span class="dot-anim dot3"></span>';
    list.append(dots);
    scrollDown();
    return () => dots.remove();
  }

  let busy = 0;  // requests in flight; their answers are appended by whoever asked
  async function post(url, data) {
    busy++;
    try {
      const res = await fetch(url, { method: 'POST', headers: { 'X-Requested-With': 'fetch' }, body: data });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } finally {
      busy--;
    }
  }

  // Messages posted in the background (the automatic inbox check) show up without a reload
  function lastId() {
    return Math.max(0, ...[...document.querySelectorAll('[data-msg]')].map((m) => +m.dataset.msg));
  }
  if (thread) setInterval(async () => {
    if (busy || document.hidden) return;
    try {
      const res = await (await fetch(`/chat/${agent}/new?after=${lastId()}`)).json();
      if (!busy && res.html.trim()) appendHtml(res.html);
    } catch {}
  }, 30000);

  // Start at the latest message, or at the one picked from search (?at=), lit up for a moment
  if (thread) {
    const at = new URLSearchParams(location.search).get('at');
    const picked = at && thread.querySelector(`[data-msg="${CSS.escape(at)}"]`);
    if (picked) {
      picked.scrollIntoView({ block: 'center' });
      picked.classList.add('found');
      setTimeout(() => picked.classList.remove('found'), 2600);
      history.replaceState(null, '', location.pathname);  // a reload goes back to the latest
    } else {
      thread.scrollTop = thread.scrollHeight;
    }
  }

  // Card actions (confirm, dismiss, edit, change category): the agent answers in the chat
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (!thread || !form.matches('form[data-ajax]')) return;
    e.preventDefault();
    const card = form.closest('.item');
    try {
      const res = await post(form.action, new FormData(form));
      appendHtml(res.html);
      setMood(res.mood || 'idle');
      if (res.remove && card) {
        card.classList.add('leaving');
        setTimeout(() => card.remove(), 250);
      } else if (res.card && card) {
        card.outerHTML = res.card;  // e.g. after an edit: the new type, date or buttons
      }
    } catch {
      form.submit();  // fall back to a normal page load
    }
  });

  // Type dropdown: "+ New type…" shows the name and "works like" fields
  document.addEventListener('change', (e) => {
    if (!e.target.matches('[data-type-select]')) return;
    const extra = e.target.closest('form').querySelector('[data-new-type]');
    const adding = e.target.value === '__new__';
    extra.hidden = !adding;
    if (adding) extra.querySelector('input').focus();
  });

  // Category dropdowns save as soon as you pick
  document.addEventListener('change', (e) => {
    const form = e.target.closest('form[data-autosubmit]');
    if (form) form.requestSubmit();
  });

  // Questions: suggestion chips and the message box
  async function ask(question) {
    const userMsg = appendUser(question);
    setMood('thinking');
    const done = typing();
    const data = new FormData();
    data.append('q', question);
    try {
      const res = await post(`/chat/${agent}/ask`, data);
      setTimeout(() => {
        done();
        userMsg.remove();  // the server's copy (with its time) replaces it
        appendHtml(res.html);
        setMood('idle');
      }, 350);
    } catch {
      done();
      appendHtml('<div class="msg from-agent"><div class="bubble">Sorry, I couldn\'t answer that just now.</div></div>');
      setMood('idle');
    }
  }
  document.querySelectorAll('[data-q]').forEach((chip) => chip.addEventListener('click', () => ask(chip.dataset.q)));

  // Dorae-3: a photo of a nutrition label or of your food (what's typed in the box goes with it, e.g. "2 servings")
  function shrink(file, most = 1280) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, most / Math.max(img.width, img.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(img.src);
        canvas.toBlob((blob) => resolve(blob || file), 'image/jpeg', 0.88);
      };
      img.onerror = () => resolve(file);
      img.src = URL.createObjectURL(file);
    });
  }
  const photoInput = document.getElementById('ask-photo');
  if (photoInput) photoInput.addEventListener('change', async () => {
    const file = photoInput.files[0];
    photoInput.value = '';
    if (!file) return;
    const input = document.getElementById('ask-q');
    const said = input.value.trim();
    input.value = '';
    const preview = document.createElement('div');
    preview.className = 'msg from-you';
    const img = document.createElement('img');
    img.className = 'sent-photo';
    img.alt = 'Your photo';
    img.src = URL.createObjectURL(file);
    preview.append(img);
    if (said) {
      const body = document.createElement('div');
      body.className = 'bubble';
      body.textContent = said;
      preview.append(body);
    }
    list.append(preview);
    scrollDown();
    setMood('thinking');
    const done = typing();
    const data = new FormData();
    data.append('photo', await shrink(file), 'photo.jpg');
    data.append('q', said);
    try {
      const res = await post(`/chat/${agent}/photo`, data);
      done();
      preview.remove();
      appendHtml(res.html);
    } catch {
      done();
      appendHtml('<div class="msg from-agent"><div class="bubble">Sorry, I couldn\'t read that photo just now.</div></div>');
    }
    setMood('idle');
  });
  const askForm = document.getElementById('ask');
  if (askForm) askForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = askForm.querySelector('input[name=q]');
    const q = input.value.trim();
    if (q) { input.value = ''; ask(q); }
  });

  // Routines: "Run now" checks the inbox in the background; results arrive as messages
  document.querySelectorAll('[data-check-now]').forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Checking…';
    setMood('thinking');
    const done = typing();
    try {
      await post('/check', new FormData());
      const poll = async () => {
        const status = await (await fetch('/check/status')).json();
        if (status.running) return setTimeout(poll, 2000);
        done();
        location.reload();  // shows Dorae-2's report and any new items
      };
      poll();
    } catch {
      done(); btn.disabled = false; btn.textContent = 'Run now'; setMood('idle');
    }
  }));

  // The agent's screen panel: hide/show on wide screens, slide over on narrow ones
  const panel = document.getElementById('panel');
  const shell = document.querySelector('.shell');
  const narrow = window.matchMedia('(max-width: 1180px)');
  try { if (localStorage.getItem('panelHidden') === '1' && !narrow.matches) shell.classList.add('panel-hidden'); } catch {}
  document.querySelectorAll('[data-panel-toggle]').forEach((b) => b.addEventListener('click', () => {
    if (!panel) return;
    if (narrow.matches) { panel.classList.toggle('open'); return; }
    const hidden = shell.classList.toggle('panel-hidden');
    try { localStorage.setItem('panelHidden', hidden ? '1' : '0'); } catch {}
  }));

  // "+" in the composer shows suggested questions
  const chips = document.getElementById('chips');
  const plus = document.querySelector('[data-toggle-chips]');
  if (plus && chips) {
    plus.setAttribute('aria-expanded', 'false');
    plus.addEventListener('click', () => {
      chips.hidden = !chips.hidden;
      plus.setAttribute('aria-expanded', String(!chips.hidden));
    });
    chips.addEventListener('click', () => { chips.hidden = true; plus.setAttribute('aria-expanded', 'false'); });
  }

  // Customize an agent: preview the colour straight away, enable Save once something changed
  const maker = document.querySelector('[data-maker]');
  if (maker) {
    const save = maker.querySelector('.maker-save');
    const nameInput = maker.querySelector('input[name="name"]');
    const startColor = (maker.querySelector('input[name="color"]:checked') || {}).value;
    const shades = (hex) => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
      const mix = (t, a) => '#' + [r, g, b].map((c) => Math.round(c + (t - c) * a).toString(16).padStart(2, '0')).join('');
      return [mix(255, .55), mix(255, .18), hex, mix(0, .3), mix(0, .65)];
    };
    const refresh = () => {
      const color = (maker.querySelector('input[name="color"]:checked') || {}).value;
      if (color) {
        const tones = shades(color);
        document.querySelectorAll('.maker [data-shade], .top-bar [data-shade]').forEach((el) => {
          const tone = tones[+el.dataset.shade];
          if (el.tagName.toLowerCase() === 'stop') el.setAttribute('stop-color', tone); else el.setAttribute('fill', tone);
        });
      }
      save.disabled = !nameInput.value.trim() || (nameInput.value.trim() === nameInput.dataset.original && color === startColor);
    };
    maker.addEventListener('input', refresh);
    maker.addEventListener('change', refresh);
  }

  // Charts: hover or focus a slice, column or meter for its exact numbers
  const tip = document.createElement('div');
  tip.className = 'viz-tip';
  tip.hidden = true;
  document.body.append(tip);
  const showTip = (el, x, y) => {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x - w / 2, window.innerWidth - w - 8)) + 'px';
    tip.style.top = (y - h - 12 < 8 ? y + 16 : y - h - 12) + 'px';
  };
  document.addEventListener('pointermove', (e) => {
    const el = e.target.closest && e.target.closest('.viz [data-tip]');
    if (el) showTip(el, e.clientX, e.clientY); else tip.hidden = true;
  });
  document.addEventListener('focusin', (e) => {
    const el = e.target.closest && e.target.closest('.viz [data-tip]');
    if (!el) { tip.hidden = true; return; }
    const r = el.getBoundingClientRect();
    showTip(el, r.left + r.width / 2, r.top);
  });
  document.addEventListener('focusout', () => { tip.hidden = true; });
  window.addEventListener('scroll', () => { tip.hidden = true; }, true);

  // Sidebar search: filters the agent list, and finds messages in your chats (GET /search) as you type.
  // Results are built with text nodes, so nothing in a message is ever read as HTML.
  const search = document.querySelector('[data-search]');
  const found = document.querySelector('[data-search-results]');
  let searchTimer;
  const showHits = (hits, q) => {
    found.replaceChildren();
    const head = document.createElement('div');
    head.className = 'search-head';
    head.textContent = hits.length ? `Messages (${hits.length}${hits.length === 30 ? '+' : ''})` : `No messages with “${q}”`;
    found.append(head);
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    for (const h of hits) {
      const a = document.createElement('a');
      a.className = 'search-hit';
      a.href = `/chat/${h.agent}?at=${h.id}`;
      const top = document.createElement('span');
      top.className = 'hit-top';
      const who = document.createElement('span');
      who.className = 'hit-who';
      who.style.setProperty('--c', h.color);
      who.textContent = h.you ? `You → ${h.name}` : h.name;
      const when = document.createElement('span');
      when.textContent = h.when;
      top.append(who, when);
      const text = document.createElement('span');
      text.className = 'hit-text';
      // mark each word typed, case-insensitively
      const lower = h.text.toLowerCase();
      let i = 0;
      while (i < h.text.length) {
        let next = -1, len = 0;
        for (const w of words) {
          const j = lower.indexOf(w, i);
          if (j >= 0 && (next < 0 || j < next)) { next = j; len = w.length; }
        }
        if (next < 0) { text.append(h.text.slice(i)); break; }
        text.append(h.text.slice(i, next));
        const mark = document.createElement('mark');
        mark.textContent = h.text.slice(next, next + len);
        text.append(mark);
        i = next + len;
      }
      a.append(top, text);
      found.append(a);
    }
    found.hidden = false;
  };
  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.trim();
      document.querySelectorAll('.agent-row').forEach((row) => {
        row.hidden = q && !row.dataset.name.toLowerCase().includes(q.toLowerCase());
      });
      clearTimeout(searchTimer);
      if (q.length < 2) { found.hidden = true; found.replaceChildren(); return; }
      searchTimer = setTimeout(async () => {
        try {
          const res = await (await fetch(`/search?q=${encodeURIComponent(q)}`)).json();
          if (search.value.trim() === q) showHits(res.results, q);
        } catch {}
      }, 200);
    });
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { search.value = ''; search.dispatchEvent(new Event('input')); }
    });
  }
})();
