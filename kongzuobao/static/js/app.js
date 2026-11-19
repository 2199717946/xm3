/* 空座宝 · 前端逻辑
   纯原生 JS，不依赖任何框架，方便后面往微信小程序移植（接口一一对应）。
*/
'use strict';

const USER = '我';
const POLL_MS = 5000;   // 地图/空位轮询间隔

const state = {
  view: 'home',
  buildingId: null,
  floorId: null,
  quiet: 0,
  people: 1,
  socketOnly: false,
  buildings: [],
  currentRoom: null,
  currentRoomMeta: null,
  currentSeats: [],
  selectedSeat: null,
  isMember: false,
  countdownTimer: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */
async function api(path, options) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, options));
  if (!res.ok && res.status >= 500) throw new Error('服务异常');
  return res.json();
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('is-on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('is-on'), 2200);
}

function hhmm(dtStr) {
  if (!dtStr) return '';
  const parts = String(dtStr).split(' ');
  return parts[1] ? parts[1].slice(0, 5) : '';
}

/* 分段条：只画非零段，避免留下空的 2px 缝隙 */
function renderMeter(el, stats) {
  if (!el || !stats) return;
  const segs = [
    ['seg-free', stats.free],
    ['seg-res', stats.reserved],
    ['seg-occ', stats.occupied],
  ].filter(([, v]) => v > 0);
  el.innerHTML = segs
    .map(([cls, v]) => `<span class="${cls}" style="flex:${v}"></span>`)
    .join('');
}

function riskBadge(room) {
  const cls = room.risk === 'low' ? 'risk-low' : room.risk === 'mid' ? 'risk-mid' : 'risk-high';
  return `<span class="risk-badge ${cls}"><i></i>${esc(room.risk_tip)}</span>`;
}

/* ------------------------------------------------------------------ */
/* 视图切换                                                            */
/* ------------------------------------------------------------------ */
function switchView(name) {
  state.view = name;
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('is-active', v.id === 'view-' + name);
  });
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('is-active', t.dataset.view === name);
  });
  $('scroll').scrollTop = 0;
  if (name === 'booking') loadReservations();
  if (name === 'mine') { loadMember(); loadReports(); }
}

/* ------------------------------------------------------------------ */
/* 找座：全局概览 + 推荐                                               */
/* ------------------------------------------------------------------ */
async function loadOverview() {
  const d = await api('/api/overview');
  $('ovFree').textContent = d.free;
  $('ovTotal').textContent = `/ ${d.total}`;

  const pct = Math.round(d.free_ratio * 100);
  const risk = d.free_ratio >= 0.35 ? 'low' : d.free_ratio >= 0.15 ? 'mid' : 'high';
  const cls = risk === 'low' ? 'risk-low' : risk === 'mid' ? 'risk-mid' : 'risk-high';
  const word = risk === 'low' ? '座位充足' : risk === 'mid' ? '还算好找' : '一座难求';
  $('ovRisk').innerHTML = `<span class="risk-badge ${cls}"><i></i>${word}</span>`;

  renderMeter($('ovMeter'), { free: d.free, reserved: 0, occupied: d.total - d.free });
  $('ovNote').textContent =
    `空位率 ${pct}% · 数据更新于 ${hhmm(d.server_time)} · 门禁与插座数据融合`;

  $('recList').innerHTML = d.recommend.map(roomCardHtml).join('');

  const hot = d.hot_reservations || [];
  if (hot.length) {
    $('recHint').textContent = '热门：' + hot.map((h) => h.room).join(' / ');
  }
}

