// Chat behaviour. Without this script every button still works as a normal form.
(() => {
  const thread = document.getElementById('thread');
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
    thread.append(tpl.content);
    scrollDown();
  }

  function appendUser(text) {
    const msg = document.createElement('div');
    msg.className = 'msg from-you';
    const body = document.createElement('div');
    body.className = 'bubble';
    body.textContent = text;
    msg.append(body);
    thread.append(msg);
    scrollDown();
    return msg;
  }

  function typing() {
    if (!thread) return () => {};
    const dots = document.createElement('div');
    dots.className = 'typing';
    dots.innerHTML = '<span class="dot-anim"></span><span class="dot-anim dot2"></span><span class="dot-anim dot3"></span>';
    thread.append(dots);
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

  // Overview panel on narrower screens
  const panel = document.getElementById('panel');
  document.querySelectorAll('[data-panel-toggle]').forEach((b) => b.addEventListener('click', () => panel && panel.classList.toggle('open')));

  // Sidebar search filters the agent list
  const search = document.querySelector('[data-search]');
  if (search) search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('.agent-row').forEach((row) => {
      row.hidden = q && !row.dataset.name.toLowerCase().includes(q);
    });
  });
})();
