const view = document.getElementById('view');
const status = document.getElementById('status');
const loader = document.getElementById('loader');
const bubble = document.getElementById('bubble');
const latest = document.getElementById('latest');
const latestWhen = document.getElementById('latestWhen');

const agoText = seconds => seconds < 90 ? Math.round(seconds) + 's ago'
    : seconds < 5400 ? Math.round(seconds / 60) + 'm ago'
    : Math.round(seconds / 3600) + 'h ago';

const setStatus = (text, stale) => {
  status.textContent = text;
  status.className = stale ? 'stale' : '';
};

class PauseToggle {
  constructor(id, name, urls, onChange = () => {}) {
    this.button = document.getElementById(id);
    this.name = name;
    this.urls = urls;
    this.onChange = onChange;
    this.paused = false;
    this.button.addEventListener('click', () => this.toggle());
  }

  get busy() { return this.button.disabled; }

  sync(paused) {
    if (this.busy || paused === this.paused) return;
    this.paused = paused;
    this.render();
  }

  render() {
    this.button.textContent = (this.paused ? 'Resume ' : 'Pause ') + this.name;
    this.button.classList.toggle('paused', this.paused);
  }

  async toggle() {
    this.button.disabled = true;
    this.button.textContent = this.paused ? 'Resuming…' : 'Pausing…';
    try {
      const response = await fetch(this.paused ? this.urls.resume : this.urls.pause, {method: 'POST'});
      if (response.ok) {
        this.paused = (await response.json()).paused;
        this.onChange(this.paused);
      }
    } finally {
      this.button.disabled = false;
      this.render();
    }
  }
}

const eyes = new PauseToggle('pause', 'Eyes', {pause: '/pause', resume: '/resume'}, paused => {
  if (paused) return;
  view.hidden = true;
  loader.hidden = false;
  loader.textContent = 'reopening the eyes…';
});
const movement = new PauseToggle('movement', 'Movement', {pause: '/ptz/pause', resume: '/ptz/resume'});

let lastStatus = null;

const showLiveFrame = async response => {
  const age = parseFloat(response.headers.get('X-Frame-Age') || '0');
  const previous = view.src;
  view.src = URL.createObjectURL(await response.blob());
  if (previous) URL.revokeObjectURL(previous);
  view.hidden = false;
  view.classList.remove('expired');
  loader.hidden = true;
  setStatus('live · frame ' + age.toFixed(1) + 's old', false);
};

const showUnavailable = async response => {
  const info = await response.json().catch(() => ({error: 'HTTP ' + response.status}));
  const frame = lastStatus && lastStatus.frame;
  if (frame && frame.expired && !view.hidden) {
    view.classList.add('expired');
    setStatus('frame expired — camera offline? last frame ' + agoText(frame.age), true);
  } else {
    setStatus(info.error, true);
  }
};

setInterval(async () => {
  if (eyes.paused) {
    view.classList.add('expired');
    setStatus('eyes paused — nothing is being captured', true);
    return;
  }
  try {
    const response = await fetch('/frame.jpg?t=' + Date.now(), {cache: 'no-store'});
    await (response.ok ? showLiveFrame(response) : showUnavailable(response));
  } catch (e) {
    setStatus('no frames yet (' + e.message + ')', true);
  }
}, 500);

const activity = document.getElementById('activity');
const activityText = document.getElementById('activityText');
setInterval(async () => {
  try {
    lastStatus = await (await fetch('/status', {cache: 'no-store'})).json();
    eyes.sync(lastStatus.paused);
    movement.sync(lastStatus.movement_paused);
    const seconds = lastStatus.activity ? Date.now() / 1000 - lastStatus.activity.at : 999;
    if (lastStatus.activity && seconds < 120) {
      activity.hidden = false;
      activityText.textContent = 'Claude: ' + lastStatus.activity.text + ' (' + agoText(seconds) + ')';
    } else {
      activity.hidden = true;
    }
  } catch (e) {}
}, 500);

const ptz = document.getElementById('ptz');
const ptzText = document.getElementById('ptzText');
const recenter = document.getElementById('recenter');
let ptzSupported = null;
let recentering = false;
const showPtz = data => {
  ptzSupported = data.supported;
  if (!data.supported) return;
  ptz.hidden = false;
  movement.sync(data.paused);
  recenter.disabled = recentering || data.paused;
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
  recentering = true;
  recenter.disabled = true;
  try {
    const response = await fetch('/ptz', {method: 'POST', body: JSON.stringify({recenter: true})});
    if (response.ok) showPtz(await response.json());
  } finally {
    recentering = false;
    recenter.disabled = movement.paused;
  }
});

setInterval(async () => {
  try {
    const data = await (await fetch('/observations', {cache: 'no-store'})).json();
    const entries = data.observations;
    if (!entries.length) {
      bubble.hidden = true;
      return;
    }
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
