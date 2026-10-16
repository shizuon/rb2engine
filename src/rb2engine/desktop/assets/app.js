'use strict';
const $ = selector => document.querySelector(selector);
const strings = {
  zh:{fillWaveforms:'补全 Engine 概览波形',waveforms:'补全波形',waveforms_complete:'波形补全完成',waveformConfirm:'补全缺失波形？',waveformProceed:'已退出，补全',fillKeys:'缺失调性时用 OpenKeyScan 补全',keyscanMissing:'未找到 OpenKeyScan。',download:'下载',chooseProgram:'选择已安装程序…',drive:'U 盘',browse:'选择…',inspect:'读取曲库',artwork:'包含封面',convert:'转换',verify:'校验',report:'报告',placeholder:'选择 U 盘…',tracks:'首曲目',playlists:'个歌单',missing:'个文件缺失',confirmTitle:'开始转换？',closeApps:'请先退出 rekordbox 和 Engine DJ。',replaceNote:'现有 Engine 数据库将备份并重建。',cancel:'取消',confirm:'已退出，转换',working:'处理中',complete:'转换完成',warning:'已完成，有提示',error:'操作失败',verified:'校验通过',inspect_first:'请先读取曲库。',close_apps:'请先退出 DJ 软件。',confirm_replace:'请确认重建现有数据库。',busy:'正在处理，请稍候。',closing:'请等待任务完成后再关闭。',bridgeError:'请使用 rb2engine 应用打开。',connectionError:'无法连接本地应用。',noTracks:'没有曲目',phase:{'preparing waveforms':'准备波形组件…','generating waveforms':'生成波形…','writing waveforms':'写入并校验波形…','starting key analysis':'启动 OpenKeyScan…','analyzing keys':'补全调性…','scanning':'读取曲库…','reading tracks':'读取曲目…','building library':'构建曲库…','album art':'写入封面…','writing tracks':'写入曲目…','verifying':'校验中…'}},
  en:{fillWaveforms:'Fill Engine overview waveforms',waveforms:'Fill waveforms',waveforms_complete:'Waveforms complete',waveformConfirm:'Fill missing waveforms?',waveformProceed:'Closed, fill',fillKeys:'Fill missing keys with OpenKeyScan',keyscanMissing:'OpenKeyScan not found.',download:'Download',chooseProgram:'Locate installation…',drive:'USB drive',browse:'Choose…',inspect:'Read library',artwork:'Include artwork',convert:'Convert',verify:'Verify',report:'Report',placeholder:'Choose your USB…',tracks:'tracks',playlists:'playlists',missing:'missing files',confirmTitle:'Start conversion?',closeApps:'Close rekordbox and Engine DJ first.',replaceNote:'The existing Engine database will be backed up and rebuilt.',cancel:'Cancel',confirm:'Closed, convert',working:'Working',complete:'Conversion complete',warning:'Finished with notes',error:'Operation failed',verified:'Verification passed',inspect_first:'Read the library first.',close_apps:'Close DJ software first.',confirm_replace:'Confirm rebuilding the existing database.',busy:'An operation is running.',closing:'Wait for the operation to finish before closing.',bridgeError:'Open using the rb2engine app.',connectionError:'Cannot connect to the local application.',noTracks:'No tracks',phase:{'preparing waveforms':'Preparing waveform tools…','generating waveforms':'Generating waveforms…','writing waveforms':'Writing and verifying waveforms…','starting key analysis':'Starting OpenKeyScan…','analyzing keys':'Filling missing keys…','scanning':'Reading library…','reading tracks':'Reading tracks…','building library':'Building library…','album art':'Writing artwork…','writing tracks':'Writing tracks…','verifying':'Verifying…'}}
};
let language = (navigator.language || '').startsWith('zh') ? 'zh' : 'en';
let state = null, bridge = null, pollActive = false, edited = false, keyscanPath = "", pendingAction = "convert";
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
  ['path','browse','inspect','artwork','fill-keys','fill-waveforms','keyscan-choose'].forEach(id => { $('#' + id).disabled = busy || !bridge; });
  $('#inspect').disabled = busy || !bridge || !$('#path').value.trim();
  $('#convert').disabled = busy || !valid || !s.summary.tracks;
  $('#waveforms').hidden = !valid || !s.summary.existing;
  $('#waveforms').disabled = busy;
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
function confirmAction(action) {
  pendingAction = action;
  $('#confirm-dialog h2').textContent = t(action === 'waveforms' ? 'waveformConfirm' : 'confirmTitle');
  $('#confirm-convert').textContent = t(action === 'waveforms' ? 'waveformProceed' : 'confirm');
  $('#replace-note').hidden = action === 'waveforms' || !state.summary.existing;
  $('#confirm-dialog').showModal();
}
$('#convert').addEventListener('click', () => confirmAction('convert'));
$('#waveforms').addEventListener('click', () => confirmAction('waveforms'));
$('#cancel-convert').addEventListener('click', () => $('#confirm-dialog').close());
$('#confirm-convert').addEventListener('click', () => {
  $('#confirm-dialog').close();
  if (pendingAction === 'waveforms') invoke('fill_waveforms', true);
  else invoke('convert_drive', $('#artwork').checked, true, Boolean(state.summary.existing), $('#fill-keys').checked, $('#fill-waveforms').checked);
});
$('#verify').addEventListener('click', () => invoke('verify_drive'));
async function checkKeyscan() {
  const info = await bridge.get_keyscan();
  keyscanPath = info.path;
  $('#keyscan-help').hidden = !$('#fill-keys').checked || Boolean(keyscanPath);
}
$('#fill-keys').addEventListener('change', () => checkKeyscan().catch(error => showError(error.message)));
$('#keyscan-choose').addEventListener('click', async () => {
  try { await bridge.choose_keyscan(); await checkKeyscan(); } catch(error) { showError(error.message); }
});
$('#keyscan-download').addEventListener('click', () => bridge.open_keyscan_download());
window.addEventListener('focus', () => { if (bridge && $('#fill-keys').checked) checkKeyscan().catch(() => {}); });
translate();
['browse','inspect','artwork','fill-keys','fill-waveforms'].forEach(id => { $('#' + id).disabled = true; });
window.addEventListener('pywebviewready', async () => {
  bridge = window.pywebview.api;
  await poll(); await refreshDrives(); await checkKeyscan();
  window.rb2engineReady = true;
  setInterval(poll, 350);
});
setTimeout(() => { if (!bridge) showError('bridgeError'); }, 12000);
