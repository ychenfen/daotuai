/* 短剧工作台前端：无构建，原生 JS。页面：首页 / 剧本 / 资产 / 分镜 / 站位图 / 提示词 / 视频 / 成片 / 对话 / 教程 */
'use strict';
const S = {page: 'home', pid: null, p: null, list: [], models: [], health: null, ep: null, timer: null,
  assetKind: 'all', assetQ: '', sceneId: null, sceneFilter: 'all', shotId: null, sel: new Set(), chatSid: null,
  notif: {items: [], unread: 0}, notifOpen: false, vp: {}, voices: []};
const PAGES = [['home', '⌂', '首页'], ['script', '✎', '剧本'], ['assets', '▣', '资产'], ['board', '▤', '分镜'],
  ['blocking', '⌖', '站位图'], ['prompts', '❝', '提示词'], ['videos', '▶', '视频'], ['edit', '✂', '剪辑'], ['final', '🎬', '成片'],
  ['chat', '💬', '对话'], ['help', '?', '教程']];
const NEED_PROJECT = new Set(['script', 'assets', 'board', 'blocking', 'prompts', 'videos', 'edit', 'final']);
const KIND = {character: '角色', scene: '场景', prop: '道具', creature: '生物', audio: '音频'};
const STATUS = {draft: '草稿', script_ready: '剧本就绪', parsed: '已解析', assets_ready: '资产就绪', storyboard_done: '分镜完成',
  prompts_done: '提示词完成', videos_running: '出片中', videos_done: '视频完成', final_ready: '成片完成',
  storyboard_ready: '待提示词', prompt_ready: '待出片', video_submitted: '生成中', video_done: '已出片', video_failed: '失败',
  submitted: '已提交', running: '生成中', succeeded: '成功', failed: '失败', cancelled: '已取消', queued: '排队'};
const MODE = {auto: '自动', reference: '全能参考', first_frame: '首帧参考', chain: '接上一镜尾帧', text: '文生视频'};
const JOB_MODE = {...MODE, video_edit: '视频编辑', motion_transfer: '动作迁移'};
const DUB = {off: '保留视频原声', mix: '配音为主 + 原声垫底', replace: '只用配音（替换原声）'};
const QC = {pass: ['ok', '合格'], warn: ['warn', '待看'], fail: ['bad', '不合格']};
const qcBadge = q => q ? `<span class="badge ${QC[q.verdict][0]}" title="${esc(q.summary)}">质检 ${q.score} · ${QC[q.verdict][1]}</span>` : '';
const voiceName = id => (S.voices.find(v => v.id === id) || {label: id}).label;
const voiceOpts = cur => S.voices.map(v => `<option value="${esc(v.id)}" ${v.id === cur ? 'selected' : ''}>${esc(v.label)}</option>`).join('');
const TOOLS = [['shot', '分镜视频'], ['video_edit', '视频编辑 · Omni Flash'], ['motion_transfer', '动作迁移 · Wan 2.2']];

// ---------------- 工具 ----------------
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const rmb = v => '¥' + (Number(v) || 0).toFixed(2);
function toast(msg, bad) {
  const d = document.createElement('div'); if (bad) d.className = 'bad'; d.textContent = msg;
  $('#toast').appendChild(d); setTimeout(() => d.remove(), bad ? 7000 : 3200);
}
function badge(st) {
  const cls = {succeeded: 'ok', video_done: 'ok', final_ready: 'ok', videos_done: 'ok', failed: 'bad', video_failed: 'bad',
    running: 'warn', submitted: 'warn', video_submitted: 'warn', videos_running: 'warn', queued: 'warn'}[st] || '';
  return `<span class="badge ${cls}">${esc(STATUS[st] || st)}</span>`;
}
const isLive = id => !!(S.models.find(m => m.id === id) || {}).live;
const model = id => S.models.find(m => m.id === id) || {};
// 挂在子路径下（例：https://stock.yuchenxu.cn/studio/ui/）时，接口、下载链接和 /media 素材都要带上前缀；
// 本机直接开 /ui/ 时 BASE 为空，行为不变。
const BASE = location.pathname.replace(/\/ui(\/.*)?$/, '').replace(/\/+$/, '');
function withBase(x) {
  if (!BASE) return x;
  if (typeof x === 'string') return x.startsWith('/media/') ? BASE + x : x;
  if (Array.isArray(x)) return x.map(withBase);
  if (x && typeof x === 'object') { for (const k in x) x[k] = withBase(x[k]); }
  return x;
}
async function api(method, path, body, raw) {
  const opt = {method, headers: {}};
  if (raw) opt.body = raw;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(BASE + path, opt);
  let data = null; try { data = withBase(await r.json()); } catch (e) { /* 非 JSON */ }
  if (!r.ok) {
    const err = new Error((data && data.error && data.error.message) || (data && data.detail) || ('HTTP ' + r.status));
    err.status = r.status; err.data = data; throw err;
  }
  return data;
}
async function act(btn, fn, okMsg) {
  if (btn) { btn.disabled = true; btn.dataset.t = btn.textContent; btn.textContent = '处理中…'; }
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); await load(); return r; }
  catch (e) {
    toast(e.message, true);
    if (e.status === 409 && e.data && e.data.error && e.data.error.detail && e.data.error.detail.conflict_fields) showConflict(e.data.error);
    await load();
  } finally { if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = btn.dataset.t; } }
}
function modal(html, narrow) {
  const m = document.createElement('div'); m.className = 'modal';
  m.innerHTML = `<div class="card ${narrow ? 'narrow' : ''}">${html}</div>`;
  m.addEventListener('click', e => { if (e.target === m) m.remove(); });
  $('#modalRoot').appendChild(m); return m;
}
const closeModal = el => el.closest('.modal').remove();
function pickFile(accept, multiple) {
  return new Promise(res => {
    const i = document.createElement('input'); i.type = 'file'; i.accept = accept || ''; i.multiple = !!multiple;
    i.onchange = () => res([...i.files]); i.click();
  });
}
function confirmCost(models, what) {
  const live = models.filter(isLive);
  return !live.length || confirm(`${what}会调用真实模型并扣费：${live.join('、')}。继续？`);
}
function go(page, pid) {
  const mr = document.getElementById('modalRoot'); if (mr) mr.innerHTML = '';   // 换页时关掉弹窗
  if (page === 'chat') S.chat = null;
  if (pid !== undefined) S.pid = pid;
  if (NEED_PROJECT.has(page) && !S.pid && !S.list.length) {
    toast('还没有项目：先在首页填项目名，点“新建项目”', true); page = 'home';
    setTimeout(() => { const n = document.getElementById('npName'); if (n) n.focus(); }, 300);
  }
  S.page = page; location.hash = `#/${page}${S.pid ? '/' + S.pid : ''}`; load();
}
const eps = () => (S.p ? S.p.episodes : []);
function curEp() {
  const list = eps(); if (!list.length) return null;
  if (!list.some(e => e.no === S.ep)) S.ep = list[0].no;
  return list.find(e => e.no === S.ep);
}
function epSelect(onchange = 'S.ep=+this.value;render()') {
  return `<select style="width:auto" onchange="${onchange}">${eps().map(e => `<option value="${e.no}" ${e.no === S.ep ? 'selected' : ''}>
    第${e.no}集 · ${e.shots} 卡 · ${e.videos_done} 成片</option>`).join('')}</select>`;
}