function roomCardHtml(room) {
  const s = room.stats;
  const zero = s.free === 0 ? ' zero' : '';
  const tags = [];
  if (room.has_socket) tags.push('<span class="tag tag-socket">⚡ 有插座</span>');
  tags.push(`<span class="tag">${esc(room.quiet_label)}</span>`);
  room.tags.forEach((t) => tags.push(`<span class="tag">${esc(t)}</span>`));

  return `
    <article class="room-card" data-room="${room.id}">
      <div class="room-top">
        <div>
          <p class="room-name">${esc(room.name)}</p>
          <p class="room-loc">${esc(room.building)} · ${esc(room.floor)}</p>
        </div>
        ${riskBadge(room)}
      </div>
      <div class="room-stats">
        <span class="room-free${zero}">${s.free}</span>
        <span class="of">个空位 / 共 ${s.total} 座</span>
      </div>
      <div class="meter">${[
        ['seg-free', s.free], ['seg-res', s.reserved], ['seg-occ', s.occupied],
      ].filter(([, v]) => v > 0).map(([c, v]) => `<span class="${c}" style="flex:${v}"></span>`).join('')}</div>
      <div class="tags">${tags.join('')}</div>
    </article>`;
}

/* ------------------------------------------------------------------ */
/* 筛选与自习室列表                                                    */
/* ------------------------------------------------------------------ */
async function loadMeta() {
  const d = await api('/api/meta');
  state.buildings = d.buildings;

  $('buildingChips').innerHTML =
    `<button class="chip is-on" data-building="" type="button">全部</button>` +
    d.buildings.map((b) =>
      `<button class="chip" data-building="${b.id}" type="button">${esc(b.name)}` +
      `<span class="n">${b.free}</span></button>`).join('');

  renderFloorChips();
}

function renderFloorChips() {
  const b = state.buildings.find((x) => x.id === state.buildingId);
  const floors = b ? b.floors : [];
  if (!floors.length) {
    $('floorChips').innerHTML = `<button class="chip is-on" type="button">全部楼层</button>`;
    return;
  }
  $('floorChips').innerHTML =
    `<button class="chip is-on" data-floor="" type="button">全部</button>` +
    floors.map((f) => `<button class="chip" data-floor="${f.id}" type="button">${esc(f.name)}</button>`).join('');
}

async function loadRooms() {
  const p = new URLSearchParams();
  if (state.buildingId) p.set('building_id', state.buildingId);
  if (state.floorId) p.set('floor_id', state.floorId);
  if (state.socketOnly) p.set('socket', '1');
  if (state.quiet) p.set('quiet', state.quiet);
  if (state.people > 1) p.set('people', state.people);

  const d = await api('/api/rooms?' + p.toString());
  $('allCount').textContent = `${d.count} 间符合条件`;

  $('roomList').innerHTML = d.count
    ? d.rooms.map(roomCardHtml).join('')
    : `<div class="card empty-card"><p class="big">🔍</p><p>没有符合条件的自习室</p>
       <p class="muted">试试放宽筛选条件</p></div>`;
}

/* ------------------------------------------------------------------ */
/* 座位图                                                              */
/* ------------------------------------------------------------------ */
async function openRoom(roomId) {
  state.currentRoom = roomId;
  switchView('map');
  await loadSeatMap(roomId);
}

async function loadSeatMap(roomId, silent) {
  const d = await api('/api/rooms/' + roomId);
  if (d.error) { toast(d.error); return; }

  state.currentRoom = d.room.id;
  state.currentRoomMeta = d.room;   // 抽屉里要用楼栋楼层信息
  state.currentSeats = d.seats;

  $('mapRoomName').textContent = d.room.name;
  $('mapRoomMeta').textContent =
    `${d.room.building} · ${d.room.floor} · ${d.room.quiet_label}` +
    (d.room.has_socket ? ' · 有插座' : ' · 无插座');

  const s = d.room.stats;
  renderMeter($('mapMeter'), s);
  $('lgFree').textContent = s.free;
  $('lgRes').textContent = s.reserved;
  $('lgOcc').textContent = s.occupied;

  renderSeatMap(d.room, d.seats);
  if (!silent) $('liveText').textContent = '实时';
}

