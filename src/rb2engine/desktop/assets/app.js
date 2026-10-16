'use strict';
const $ = selector => document.querySelector(selector);
const strings = {
  zh:{drive:'U 盘',browse:'选择…',inspect:'读取曲库',artwork:'包含封面',convert:'转换',verify:'校验',report:'报告',placeholder:'选择 U 盘…',tracks:'首曲目',playlists:'个歌单',missing:'个文件缺失',confirmTitle:'开始转换？',closeApps:'请先退出 rekordbox 和 Engine DJ。',replaceNote:'现有 Engine 数据库将备份并重建。',cancel:'取消',confirm:'已退出，转换',working:'处理中',complete:'转换完成',warning:'已完成，有提示',error:'操作失败',verified:'校验通过',inspect_first:'请先读取曲库。',close_apps:'请先退出 DJ 软件。',confirm_replace:'请确认重建现有数据库。',busy:'正在处理，请稍候。',closing:'请等待任务完成后再关闭。',bridgeError:'请使用 rb2engine 应用打开。',connectionError:'无法连接本地应用。',noTracks:'没有曲目',phase:{'scanning':'读取曲库…','reading tracks':'读取曲目…','building library':'构建曲库…','album art':'写入封面…','writing tracks':'写入曲目…','verifying':'校验中…'}},
  en:{drive:'USB drive',browse:'Choose…',inspect:'Read library',artwork:'Include artwork',convert:'Convert',verify:'Verify',report:'Report',placeholder:'Choose your USB…',tracks:'tracks',playlists:'playlists',missing:'missing files',confirmTitle:'Start conversion?',closeApps:'Close rekordbox and Engine DJ first.',replaceNote:'The existing Engine database will be backed up and rebuilt.',cancel:'Cancel',confirm:'Closed, convert',working:'Working',complete:'Conversion complete',warning:'Finished with notes',error:'Operation failed',verified:'Verification passed',inspect_first:'Read the library first.',close_apps:'Close DJ software first.',confirm_replace:'Confirm rebuilding the existing database.',busy:'An operation is running.',closing:'Wait for the operation to finish before closing.',bridgeError:'Open using the rb2engine app.',connectionError:'Cannot connect to the local application.',noTracks:'No tracks',phase:{'scanning':'Reading library…','reading tracks':'Reading tracks…','building library':'Building library…','album art':'Writing artwork…','writing tracks':'Writing tracks…','verifying':'Verifying…'}}
};
let language = (navigator.language || '').startsWith('zh') ? 'zh' : 'en';
let state = null, bridge = null, pollActive = false, edited = false;
const t = key => strings[language][key] || key;
function translate() {
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-i]').forEach(el => { el.textContent = t(el.dataset.i); });
  $('#language').textContent = language === 'zh' ? 'EN' : '中文';
  $('#path').placeholder = t('placeholder');
  $('#inspect').title = t('inspect'); $('#inspect').setAttribute('aria-label', t('inspect'));
  if (state) render(state);
}
function showError(message) {
  $('#status-box').hidden = false; $('#status-box').className = 'error';
  $('#status-title').textContent = t('error');
  $('#status-message').textContent = t(message); $('#status-message').hidden = false;
  $('#progress').hidden = true;
}
function render(s) {
  state = s;
  const valid = Boolean(s.summary && !edited), busy = s.busy;
  ['path','browse','inspect','artwork'].forEach(id => { $('#' + id).disabled = busy || !bridge; });
  $('#inspect').disabled = busy || !bridge || !$('#path').value.trim();
  $('#convert').disabled = busy || !valid || !s.summary.tracks;
  $('#counts').textContent = valid ? `${s.summary.tracks.toLocaleString()} ${t('tracks')} · ${s.summary.playlists.toLocaleString()} ${t('playlists')}` : '';
  $('#missing').textContent = valid && s.summary.missing ? `${s.summary.missing} ${t('missing')}` : '';
  $('#status-box').hidden = ['idle','ready'].includes(s.status) || (!busy && edited);
  $('#status-box').className = s.status;
  $('#status-title').textContent = busy ? (strings[language].phase[s.phase] || t('working')) : t(s.status);
  const hasTotal = s.total > 0;
  $('#progress-count').textContent = busy && hasTotal ? `${s.done.toLocaleString()} / ${s.total.toLocaleString()}` : '';
  $('#progress').hidden = !busy; $('#progress').classList.toggle('indeterminate', !hasTotal);
  const percent = hasTotal ? Math.max(0, Math.min(100, s.done / s.total * 100)) : 0;
  $('#progress i').style.width = hasTotal ? percent + '%' : '';
  if (hasTotal) $('#progress').setAttribute('aria-valuenow', Math.round(percent)); else $('#progress').removeAttribute('aria-valuenow');
  const message = s.close_blocked ? t('closing') : (s.status === 'error' ? s.error : '');
  $('#status-message').textContent = message; $('#status-message').hidden = !message;
  $('#verify').hidden = !valid || !s.summary.existing || busy;
  $('#details').hidden = !s.details || edited;
  // Source filenames and error text must never become HTML.
  $('#report-content').textContent = s.details || '';
}
async function poll() {
  if (!bridge || pollActive) return;
  pollActive = true;
  try { render(await bridge.get_state()); } catch { showError('connectionError'); }
  finally { pollActive = false; }
}
async function invoke(method, ...args) {
  if (!bridge) return;
  try {
    const result = await bridge[method](...args);
    if (result && result.ok === false) showError(result.error); else await poll();
  } catch (error) { showError(error.message || 'connectionError'); }
}
async function inspect() {
  if (!$('#path').value.trim() || (state && state.busy)) return;
  edited = false;
  await invoke('inspect_drive', $('#path').value);
}
async function refreshDrives() {
  try {
    $('#volumes').replaceChildren();
    for (const drive of await bridge.get_drives()) {
      const option = document.createElement('option'); option.value = drive.path; option.label = drive.name;
      $('#volumes').appendChild(option);
    }
  } catch { /* Folder chooser and manual paths remain available. */ }
}
$('#language').addEventListener('click', () => { language = language === 'zh' ? 'en' : 'zh'; translate(); });
$('#path').addEventListener('input', () => { edited = true; if (state) render(state); });
$('#path').addEventListener('change', inspect);
$('#path').addEventListener('keydown', event => { if (event.key === 'Enter') inspect(); });
$('#browse').addEventListener('click', async () => {
  try { const path = await bridge.choose_folder(); if (path) { $('#path').value = path; await inspect(); } await refreshDrives(); }
  catch (error) { showError(error.message); }
});
$('#inspect').addEventListener('click', inspect);
$('#convert').addEventListener('click', () => { $('#replace-note').hidden = !state.summary.existing; $('#confirm-dialog').showModal(); });
$('#cancel-convert').addEventListener('click', () => $('#confirm-dialog').close());
$('#confirm-convert').addEventListener('click', () => {
  $('#confirm-dialog').close();
  invoke('convert_drive', $('#artwork').checked, true, Boolean(state.summary.existing));
});
$('#verify').addEventListener('click', () => invoke('verify_drive'));
translate();
['browse','inspect','artwork'].forEach(id => { $('#' + id).disabled = true; });
window.addEventListener('pywebviewready', async () => {
  bridge = window.pywebview.api;
  await poll(); await refreshDrives();
  window.rb2engineReady = true;
  setInterval(poll, 350);
});
setTimeout(() => { if (!bridge) showError('bridgeError'); }, 12000);