// ---------------- 加载与外框 ----------------
async function load() {
  try {
    [S.list, S.notif] = await Promise.all([api('GET', '/projects').then(r => r.items), api('GET', '/notifications?limit=30')]);
    if (S.pid && !S.list.some(p => p.id === S.pid)) S.pid = null;
    if (!S.pid && NEED_PROJECT.has(S.page) && S.list.length) S.pid = S.list[0].id;
    S.p = S.pid ? await api('GET', '/projects/' + S.pid) : null;
  } catch (e) { toast(e.message, true); }
  const a = document.activeElement;
  const typing = a && ['TEXTAREA', 'INPUT'].includes(a.tagName) && a.type !== 'checkbox' && $('#page').contains(a);
  renderChrome();
  if (!typing) render();
  clearTimeout(S.timer);
  const t = S.p && S.p.task, active = t && (t.status === 'running' || t.status === 'queued');
  const jobs = S.p && S.p.shots.some(s => s.jobs.some(j => j.status === 'submitted' || j.status === 'running'));
  if (active || jobs) S.timer = setTimeout(load, active ? 2000 : 8000);
}
function renderChrome() {
  $('#rail').innerHTML = `<div class="logo">剧</div>` + PAGES.map(([k, ic, t]) =>
    `<div class="nav ${S.page === k ? 'on' : ''}" title="${NEED_PROJECT.has(k) && !S.pid ? '先在首页新建或选择一个项目' : t}" onclick="go('${k}')"><span class="ic">${ic}</span>${t}</div>`).join('')
    + `<div class="ver">v0.3</div>`;
  const p = S.p, sp = p && p.spend, t = p && p.task, busy = t && (t.status === 'running' || t.status === 'queued');
  const pct = sp ? (sp.budget_rmb > 0 ? Math.min(100, sp.total_rmb / sp.budget_rmb * 100) : 100) : 0;
  $('#top').innerHTML = `<span class="title">${esc((PAGES.find(x => x[0] === S.page) || [])[2] || '')}</span>
    ${sp ? `<span class="pill" title="视频 ${rmb(sp.videos_rmb)} · 图片 ${rmb(sp.images_rmb)} · 文本 ${rmb(sp.text_rmb || 0)} · 配音 ${rmb(sp.audio_rmb || 0)}（超出预算的出片、出图、配音不会提交）">已花 <b>${rmb(sp.total_rmb)}</b> / ${rmb(sp.budget_rmb)}
      <span class="meter"><div class="${pct >= 100 ? 'over' : ''}" style="width:${pct}%"></div></span></span>` : ''}
    ${t ? `<span class="pill" title="${esc(t.error || '')}">任务 ${badge(t.status)} ${(t.steps || []).filter(s => s.status === 'succeeded').length}/${(t.steps || []).length || '…'}</span>` : ''}
    <span class="spacer"></span>
    <span class="rel"><button class="ghost" onclick="toggleNotif()">🔔 ${S.notif.unread ? `<span class="badge bad">${S.notif.unread}</span>` : ''}</button>
      ${S.notifOpen ? notifPanel() : ''}</span>
    <select style="width:200px" onchange="go(S.page==='home'?'script':S.page, this.value||null)">
      <option value="">选择项目…</option>${S.list.map(x => `<option value="${x.id}" ${x.id === S.pid ? 'selected' : ''}>${esc(x.name)} · ${x.episodes} 集</option>`).join('')}</select>
    ${p ? `<button onclick="settingsModal()">生成设置</button>
      <button class="primary" ${busy ? 'disabled' : ''} onclick="runAll(this)">${busy ? '一键成片运行中…' : '一键成片'}</button>` : ''}
    <button class="ghost" title="切换深浅色" onclick="toggleTheme()">◐</button>`;
}
function notifPanel() {
  return `<div class="card drop"><div class="row"><b>通知中心</b><span class="spacer"></span>
    <button class="small" onclick="act(this,()=>api('POST','/notifications/read',{}),'已全部标为已读')">全部已读</button></div>
    ${S.notif.items.map(n => `<div class="notif ${n.read ? '' : 'unread'}"><b class="${n.level === 'error' ? 'err' : n.level === 'success' ? 'ok' : n.level === 'warning' ? 'warnc' : ''}">${esc(n.title)}</b>
      <div class="small muted">${esc(n.project_name)} · ${esc(n.created_at)} UTC</div>${n.body ? `<div class="small">${esc(n.body)}</div>` : ''}</div>`).join('') || '<div class="muted">暂无通知</div>'}</div>`;
}
function toggleNotif() { S.notifOpen = !S.notifOpen; renderChrome(); }
function toggleTheme() {
  const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = cur; try { localStorage.setItem('theme', cur); } catch (e) { /* 忽略 */ }
}
function render() {
  const fn = {home: pgHome, script: pgScript, assets: pgAssets, board: pgBoard, blocking: pgBlocking, prompts: pgPrompts,
    videos: pgVideos, edit: pgEdit, final: pgFinal, chat: pgChat, help: pgHelp}[S.page] || pgHome;
  if (NEED_PROJECT.has(S.page) && !S.p) { $('#page').innerHTML = `<div class="empty">先新建或选择一个项目<br><br><button class="primary" onclick="go('home')">去首页新建项目</button></div>`; return; }
  $('#page').innerHTML = fn();
  if (S.page === 'chat') { const m = $('#msgs'); if (m) m.scrollTop = m.scrollHeight; }
}
async function runAll(btn) {
  const st = S.p.settings;
  if (!S.p.script_text.trim()) { toast('先写剧本', true); go('script'); return; }
  if (!confirmCost([st.text_model, st.image_model, st.video_model], '一键成片')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/run`), '已在后台开始，进度在顶部和通知中心');
}

// ---------------- 首页 ----------------
function pgHome() {
  return `<div class="grid">
    <div class="card"><h3>新建项目</h3><div class="kv"><span>项目名</span><input id="npName" placeholder="比如：妖王">
      <span>风格</span><select id="npStyle">${['真人', '3D', '2D 动漫', '国漫', '写实电影'].map(s => `<option>${s}</option>`).join('')}</select></div>
      <div class="row" style="margin-top:12px"><button class="primary" onclick="createProject(this)">＋ 新建项目</button>
      <button onclick="importProject(this)">⇧ 上传项目</button></div></div>
    ${S.list.map(projCard).join('')}</div>
  <div class="hero">
    <div class="card"><div class="muted small">完整制作流程</div><h2>一键成片</h2><div class="muted">从剧本进入 AI 制作工作流：资产 → 分镜 → 站位 → 提示词 → 视频 → 成片。</div>
      <button class="primary" style="margin-top:14px" onclick="S.pid?go('script'):toast('先新建或选择项目',true)">开始制作 →</button></div>
    <div class="card"><div class="muted small">独立创作模块</div><h2>一键剧本</h2><div class="muted">从小说、故事或一句灵感，创作分集剧本。</div>
      <button class="primary" style="margin-top:14px" onclick="scriptGenModal()">开始创作 ↗</button></div></div>
  <div class="card" id="storageBox"><div class="row"><h3 style="margin:0">存储与运行状态</h3><span class="spacer"></span><button class="small" onclick="reconcileModal()">账单对账</button><button class="small" onclick="loadStorage()">查看</button></div>
    <div class="small muted">各项目素材占用、磁盘余量、数据库备份；可清理没选用的旧视频版本。</div></div>`;
}
const mb = b => (b / 1048576).toFixed(b > 1073741824 ? 0 : 1) + ' MB';
async function loadStorage() {
  const box = $('#storageBox'); if (!box) return;
  try {
    const [s, o] = await Promise.all([api('GET', '/api/storage'), api('GET', '/api/ops')]);
    const d = s.disk || {};
    box.innerHTML = `<div class="row"><h3 style="margin:0">存储与运行状态</h3><span class="spacer"></span><button class="small" onclick="reconcileModal()">账单对账</button><button class="small" onclick="loadStorage()">刷新</button></div>
      <div class="row small" style="margin-top:6px"><span class="badge ${d.used_pct > 85 ? 'bad' : d.used_pct > 70 ? 'warn' : 'ok'}">磁盘已用 ${d.used_pct ?? '?'}% · 剩 ${d.free ? mb(d.free) : '?'}</span>
        <span class="badge">素材共 ${mb(s.total_bytes)}</span><span class="badge">在跑视频 ${o.running_jobs}</span><span class="badge">后台任务 ${o.running_tasks}</span>
        <span class="badge">备份 ${o.backups} 份${o.last_backup ? ' · 最近 ' + esc(o.last_backup) : ''}</span><span class="badge">已运行 ${(o.uptime_s / 3600).toFixed(1)} 小时</span>
        ${o.revision ? `<span class="badge">版本 ${esc(o.revision)}</span>` : ''}${o.daily_backups ? `<span class="badge">每日备份 ${o.daily_backups} 天 · 最近 ${esc(o.last_daily)}</span>` : ''}
        ${o.update ? `<span class="badge ${({updated: 'ok', up_to_date: 'ok', tests_failed: 'bad', rolled_back: 'bad'})[o.update.result] || 'warn'}" title="${esc(o.update.detail)}">自动更新：${esc(({updated: '刚更新', up_to_date: '已是最新', tests_failed: '新版测试没过，未更新', rolled_back: '新版启动失败，已回滚', no_access: '等待添加部署公钥', no_key: '未配置', fetch_failed: '拉取失败'})[o.update.result] || o.update.result)} · ${esc(o.update.at)}</span>` : ''}</div>
      <table class="small" style="margin-top:8px;width:100%"><tr class="muted"><td>项目</td><td>占用</td><td>明细</td><td></td></tr>
      ${s.projects.map(p => `<tr><td>${esc(p.name)}</td><td>${mb(p.bytes)}</td><td class="muted">${Object.entries(p.parts).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => `${esc(k)} ${mb(v)}`).join(' · ')}</td>
        <td>${p.orphan ? '' : `<button class="small" onclick="cleanupProj('${p.project}','${esc(p.name)}',this)">清理旧版本</button>`}</td></tr>`).join('')}</table>`;
  } catch (e) { toast(e.message, true); }
}
function reconcileModal() {
  modal(`<div class="row"><h3 style="margin:0">账单对账</h3><span class="spacer"></span><button onclick="closeModal(this)">✕</button></div>
    <div class="small muted" style="margin-top:6px">从费用中心导出账单明细 CSV（阿里云：费用与成本 → 账单详情 → 导出；火山引擎：费用中心 → 账单明细 → 导出；DeepSeek：用量信息 → 导出），
      按天和工作台按官方单价估算的成本对比。只统计 AI 相关的行（ECS 等其他产品自动跳过）。</div>
    <div class="row" style="margin-top:10px"><select id="rcAcc"><option value="aliyun">阿里云（百炼）</option><option value="volcengine">火山引擎（方舟）</option><option value="deepseek">DeepSeek</option></select>
      <button class="primary" onclick="runReconcile(this)">选择账单 CSV…</button></div><div id="rcOut" style="margin-top:10px"></div>`);
}
async function runReconcile(btn) {
  const [f] = await pickFile('.csv,text/csv'); if (!f) return;
  const r = await act(btn, () => api('POST', `/api/reconcile?account=${$('#rcAcc').value}`, undefined, f), r => `对账完成：${r.range.join(' ~ ')}`);
  if (!r) return;
  $('#rcOut').innerHTML = `<div class="row small"><span class="badge">账单 ${rmb(r.bill_total)}</span><span class="badge">我们估算 ${rmb(r.our_total)}</span>
    <span class="badge ${Math.abs(r.diff_total) > Math.max(1, r.our_total * 0.1) ? 'warn' : 'ok'}">差 ${rmb(r.diff_total)}${r.ratio ? `（${r.ratio}×）` : ''}</span><span class="muted">${esc(r.advice)}</span></div>
    <div class="small muted" style="margin-top:4px">认到的列：日期「${esc(r.columns.date)}」金额「${esc(r.columns.amount)}」产品「${esc(r.columns.item || '无')}」；AI 相关 ${r.bill_rows} 行，跳过 ${r.skipped_rows} 行（${rmb(r.skipped_rmb)}）</div>
    <table class="small" style="width:100%;margin-top:8px"><tr class="muted"><td>日期</td><td>账单</td><td>我们估算</td><td>调用次数</td><td>差额</td></tr>
    ${r.days.map(d => `<tr class="${d.flag ? 'warnc' : ''}"><td>${d.date}</td><td>${rmb(d.bill_rmb)}</td><td>${rmb(d.our_cost_rmb)}</td><td>${d.calls}</td><td>${rmb(d.diff_rmb)}${d.flag ? ' ⚠' : ''}</td></tr>`).join('')}</table>`;
}
async function cleanupProj(pid, name, btn) {
  const pv = await api('POST', `/projects/${pid}/cleanup`, {dry_run: true, keep_versions: 1});
  if (!pv.files) return toast('没有可清理的文件');
  if (!confirm(`「${name}」可清理 ${pv.mb} MB：${pv.jobs} 个没选用的旧视频版本（每镜保留当前版本、审片通过的和最近 1 个旧版本）、旧质检抽帧和旧配音分句。\n版本记录、提示词、质检结论都保留，只是旧版本不能再本地播放。继续？`)) return;
  await act(btn, () => api('POST', `/projects/${pid}/cleanup`, {dry_run: false, keep_versions: 1}), r => `已清理 ${r.mb} MB`);
  loadStorage();
}
function projCard(p) {
  return `<div class="card proj"><div class="cv" onclick="go('script','${p.id}')" style="cursor:pointer">${p.cover_url ? `<img src="${esc(p.cover_url)}">` : esc(p.name.slice(0, 1))}
    <span class="tag">${p.id === S.pid ? '当前项目' : esc(STATUS[p.status] || p.status)}</span></div>
    <div class="bd"><div class="row"><b>${esc(p.name)}</b>${p.pinned ? '<span class="badge acc">置顶</span>' : ''}<span class="spacer"></span><span class="muted small">${esc(p.style)}</span></div>
    <div class="muted small">${p.episodes} 集 · ${p.shots} 卡 · ${p.assets} 资产 · ${p.finals} 成片</div>
    <div class="row" style="margin-top:8px"><button class="small" onclick="go('script','${p.id}')">打开</button>
      <button class="small" onclick="setCover('${p.id}')">封面</button>
      <button class="small" onclick="act(null,()=>api('POST','/projects/${p.id}/pin?pinned=${!p.pinned}'))">${p.pinned ? '取消置顶' : '置顶'}</button>
      <a class="small" href="${BASE}/projects/${p.id}/export">下载</a><span class="spacer"></span>
      <button class="small danger" onclick="delProject('${p.id}','${esc(p.name)}')">删除</button></div></div></div>`;
}
async function createProject(btn) {
  const name = $('#npName').value.trim(); if (!name) return toast('先填项目名', true);
  const p = await act(btn, () => api('POST', '/projects', {name, style: $('#npStyle').value}), '项目已创建');
  if (p) go('script', p.id);
}
async function importProject(btn) {
  const [f] = await pickFile('.zip'); if (!f) return;
  const p = await act(btn, () => api('POST', '/projects/import', undefined, f), '项目已导入');
  if (p) go('script', p.id);
}
async function setCover(pid) {
  const [f] = await pickFile('image/*'); if (!f) return;
  await act(null, () => api('POST', `/projects/${pid}/cover?filename=${encodeURIComponent(f.name)}`, undefined, f), '封面已更新');
}
async function delProject(pid, name) {
  if (!confirm(`删除项目「${name}」？记录会删除，本地素材文件保留。`)) return;
  await act(null, () => api('DELETE', '/projects/' + pid), '已删除');
  if (S.pid === pid) { S.pid = null; go('home', null); }
}
function scriptGenModal() {
  if (!S.pid) { toast('先新建或选择一个项目，剧本会写进这个项目', true); return; }
  modal(`<h3>一键剧本</h3><div class="muted small">从小说、故事或一句灵感创作分集剧本，写入当前项目「${esc(S.p ? S.p.name : '')}」。</div>
    <textarea id="sgIdea" rows="8" style="margin-top:10px" placeholder="贴一段小说，或写一句灵感：外卖员捡到一只会说话的猫……"></textarea>
    <div class="kv" style="margin-top:10px"><span>集数</span><input id="sgN" type="number" min="1" max="30" value="3">
      <span>每集时长（秒）</span><input id="sgSec" type="number" min="30" max="600" value="90">
      <span>写入方式</span><select id="sgMode"><option value="replace">替换当前剧本</option><option value="append">追加到末尾</option></select></div>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button onclick="closeModal(this)">取消</button>
      <button class="primary" onclick="genScript(this)">生成剧本</button></div>`, true);
}
async function genScript(btn) {
  if (!confirmCost([S.p.settings.text_model], '一键剧本')) return;
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/script/generate`, {idea: $('#sgIdea').value,
    episodes: +$('#sgN').value, seconds_per_ep: +$('#sgSec').value, mode: $('#sgMode').value}), '剧本已生成');
  if (r) { closeModal(btn); go('script'); }
}

