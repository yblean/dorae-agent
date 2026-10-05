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

  async function post(url, data) {
    const res = await fetch(url, { method: 'POST', headers: { 'X-Requested-With': 'fetch' }, body: data });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  // Start at the latest message
  if (thread) thread.scrollTop = thread.scrollHeight;

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
      }
    } catch {
      form.submit();  // fall back to a normal page load
    }
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
  const askForm = document.getElementById('ask');
  if (askForm) askForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = askForm.querySelector('input');
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

  // Sidebar search filters the agent list
  const search = document.querySelector('[data-search]');
  if (search) search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('.agent-row').forEach((row) => {
      row.hidden = q && !row.dataset.name.toLowerCase().includes(q);
    });
  });
})();
