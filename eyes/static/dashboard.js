const view = document.getElementById('view');
const status = document.getElementById('status');
const bubble = document.getElementById('bubble');
const latest = document.getElementById('latest');
const latestWhen = document.getElementById('latestWhen');
const pause = document.getElementById('pause');
const loader = document.getElementById('loader');

let paused = false;
pause.addEventListener('click', async () => {
  pause.disabled = true;
  pause.textContent = paused ? 'Resuming…' : 'Pausing…';
  try {
    const data = await (await fetch(paused ? '/resume' : '/pause', {method: 'POST'})).json();
    paused = data.paused;
    if (!paused) {
      view.hidden = true;
      loader.hidden = false;
      loader.textContent = 'reopening the eyes…';
    }
  } finally {
    pause.disabled = false;
    pause.textContent = paused ? 'Resume Eyes' : 'Pause Eyes';
    pause.className = paused ? 'paused' : '';
  }
});

const agoText = seconds => seconds < 90 ? Math.round(seconds) + 's ago'
    : seconds < 5400 ? Math.round(seconds / 60) + 'm ago'
    : Math.round(seconds / 3600) + 'h ago';

setInterval(async () => {
  if (paused) {
    status.textContent = 'eyes paused — nothing is being captured';
    status.className = 'stale';
    return;
  }
  try {
    const response = await fetch('/frame.jpg?t=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error(response.status);
    const age = parseFloat(response.headers.get('X-Frame-Age') || '0');
    view.src = URL.createObjectURL(await response.blob());
    view.hidden = false;
    loader.hidden = true;
    status.textContent = age > 10
      ? 'camera offline? last frame ' + Math.round(age) + 's old'
      : 'live · frame ' + age.toFixed(1) + 's old';
    status.className = age > 10 ? 'stale' : '';
  } catch (e) {
    status.textContent = 'no frames yet (' + e.message + ')';
    status.className = 'stale';
  }
}, 500);

const activity = document.getElementById('activity');
const activityText = document.getElementById('activityText');
setInterval(async () => {
  try {
    const data = await (await fetch('/status', {cache: 'no-store'})).json();
    const seconds = data.activity ? Date.now() / 1000 - data.activity.at : 999;
    if (data.activity && seconds < 120) {
      activity.hidden = false;
      activityText.textContent = 'Claude: ' + data.activity.text + ' (' + agoText(seconds) + ')';
    } else {
      activity.hidden = true;
    }
  } catch (e) {}
}, 500);

const ptz = document.getElementById('ptz');
const ptzText = document.getElementById('ptzText');
const recenter = document.getElementById('recenter');
let ptzSupported = null;
const showPtz = data => {
  ptzSupported = data.supported;
  if (!data.supported) return;
  ptz.hidden = false;
  const pose = data.commanded || {};
  const axis = (name, unit) => name in pose ? pose[name] + unit : '?';
  ptzText.textContent = 'gimbal: pan ' + axis('pan', '°') + ' · tilt '
      + axis('tilt', '°') + ' · zoom ' + axis('zoom', '');
};
setInterval(async () => {
  if (ptzSupported === false) return;
  try { showPtz(await (await fetch('/ptz', {cache: 'no-store'})).json()); } catch (e) {}
}, 2000);
recenter.addEventListener('click', async () => {
  recenter.disabled = true;
  try {
    const response = await fetch('/ptz', {method: 'POST', body: JSON.stringify({recenter: true})});
    showPtz(await response.json());
  } finally { recenter.disabled = false; }
});

setInterval(async () => {
  try {
    const data = await (await fetch('/observations', {cache: 'no-store'})).json();
    const entries = data.observations;
    if (!entries.length) return;
    bubble.hidden = false;
    latest.textContent = entries[0].text;
    latestWhen.textContent = agoText(Date.now() / 1000 - entries[0].at);
  } catch (e) {}
}, 1000);

const auditList = document.getElementById('auditList');
const auditCount = document.getElementById('auditCount');
let auditSnapshot = '';
setInterval(async () => {
  try {
    const body = await (await fetch('/events', {cache: 'no-store'})).text();
    if (body === auditSnapshot) return;
    auditSnapshot = body;
    const events = JSON.parse(body).events;
    const scrolled = auditList.scrollTop;
    auditList.replaceChildren(...events.map(event => {
      const item = document.createElement('li');
      const when = document.createElement('span');
      when.className = 't';
      when.textContent = new Date(event.at * 1000).toLocaleTimeString([], {hour12: false});
      const kind = document.createElement('span');
      kind.className = 'k ' + event.kind;
      kind.textContent = event.kind;
      const text = document.createElement('span');
      text.className = 'x';
      text.textContent = event.text;
      if (event.count > 1) {
        const times = document.createElement('span');
        times.className = 'n';
        times.textContent = ' (×' + event.count + ')';
        text.appendChild(times);
      }
      item.append(when, kind, text);
      return item;
    }));
    auditList.scrollTop = scrolled;
    auditCount.textContent = events.length + (events.length === 500 ? ' (cap)' : '');
  } catch (e) {}
}, 1000);