// ---------------- 设置 ----------------
function opt(list, cur) {
  if (cur && !list.some(m => m.id === cur)) list = [{id: cur}, ...list];
  return list.map(m => `<option value="${esc(m.id)}" ${m.id === cur ? 'selected' : ''}>${esc(m.id)}${m.live ? '（真实）' : '（模拟）'}</option>`).join('');
}
function settingsModal() {
  const st = S.p.settings, M = S.models, by = t => M.filter(m => m.type === t), vm = model(st.video_model);
  const sel = (k, vals) => `<select data-k="${k}">${vals.map(([v, t]) => `<option value="${v}" ${String(st[k]) === String(v) ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
  const tog = (k, label) => `<span>${label}</span>${sel(k, [['true', '开'], ['false', '关']])}`;
  setTimeout(() => libtvBox(false), 0);
  modal(`<h3>生成设置 · ${esc(S.p.name)}</h3>
    <div class="card" id="libtvBox" style="margin:0 0 12px">LibTV 连接状态检查中…</div><div class="kv">
    <span>项目名</span><input data-k="name" value="${esc(S.p.name)}">
    <span>画风</span><input data-k="style" value="${esc(S.p.style)}">
    <span>画面比例</span>${sel('aspect_ratio', ['9:16', '16:9', '1:1', '3:4', '4:3'].map(x => [x, x]))}
    <span>文本模型</span><select data-k="text_model">${opt(by('chat'), st.text_model)}</select>
    <span>图像模型</span><select data-k="image_model">${opt(by('image'), st.image_model)}</select>
    <span>出图清晰度</span>${sel('image_resolution', [['1K', '1K'], ['2K', '2K'], ['4K', '4K']])}
    <span>视频模型</span><select data-k="video_model">${opt(by('video'), st.video_model)}</select>
    <span>分辨率</span>${sel('resolution', (vm.specs && vm.specs.length ? vm.specs : ['480P', '720P', '1080P']).map(x => [x, x]))}
    <span>参考模式</span>${sel('video_mode', Object.entries(MODE))}
    <span>生成引擎</span>${sel('engine', [['auto', 'AI（有模型用模型）'], ['local', '本地规则（零成本）']])}
    <span>每卡目标时长（秒）</span><input data-k="shot_seconds" type="number" min="2" max="30" value="${st.shot_seconds}">
    <span>每集最多卡片</span><input data-k="max_shots" type="number" min="1" max="80" value="${st.max_shots}">
    <span>提示词格式</span>${sel('prompt_format', [['text', '文本（刀徒格式）'], ['json', 'JSON 结构']])}
    <span>项目预算（元）</span><input data-k="budget_rmb" type="number" min="0" step="1" value="${st.budget_rmb}">
    <span>成片配乐</span><select data-k="bgm_material_id"><option value="">不加配乐</option>${S.p.materials.filter(m => m.media_type === 'audio').map(m => `<option value="${m.id}" ${m.id === st.bgm_material_id ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select>
    <span>配乐音量（0~1）</span><input data-k="bgm_volume" type="number" min="0" max="1" step="0.05" value="${st.bgm_volume}">
    <span>视频超时（分钟）</span><input data-k="job_timeout_min" type="number" min="5" max="600" step="5" value="${st.job_timeout_min}">
    <span>成片配音方式</span>${sel('dub_mode', Object.entries(DUB))}
    <span>配音时原声音量（0~1）</span><input data-k="dub_orig_volume" type="number" min="0" max="1" step="0.05" value="${st.dub_orig_volume}">
    <span>旁白音色</span><select data-k="narrator_voice">${voiceOpts(st.narrator_voice)}</select>
    <span>配音模型</span><select data-k="tts_model">${opt(by('tts'), st.tts_model)}</select>
    <span>质检视觉模型</span><select data-k="qc_model">${opt(by('chat').filter(m => /vl|vision|gpt|gemini/i.test(m.id)), st.qc_model)}</select>
    <span>质检合格分</span><input data-k="qc_pass_score" type="number" min="0" max="100" step="5" value="${st.qc_pass_score}">
    ${tog('auto_qc', '出片后自动质检')}${tog('auto_retake', '不合格自动按意见重抽')}
    <span>每镜最多自动重抽</span><input data-k="retake_max" type="number" min="0" max="3" step="1" value="${st.retake_max}">
    ${tog('gen_asset_images', '一键时资产出图')}${tog('gen_blocking', '一键时生成站位图')}${tog('use_blocking_ref', '站位图作为出片参考')}
    ${tog('gen_frames', '一键时生成分镜图')}${tog('auto_assemble', '一键最后自动合成')}${tog('burn_subtitles', '字幕烧进画面')}
  </div><div class="row" style="margin-top:12px"><span class="muted small">“真实”模型会真实调用并扣费；“模拟”零成本，用来走通流程。超出项目预算的视频不会提交。</span>
  <span class="spacer"></span><button onclick="closeModal(this)">取消</button><button class="primary" onclick="saveSettings(this)">保存</button></div>`);
}
async function libtvBox(check) {
  const box = document.getElementById('libtvBox'); if (!box) return;
  try {
    const st = await api('GET', '/api/libtv' + (check ? '?check=true' : ''));
    const d = st.doctor || {};
    box.innerHTML = `<div class="row"><b>LibTV</b>${st.authorized ? '<span class="badge ok">已连接</span>' : '<span class="badge">未连接</span>'}
      <span class="muted small">官方远程 MCP，扣 LibTV 账户积分；视频模型里选 libtv- 开头的即用 LibTV 出片</span><span class="spacer"></span>
      ${st.authorized ? `<button class="small" onclick="libtvBox(true)">检查连接</button><button class="small danger" onclick="libtvLogout()">断开</button>`
        : `<a href="${esc(BASE + st.login_url)}" target="_blank"><button class="small primary">连接 LibTV</button></a><button class="small" onclick="libtvBox(false)">我已授权，刷新</button>`}</div>
      ${check ? (d.ok ? `<div class="small ok" style="margin-top:6px">连接正常 · 可用工具 ${d.tools.length} 个 · 准入 ${esc(JSON.stringify((d.doctor || {}).executionAdmission ?? '未返回'))}</div>`
        : `<div class="small err" style="margin-top:6px">${esc(d.error || '检查失败')}</div>`) : ''}
      ${!st.authorized ? '<div class="small muted" style="margin-top:6px">点“连接 LibTV”会打开 LibTV 登录授权页，授权完成后回到这里点“我已授权，刷新”。</div>' : ''}`;
    if (st.authorized && !S.models.some(m => m.id.startsWith('libtv-') && m.live)) S.models = (await api('GET', '/api/models')).items;
  } catch (e) { box.innerHTML = `<span class="err small">LibTV 状态读取失败：${esc(e.message)}</span>`; }
}
async function libtvLogout() {
  if (!confirm('断开 LibTV？之后选 libtv- 模型将无法真实出片。')) return;
  await api('POST', '/api/libtv/logout'); S.models = (await api('GET', '/api/models')).items; libtvBox(false);
}
async function saveSettings(btn) {
  const body = {};
  btn.closest('.card').querySelectorAll('[data-k]').forEach(el => {
    let v = el.value; if (v === 'true' || v === 'false') v = v === 'true'; else if (el.type === 'number') v = Number(v); body[el.dataset.k] = v;
  });
  const r = await act(btn, () => api('PATCH', `/projects/${S.pid}/settings`, body), '设置已保存');
  if (r) closeModal(btn);
}

// ---------------- 剧本 ----------------
function pgScript() {
  const p = S.p, chk = S.scriptCheck;
  return `<div class="card"><div class="bar"><h3 style="margin:0">原始剧本</h3><span class="spacer"></span>
    <button onclick="uploadScript(this)">⇧ 上传剧本</button><button onclick="saveScript(this)">保存剧本</button>
    <button onclick="parseScript(this)">全剧解析</button>
    <button onclick="extract(this,'local')">本地抽取资产</button><button class="primary" onclick="extract(this,'ai')">AI 智能解析</button>
    <button onclick="scriptGenModal()">一键剧本</button><button onclick="settingsModal()">生成设置</button></div>
    <textarea id="script" rows="20" style="margin-top:10px" placeholder="粘贴剧本，或上传 TXT / DOCX / PDF。格式：第1集 / 1-1 日 外 地点 / 人物：甲、乙 / △动作 / 甲（情绪）：台词" oninput="S.dirty=true">${esc(p.script_text)}</textarea>
    <div class="row" style="margin-top:8px"><span class="badge">${p.script_text.length} 字</span>
      ${chk ? `${chk.warnings.map(w => `<span class="badge warn">${esc(w)}</span>`).join('')}<span class="badge acc">${chk.episodes} 集 · ${chk.scenes} 场</span>` : ''}
      <span class="spacer"></span><label class="small muted"><input type="checkbox" id="forceParse"> 重新解析时清空旧分镜、提示词和视频任务</label></div></div>
  <div class="grid">${p.episodes.map(e => `<div class="card"><div class="row"><b>${esc(e.title)}</b><span class="spacer"></span>
    ${e.final_url ? '<span class="badge ok">已成片</span>' : ''}</div>
    <div class="muted small" style="margin:6px 0">${esc(e.raw_text.slice(0, 70))}…</div>
    <div class="small">角色 ${e.stats.characters} · 生物 ${e.stats.creatures} · 场景 ${e.stats.scenes} · 道具 ${e.stats.props}</div>
    <div class="small muted">${e.scenes} 场 · ${e.shots} 卡 · ${e.prompts} 提示词 · ${e.videos_done} 成片</div>
    <div class="row" style="margin-top:8px"><button class="small" onclick="S.ep=${e.no};go('board')">去分镜</button></div></div>`).join('') || '<div class="muted">保存剧本后点“全剧解析”分集分场</div>'}</div>`;
}
async function saveScript(btn) {
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/script`, {text: $('#script').value}), '剧本已保存');
  if (r) { S.scriptCheck = r.script_check; S.dirty = false; render(); }
}
async function uploadScript(btn) {
  const [f] = await pickFile('.txt,.docx,.pdf,.md'); if (!f) return;
  const mode = S.p.script_text.trim() && confirm('追加到现有剧本末尾？（取消 = 替换）') ? 'append' : 'replace';
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/script/upload?filename=${encodeURIComponent(f.name)}&mode=${mode}`, undefined, f), '剧本已导入');
  if (r) { S.scriptCheck = r.script_check; render(); }
}
async function parseScript(btn) {
  const force = $('#forceParse').checked;
  if (force && !confirm('会清空已有分镜、提示词和视频任务记录，确定？')) return;
  const text = $('#script').value;
  const r = await act(btn, async () => { await api('POST', `/projects/${S.pid}/script`, {text}); return api('POST', `/projects/${S.pid}/parse?force=${force}`); },
    r => `已解析：${r.episodes.length} 集 ${r.scenes.length} 场`);
  if (r) { S.scriptCheck = r.script_check; render(); }
}
async function extract(btn, engine) {
  if (engine === 'ai' && !confirmCost([S.p.settings.text_model], 'AI 解析')) return;
  const text = $('#script').value;
  await act(btn, async () => {
    await api('POST', `/projects/${S.pid}/script`, {text});
    if (!S.p.episodes.length) await api('POST', `/projects/${S.pid}/parse`);
    return api('POST', `/projects/${S.pid}/assets/extract?engine=${engine}`);
  }, r => `新增资产 ${r.added.length} 个${r.forms_added.length ? `，新增形态 ${r.forms_added.length} 个` : ''}（${r.source === 'real' ? '模型' : '本地规则'}）`);
}

// ---------------- 资产 ----------------
function pgAssets() {
  const A = S.p.assets, nForms = A.reduce((n, a) => n + a.forms.length - 1, 0);
  const cnt = k => A.filter(a => a.kind === k).length;
  const list = A.filter(a => (S.assetKind === 'all' || a.kind === S.assetKind) && (!S.assetQ || a.name.includes(S.assetQ) || a.aliases.some(x => x.includes(S.assetQ))));
  return `<div class="card"><div class="bar"><h3 style="margin:0">资产库</h3>
    <span class="muted small">${A.length} 实体 · ${nForms} 子形态 · ${A.filter(a => a.ready).length} 已出图 · ${cnt('audio')} 音频</span></div>
    <div class="bar" style="margin-top:10px"><div class="tabs" style="margin:0">${[['all', '全部', A.length], ...Object.entries(KIND).map(([k, v]) => [k, v, cnt(k)])]
      .map(([k, v, n]) => `<span class="tab ${S.assetKind === k ? 'on' : ''}" onclick="S.assetKind='${k}';render()">${v} ${n}</span>`).join('')}</div>
      <span class="spacer"></span><input style="width:160px" placeholder="搜索资产" value="${esc(S.assetQ)}" oninput="S.assetQ=this.value;clearTimeout(S.qt);S.qt=setTimeout(render,250)">
      <button class="primary" onclick="addAssetModal()">＋ 新增资产</button><button onclick="batchImages(this)">批量生图</button>
      <a href="${BASE}/projects/${S.pid}/download?what=assets"><button>批量下载</button></a><button onclick="bulkUpload(this)">批量上传</button>
      <button onclick="lockAll(true,this)" title="把所有角色/生物当前版本锁住，跨集保持同一张定版图">🔒 一键定版角色</button>
      <button onclick="settingsModal()">生成设置</button></div></div>
  ${list.length ? `<div class="grid">${list.map(assetCard).join('')}</div>` : '<div class="empty">还没有资产。去剧本页点“AI 智能解析 / 本地抽取资产”，或手动新增。</div>'}`;
}
function assetCard(a) {
  const img = a.kind === 'audio'
    ? `<div class="thumb ph">♪</div>${a.display_url ? `<audio controls src="${esc(a.display_url)}" style="margin-top:6px"></audio>` : ''}`
    : (a.display_url ? `<img class="thumb" src="${esc(a.display_url)}" onclick="assetDetail('${a.id}')" style="cursor:zoom-in">`
      : `<div class="thumb ph">${KIND[a.kind].slice(0, 1)}</div>`);
  return `<div class="card" style="margin:0">${img}
    <div class="row" style="margin-top:8px"><b>${esc(a.name)}</b><span class="spacer"></span><span class="badge">${KIND[a.kind]}</span></div>
    <div style="margin-top:4px">${a.forms.map(f => `<span class="chip click ${f.is_default ? 'on' : ''}" onclick="assetDetail('${a.id}','${f.id}')" title="${f.locked ? '已定版 v' + f.current_version + '（锁定）' : ''}">${f.locked ? '🔒' : ''}${esc(f.name)} <span class="dot ${f.current_version ? 'ok' : ''}"></span></span>`).join('')}</div>
    <div class="small muted" style="margin-top:4px">适用集数：${a.episodes.length ? a.episodes.map(n => '第' + n + '集').join('、') : '—'} · 引用 ${a.usage} 次${a.aliases.length ? ' · 别称：' + esc(a.aliases.join('、')) : ''}</div>
    ${['character', 'creature'].includes(a.kind) ? voiceRow(a) : ''}
    <div class="row" style="margin-top:8px"><button class="small" onclick="assetDetail('${a.id}')">编辑 / 历史版本</button>
      ${a.kind !== 'audio' && !(a.forms.find(f => f.id === a.default_form_id) || {}).locked ? `<button class="small" onclick="genForm('${a.default_form_id}',this)">${a.current_version ? '重新出图' : '生成图片'}</button>` : ''}
      ${['character', 'creature'].includes(a.kind) && a.current_version ? lockBtn(a) : ''}
      <button class="small" onclick="uploadForm('${a.default_form_id}','${a.kind}')">上传</button><span class="spacer"></span>
      <button class="small danger" onclick="delAsset('${a.id}','${esc(a.name)}')">删除</button></div></div>`;
}
function addAssetModal() {
  modal(`<h3>新增资产</h3><div class="kv"><span>类型</span><select id="naKind">${Object.entries(KIND).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
    <span>名称</span><input id="naName" placeholder="孔宣 / 金鸡岭 / 孔宣音色"><span>描述</span><textarea id="naDesc" rows="3"></textarea>
    <span>别称</span><input id="naAlias" placeholder="逗号分隔"><span>形态</span><input id="naForms" placeholder="逗号分隔：蛋，战斗，重伤（默认形态自动有）"></div>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button onclick="closeModal(this)">取消</button><button class="primary" onclick="addAsset(this)">添加</button></div>`, true);
}
async function addAsset(btn) {
  const split = v => v.split(/[,，、]/).map(x => x.trim()).filter(Boolean);
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/assets`, {kind: $('#naKind').value, name: $('#naName').value,
    description: $('#naDesc').value, aliases: split($('#naAlias').value), forms: split($('#naForms').value)}), '已添加');
  if (r) closeModal(btn);
}
async function batchImages(btn) {
  if (!confirmCost([S.p.settings.image_model], '批量生图')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/assets/images`), r => `出图 ${r.generated.length} 张${r.failed.length ? `，失败 ${r.failed.length}` : ''}`);
}
async function bulkUpload(btn) {
  const files = await pickFile('image/*,audio/*,.zip', true); if (!files.length) return;
  let ok = 0, miss = [];
  for (const f of files) {
    try { const r = await api('POST', `/projects/${S.pid}/assets/bulk-upload?filename=${encodeURIComponent(f.name)}`, undefined, f); ok += r.matched.length; miss = miss.concat(r.unmatched); }
    catch (e) { miss.push(f.name + '：' + e.message); }
  }
  toast(`已匹配 ${ok} 个文件${miss.length ? `；没匹配上：${miss.slice(0, 5).join('、')}` : ''}（文件名用「资产名.png」或「资产名-形态.png」）`, !!miss.length);
  load();
}
async function genForm(fid, btn) { if (confirmCost([S.p.settings.image_model], '出图')) await act(btn, () => api('POST', `/forms/${fid}/image`), '已出新版本'); }
async function uploadForm(fid, kind) {
  const [f] = await pickFile(kind === 'audio' ? 'audio/*' : 'image/*'); if (!f) return;
  await act(null, () => api('POST', `/forms/${fid}/upload?filename=${encodeURIComponent(f.name)}`, undefined, f), '已上传为新版本');
}
async function delAsset(id, name) { if (confirm(`删除资产「${name}」及其所有形态和版本？`)) await act(null, () => api('DELETE', '/assets/' + id), '已删除'); }
function assetDetail(aid, fid) {
  const a = S.p.assets.find(x => x.id === aid); if (!a) return;
  const f = a.forms.find(x => x.id === fid) || a.forms.find(x => x.is_default) || a.forms[0];
  const cur = f.versions.find(v => v.version === f.current_version);
  const mediaEl = a.kind === 'audio' ? (cur ? `<audio controls src="${esc(cur.display_url)}"></audio>` : '<div class="thumb ph">♪</div>')
    : (cur && cur.display_url ? `<a href="${esc(cur.display_url)}" target="_blank"><img class="thumb" src="${esc(cur.display_url)}"></a>` : '<div class="thumb ph">尚未生成</div>');
  const old = $('#modalRoot .modal[data-asset]'); if (old) old.remove();
  const m = modal(`<div class="row"><h3 style="margin:0">${esc(a.name)} · ${esc(f.name)}</h3><span class="badge">${KIND[a.kind]}</span>
      <span class="muted small">v${f.current_version || '-'}${cur ? ' · ' + esc(cur.source) + (cur.model ? ' · ' + esc(cur.model) : '') : ''}</span><span class="spacer"></span><button onclick="closeModal(this)">✕</button></div>
    <div class="split2" style="margin-top:10px"><div>${mediaEl}
      <div class="small muted" style="margin:10px 0 4px">形态</div><div class="forms">${a.forms.map(x => {
        const v = x.versions.find(y => y.version === x.current_version);
        return `<div class="form ${x.id === f.id ? 'on' : ''}" onclick="assetDetail('${a.id}','${x.id}')">${v && v.display_url && a.kind !== 'audio' ? `<img src="${esc(v.display_url)}">` : `<div class="ph">${x.current_version ? '♪' : '尚未生成'}</div>`}
          <div class="row small"><span>${esc(x.name)}</span><span class="spacer"></span><span class="dot ${x.current_version ? 'ok' : ''}"></span></div></div>`;
      }).join('')}<div class="form" onclick="addForm('${a.id}')"><div class="ph">＋ 形态</div><div class="small">新增形态</div></div></div>
      <div class="small muted" style="margin:10px 0 4px">历史版本（点击设为当前）</div>
      <div class="vers">${f.versions.map(v => `<div class="v ${v.version === f.current_version ? 'on' : ''}" onclick="selVersion('${f.id}',${v.version},'${a.id}')" title="${esc(v.prompt.slice(0, 200))}">
        ${a.kind === 'audio' ? '<div class="ph" style="height:36px">♪</div>' : `<img src="${esc(v.display_url)}">`}<div>v${v.version} ${esc(v.source)}</div></div>`).join('') || '<span class="muted small">还没有版本</span>'}</div></div>
    <div><div class="small muted">资产描述（出图用）</div><textarea rows="5" onchange="editAsset('${a.id}',${a.rev},{description:this.value})">${esc(a.description)}</textarea>
      <div class="row" style="margin:6px 0"><button class="small" onclick="polish('${a.id}',this)">AI 润色</button></div>
      <div class="small muted">别称</div><input value="${esc(a.aliases.join('，'))}" onchange="editAsset('${a.id}',${a.rev},{aliases:this.value.split(/[,，、]/)})">
      <div class="small muted" style="margin-top:6px">审核意见 / 备注</div><textarea rows="2" onchange="editAsset('${a.id}',${a.rev},{notes:this.value})">${esc(a.notes)}</textarea>
      ${!f.is_default ? `<div class="small muted" style="margin-top:6px">「${esc(f.name)}」形态描述</div><textarea rows="2" onchange="act(null,()=>api('PATCH','/forms/${f.id}',{fields:{description:this.value},expected_rev:${f.rev}}),'已保存')">${esc(f.description)}</textarea>` : ''}
      <div class="row" style="margin-top:10px">${a.kind !== 'audio' ? `<button class="primary small" onclick="genForm('${f.id}',this)">${f.current_version ? '重新生成' : '生成图片'}</button>` : ''}
        <button class="small" onclick="uploadForm('${f.id}','${a.kind}')">上传${a.kind === 'audio' ? '音频' : '图片'}</button>
        ${a.kind !== 'audio' && cur ? `<button class="small" onclick="upscale('${f.id}','${a.id}',this)">4K 高清</button>` : ''}
        ${cur ? `<a class="small" href="${esc(cur.display_url)}" download>下载</a>` : ''}
        ${!f.is_default ? `<button class="small danger" onclick="delForm('${f.id}','${a.id}')">删除形态</button>` : ''}</div>
      ${a.kind !== 'audio' && cur ? `<div class="small muted" style="margin-top:10px">编辑图片（在当前图基础上改）</div>
        <div class="row"><input id="editIns" placeholder="把眼睛改成黑色 / 换成战损服装"><button class="small" onclick="editImage('${f.id}','${a.id}',this)">改图</button></div>` : ''}
      <div class="small muted" style="margin-top:10px">适用集数：${a.episodes.map(n => '第' + n + '集').join('、') || '—'} · 在分镜里引用 ${a.usage} 次<br>提示词写法：<code>@${esc(a.name)}${f.is_default ? '' : '（' + esc(f.name) + '形态）'}</code></div></div></div>`);
  m.dataset.asset = aid;
}
async function upscale(fid, aid, btn) {
  if (!confirmCost([S.p.settings.image_model], '4K 高清')) return;
  const r = await act(btn, () => api('POST', `/forms/${fid}/upscale`), '已生成 4K 版本'); if (r) reopen(aid, fid);
}
async function reopen(aid, fid) { await load(); assetDetail(aid, fid); }
async function editAsset(id, rev, fields) { await act(null, () => api('PATCH', '/assets/' + id, {fields, expected_rev: rev}), '已保存'); }
async function polish(aid, btn) {
  if (!confirmCost([S.p.settings.text_model], 'AI 润色')) return;
  const r = await act(btn, () => api('POST', `/assets/${aid}/polish`), '描述已润色'); if (r) reopen(aid);
}
async function selVersion(fid, v, aid) { await act(null, () => api('POST', `/forms/${fid}/select?version=${v}`), '已切换到 v' + v); reopen(aid, fid); }
async function addForm(aid) {
  const name = prompt('形态名（如：蛋、战斗、重伤、黑化）'); if (!name) return;
  await act(null, () => api('POST', `/assets/${aid}/forms`, {name}), '已新增形态'); reopen(aid);
}
async function delForm(fid, aid) { if (confirm('删除这个形态及其版本？')) { await act(null, () => api('DELETE', '/forms/' + fid), '已删除'); reopen(aid); } }
async function editImage(fid, aid, btn) {
  if (!confirmCost([S.p.settings.image_model], '改图')) return;
  const r = await act(btn, () => api('POST', `/forms/${fid}/edit-image`, {instruction: $('#editIns').value}), '已生成修改版');
  if (r) reopen(aid, fid);
}

function lockBtn(a) {
  const f = a.forms.find(x => x.id === a.default_form_id) || {};
  return f.locked ? `<button class="small" onclick="lockForm('${f.id}',false,this)" title="解锁后可以换版本、重新出图">🔓 解锁</button>`
    : `<button class="small" onclick="lockForm('${f.id}',true,this)" title="锁住当前版本 v${f.current_version}，之后的出片都用它">🔒 定版</button>`;
}
async function lockForm(fid, locked, btn) { await act(btn, () => api('POST', `/forms/${fid}/lock`, {locked}), locked ? '已定版' : '已解锁'); }
async function lockAll(locked, btn) { await act(btn, () => api('POST', `/projects/${S.pid}/lock`, {locked}), r => `${locked ? '已定版' : '已解锁'} ${r.changed} 个形态`); }

// ---------------- 分镜 ----------------
function pgBoard() {
  const e = curEp(), st = S.p.settings, vm = model(st.video_model), d = vm.duration || {};
  const shots = S.p.shots.filter(s => e && s.episode_id === e.id);
  return `<div class="card"><div class="bar"><h3 style="margin:0">分镜脚本</h3><span class="muted small">按集生成：AI 模式调用模型；本地模式用规则零成本快速验证。</span>
    <span class="spacer"></span>${e ? epSelect() : ''}<span class="badge">${esc(st.video_model)} · 单镜 ${d.min || 4}-${Math.min(15, d.max || 15)} 秒</span>
    <button onclick="genBoard(this,'local')">本地生成本集</button><button class="primary" onclick="genBoard(this,'auto')">AI 生成本集</button>
    <button onclick="addShot(this)">新增卡片</button></div>
    ${shots.some(s => s.density.length) ? `<div class="small warnc" style="margin-top:8px">⚠ ${shots.filter(s => s.density.length).length} 张卡塞得太满（视频模型一张卡只能可靠演出一两个动作），建议拆卡：${shots.filter(s => s.density.length).map(s => `<span class="chip click" onclick="shotModal('${s.id}')">${s.code}</span>`).join('')}</div>` : ''}</div>
  ${shots.length ? `<div class="grid">${shots.map(s => `<div class="shot"><div class="hd"><div><div class="code">${s.code}</div><div class="small muted">${s.duration_s} 秒</div></div>
      <div class="small muted" style="text-align:right">${esc([s.size, s.angle, s.movement].filter(Boolean).join('、'))}</div></div>
    <div class="bd"><div class="lab">画面</div><div class="txt">${esc(s.picture)}</div><div class="lab">表演</div><div class="txt">${esc(s.performance) || '—'}</div>
      <div class="lab">台词</div><div class="txt">${esc(s.dialogue) || '—'}</div>
      <div style="margin-top:6px">${s.characters.map(c => `<span class="chip">@${esc(c)}</span>`).join('')}</div>
      ${s.density.length ? `<div class="small warnc" style="margin-top:6px">⚠ ${esc(s.density.join('；'))} <a href="javascript:void 0" onclick="splitShot('${s.id}',this)">拆卡</a></div>` : ''}</div>
    <div class="ft"><span>${esc(s.scene || s.scene_asset)}</span><span class="row">${badge(s.status)}<a href="javascript:void 0" onclick="shotModal('${s.id}')">点击预览</a></span></div></div>`).join('')}</div>`
    : `<div class="empty">${e ? '本集还没有分镜。点“AI 生成本集”或“本地生成本集”。' : '先在剧本页解析分集'}</div>`}`;
}
async function genBoard(btn, engine) {
  const e = curEp(); if (!e) return;
  if (e.shots && !confirm(`第${e.no}集已有 ${e.shots} 张卡，重新生成会覆盖（含提示词和视频记录）。继续？`)) return;
  if (engine !== 'local' && !confirmCost([S.p.settings.text_model], 'AI 生成分镜')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/storyboard?episode_no=${e.no}&force=true&engine=${engine}`), r => `已生成 ${r.results[0].shots} 张卡（${r.results[0].source === 'real' ? '模型' : '本地规则'}）`);
}
async function addShot(btn) {
  const e = curEp(); if (!e) return;
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/shots`, {episode_no: e.no, fields: {picture: '', duration_s: 5}}), '已新增卡片');
  if (r) shotModal(r.id);
}
function shotModal(sid) {
  const s = S.p.shots.find(x => x.id === sid); if (!s) return;
  const f = (k, label, rows) => `<span>${label}</span>${rows ? `<textarea rows="${rows}" data-f="${k}">${esc(s[k])}</textarea>` : `<input data-f="${k}" value="${esc(s[k])}">`}`;
  modal(`<div class="row"><h3 style="margin:0">镜头 ${s.code}</h3>${badge(s.status)}<span class="spacer"></span><button onclick="closeModal(this)">✕</button></div>
    <div class="split2" style="margin-top:10px"><div class="kv">
      ${f('picture', '画面（时间段）', 6)}${f('performance', '表演', 3)}${f('dialogue', '台词', 3)}${f('blocking', '站位', 2)}${f('sfx', '音效')}
      ${f('size', '景别')}${f('angle', '角度')}${f('movement', '运镜')}${f('transition', '转场')}
      <span>时长（秒）</span><input data-f="duration_s" type="number" min="1" max="30" value="${s.duration_s}">
      <span>出场角色</span><input data-f="characters" value="${esc(s.characters.join('，'))}" placeholder="逗号分隔；形态写成 孔宣（蛋）">
      <span>场景资产</span><input data-f="scene_asset" value="${esc(s.scene_asset)}"></div>
    <div>${s.frame ? `<img class="thumb ${S.p.settings.aspect_ratio === '9:16' ? 'tall' : ''}" src="${esc(s.frame)}">` : '<div class="thumb ph small">还没有分镜图</div>'}
      ${s.blocking_image ? `<div class="small muted" style="margin-top:8px">所在场次 ${esc(s.scene_code)} 站位图</div><img class="thumb" src="${esc(s.blocking_image)}">` : ''}
      ${s.original_text ? `<div class="small muted" style="margin-top:8px">原文</div><pre>${esc(s.original_text)}</pre>` : ''}</div></div>
    <div class="row" style="margin-top:12px"><button class="danger" onclick="delShot('${s.id}',this)">删除卡片</button>
      <button onclick="genFrame('${s.id}',this)">生成分镜图</button><button onclick="insertAfter(${s.episode_no},${s.shot_no},this)">在后面插一张</button>
      <button onclick="splitShot('${s.id}',this)" title="按时间轴和台词一分为二">拆成两张</button>
      ${s.density.length ? `<span class="small warnc">⚠ ${esc(s.density.join('；'))}</span>` : ''}
      <span class="spacer"></span><button onclick="closeModal(this)">取消</button><button class="primary" onclick="saveShot('${s.id}',${s.rev},this)">保存</button></div>`);
}
async function saveShot(sid, rev, btn) {
  const s = S.p.shots.find(x => x.id === sid), fields = {};
  btn.closest('.card').querySelectorAll('[data-f]').forEach(el => {
    const k = el.dataset.f; let v = el.value;
    if (k === 'characters') v = v.split(/[,，、]/).map(x => x.trim()).filter(Boolean);
    if (k === 'duration_s') v = Number(v);
    if (JSON.stringify(v) !== JSON.stringify(s[k])) fields[k] = v;
  });
  if (!Object.keys(fields).length) { closeModal(btn); return; }
  const r = await act(btn, () => api('PATCH', '/shots/' + sid, {fields, expected_rev: rev}), r => r.merged ? '已保存（与其他修改自动合并）' : '已保存');
  if (r) closeModal(btn);
}
async function delShot(sid, btn) { if (confirm('删除这张卡片？')) { const r = await act(btn, () => api('DELETE', '/shots/' + sid), '已删除'); if (r) closeModal(btn); } }
async function insertAfter(epNo, shotNo, btn) {
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/shots`, {episode_no: epNo, after_shot_no: shotNo, fields: {duration_s: 5}}), '已插入');
  if (r) { closeModal(btn); shotModal(r.id); }
}
async function genFrame(sid, btn) { if (confirmCost([S.p.settings.image_model], '分镜图')) await act(btn, () => api('POST', `/projects/${S.pid}/frames`, {shot_ids: [sid]}), '分镜图已生成'); }