function renderSeatMap(room, seats) {
  const wrap = $('seatmap');
  $('mapEmpty').style.display = 'none';

  // 座位图上没有座位的位置（过道）后端不会返回，渲染成空格子
  wrap.style.gridTemplateColumns = `repeat(${room.grid_cols}, minmax(0, 1fr))`;
  wrap.style.maxWidth = Math.min(room.grid_cols * 38, 400) + 'px';

  const byPos = {};
  seats.forEach((s) => { byPos[s.row + '-' + s.col] = s; });

  let html = '';
  for (let r = 1; r <= room.grid_rows; r++) {
    for (let c = 1; c <= room.grid_cols; c++) {
      const s = byPos[r + '-' + c];
      if (!s) {
        html += `<div class="seat aisle"></div>`;
        continue;
      }
      const cls = s.status === 'free' ? 's-free' : s.status === 'reserved' ? 's-res' : 's-occ';
      const bolt = s.has_socket ? '<span class="bolt">⚡</span>' : '';
      html += `<button class="seat ${cls}" data-seat="${s.id}" type="button"
                 title="${esc(s.code)} · ${s.has_socket ? '有插座' : '无插座'}">${bolt}</button>`;
    }
  }
  wrap.innerHTML = html;
}

/* ------------------------------------------------------------------ */
/* 座位详情抽屉                                                        */
/* ------------------------------------------------------------------ */
function openSeatSheet(seatId) {
  const s = state.currentSeats.find((x) => x.id === seatId);
  if (!s) return;
  state.selectedSeat = s;

  const rm = state.currentRoomMeta || {};
  $('sheetSeat').textContent = s.code + ' 号座';
  $('sheetRoom').textContent = [rm.building, rm.floor, rm.name].filter(Boolean).join(' · ');
  $('sheetSocket').textContent = s.has_socket ? '有插座 ⚡' : '无插座';
  $('sheetSource').textContent = s.source;
  $('sheetUpdated').textContent = hhmm(s.updated_at) + (s.updated_at ? ':' + s.updated_at.slice(-2) : '');

  const map = {
    free: ['空闲', 'status-free'],
    reserved: ['已预约', 'status-res'],
    occupied: ['有人', 'status-occ'],
  };
  const [label, cls] = map[s.status] || ['未知', 'status-occ'];
  $('sheetStatus').textContent = label;
  $('sheetStatus').className = 'status-badge ' + cls;

  const btn = $('sheetReserve');
  if (s.status === 'free') {
    btn.disabled = false;
    btn.textContent = '预约这个座位';
    btn.style.opacity = 1;
  } else if (s.status === 'reserved') {
    btn.disabled = true;
    btn.textContent = '已被预约，换一个吧';
    btn.style.opacity = .55;
  } else {
    btn.disabled = !state.isMember;
    btn.textContent = state.isMember ? '会员优先：仍要预约' : '已有人，等它空出来';
    btn.style.opacity = state.isMember ? 1 : .55;
  }

  $('sheetMask').classList.add('is-open');
  $('seatSheet').classList.add('is-open');
}

function closeSheet() {
  $('sheetMask').classList.remove('is-open');
  $('seatSheet').classList.remove('is-open');
}