// ---------------- 站位图 ----------------
function pgBlocking() {
  const e = curEp();
  const all = S.p.scenes.filter(s => e && s.episode_id === e.id);
  const list = all.filter(s => S.sceneFilter === 'all' || (S.sceneFilter === 'missing' ? !s.blocking : !!s.blocking));
  if (!all.some(s => s.id === S.sceneId)) S.sceneId = all[0] ? all[0].id : null;
  const sc = all.find(s => s.id === S.sceneId);
  const shots = sc ? S.p.shots.filter(s => s.scene_id === sc.id) : [];
  const chars = sc ? (sc.characters.length ? sc.characters : S.p.assets.filter(a => ['character', 'creature'].includes(a.kind) && sc.text.includes(a.name)).map(a => a.name)) : [];
  const refAssets = sc ? S.p.assets.filter(a => a.name === sc.location || chars.includes(a.name)) : [];
  return `<div class="card"><div class="bar"><h3 style="margin:0">站位图</h3><span class="muted small">${all.length} 个场景 · ${all.filter(s => s.blocking).length} 个已出图 · 每场景一张，人物到场后的位置关系，同场景分镜共用</span>
    <span class="spacer"></span>${e ? epSelect() : ''}<button class="primary" onclick="genBlocking(this,null)">一键生成本集站位图</button>
    <button onclick="go('videos')">前往视频</button></div></div>
  ${!all.length ? '<div class="empty">先在剧本页解析出场次</div>' : `<div class="split">
    <div class="card list"><div class="tabs">${[['all', '全部'], ['missing', '缺图'], ['ok', '可用']].map(([k, v]) => `<span class="tab ${S.sceneFilter === k ? 'on' : ''}" onclick="S.sceneFilter='${k}';render()">${v}</span>`).join('')}</div>
      ${list.map(s => `<div class="it ${s.id === S.sceneId ? 'on' : ''}" onclick="S.sceneId='${s.id}';render()"><b>${esc(s.location || s.heading || '本集场景')}</b>
        <div class="small muted">${esc(s.code)} · ${esc(s.time_of_day)} · ${s.blocking ? '<span class="ok">已出图</span>' : '缺图'}</div></div>`).join('')}</div>
    <div class="card"><div class="row"><div><div class="small muted">当前场景</div><h3 style="margin:0">${esc(sc.location || sc.heading || '本集场景')}</h3></div><span class="spacer"></span>
      ${refAssets.some(a => !a.current_version) ? '<span class="badge warn">需核对素材</span>' : '<span class="badge ok">素材齐全</span>'}</div>
      <div style="margin-top:10px">${sc.blocking ? `<a href="${esc(sc.blocking)}" target="_blank"><img class="thumb" src="${esc(sc.blocking)}"></a>` : '<div class="thumb ph small">这个场景还没有站位图<br>核对场景正文与参考素材，生成人物到场后的位置关系</div>'}</div>
      <div class="small muted" style="margin-top:6px">参考素材 ${refAssets.filter(a => a.current_version).length} 张 · 16:9 · 生成后自动启用，同场景分镜共用</div>
      <div class="small muted" style="margin-top:8px">站位说明（可选，写进生成提示）</div>
      <textarea rows="2" onchange="act(null,()=>api('PATCH','/scenes/${sc.id}',{fields:{blocking_note:this.value},expected_rev:${sc.rev}}),'已保存')">${esc(sc.blocking_note)}</textarea>
      <div class="row" style="margin-top:10px;justify-content:flex-end"><button onclick="go('videos')">前往视频</button>
        <button class="primary" onclick="genBlocking(this,'${sc.id}')">${sc.blocking ? '重新生成站位图' : '生成站位图'}</button></div></div>
    <div class="card"><div class="small muted">场景正文 · ${esc(sc.heading)}</div><pre style="max-height:220px;overflow:auto;margin:6px 0 12px">${esc(sc.text)}</pre>
      <div class="small muted">出场人物</div><div>${chars.map(c => `<span class="chip">${esc(c)}</span>`).join('') || '—'}</div>
      <div class="small muted" style="margin-top:10px">参考素材（自动绑定）</div>
      ${refAssets.map(a => `<div class="row small" style="margin:4px 0">${a.display_url ? `<img src="${esc(a.display_url)}" style="width:48px;height:27px;object-fit:cover;border-radius:4px">` : '<span class="dot"></span>'}
        ${esc(a.name)} <span class="muted">${KIND[a.kind]}</span><span class="spacer"></span>${a.current_version ? '<span class="ok">✓</span>' : `<button class="small" onclick="genForm('${a.default_form_id}',this)">补图</button>`}</div>`).join('') || '<div class="muted small">没有匹配的资产</div>'}
      <div class="small muted" style="margin-top:10px">关联分镜</div><div>${shots.map(s => `<span class="chip click" onclick="shotModal('${s.id}')">${s.code}</span>`).join('') || '<span class="muted small">暂无；生成分镜后自动关联</span>'}</div></div></div>`}`;
}
async function genBlocking(btn, sid) {
  if (!confirmCost([S.p.settings.image_model], '站位图')) return;
  const body = sid ? {scene_ids: [sid], only_missing: false} : {episode_no: S.ep, only_missing: true};
  await act(btn, () => api('POST', `/projects/${S.pid}/blocking`, body), r => `站位图 ${r.generated.length} 张${r.failed.length ? `，失败 ${r.failed.length}` : ''}`);
}

// ---------------- 提示词 ----------------
function pgPrompts() {
  const e = curEp(), st = S.p.settings;
  const shots = S.p.shots.filter(s => e && s.episode_id === e.id);
  return `<div class="card"><div class="bar"><h3 style="margin:0">视频提示词</h3><span class="muted small">依据分镜生成视频提示词，可选已确认站位；也可从剧本直接生成。</span>
    <span class="spacer"></span>${e ? epSelect() : ''}<span class="badge">${esc(st.video_model)}</span>
    <button onclick="genPrompts(this,'local')">本地转换</button><button class="primary" onclick="genPrompts(this,'auto')">生成最终提示词</button>
    <button onclick="fromScript(this)">剧本直转提示词</button><button onclick="act(this,()=>api('POST','/projects/${S.pid}/references/resolve'),r=>'重新解析了 '+r.updated+' 条引用')">重新解析 @引用</button></div></div>
  ${shots.length ? `<div class="grid">${shots.map(s => {
    const p = s.prompt;
    return `<div class="shot"><div class="hd"><span class="code">${s.code}</span><span class="badge acc">${esc(st.video_model)}</span></div>
      <div class="bd">${p ? `<div class="txt" style="-webkit-line-clamp:5">${esc(p.positive)}</div>
        <div style="margin-top:6px">${p.bindings.map(b => `<span class="chip">${esc(b.mention)} v${b.version}</span>`).join('')}
        ${p.unresolved.map(u => `<span class="chip bad" title="资产表里没有">@${esc(u)} 未找到</span>` + ((p.suggestions || {})[u] || []).map(g =>
          `<span class="chip click warn" title="点一下替换" onclick="fixMention('${s.id}','${esc(u)}','${esc(g)}')">是不是 ${esc(g)}？</span>`).join('')).join('')}
        ${p.stale.map(b => `<span class="chip warn" title="资产换过图，出片时自动用最新版本">${esc(b.name)} 已更新</span>`).join('')}</div>`
        : '<div class="muted">还没有提示词</div>'}
        <div class="small muted" style="margin-top:6px">负向：${p ? esc(p.negative.slice(0, 40)) : '—'}</div>
        <div class="small muted">${esc(st.aspect_ratio)} · ${esc(S.p.style)} · ${s.duration_s} 秒</div></div>
      <div class="ft"><button class="small" onclick="regenPrompt('${s.id}',this)">重新转提示词</button>
        ${p ? `<button class="small" onclick="promptModal('${s.id}')">编辑</button>` : ''}
        <button class="small primary" onclick="S.shotId='${s.id}';go('videos')">配置并生成视频</button></div></div>`;
  }).join('')}</div>` : `<div class="empty">${e ? '当前集没有分镜卡片，请先在“分镜”页生成，或点“剧本直转提示词”。' : '先在剧本页解析分集'}</div>`}`;
}
async function fixMention(sid, bad, good) {
  const s = S.p.shots.find(x => x.id === sid), p = s.prompt;
  const text = p.positive.split('@' + bad).join(good);
  await act(null, () => api('PATCH', '/prompts/' + p.id, {fields: {positive: text}, expected_rev: p.rev}), `已改为 ${good}`);
}
async function genPrompts(btn, engine) {
  const e = curEp(); if (!e) return;
  if (engine !== 'local' && !confirmCost([S.p.settings.text_model], 'AI 生成提示词')) return;
  const has = S.p.shots.some(s => s.episode_id === e.id && s.prompt);
  const force = has && confirm('本集已有提示词。覆盖全部（包括手改过的）？取消 = 只补缺的');
  await act(btn, () => api('POST', `/projects/${S.pid}/prompts?episode_no=${e.no}&force=${force}&engine=${engine}`), r => `生成 ${r.generated} 条（${r.source === 'real' ? '模型' : '本地规则'}）`);
}
async function fromScript(btn) {
  const e = curEp(); if (!e) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/prompts/from-script?episode_no=${e.no}`), r => `生成 ${r.generated} 条`);
}
async function regenPrompt(sid, btn) { await act(btn, () => api('POST', `/projects/${S.pid}/prompts`, {shot_ids: [sid]}), '已重新转换'); }
function refChips(targetId) {
  return S.p.assets.flatMap(a => a.forms.map(f => {
    const m = '@' + a.name + (f.is_default ? '' : `（${f.name}形态）`);
    return `<span class="chip click" onclick="insertAt('${targetId}','${esc(m)}')">${esc(m)}</span>`;
  })).join('');
}
function insertAt(id, text) {
  const t = document.getElementById(id); if (!t) return;
  const p = t.selectionStart ?? t.value.length; t.value = t.value.slice(0, p) + text + t.value.slice(p); t.focus();
}
function promptModal(sid) {
  const s = S.p.shots.find(x => x.id === sid), p = s.prompt;
  modal(`<div class="row"><h3 style="margin:0">提示词 · ${s.code}</h3><span class="spacer"></span><button onclick="closeModal(this)">✕</button></div>
    <textarea id="pmPos" rows="16" style="margin-top:10px">${esc(p.positive)}</textarea>
    <div class="small muted" style="margin:6px 0">点击插入引用：</div><div style="max-height:90px;overflow:auto">${refChips('pmPos')}</div>
    <div class="small muted" style="margin-top:8px">负向</div><textarea id="pmNeg" rows="2">${esc(p.negative)}</textarea>
    <div class="row" style="margin-top:10px;justify-content:flex-end"><button onclick="closeModal(this)">取消</button>
      <button class="primary" onclick="savePrompt('${p.id}',${p.rev},this)">保存</button></div>`);
}
async function savePrompt(id, rev, btn) {
  const r = await act(btn, () => api('PATCH', '/prompts/' + id, {fields: {positive: $('#pmPos').value, negative: $('#pmNeg').value}, expected_rev: rev}), '已保存');
  if (r) closeModal(btn);
}

// ---------------- 视频 ----------------
async function submitVideos(body) {   // 参考一致性闸门：409 reference_changed → 确认后带上确认标志重发
  try { return await api('POST', `/projects/${S.pid}/videos`, body); }
  catch (e) {
    const d = e.data && e.data.error && e.data.error.detail;
    if (e.status === 409 && d && d.code === 'reference_changed' && confirm(e.message + '\n\n确定按当前版本提交？')) {
      return api('POST', `/projects/${S.pid}/videos`, {...body, accept_stale: true, allow_missing: true});
    }
    throw e;
  }
}
function vp() {   // 当前镜头的生成参数（页面内暂存）
  const st = S.p.settings;
  const d = S.vp[S.shotId] || (S.vp[S.shotId] = {mode: st.video_mode, model: st.video_model, resolution: st.resolution,
    aspect: st.aspect_ratio, duration: 'shot', forms: null, mats: []});
  return d;
}
function pgVideos() {
  const e = curEp(); const shots = S.p.shots.filter(s => e && s.episode_id === e.id);
  if (!shots.some(s => s.id === S.shotId)) S.shotId = shots[0] ? shots[0].id : null;
  const s = shots.find(x => x.id === S.shotId);
  if (!e) return '<div class="empty">先在剧本页解析分集</div>';
  const doneN = shots.filter(x => x.job && x.job.status === 'succeeded').length;
  S.vtool = S.vtool || 'shot';
  const head = `<div class="tabs">${TOOLS.map(([k, v]) => `<span class="tab ${S.vtool === k ? 'on' : ''}" onclick="S.vtool='${k}';render()">${v}</span>`).join('')}</div>
  <div class="card"><div class="bar">${epSelect('S.ep=+this.value;S.sel.clear();render()')}
    <span class="badge">${shots.length} 卡片 · ${doneN} 成片 · 已选 ${S.sel.size}</span><span class="spacer"></span>
    <button onclick="addShot(this)">新增卡片</button><button onclick="playAll()" ${doneN ? '' : 'disabled'}>播放全部</button>
    <button onclick="shots_select(true)">全选本集</button><button onclick="shots_select(false)">清除选择</button>
    <button class="primary" onclick="batchGen(this)">批量生成${S.sel.size ? `（${S.sel.size}）` : '（未出片的）'}</button>
    <a href="${BASE}/projects/${S.pid}/download?what=videos&episode_no=${e.no}"><button ${doneN ? '' : 'disabled'}>批量下载</button></a>
    <a href="${BASE}/projects/${S.pid}/edit-package?episode_no=${e.no}"><button ${doneN ? '' : 'disabled'} title="编号片段 + 字幕 + EDL/FCPXML，Premiere/达芬奇/剪映可用">导出剪辑工程</button></a>
    <button onclick="qcEpisode(this,${e.no})" ${doneN ? '' : 'disabled'} title="技术检查 + 视觉模型核对人物一致性/画面/字幕水印">质检本集</button>
    <button onclick="retakeEpisode(this,${e.no})" title="把质检不合格、审片打回的镜头按意见改提示词后重抽">重抽不合格的</button>
    <button onclick="dubEpisode(this,${e.no})" title="按台词用角色音色合成配音轨">配音本集</button>
    <button onclick="act(this,()=>api('POST','/projects/${S.pid}/videos/poll'),'已刷新回执')">刷新回执</button></div>
    ${qcSummary(shots)}</div>`;
  if (!s) return head + '<div class="empty">本集没有卡片</div>';
  if (S.vtool !== 'shot') return head + toolPanel(s, shots);
  const P = vp(), m = model(P.model), st = S.p.settings;
  const bound = new Set((s.prompt ? s.prompt.bindings : []).map(b => b.form_id));
  const chosen = P.forms || [...bound];
  const forms = S.p.assets.flatMap(a => a.forms.map(f => ({a, f})));
  const j = s.job;
  return head + `<div class="split2"><div>
    <div class="card"><h3>生成参数 · 镜头 ${s.code}</h3><div class="row">
      <label class="small">参考模式<select onchange="vp().mode=this.value">${Object.entries(MODE).map(([k, v]) => `<option value="${k}" ${P.mode === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
      <label class="small">模型<select onchange="vp().model=this.value;render()">${opt(S.models.filter(x => x.type === 'video'), P.model)}</select></label>
      <label class="small">分辨率<select onchange="vp().resolution=this.value">${(m.specs || ['480P']).map(r => `<option ${r === P.resolution ? 'selected' : ''}>${r}${m.price && m.price.rmb_per_second ? `（${Math.round((m.price.rmb_per_second[r] || 0) * 10)}积分/秒）` : ''}</option>`).join('')}</select></label>
      <label class="small">画面比例<select onchange="vp().aspect=this.value">${['9:16', '16:9', '1:1', '3:4', '4:3'].map(x => `<option ${x === P.aspect ? 'selected' : ''}>${x}</option>`).join('')}</select></label>
      <label class="small">视频时长<select onchange="vp().duration=this.value">${[['shot', `跟随分镜（${s.duration_s}s）`], ...[4, 5, 6, 8, 10, 12, 15].map(x => [x, x + ' 秒'])].map(([v, t]) => `<option value="${v}" ${String(P.duration) === String(v) ? 'selected' : ''}>${t}</option>`).join('')}</select></label></div>
      ${m.live ? '<div class="small err" style="margin-top:6px">真实模型：生成会真实扣费</div>' : '<div class="small muted" style="margin-top:6px">模拟模型：不花钱，用来走流程</div>'}</div>
    <div class="card"><div class="row"><h3 style="margin:0">镜头提示词</h3><span class="spacer"></span>${s.blocking_image ? `<a href="${esc(s.blocking_image)}" target="_blank"><button class="small">站位参考</button></a>` : ''}
      ${s.prompt ? '' : `<button class="small" onclick="regenPrompt('${s.id}',this)">生成提示词</button>`}</div>
      ${s.prompt ? `<textarea id="vpPrompt" rows="12" style="margin-top:8px" onchange="act(null,()=>api('PATCH','/prompts/${s.prompt.id}',{fields:{positive:this.value},expected_rev:${s.prompt.rev}}),'提示词已保存')">${esc(s.prompt.positive)}</textarea>
        <div style="margin-top:6px;max-height:70px;overflow:auto">${refChips('vpPrompt')}</div>` : '<div class="muted">还没有提示词</div>'}</div>
    <div class="card"><div class="row"><h3 style="margin:0">参考素材</h3><span class="spacer"></span>
      <button class="small" onclick="vp().forms=null;render()">按提示词自动</button><button class="small" onclick="vp().forms=[];render()">全不选</button>
      <button class="small" onclick="uploadMaterial(this)">⇧ 上传本地素材</button></div>
      <div class="grid sm" style="margin-top:8px">${forms.map(({a, f}) => `<label class="row small" style="gap:6px"><input type="checkbox" ${chosen.includes(f.id) ? 'checked' : ''} onchange="toggleRef('${f.id}',this.checked)">
        ${f.display_url && a.kind !== 'audio' ? `<img src="${esc(f.display_url)}" style="width:44px;height:25px;object-fit:cover;border-radius:4px">` : `<span>${a.kind === 'audio' ? '♪' : '·'}</span>`}
        <span>${esc(a.name)}（${esc(f.name)}）</span><span class="muted">${KIND[a.kind]}${f.current_version ? '' : ' · 无图'}</span></label>`).join('')}</div>
      ${S.p.materials.length ? `<div class="small muted" style="margin-top:8px">本地素材</div><div class="grid sm">${S.p.materials.map(x => `<label class="row small"><input type="checkbox" ${P.mats.includes(x.id) ? 'checked' : ''} onchange="toggleMat('${x.id}',this.checked)">
        ${x.media_type === 'image' ? `<img src="${esc(x.url)}" style="width:44px;height:25px;object-fit:cover;border-radius:4px">` : x.media_type === 'audio' ? '♪' : '▶'} ${esc(x.name)}
        <button class="small ghost" onclick="act(null,()=>api('DELETE','/materials/${x.id}'),'已删除')">✕</button></label>`).join('')}</div>` : ''}
      ${s.blocking_image && st.use_blocking_ref ? '<div class="small muted" style="margin-top:6px">全能参考时会自动带上所在场次的站位图。</div>' : ''}</div>
    <div class="card row"><span id="vpQuote" class="muted">点“报价”查看本次预计消耗</span><span class="spacer"></span>
      <button onclick="quoteOne(this)">报价</button><button class="primary" onclick="genOne(this)">${s.jobs.length ? '重新生成当前镜头' : '生成当前镜头'}</button></div></div>
  <div><div class="card"><div class="row"><b>镜头 ${s.code}</b>${badge(s.status)}<span class="spacer"></span><span class="muted small">${s.duration_s}s</span></div>
      ${j && j.play_url ? `<video id="player" controls preload="metadata" poster="${esc(j.poster_url || '')}" src="${esc(j.play_url)}" style="margin-top:8px"></video>`
        : `<div class="thumb ${S.p.settings.aspect_ratio === '9:16' ? 'tall' : ''} ph small" style="margin-top:8px">${j && (j.status === 'submitted' || j.status === 'running') ? '生成中…' : '选择镜头或历史版本，在此查看成片'}</div>`}
      ${j && j.error ? `<div class="err small">${esc(j.error)}</div>` : ''}
      ${j && j.status === 'succeeded' ? reviewBar(j) + qcPanel(j) : ''}${j && j.retake ? retakeInfo(j, s) : ''}${dubPanel(s)}</div>
    <div class="card"><h3>历史版本</h3>${s.jobs.slice().reverse().map(x => `<div class="row small" style="padding:6px 0;border-bottom:1px solid var(--line)">
      <b>v${x.version}</b>${badge(x.status)}${qcBadge(x.qc)}${x.review === 'approved' ? '<span class="badge ok">已通过</span>' : x.review === 'rejected' ? `<span class="badge bad" title="${esc(x.review_note)}">打回</span>` : ''}${x.retake ? `<span class="badge acc" title="${esc((x.retake.changes || []).join('；'))}">重抽自 v${x.retake.from_version}</span>` : ''}<span class="muted">${esc(JOB_MODE[x.mode] || x.mode)} · ${esc(x.model)} · ${esc(x.resolution)} · ${x.duration_s}s · ${rmb(x.charged_rmb || x.quoted_rmb)}</span><span class="spacer"></span>
      ${x.status === 'succeeded' ? (x.id === s.selected_job_id ? '<span class="ok">当前</span>' : `<button class="small" onclick="act(this,()=>api('POST','/shots/${s.id}/select?job_id=${x.id}'),'已选用 v${x.version}')">选用</button>`)
        + `<button class="small" onclick="voiceModal('${x.id}')">截音色</button>${x.play_url ? `<a class="small" href="${esc(x.play_url)}" download>下载</a>` : ''}` : ''}
      ${x.status === 'submitted' || x.status === 'running' ? `<button class="small danger" onclick="act(this,()=>api('POST','/jobs/${x.id}/cancel'),'已取消')">取消</button>` : ''}</div>`).join('') || '<div class="muted small">生成完成后，历史版本会显示在这里。</div>'}</div>
    <div class="card"><h3>镜头目录</h3><div class="grid sm">${shots.map(x => `<div class="shot ${x.id === S.shotId ? 'sel' : ''}" style="cursor:pointer" onclick="S.shotId='${x.id}';render()">
      <div class="hd"><label onclick="event.stopPropagation()"><input type="checkbox" ${S.sel.has(x.id) ? 'checked' : ''} onchange="this.checked?S.sel.add('${x.id}'):S.sel.delete('${x.id}');render()"></label>
        <b>${x.code}</b><span class="small muted">${x.duration_s}s</span>${x.job && x.job.qc ? `<span class="dot ${QC[x.job.qc.verdict][0]}" title="质检 ${x.job.qc.score}"></span>` : ''}${x.dub ? '<span class="small" title="已配音">🎙</span>' : ''}</div>
      <div class="bd">${x.job && x.job.poster_url ? `<img src="${esc(x.job.poster_url)}" class="thumb">` : `<div class="small">${badge(x.status)}</div>`}</div></div>`).join('')}</div></div></div></div>`;
}
function toolPanel(s, shots) {
  const tool = S.vtool, want = tool === 'video_edit' ? '视频编辑' : '动作迁移';
  const models = S.models.filter(m => (m.modes || []).includes(want));
  const srcs = S.p.shots.flatMap(x => x.jobs.filter(j => j.status === 'succeeded').map(j => ({id: 'job:' + j.id, label: `镜头 ${x.code} · v${j.version}（${JOB_MODE[j.mode] || j.mode}）`, cur: x.id === s.id && j.id === x.selected_job_id})))
    .concat(S.p.materials.filter(m => m.media_type === 'video').map(m => ({id: 'mat:' + m.id, label: '本地素材 · ' + m.name})));
  const forms = S.p.assets.filter(a => ['character', 'creature'].includes(a.kind)).flatMap(a => a.forms.filter(f => f.current_version).map(f => ({id: f.id, label: a.name + (f.is_default ? '' : '（' + f.name + '）'), url: f.display_url})));
  return `<div class="split2"><div class="card"><h3>${want} · 结果作为镜头 ${s.code} 的新版本</h3>
    <div class="muted small">${tool === 'video_edit' ? '在已有视频上按要求修改（换背景、改天气、加特效、改服装……），保留原有动作与镜头。' : '用角色图去演参考视频里的动作：参考视频可以是别的镜头成片，也可以上传一段真人表演。'}</div>
    <div class="kv" style="margin-top:10px">
      <span>目标镜头</span><select onchange="S.shotId=this.value;render()">${shots.map(x => `<option value="${x.id}" ${x.id === s.id ? 'selected' : ''}>${x.code}</option>`).join('')}</select>
      <span>${tool === 'video_edit' ? '源视频' : '动作参考视频'}</span><select id="tSrc">${srcs.map(x => `<option value="${x.id}" ${x.cur ? 'selected' : ''}>${esc(x.label)}</option>`).join('') || '<option value="">（还没有成片，先出片或上传视频素材）</option>'}</select>
      ${tool === 'motion_transfer' ? `<span>角色形态</span><select id="tForm">${forms.map(f => `<option value="${f.id}">${esc(f.label)}</option>`).join('') || '<option value="">（先给角色出图）</option>'}</select>` : ''}
      <span>模型</span><select id="tModel">${models.map(m => `<option value="${m.id}">${esc(m.id)}${m.live ? '（真实）' : '（模拟）'}</option>`).join('') || '<option value="">（目录里没有支持的模型）</option>'}</select>
      <span>${tool === 'video_edit' ? '修改要求' : '补充要求（可选）'}</span><textarea id="tIns" rows="4" placeholder="${tool === 'video_edit' ? '把背景换成雨夜霓虹街道，保持人物动作不变' : '动作幅度更大一些'}"></textarea></div>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button onclick="uploadMaterial(this)">⇧ 上传视频素材</button>
      <button class="primary" onclick="submitTool(this)">提交${want}</button></div></div>
  <div class="card"><h3>镜头 ${s.code} 历史版本</h3>${s.jobs.slice().reverse().map(x => `<div class="row small" style="padding:6px 0;border-bottom:1px solid var(--line)"><b>v${x.version}</b>${badge(x.status)}
    <span class="muted">${esc(JOB_MODE[x.mode] || x.mode)} · ${esc(x.model)}</span><span class="spacer"></span>${x.play_url ? `<a href="${esc(x.play_url)}" target="_blank">播放</a>` : ''}</div>`).join('') || '<div class="muted small">暂无</div>'}
    ${s.job && s.job.play_url ? `<video controls src="${esc(s.job.play_url)}" style="margin-top:8px"></video>` : ''}</div></div>`;
}
async function submitTool(btn) {
  const src = ($('#tSrc') || {}).value || '', model = ($('#tModel') || {}).value || '';
  if (!src) return toast('先选源视频', true);
  if (!confirmCost([model], S.vtool === 'video_edit' ? '视频编辑' : '动作迁移')) return;
  const body = {shot_id: S.shotId, tool: S.vtool, instruction: $('#tIns').value, model,
    source_job_id: src.startsWith('job:') ? src.slice(4) : null, source_material_id: src.startsWith('mat:') ? src.slice(4) : null,
    form_id: $('#tForm') ? $('#tForm').value : null};
  await act(btn, () => api('POST', `/projects/${S.pid}/videos/tool`, body), r => `已提交 v${r.version}（${r.model}，${r.seconds}s，约 ${rmb(r.quoted_rmb)}）`);
}
function toggleRef(fid, on) {
  const s = S.p.shots.find(x => x.id === S.shotId), P = vp();
  if (!P.forms) P.forms = (s.prompt ? s.prompt.bindings : []).map(b => b.form_id);
  P.forms = on ? [...new Set([...P.forms, fid])] : P.forms.filter(x => x !== fid);
}
function toggleMat(mid, on) { const P = vp(); P.mats = on ? [...new Set([...P.mats, mid])] : P.mats.filter(x => x !== mid); }
function oneBody() {
  const P = vp();
  return {shot_ids: [S.shotId], mode: P.mode, model: P.model, resolution: P.resolution, aspect_ratio: P.aspect,
    duration: P.duration === 'shot' ? null : Number(P.duration), form_ids: P.forms, material_ids: P.mats};
}
async function quoteOne(btn) {
  try { btn.disabled = true; const q = await api('POST', `/projects/${S.pid}/videos/quote`, oneBody());
    $('#vpQuote').innerHTML = q.items.length ? `本次预计消耗 <b>${Math.round(q.total_points)} 积分</b>（${rmb(q.total_rmb)}），剩余预算 ${rmb(q.remaining_rmb)}
      ${q.within_budget ? '<span class="badge ok">预算内</span>' : '<span class="badge bad">超预算</span>'}` : esc((q.skipped[0] || {}).reason || '没有可提交的');
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}
async function genOne(btn) {
  if (!confirmCost([vp().model], '生成视频')) return;
  await act(btn, () => submitVideos(oneBody()), r => r.submitted.length ? `已提交 v${r.submitted[0].version}（${MODE[r.submitted[0].mode]}，${r.submitted[0].refs} 个参考）` : (r.skipped[0] || {}).reason || '没有提交');
}
function shots_select(all) {
  const e = curEp(); S.sel.clear();
  if (all) S.p.shots.filter(s => s.episode_id === e.id).forEach(s => S.sel.add(s.id));
  render();
}
async function batchGen(btn) {
  const e = curEp(), st = S.p.settings;
  if (!confirmCost([st.video_model], '批量生成视频')) return;
  const body = S.sel.size ? {shot_ids: [...S.sel], force: false} : {episode_no: e.no};
  await act(btn, () => submitVideos(body), r => `已提交 ${r.submitted.length} 个，跳过 ${r.skipped.length} 个（预计 ${rmb(r.quote.total_rmb)}）`);
}
async function uploadMaterial(btn) {
  const files = await pickFile('image/*,audio/*,video/*', true);
  for (const f of files) await act(btn, () => api('POST', `/projects/${S.pid}/materials?filename=${encodeURIComponent(f.name)}`, undefined, f), '素材已上传');
}
function playAll() {
  const e = curEp(); const urls = S.p.shots.filter(s => s.episode_id === e.id && s.job && s.job.play_url).map(s => s.job.play_url);
  if (!urls.length) return;
  const m = modal(`<div class="row"><h3 style="margin:0">播放全部 · 第${e.no}集</h3><span class="spacer"></span><span id="paIdx" class="muted"></span><button onclick="closeModal(this)">✕</button></div>
    <video id="paV" controls autoplay style="margin-top:10px"></video>`, true);
  let i = 0; const v = m.querySelector('#paV');
  const next = () => { if (i >= urls.length) return; v.src = urls[i]; m.querySelector('#paIdx').textContent = `${i + 1}/${urls.length}`; i++; v.play().catch(() => {}); };
  v.addEventListener('ended', next); next();
}
function voiceModal(jid) {
  const chars = S.p.assets.filter(a => ['character', 'creature'].includes(a.kind)).map(a => a.name);
  modal(`<h3>从这个版本截取角色音色</h3><div class="muted small">截出的音频会成为音频资产「角色名音色」的新版本，之后的提示词自动写“声线参考@角色音色”。</div>
    <div class="kv" style="margin-top:10px"><span>角色</span><select id="vcChar">${chars.map(c => `<option>${esc(c)}</option>`).join('')}</select>
    <span>开始（秒）</span><input id="vcStart" type="number" step="0.1" min="0" value="0"><span>时长（秒）</span><input id="vcSec" type="number" step="0.5" min="1" max="30" value="5"></div>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button onclick="closeModal(this)">取消</button>
      <button class="primary" onclick="extractVoice('${jid}',this)">截取</button></div>`, true);
}
async function extractVoice(jid, btn) {
  const r = await act(btn, () => api('POST', `/jobs/${jid}/extract-voice`, {character: $('#vcChar').value, start: +$('#vcStart').value, seconds: +$('#vcSec').value}),
    r => `已生成 ${r.asset.name} v${r.asset.current_version}`);
  if (r) closeModal(btn);
}

// ---------------- 质检 / 配音 ----------------
function qcSummary(shots) {
  const qs = shots.map(s => s.job && s.job.qc).filter(Boolean);
  if (!qs.length) return '';
  const n = k => qs.filter(q => q.verdict === k).length;
  const bad = shots.filter(s => s.job && s.job.qc && s.job.qc.verdict === 'fail');
  return `<div class="row small" style="margin-top:8px"><span class="muted">质检</span><span class="badge ok">合格 ${n('pass')}</span><span class="badge warn">待看 ${n('warn')}</span><span class="badge bad">不合格 ${n('fail')}</span>
    ${bad.length ? `<span class="muted">不合格：</span>${bad.map(s => `<span class="chip click" onclick="S.shotId='${s.id}';render()">${s.code}</span>`).join('')}
    <button class="small" onclick="S.sel=new Set(${esc(JSON.stringify(bad.map(s => s.id)))});render()">选中不合格的</button>` : ''}</div>`;
}
function qcPanel(j) {
  const q = j.qc;
  if (!q) return `<div class="row small" style="margin-top:8px"><span class="muted">还没质检</span><span class="spacer"></span><button class="small" onclick="qcJob('${j.id}',this)">质检这个版本</button></div>`;
  return `<div class="card" style="margin:8px 0 0;padding:10px"><div class="row">${qcBadge(q)}
    ${q.character_consistency != null ? `<span class="small muted">人物一致 ${q.character_consistency} · 符合分镜 ${q.prompt_adherence}</span>` : ''}
    <span class="spacer"></span><button class="small" onclick="qcJob('${j.id}',this)">重新质检</button>
    ${q.issues.length ? `<button class="small" onclick="retakeShot('${j.shot_id}',this)" title="把下面的问题写进提示词再出一版">按意见重抽</button>` : ''}</div>
    <div class="small" style="margin-top:6px">${esc(q.summary)}</div>
    ${q.issues.length ? `<ul class="small" style="margin:6px 0 0 18px;padding:0">${q.issues.map(i => `<li><b>${esc(i.type)}</b>：${esc(i.detail)}</li>`).join('')}</ul>` : ''}
    <div class="small muted" style="margin-top:4px">${q.checks.map(c => `${c.ok ? '✓' : '✗'} ${esc(c.name)}（${esc(c.detail)}）`).join(' · ')}</div>
    ${q.note ? `<div class="small muted">${esc(q.note)}</div>` : ''}
    ${q.frame_urls.length ? `<div class="row" style="margin-top:6px;gap:4px">${q.frame_urls.map(u => `<a href="${esc(u)}" target="_blank"><img src="${esc(u)}" style="height:64px;border-radius:4px"></a>`).join('')}</div>` : ''}</div>`;
}
function dubPanel(s) {
  const d = s.dub, lines = s.dialogue && s.dialogue.trim();
  if (!lines) return '';
  return `<div class="card" style="margin:8px 0 0;padding:10px"><div class="row"><b class="small">配音</b>
    ${d ? `<span class="small muted">${d.lines.length} 句${d.tempo > 1.001 ? ` · 加速 ${d.tempo}×` : ''}</span>${d.overflow ? '<span class="badge warn">台词超出镜头时长，已截断</span>' : ''}` : '<span class="small muted">还没配音</span>'}
    <span class="spacer"></span><button class="small" onclick="dubShot('${s.id}',this)">${d ? '重新配音' : '配音本镜'}</button>
    ${d ? `<button class="small ghost" onclick="act(this,()=>api('DELETE','/projects/${S.pid}/dub?shot_id=${s.id}'),'已清除配音')">清除</button>` : ''}</div>
    ${d ? `<audio controls src="${esc(d.url)}" style="margin-top:6px;width:100%"></audio>
      <div class="small muted">${d.lines.map(x => `${x.start.toFixed(1)}s ${esc(x.speaker)}（${esc(voiceName(x.voice))}）：${esc(x.text)}`).join('<br>')}</div>` : ''}</div>`;
}
function voiceRow(a) {
  return `<div class="row small" style="margin-top:6px;gap:6px"><span class="muted">配音</span>
    <select style="flex:1;min-width:0" onchange="setVoice('${a.id}',this.value)"><option value="">自动（${esc(voiceName(a.voice))}）</option>${voiceOpts(a.tts_voice)}</select>
    <button class="small" title="试听（真实配音模型会扣 1 分钱以内）" onclick="sampleVoice(this,'${a.id}')">▶ 试听</button></div>`;
}
async function setVoice(aid, voice) { await act(null, () => api('POST', `/assets/${aid}/voice`, {voice}), voice ? '已设置音色：' + voiceName(voice) : '改为自动分配'); }
async function sampleVoice(btn, aid) {
  const vs = await api('GET', `/api/voices?project=${S.pid}`), c = vs.characters.find(x => x.asset_id === aid);
  if (!c) return;
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/voice-sample`, {voice: c.voice, text: `我是${c.name}，这是我的声音。`}), `试听：${voiceName(c.voice)}${c.auto ? '（自动分配）' : ''}`);
  if (r) new Audio(r.url).play().catch(() => {});
}
async function qcJob(jid, btn) { await act(btn, () => api('POST', `/jobs/${jid}/qc`), r => `质检 ${r.score} 分 · ${QC[r.verdict][1]}`); }
async function qcEpisode(btn, epNo) {
  await act(btn, () => api('POST', `/projects/${S.pid}/qc`, {episode_no: epNo, force: true}), r => `质检 ${r.checked} 镜：合格 ${r.pass} · 待看 ${r.warn} · 不合格 ${r.fail}`);
}
async function dubShot(sid, btn) {
  if (!confirmCost([S.p.settings.tts_model], '配音')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/dub`, {shot_ids: [sid], force: true}), r => `已配音（${rmb(r.charged_rmb)}）`);
}
async function dubEpisode(btn, epNo) {
  if (!confirmCost([S.p.settings.tts_model], '配音本集')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/dub`, {episode_no: epNo}), r => `配音 ${r.dubbed} 镜，跳过 ${r.skipped} 镜（${rmb(r.charged_rmb)}）${r.errors && r.errors.length ? '，失败 ' + r.errors.length : ''}`);
}

function reviewBar(j) {
  return `<div class="row small" style="margin-top:8px"><span class="muted">审片</span>
    ${j.review === 'approved' ? '<span class="badge ok">已通过</span>' : j.review === 'rejected' ? `<span class="badge bad">打回</span><span class="muted">${esc(j.review_note)}</span>` : '<span class="muted">未审</span>'}
    <span class="spacer"></span><button class="small" onclick="reviewJob('${j.id}','approved',this)">✓ 通过</button>
    <button class="small danger" onclick="reviewJob('${j.id}','rejected',this)">✗ 打回</button>
    ${j.review ? `<button class="small ghost" onclick="reviewJob('${j.id}','',this)">撤销</button>` : ''}</div>`;
}
function retakeInfo(j, s) {
  const r = j.retake, old = s.jobs.find(x => x.id === r.from_job);
  return `<div class="small muted" style="margin-top:6px">这一版是按意见重抽自 v${r.from_version}${r.from_score != null ? `（原 ${r.from_score} 分${j.qc ? ` → 现 ${j.qc.score} 分` : ''}）` : ''}${r.auto ? '，自动' : ''}：${esc((r.changes || []).join('；'))}
    ${old && old.play_url ? ` <a href="${esc(old.play_url)}" target="_blank">看原版本</a>` : ''}</div>`;
}
function reviewStats(e) {
  const shots = S.p.shots.filter(s => s.episode_id === e.id && s.job && s.job.status === 'succeeded');
  if (!shots.length) return '';
  const n = k => shots.filter(s => (s.job.review || '') === k).length;
  return `<div class="small muted" style="margin-top:4px">审片：通过 ${n('approved')} · 打回 ${n('rejected')} · 未审 ${n('')}</div>`;
}
async function reviewJob(jid, status, btn) {
  let note = '';
  if (status === 'rejected') { note = prompt('打回原因（会写进重抽的提示词）：', ''); if (note === null) return; if (!note.trim()) return toast('打回要写一句原因', true); }
  await act(btn, () => api('POST', `/jobs/${jid}/review`, {status, note}), status === 'approved' ? '已通过' : status === 'rejected' ? '已打回' : '已撤销');
}
async function retakeShot(sid, btn) {
  if (!confirmCost([S.p.settings.video_model, S.p.settings.text_model], '按意见重抽')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/retake`, {shot_ids: [sid]}), r => r.submitted.length ? `已重抽 v${r.submitted[0].version}：${(r.plans[0].changes || []).join('；').slice(0, 60)}` : (r.skipped[0] || {}).reason || '没有提交');
}
async function retakeEpisode(btn, epNo) {
  if (!confirmCost([S.p.settings.video_model, S.p.settings.text_model], '重抽不合格镜头')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/retake`, {episode_no: epNo}), r => `已重抽 ${r.submitted.length} 个：${r.submitted.map(x => x.code).join('、')}（约 ${rmb(r.quote.total_rmb)}）`);
}
async function splitShot(sid, btn) {
  const s = S.p.shots.find(x => x.id === sid);
  if (!confirm(`把镜头 ${s.code} 按时间轴一分为二？两张卡都要重新生成提示词和视频，原来的视频保留在历史里。`)) return;
  const r = await act(btn, () => api('POST', `/shots/${sid}/split`), r => `已拆成 ${r.codes.join(' 和 ')}（${r.durations.join('s / ')}s）`);
  if (r) { const m = btn && btn.closest('.modal'); if (m) m.remove(); }
}

const EXPORTS = [['douyin', '抖音 1080P'], ['kuaishou', '快手 1080P'], ['shipinhao', '视频号 1080P'], ['bilibili', 'B 站 1080P'], ['master', '母版（原分辨率）']];
function produceModal() {
  const st = S.p.settings;
  modal(`<h3>批量生产</h3><div class="muted small">逐集：分镜 → 站位图 → 提示词 → 报价 → 出片 → 质检 → 重抽不合格（一轮）→ 配音/合成 → 导出。
    一集失败不影响下一集；结束后生成生产报告并发通知。已有的分镜、提示词、已出片的镜头会跳过，不会重复花钱。</div>
    <div class="kv" style="margin-top:10px"><span>集数</span><div>${S.p.episodes.map(e => `<label class="small" style="margin-right:10px"><input type="checkbox" class="pe" value="${e.no}" ${e.final_url ? '' : 'checked'}> 第${e.no}集${e.final_url ? '（已成片）' : ''}</label>`).join('') || '先解析剧本'}</div>
    <span>单集出片上限（元）</span><input id="pBud" type="number" min="0" step="1" value="0" title="0 = 不限（仍受项目预算约束）">
    <span>不合格重抽一轮</span><select id="pRe"><option value="1">开</option><option value="0">关</option></select>
    <span>导出</span><select id="pEx"><option value="">不导出</option>${EXPORTS.map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
    <span>开始时间</span><input id="pAt" placeholder="留空 = 立即；例如 01:30（服务器时间）"></div>
    <div class="small muted" style="margin-top:8px">出片：${esc(st.video_model)} · ${esc(st.resolution)}；配音：${esc(DUB[st.dub_mode] || st.dub_mode)}；项目剩余预算 ${rmb(S.p.spend.remaining_rmb)}</div>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button onclick="closeModal(this)">取消</button><button class="primary" onclick="startProduce(this)">开始</button></div>`, true);
}
async function startProduce(btn) {
  const eps = [...document.querySelectorAll('.pe:checked')].map(x => +x.value);
  if (!eps.length) return toast('至少选一集', true);
  if (!confirmCost([S.p.settings.video_model, S.p.settings.image_model, S.p.settings.text_model], `批量生产 ${eps.length} 集`)) return;
  const r = await act(btn, () => api('POST', `/projects/${S.pid}/produce`, {episodes: eps, per_episode_budget_rmb: +$('#pBud').value,
    retake: $('#pRe').value === '1', export_preset: $('#pEx').value, start_at: $('#pAt').value.trim()}), '已开始，进度在右上角');
  if (r) closeModal(btn);
}
async function exportEp(btn, no) {
  await act(btn, () => api('POST', `/projects/${S.pid}/export`, {episode_no: no, preset: $('#xp-' + no).value, cover_first: $('#xc-' + no).checked}),
    r => `已导出 ${r.width}×${r.height}，${r.seconds.toFixed(1)} 秒，${(r.bytes / 1048576).toFixed(1)} MB`);
}

// ---------------- 剪辑（时间线精修 + 审片） ----------------
const XF = [[0, '硬切'], [0.3, '叠化 0.3s'], [0.5, '叠化 0.5s'], [1, '叠化 1s']];
function keptSec(s) { const d = (s.job && s.job.seconds) || s.duration_s; return Math.max(0.5, d - (s.trim_in || 0) - (s.trim_out || 0)); }
function pgEdit() {
  const e = curEp(); if (!e) return '<div class="empty">先在剧本页解析分集</div>';
  const shots = S.p.shots.filter(s => s.episode_id === e.id);
  const ready = shots.filter(s => s.job && s.job.status === 'succeeded');
  const total = ready.reduce((t, s, i) => t + keptSec(s) - (i < ready.length - 1 ? (s.xfade_s || 0) : 0), 0);
  const col = s => !s.job || s.job.status !== 'succeeded' ? 'var(--line)' : s.job.review === 'rejected' ? 'var(--bad)'
    : s.job.qc ? ({pass: 'var(--ok)', warn: 'var(--warn)', fail: 'var(--bad)'})[s.job.qc.verdict] : 'var(--accent)';
  const n = k => ready.filter(s => (s.job.review || '') === k).length;
  return `<div class="card"><div class="bar"><h3 style="margin:0">剪辑</h3>${epSelect()}
    <span class="badge">${ready.length}/${shots.length} 镜可用 · 约 ${total.toFixed(1)} 秒</span>
    <span class="badge ok">通过 ${n('approved')}</span><span class="badge bad">打回 ${n('rejected')}</span><span class="badge">未审 ${n('')}</span>
    <span class="spacer"></span><button onclick="retakeEpisode(this,${e.no})">重抽不合格/打回的</button>
    <button class="primary" onclick="act(this,()=>api('POST','/projects/${S.pid}/assemble?episode_no=${e.no}&allow_partial=true'),'已按剪辑合成本集')">按剪辑合成本集</button>
    ${e.final_url ? `<a href="${esc(e.final_url)}" target="_blank"><button>看成片</button></a>` : ''}</div>
    <div class="small muted" style="margin-top:6px">模型出片常见“起势慢、收尾拖”：用片头/片尾剪掉多余的部分，镜头之间可选叠化。颜色：绿=质检合格，黄=待看，红=不合格或打回，灰=还没出片。</div>
    <div class="timeline">${shots.map(s => `<div class="tl-clip" title="${s.code}" style="flex:${keptSec(s)};border-color:${col(s)}" onclick="document.getElementById('cut-${s.id}').scrollIntoView({behavior:'smooth',block:'center'})">
      ${s.job && s.job.poster_url ? `<img src="${esc(s.job.poster_url)}">` : ''}<b>${s.code}</b></div>${s.xfade_s ? '<span class="tl-x" title="叠化">⋈</span>' : ''}`).join('')}</div></div>
  ${shots.map(cutRow).join('')}`;
}
function cutRow(s) {
  const j = s.job && s.job.status === 'succeeded' ? s.job : null;
  if (!j) return `<div class="card row" id="cut-${s.id}"><b>${s.code}</b>${badge(s.status)}<span class="muted small">还没出片</span><span class="spacer"></span><button class="small" onclick="S.shotId='${s.id}';go('videos')">去视频页</button></div>`;
  const d = j.seconds || s.duration_s, a = s.trim_in || 0, b = d - (s.trim_out || 0);
  const src = j.play_url ? `${j.play_url}#t=${a.toFixed(2)},${b.toFixed(2)}` : '';
  return `<div class="card" id="cut-${s.id}"><div class="split2" style="grid-template-columns:220px 1fr">
    <div>${src ? `<video controls preload="metadata" src="${esc(src)}" poster="${esc(j.poster_url || '')}" style="max-height:300px"></video>` : '<div class="thumb ph small">无本地文件</div>'}</div>
    <div><div class="row"><b>${s.code}</b><span class="muted small">v${j.version} · 原长 ${(+d).toFixed(1)}s → 保留 ${keptSec(s).toFixed(1)}s</span>${qcBadge(j.qc)}
      <span class="spacer"></span><button class="small" onclick="S.shotId='${s.id}';go('videos')">版本 / 重抽</button></div>
      <div class="small" style="margin-top:6px">${esc(s.dialogue || '').replace(/\n/g, ' / ') || '<span class="muted">（无台词）</span>'}</div>
      <div class="row small" style="margin-top:8px;gap:12px">
        <label>片头剪掉 <input type="number" min="0" step="0.1" value="${a}" style="width:70px" onchange="setCut('${s.id}',{trim_in:+this.value})"> 秒</label>
        <label>片尾剪掉 <input type="number" min="0" step="0.1" value="${s.trim_out || 0}" style="width:70px" onchange="setCut('${s.id}',{trim_out:+this.value})"> 秒</label>
        <label>到下一镜 <select onchange="setCut('${s.id}',{xfade_s:+this.value})">${XF.map(([v, t]) => `<option value="${v}" ${(+s.xfade_s || 0) === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label></div>
      ${s.dub ? `<div class="small muted" style="margin-top:4px">配音：${s.dub.lines.map(x => `${x.start.toFixed(1)}-${x.end.toFixed(1)}s ${esc(x.speaker)}`).join('，')}（剪辑不要剪到台词）</div>` : ''}
      ${j.qc && j.qc.issues.length ? `<div class="small warnc" style="margin-top:4px">质检：${esc(j.qc.issues.slice(0, 2).map(i => i.detail).join('；'))}</div>` : ''}
      ${reviewBar(j)}</div></div></div>`;
}
async function setCut(sid, body) {
  const r = await act(null, () => api('POST', `/shots/${sid}/cut`, body), r => `已保存：保留 ${r.kept_seconds}s`);
  if (r && r.cuts_dialogue) toast(`注意：剪到了配音台词“${r.cuts_dialogue.slice(0, 20)}”`, true);
}

// ---------------- 成片 ----------------
function pgFinal() {
  const st = S.p.settings;
  return `<div class="card"><div class="bar"><h3 style="margin:0">成片</h3><span class="muted small">按每个镜头当前选用的版本拼接；字幕取自台词（自动去掉角色名）。</span><span class="spacer"></span>
    <label class="small"><input type="checkbox" ${st.burn_subtitles ? 'checked' : ''} onchange="act(null,()=>api('PATCH','/projects/${S.pid}/settings',{burn_subtitles:this.checked}),'已保存')"> 字幕烧进画面</label>
    <button class="primary" onclick="act(this,()=>api('POST','/projects/${S.pid}/assemble'),'已合成')">合成全部剧集</button>
    <button onclick="act(this,()=>api('POST','/projects/${S.pid}/assemble?allow_partial=true'),'已合成（跳过缺的镜头）')">跳过缺镜头合成</button>
    <a href="${BASE}/projects/${S.pid}/download?what=finals"><button>下载全部成片</button></a>
    <a href="${BASE}/projects/${S.pid}/edit-package"><button title="编号片段 + 字幕 + EDL/FCPXML">导出剪辑工程</button></a>
    <button onclick="genCovers(this,null)">生成全部封面</button><button class="primary" onclick="produceModal()" title="逐集出片→质检→重抽不合格→配音合成→导出，最后出报告">批量生产…</button></div>
    ${S.p.task && S.p.task.kind === 'produce' ? `<div class="small" style="margin-top:6px">最近一次批量生产：${badge(S.p.task.status)} ${(S.p.task.steps || []).filter(x => x.status === 'succeeded').length} 步完成
      ${S.p.task.result && S.p.task.result.report_url ? `· <a href="${esc(S.p.task.result.report_url)}" target="_blank">看生产报告</a>` : ''}${S.p.task.error ? `<span class="err"> ${esc(S.p.task.error)}</span>` : ''}</div>` : ''}
    <div class="row" style="margin-top:8px"><label class="small">配音<select onchange="act(null,()=>api('PATCH','/projects/${S.pid}/settings',{dub_mode:this.value}),'已保存')">${Object.entries(DUB).map(([k, v]) => `<option value="${k}" ${st.dub_mode === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
      <span class="small muted">${st.dub_mode === 'off' ? '用视频模型自带的台词声音' : `合成时自动给缺配音的镜头补配音（旁白：${esc(voiceName(st.narrator_voice))}）`}</span>
      <span class="spacer"></span><button class="small" onclick="go('assets')">设置角色音色</button></div>
    <div class="small muted" style="margin-top:6px">配乐：${st.bgm_material_id ? '已选 ' + esc((S.p.materials.find(m => m.id === st.bgm_material_id) || {}).name || '') + `，音量 ${st.bgm_volume}` : '未设置（在“生成设置”里选一段上传的音频）'}</div></div>
  <div class="grid">${S.p.episodes.map(e => `<div class="card"><div class="row"><b>${esc(e.title)}</b>${e.final_url ? `<span class="badge ok">${e.final_seconds.toFixed(1)} 秒</span>` : '<span class="badge">未合成</span>'}
    <span class="spacer"></span><span class="small muted">${e.videos_done}/${e.shots} 镜头已出片</span></div>${reviewStats(e)}
    ${e.cover_url ? `<img src="${esc(e.cover_url)}" class="thumb tall" style="margin-top:8px;max-width:180px">` : ''}
    ${e.final_url ? `<video controls src="${esc(e.final_url)}" style="margin-top:8px"></video><div class="row" style="margin-top:6px"><a href="${esc(e.final_url)}" download>下载视频</a><a href="${esc(e.srt_url)}" download>下载字幕</a></div>` : ''}
    <div class="row" style="margin-top:8px"><button class="small" onclick="act(this,()=>api('POST','/projects/${S.pid}/assemble?episode_no=${e.no}'),'已合成')">合成本集</button>
      <button class="small" onclick="S.ep=${e.no};go('videos')">去视频页</button>
      <button class="small" onclick="dubEpisode(this,${e.no})">配音本集</button><button class="small" onclick="qcEpisode(this,${e.no})">质检本集</button>
      <button class="small" onclick="genCovers(this,${e.no})">${e.cover_url ? '重做封面' : '生成封面'}</button></div>
    <div class="row small" style="margin-top:8px"><span class="muted">发布导出</span>
      <select id="xp-${e.no}">${EXPORTS.map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
      <label><input type="checkbox" id="xc-${e.no}" ${e.cover_url ? '' : 'disabled'}> 封面放首帧</label>
      <button class="small primary" onclick="exportEp(this,${e.no})" title="放大到平台分辨率、响度统一到 -14 LUFS">导出</button></div>
    ${e.exports && e.exports.length ? `<div class="small" style="margin-top:4px">${e.exports.map(x => `<a href="${esc(x.url)}" download>${esc(x.label)}（${x.mb} MB）</a>`).join(' · ')}</div>` : ''}</div>`).join('') || '<div class="empty">还没分集</div>'}</div>`;
}

async function genCovers(btn, epNo) {
  if (!confirmCost([S.p.settings.image_model], '生成封面')) return;
  await act(btn, () => api('POST', `/projects/${S.pid}/covers${epNo ? '?episode_no=' + epNo : ''}`), r => `封面 ${r.generated.length} 张${r.failed.length ? '，失败 ' + r.failed.length : ''}`);
}

// ---------------- 对话 ----------------
function pgChat() {
  if (!S.chat) { loadChat(); return '<div class="empty">加载中…</div>'; }
  const cur = S.chat.cur;
  return `<div class="split2" style="grid-template-columns:300px 1fr"><div class="card list"><div class="row"><b>对话记录</b><span class="badge">${S.chat.sessions.length} 条</span></div>
    <div class="small muted" style="margin:4px 0 10px">选择智能体新建，或继续已有对话。${S.p ? `会读取项目「${esc(S.p.name)}」的剧本/资产。` : ''}</div>
    ${S.chat.agents.map(a => `<div class="it row" onclick="newChat('${a.id}')"><b>${esc(a.name)}</b><span class="spacer"></span><span class="small muted">＋ 新对话</span></div>`).join('')}
    <div class="small muted" style="margin:10px 0 6px">历史</div>
    ${S.chat.sessions.map(x => `<div class="it ${cur && cur.id === x.id ? 'on' : ''}" onclick="openChat('${x.id}')"><b>${esc(x.title)}</b><div class="small muted">${esc((S.chat.agents.find(a => a.id === x.agent) || {}).name || x.agent)} · ${x.messages} 条</div></div>`).join('') || '<div class="muted small">点击上面的智能体开始第一条对话。</div>'}</div>
  <div class="card">${cur ? `<div class="row"><div><div class="small muted">大模型连续对话</div><h3 style="margin:0">${esc(cur.title)}</h3></div><span class="spacer"></span>
      <button class="danger" onclick="delChat('${cur.id}')">删除对话</button><button onclick="clearChats()">清空记录</button></div>
    <div class="msgs" id="msgs">${cur.messages.map(m => `<div class="msg ${m.role}">${esc(m.content)}</div>`).join('') || '<div class="empty">说点什么吧</div>'}</div>
    <div class="row"><textarea id="chatIn" rows="2" placeholder="输入消息，Enter 发送，Shift+Enter 换行" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat(this.nextElementSibling.nextElementSibling)}"></textarea>
      <select id="chatModel" style="width:180px">${opt(S.models.filter(x => x.type === 'chat'), cur.model || (S.p ? S.p.settings.text_model : ''))}</select>
      <button class="primary" onclick="sendChat(this)">发送</button></div>`
    : '<div class="empty">AI 对话<br>请先创建一条对话。</div>'}</div></div>`;
}
async function loadChat(sid) {
  const [agents, sessions] = await Promise.all([api('GET', '/chat/agents'), api('GET', '/chat/sessions' + (S.pid ? '?project_id=' + S.pid : ''))]);
  S.chat = {agents: agents.items, sessions: sessions.items, cur: null};
  const id = sid || S.chatSid; if (id && sessions.items.some(x => x.id === id)) S.chat.cur = await api('GET', '/chat/sessions/' + id);
  if (S.page === 'chat') render();
}
async function newChat(agent) {
  const s = await api('POST', '/chat/sessions', {agent, project_id: S.pid || ''}).catch(e => toast(e.message, true));
  if (s) { S.chatSid = s.id; loadChat(s.id); }
}
async function openChat(id) { S.chatSid = id; loadChat(id); }
async function sendChat(btn) {
  const t = $('#chatIn'), content = t.value.trim(); if (!content) return;
  const mdl = $('#chatModel').value; if (!confirmCost([mdl], '对话')) return;
  btn.disabled = true; t.value = '';
  S.chat.cur.messages.push({role: 'user', content}, {role: 'assistant', content: '……'}); render();
  try { S.chat.cur = await api('POST', `/chat/sessions/${S.chat.cur.id}/messages`, {content, model: mdl}); await loadChat(S.chat.cur.id); }
  catch (e) { toast(e.message, true); await loadChat(S.chat.cur.id); }
}
async function delChat(id) { if (confirm('删除这条对话？')) { await api('DELETE', '/chat/sessions/' + id); S.chatSid = null; loadChat(); } }
async function clearChats() { if (confirm('清空全部对话记录？')) { await api('DELETE', '/chat/sessions'); S.chatSid = null; loadChat(); } }

// ---------------- 教程 ----------------
function pgHelp() {
  const step = (t, sub, items, page) => `<div class="card"><h3>${t}</h3><div class="muted small">${sub}</div><ol>${items.map(x => `<li>${x}</li>`).join('')}</ol>
    ${page ? `<button onclick="go('${page}')">前往${(PAGES.find(p => p[0] === page) || [])[2]}</button>` : ''}</div>`;
  return `<div class="card"><div class="muted small">短剧工作台 v0.3 · 本机项目、资产版本、站位图、提示词、视频任务和成片，一次讲清</div>
    <h2 style="margin:6px 0">从剧本到成片</h2><div class="flow">${['项目', '剧本', '资产', '分镜', '站位图', '提示词', '视频', '质检', '重抽/审片', '剪辑', '配音', '成片', '发布'].map(x => `<span>${x}</span>`).join('→')}</div>
    <div class="small muted" style="margin-top:8px">模型分“真实 / 模拟”：模拟零成本，用来练手和走流程；真实会扣费，受项目预算约束（超预算的视频不会提交）。</div></div>
  <div class="grid tut">
    ${step('创建项目', '剧本、资产、分镜、成片和历史版本都保存在本机。', ['在首页新建项目并选择风格（真人 / 3D / 2D）。', '项目可以设封面、置顶、下载（导出项目包）；别人用“上传项目”导入。', '右上角“生成设置”选模型、清晰度、画面比例和预算。'], 'home')}
    ${step('上传并解析剧本', '系统识别剧集、场次、角色、场景、道具和生物。', ['粘贴正文或上传 TXT / DOCX / PDF；没有剧本可以用“一键剧本”。', '“第1集”分集，“1-1 日 外 金鸡岭”分场，“人物：甲、乙”列角色，“孔宣（蛋形态）”会自动建形态。', '“AI 智能解析”走模型；“本地抽取”零费用。重新解析前看清是否勾选“清空旧分镜”。'], 'script')}
    ${step('制作资产与管理历史版本', '每个角色形态、场景、道具都可以保留多个版本。', ['核对名称、描述、别称和形态，可点“AI 润色”补全描述。', '生成图片（角色为面部特写 + 三视图）、改图、上传本地图；历史版本里点一下就切换。', '批量上传按文件名匹配：孔宣.png、孔宣-蛋.png。', '音频资产「角色名音色」会自动写进提示词的声线参考；也可在视频页从成片截取。'], 'assets')}
    ${step('按集生成分镜', '每张卡就是一次视频生成，卡内按时间段写画面。', ['选集后点“AI 生成本集”；时长受视频模型限制（如 4-15 秒）。', '逐卡检查画面、表演、台词、站位和时长；可新增、插入、删除卡片。'], 'board')}
    ${step('站位图', '每个场次一张，人物到场后的位置关系，同场景分镜共用。', ['先确保场景和角色有图（右侧会提示“需核对素材”）。', '一键生成本集，或逐场生成；可写站位说明。', '出片选“全能参考”时自动带上站位图。'], 'blocking')}
    ${step('转换并精修视频提示词', '格式：参考资产前缀 + 【镜头】时间轴 + 音效约束。', ['“生成最终提示词”走模型，“本地转换”零费用，“剧本直转提示词”跳过手动分镜。', '用 @名字 或 @名字（形态）引用资产；红色“未找到”要修正，黄色“已更新”表示资产换过图。'], 'prompts')}
    ${step('配置视频卡片并提交', '单镜可改参考模式、模型、分辨率、比例、时长和参考素材。', ['先“报价”看积分，再“生成当前镜头”；或勾选多个镜头“批量生成”。', '每次生成都是新版本，历史版本可选用、下载、截音色。', '失败自动退回预扣；刷新回执或等后台自动轮询。'], 'videos')}
    ${step('自动质检', '每个版本出片后自动打分，不合格的标红，方便集中重抽。', ['技术检查：时长、画幅、分辨率、有台词却没声音。', '视觉检查（真实出片时）：抽 4 帧对照角色定版图，看人物是否一致、是否符合分镜、有无字幕水印和崩坏；约 1 分钱/镜。', '视频页点“质检本集”统一复查；“选中不合格的”后直接批量重新生成。'], 'videos')}
    ${step('重抽与审片', '把“不合格”变成可执行的修改，而不是人工反复试。', ['视频页每个版本可以“通过 / 打回”，打回要写一句原因。', '“按意见重抽”：把质检问题和打回原因写进提示词（如“必须清楚演出：抬头看红绿灯”“画面中只有 @孔宣”），再出一版；新版本记着来自哪一版、改了什么。', '新版本质检分更低时自动保留原版本。设置里可开“不合格自动重抽”（每镜最多 1~3 次，受预算约束）。', '分镜页会标出塞得太满的卡（5 秒里写了 3、4 个动作），一键“拆成两张”。'], 'videos')}
    ${step('剪辑与发布', '把模型出片的“起势慢、收尾拖”剪掉，再按平台导出。', ['“剪辑”页：时间线按质检结果着色；每镜设置片头/片尾剪掉几秒、和下一镜硬切或叠化；剪到配音台词会提醒。', '成片页每集可导出抖音/快手/视频号/B 站 1080P 或母版：自动放大、响度统一到 -14 LUFS，可把封面放在首帧。', '“批量生产…”：选几集、单集上限、是否重抽、导出预设和开始时间，挂着跑，结束出生产报告。', '资产页“一键定版角色”：锁住当前定版图，跨集不会被误改。'], 'edit')}
    ${step('配音 / 旁白', '给角色指定音色，按台词逐句合成配音轨。', ['资产页每个角色卡下选音色、点“试听”；不选会按角色自动分配。旁白音色在“生成设置”里。', '台词写成“角色：台词”，“旁白：…”或没写说话人的行用旁白音色；括号里的表演提示不会读出来。', '台词太长会自动加速（最多 1.35 倍），仍放不下会提示“超出镜头时长”。', '成片页选配音方式：保留原声 / 配音为主 + 原声垫底 / 只用配音；字幕按配音的真实时间对齐。'], 'final')}
    ${step('合成成片', '按集拼接选中的版本，自动出字幕。', ['“合成全部剧集”或单集合成；可选择把字幕烧进画面。', '下载 mp4 和 srt，导入剪辑软件继续精修。'], 'final')}
  </div>`;
}

// ---------------- 冲突提示 ----------------
function showConflict(err) {
  const d = err.detail || {};
  modal(`<h3>保存冲突</h3><p class="muted">${esc(err.message)}。页面已刷新为最新内容，你的草稿在下面，需要的话复制回去再保存。</p>
    ${(d.conflict_fields || []).map(f => `<div style="margin:8px 0"><b>${esc(f)}</b><div class="kv"><span class="muted">最新内容</span><pre>${esc(JSON.stringify(d.current[f]))}</pre>
    <span class="muted">你的草稿</span><pre>${esc(JSON.stringify(d.yours[f]))}</pre></div></div>`).join('')}
    <div class="row" style="justify-content:flex-end"><button class="primary" onclick="closeModal(this)">知道了</button></div>`, true);
}

// ---------------- 启动 ----------------
(async function boot() {
  try { const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* 忽略 */ }
  const [, page, pid] = (location.hash || '').split('/');
  if (page && PAGES.some(p => p[0] === page)) S.page = page;
  if (pid) S.pid = pid;
  try {
    S.health = await api('GET', '/healthz');
    if (S.health.aggregator) S.models = (await api('GET', '/api/models')).items;
    else toast('聚合层没连上（8010），模型列表为空；请用“启动工作台”同时启动两个服务', true);
  } catch (e) { toast('后端连不上：' + e.message, true); }
  try { S.voices = (await api('GET', '/api/voices')).voices; } catch (e) { /* 音色列表拿不到不影响其他功能 */ }
  document.addEventListener('click', e => { if (S.notifOpen && !e.target.closest('.rel')) { S.notifOpen = false; renderChrome(); } });
  window.addEventListener('hashchange', () => { const [, pg, id] = location.hash.split('/'); if (pg && pg !== S.page) { S.page = pg; S.pid = id || S.pid; load(); } });
  await load();
})();