/* ------------------------------------------------------------------ */
/* 预约 / 提醒 / 上报                                                  */
/* ------------------------------------------------------------------ */
async function reserveSelected() {
  const s = state.selectedSeat;
  if (!s) return;
  const d = await api('/api/reserve', {
    method: 'POST',
    body: JSON.stringify({ seat_id: s.id, user: USER, minutes: state.isMember ? 240 : 120 }),
  });
  toast(d.msg || (d.ok ? '预约成功' : '预约失败'));
  if (d.ok) {
    closeSheet();
    await loadSeatMap(state.currentRoom, true);
    loadOverview();
    if (window.Notification && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }
}

async function reportSeat(claim) {
  const s = state.selectedSeat;
  if (!s) return;
  const d = await api('/api/report', {
    method: 'POST',
    body: JSON.stringify({ seat_id: s.id, user: USER, claim }),
  });
  toast(d.msg || '已上报');
  closeSheet();
  await loadSeatMap(state.currentRoom, true);
}

async function loadReservations() {
  const d = await api('/api/reservations?user=' + encodeURIComponent(USER));
  const list = d.reservations || [];

  $('bookingEmpty').style.display = list.length ? 'none' : 'block';
  $('bookingList').innerHTML = list.map((r) => `
    <article class="booking-card" data-res="${r.id}">
      <div class="booking-top">
        <div>
          <p class="booking-seat">${esc(r.seat_code)} 号座${r.has_socket ? ' ⚡' : ''}</p>
          <p class="booking-loc muted">${esc(r.building)} · ${esc(r.floor)} · ${esc(r.room)}</p>
        </div>
        <span class="countdown" data-end="${esc(r.end_at)}">剩余 --</span>
      </div>
      <p class="booking-time">${hhmm(r.start_at)} – ${hhmm(r.end_at)} · 提前 15 分钟提醒</p>
      <button class="btn btn-cancel" data-cancel="${r.id}" type="button">取消预约</button>
    </article>`).join('');

  tickCountdowns();
  clearInterval(state.countdownTimer);
  state.countdownTimer = setInterval(tickCountdowns, 1000);

  (d.reminders || []).forEach((r) => {
    const msg = `提醒：${r.room} ${r.seat_code} 号座已为你锁定，别被同学坐了`;
    toast(msg);
    if (window.Notification && Notification.permission === 'granted') {
      new Notification('空座宝', { body: msg });
    }
  });
}

function tickCountdowns() {
  const now = Date.now();
  document.querySelectorAll('.countdown').forEach((el) => {
    const end = new Date(el.dataset.end.replace(/-/g, '/')).getTime();
    const left = Math.max(0, Math.floor((end - now) / 1000));
    const h = String(Math.floor(left / 3600)).padStart(2, '0');
    const m = String(Math.floor((left % 3600) / 60)).padStart(2, '0');
    const s = String(left % 60).padStart(2, '0');
    el.textContent = left > 0 ? `剩余 ${h}:${m}:${s}` : '已到期';
  });
}

async function cancelReservation(id) {
  const d = await api(`/api/reservations/${id}/cancel`, { method: 'POST' });
  toast(d.msg || '已取消');
  loadReservations();
  if (state.currentRoom) loadSeatMap(state.currentRoom, true);
}

/* ------------------------------------------------------------------ */
/* 会员 / 上报记录                                                     */
/* ------------------------------------------------------------------ */
async function loadMember() {
  const d = await api('/api/member?user=' + encodeURIComponent(USER));
  state.isMember = d.is_member;
  $('memberState').textContent = d.is_member ? '已开通' : '未开通';
  $('memberState').className = 'pill' + (d.is_member ? ' is-on' : '');
  $('memberBtn').textContent = d.is_member ? '退订会员' : '开通会员（¥9.9/月）';
  $('memberBenefits').innerHTML = d.benefits.map((b) => `<li>${esc(b)}</li>`).join('');
}

async function toggleMember() {
  const plan = state.isMember ? 'free' : 'pro';
  const d = await api('/api/member', { method: 'POST', body: JSON.stringify({ user: USER, plan }) });
  toast(d.msg || '已更新');
  loadMember();
}

async function loadReports() {
  const d = await api('/api/reports');
  const list = d.reports || [];
  $('reportList').innerHTML = list.length
    ? list.map((r) => `
        <div class="report-item">
          <span>${esc(r.room)} ${esc(r.seat_code)} · ${r.claim === 'free' ? '其实空着' : '被占了'}</span>
          <span class="who">${esc(r.user_name)} ${hhmm(r.created_at)}</span>
        </div>`).join('')
    : '<p class="muted">还没有上报记录</p>';
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */
function bindEvents() {
  $('tabbar').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab) switchView(tab.dataset.view);
  });

  document.addEventListener('click', (e) => {
    const goto = e.target.closest('[data-goto]');
    if (goto) { switchView(goto.dataset.goto); return; }

    const room = e.target.closest('[data-room]');
    if (room) { openRoom(Number(room.dataset.room)); return; }

    const seat = e.target.closest('[data-seat]');
    if (seat) { openSeatSheet(Number(seat.dataset.seat)); return; }

    const cancel = e.target.closest('[data-cancel]');
    if (cancel) { cancelReservation(Number(cancel.dataset.cancel)); return; }

    const rep = e.target.closest('[data-report]');
    if (rep) { reportSeat(rep.dataset.report); return; }
  });

  // 筛选：楼栋
  $('buildingChips').addEventListener('click', (e) => {
    const c = e.target.closest('.chip');
    if (!c) return;
    $('buildingChips').querySelectorAll('.chip').forEach((x) => x.classList.remove('is-on'));
    c.classList.add('is-on');
    state.buildingId = c.dataset.building ? Number(c.dataset.building) : null;
    state.floorId = null;
    renderFloorChips();
    loadRooms();
  });

  // 筛选：楼层（"全部"这个 chip 没有 data-floor，取到 undefined 即视为不限）
  $('floorChips').addEventListener('click', (e) => {
    const c = e.target.closest('.chip');
    if (!c) return;
    $('floorChips').querySelectorAll('.chip').forEach((x) => x.classList.remove('is-on'));
    c.classList.add('is-on');
    state.floorId = c.dataset.floor ? Number(c.dataset.floor) : null;
    loadRooms();
  });

  // 筛选：安静程度
  $('quietChips').addEventListener('click', (e) => {
    const c = e.target.closest('.chip');
    if (!c) return;
    $('quietChips').querySelectorAll('.chip').forEach((x) => x.classList.remove('is-on'));
    c.classList.add('is-on');
    state.quiet = Number(c.dataset.quiet);
    loadRooms();
  });

  // 筛选：同行人数
  $('peopleChips').addEventListener('click', (e) => {
    const c = e.target.closest('.chip');
    if (!c) return;
    $('peopleChips').querySelectorAll('.chip').forEach((x) => x.classList.remove('is-on'));
    c.classList.add('is-on');
    state.people = Number(c.dataset.people);
    loadRooms();
  });

  $('socketOnly').addEventListener('change', (e) => {
    state.socketOnly = e.target.checked;
    loadRooms();
  });

  $('resetFilter').addEventListener('click', () => {
    state.buildingId = null; state.floorId = null;
    state.quiet = 0; state.people = 1; state.socketOnly = false;
    $('socketOnly').checked = false;
    $('buildingChips').querySelectorAll('.chip').forEach((x, i) => x.classList.toggle('is-on', i === 0));
    $('quietChips').querySelectorAll('.chip').forEach((x, i) => x.classList.toggle('is-on', i === 0));
    $('peopleChips').querySelectorAll('.chip').forEach((x, i) => x.classList.toggle('is-on', i === 0));
    renderFloorChips();
    loadRooms();
    toast('筛选已重置');
  });

  $('refreshMap').addEventListener('click', () => {
    if (!state.currentRoom) { toast('先去「找座」选一间自习室'); return; }
    loadSeatMap(state.currentRoom);
    toast('座位图已刷新');
  });

  $('sheetMask').addEventListener('click', closeSheet);
  $('sheetReserve').addEventListener('click', reserveSelected);
  $('memberBtn').addEventListener('click', toggleMember);
}

/* ------------------------------------------------------------------ */
/* 轮询：让地图看起来是"实时"的                                        */
/* ------------------------------------------------------------------ */
function startPolling() {
  setInterval(async () => {
    try {
      if (state.view === 'home') await loadOverview();
      else if (state.view === 'map' && state.currentRoom) await loadSeatMap(state.currentRoom, true);
      else if (state.view === 'booking') await loadReservations();
    } catch (err) {
      console.warn('轮询失败', err);
    }
  }, POLL_MS);
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
(async function init() {
  bindEvents();
  try {
    await Promise.all([loadMeta(), loadOverview(), loadMember()]);
    await loadRooms();
    // 首屏没选自习室时，自动进推荐第一间，省一次点击
    const first = document.querySelector('#recList [data-room]');
    if (first) await loadSeatMap(Number(first.dataset.room));
  } catch (err) {
    console.error(err);
    toast('加载失败，请刷新页面');
  }
  startPolling();
})();
