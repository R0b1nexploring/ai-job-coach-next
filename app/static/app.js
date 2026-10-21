const form = document.querySelector('#generator-form');
const jdInput = document.querySelector('#jd');
const materialInput = document.querySelector('#material');
const statusEl = document.querySelector('#status');
const resultsEl = document.querySelector('#results');
const resumeEl = document.querySelector('#resume');
const guideEl = document.querySelector('#guide');
const guideTitleEl = document.querySelector('#guide-title');
const guideStatusEl = document.querySelector('#guide-status');
const guideDownloadEl = document.querySelector('#download-guide');
const retryGuideEl = document.querySelector('#retry-guide');
const generateButton = document.querySelector('#generate');
const historyEl = document.querySelector('#batch-history');
const editor = document.querySelector('#editor');
const editItems = document.querySelector('#edit-items');
const editPlans = document.querySelector('#edit-plans');
const retryBatchEl = document.querySelector('#retry-batch');
let currentBatch = null;
let currentKind = 'job';
let pollTimer = null;
let editSnapshot = null;

function textElement(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}
function setStatus(message, kind = '') {
  statusEl.textContent = message;
  statusEl.className = `status ${kind}`;
}
function describeStatus(batch) {
  if (batch.status === 'queued') return ['任务已排队。离开页面后回来也能继续查看。', ''];
  if (batch.status === 'running') return ['正在生成双版本简历…', ''];
  if (batch.status === 'guides_pending') return ['双版本已生成，正在分别准备面试指南。', ''];
  if (batch.status === 'guides_partial') return ['简历已生成；一份指南未完成，可单独重试。', 'error'];
  if (batch.status === 'failed') return ['生成没有完成，可重试这次任务。', 'error'];
  if (batch.source === 'degraded_local') return ['真实模型未完成，本次显示的是离线兜底草稿，请勿当作质量验收。', 'error'];
  if (batch.unverified_targets?.length) return [`双版本与指南已生成；${batch.unverified_targets.length} 项岗位目标为 AI 推断，请对照 JD 核对。`, 'error'];
  return ['双版本简历与两份面试指南已生成。', 'ready'];
}
function renderResume(resume) {
  resumeEl.replaceChildren();
  if (!resume) { resumeEl.append(textElement('p', '简历正在生成…', 'empty')); return; }
  const header=resume.header || {};
  const personal=textElement('div','','resume-personal');
  personal.append(textElement('h3',header.name || '姓名：________________',header.name?'':'unfilled'));
  personal.append(textElement('p',[['phone','电话','____________'],['email','邮箱','________________'],['location','所在城市','________']].map(([key,label,blank])=>`${label}：${header[key] || blank}`).join('  |  '),'resume-contact'));
  const fill=textElement('button','填写个人信息与教育经历','resume-fill'); fill.type='button'; fill.addEventListener('click',()=>{document.querySelector('#edit-resume').click();document.querySelector('#edit-name').focus()}); personal.append(fill);
  resumeEl.append(personal);
  const education=textElement('section','','resume-education'); education.append(textElement('h4','教育经历')); education.append(textElement('p',header.education || '学校：________________    专业：____________\n学历：________    就读时间：________________',header.education?'':'unfilled')); resumeEl.append(education);
  resumeEl.append(textElement('h4', resume.title,'resume-body-title'));
  resumeEl.append(textElement('p', resume.summary, 'summary'));
  resume.items.forEach((item, index) => {
    const wrapper = textElement('div', '', 'resume-item');
    wrapper.append(textElement('span', `项目要点 ${String(index + 1).padStart(2, '0')}`, 'index'));
    wrapper.append(textElement('p', item.text));
    resumeEl.append(wrapper);
  });
}
function renderGuide(guide) {
  guideEl.replaceChildren();
  retryGuideEl.hidden = true;
  guideDownloadEl.hidden = !guide || guide.status !== 'ready';
  if (!guideDownloadEl.hidden) guideDownloadEl.href = `/api/batches/${currentBatch.id}/guides/${currentKind}/download`;
  if (!guide) { guideStatusEl.textContent = '等待简历'; guideEl.append(textElement('p', '简历生成后会自动准备对应指南。', 'empty')); return; }
  guideStatusEl.textContent = {queued:'排队中',running:'生成中',ready:'已完成',failed:'生成失败',stale:'需更新'}[guide.status] || guide.status;
  if (guide.status === 'stale') { guideEl.append(textElement('p', '简历已修改，当前指南对应旧正文。请更新这份指南。', 'empty')); retryGuideEl.textContent = '更新这份指南'; retryGuideEl.hidden = false; return; }
  if (guide.status === 'failed') { guideEl.append(textElement('p', '这份指南没有生成成功，简历仍可正常查看。', 'empty')); retryGuideEl.hidden = false; return; }
  if (guide.status !== 'ready') { guideEl.append(textElement('p', '正在准备与这份简历逐条对应的面试内容…', 'empty')); return; }
  if(guide.source==='degraded_local')guideEl.append(textElement('p','模型调用未完成，以下为离线兜底指南，请核对后使用。','status error'));
  for (const entry of guide.entries) {
    const wrapper = textElement('article', '', 'guide-entry');
    wrapper.append(textElement('h4', entry.question));
    for (const [label, value] of [['回答思路',entry.answer_outline],['贡献边界',entry.contribution_boundary],['验证方式',entry.verification],['准备动作',entry.practice_step]]) {
      const p = document.createElement('p');
      p.append(textElement('strong', `${label}：`));
      p.append(document.createTextNode(value));
      wrapper.append(p);
    }
    guideEl.append(wrapper);
  }
}
function render(batch) {
  const changedBatch = currentBatch?.id !== batch.id;
  currentBatch = batch;
  retryBatchEl.hidden = batch.status !== 'failed';
  if (changedBatch) {
    editSnapshot=null;document.querySelector('#delete-options').hidden=true;
    jdInput.value = batch.jd_text || '';
    materialInput.value = batch.material_text || '';
    document.querySelector('#confirmed').checked = Boolean(batch.material_confirmed);
    jdInput.dispatchEvent(new Event('input'));
    materialInput.dispatchEvent(new Event('input'));
  }
  historyEl.value = batch.id;
  const [message, kind] = describeStatus(batch);
  setStatus(message, kind);
  resultsEl.hidden = !Object.keys(batch.resumes || {}).length;
  if (!resultsEl.hidden) {
    if (!editSnapshot) editor.hidden = true;
    renderResume(batch.resumes[currentKind]);
    for (const format of ['html','docx','pdf']) document.querySelector(`#download-${format}`).href = `/api/batches/${batch.id}/resumes/${currentKind}/download/${format}`;
    guideTitleEl.textContent = currentKind === 'job' ? '求职版面试指南' : '蓝图 Plus 面试指南';
    renderGuide(batch.guides[currentKind]);
    document.querySelector('#download-data').href = `/api/batches/${batch.id}/archive`;
  }
  generateButton.disabled = ['queued','running','guides_pending'].includes(batch.status);
  if (['queued','running','guides_pending'].includes(batch.status) || Object.values(batch.guides || {}).some(g => ['queued','running'].includes(g.status))) {
    clearTimeout(pollTimer); pollTimer = setTimeout(() => loadBatch(batch.id), 1600);
  } else clearTimeout(pollTimer);
}
async function loadBatch(id) {
  try {
    const response = await fetch(`/api/batches/${id}`, {cache:'no-store'});
    if(response.status===404){clearTimeout(pollTimer);sessionStorage.removeItem('coach_batch_id');currentBatch=null;editSnapshot=null;resultsEl.hidden=true;generateButton.disabled=false;retryBatchEl.hidden=true;setStatus('这份生成记录已删除，可以开始新任务。');await loadHistory();return;}
    if (!response.ok) throw Error('无法读取生成记录');
    render(await response.json());
  } catch { setStatus('暂时无法连接本机服务，正在等待连接恢复…', 'error'); clearTimeout(pollTimer); pollTimer=setTimeout(()=>loadBatch(id),4000); }
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  const jd = jdInput.value.trim(); const material = materialInput.value.trim();
  if (jd.length < 20) { setStatus('请粘贴更完整的岗位描述（至少 20 字）。', 'error'); return; }
  generateButton.disabled = true; setStatus('正在保存输入…');
  try {
    const body = JSON.stringify({jd_text:jd,material_text:material,material_confirmed:document.querySelector('#confirmed').checked});
    const fingerprint = [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body)))].map(value=>value.toString(16).padStart(2,'0')).join('');
    let pending;
    try { pending=JSON.parse(sessionStorage.getItem('coach_pending') || 'null'); } catch { pending=null; }
    if (!pending || pending.fingerprint!==fingerprint) pending={fingerprint,key:crypto.randomUUID()};
    sessionStorage.setItem('coach_pending',JSON.stringify(pending));
    const response = await fetch('/api/batches', {method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},body});
    if (!response.ok) throw Error('提交失败，请稍后重试');
    const batch = await response.json();
    sessionStorage.setItem('coach_batch_id',batch.id);
    sessionStorage.removeItem('coach_pending');
    editSnapshot=null;
    await loadHistory();
    await loadBatch(batch.id);
  } catch (error) { setStatus(error.message || '提交失败，请重试。', 'error'); generateButton.disabled = false; }
});
document.querySelector('#material-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  const note = document.querySelector('#upload-status'); note.textContent = '正在提取文字…';
  try {
    const payload = new FormData(); payload.append('file', file);
    const response = await fetch('/api/materials/extract', {method:'POST',body:payload});
    const result = await response.json();
    if (!response.ok) throw Error(result.detail || '无法读取文件');
    materialInput.value = result.text;
    materialInput.dispatchEvent(new Event('input'));
    document.querySelector('#confirmed').checked = false;
    note.textContent = '文字已提取，请核对和修改后再勾选确认。';
  } catch (error) { note.textContent = error.message || '无法读取文件，请手工粘贴。'; }
});
document.querySelector('#edit-resume').addEventListener('click', () => {
  const resume = currentBatch?.resumes[currentKind]; if (!resume) return;
  editSnapshot={batchId:currentBatch.id,kind:currentKind,resume:structuredClone(resume)};
  editor.querySelectorAll('[data-header]').forEach(field=>{field.value=resume.header?.[field.dataset.header] || ''});
  document.querySelector('#edit-title').value = resume.title;
  document.querySelector('#edit-summary').value = resume.summary;
  editItems.replaceChildren();
  resume.items.forEach(item => addEditorItem(item));
  editPlans.replaceChildren();
  (resume.plans || []).forEach(plan=>{
    const section=textElement('div','','editor-item'); section.dataset.key=plan.key;
    for (const [key,label] of [['title','项目名称'],['background','背景与限制'],['task','本人任务'],['actions','方法与步骤（每行一项）'],['outcome','成果或预期产物'],['verification','核验方法']]) {
      const fieldLabel=textElement('label',label); const area=document.createElement('textarea'); area.id=`edit-plan-${editPlans.children.length}-${key}`;fieldLabel.htmlFor=area.id;section.append(fieldLabel);area.dataset.field=key; area.value=Array.isArray(plan[key])?plan[key].join('\n'):(plan[key] || ''); area.maxLength=2000; section.append(area);
    }
    editPlans.append(section);
  });
  editor.hidden = false;
  document.querySelector('#edit-status').textContent = `正在编辑第 ${resume.revision} 版。锁定要点不能改写。`;
});
function addEditorItem(item = {id:'',text:'',locked:false}) {
  const row = textElement('div','', 'editor-item'); row.dataset.id = item.id;
  const label = textElement('label','项目要点');
  const area = document.createElement('textarea'); area.value = item.text; area.maxLength = 600; area.required = true; area.disabled = Boolean(item.locked);
  area.id=`edit-item-${crypto.randomUUID()}`;label.htmlFor=area.id;
  const lock = document.createElement('label'); lock.className = 'checkbox-row';
  const check = document.createElement('input'); check.type = 'checkbox'; check.checked = Boolean(item.locked);
  lock.append(check, document.createTextNode('锁定此要点'));
  const remove = textElement('button','删除要点','secondary'); remove.type='button'; remove.disabled = Boolean(item.locked); remove.addEventListener('click',()=>row.remove());
  const confirmation=textElement('label','','checkbox-row'); const confirmed=document.createElement('input'); confirmed.type='checkbox'; confirmed.className='item-confirm'; confirmed.checked=item.verification_status==='user_confirmed';
  confirmation.append(confirmed,document.createTextNode('我已核对这段文字'));
  const completion=textElement('label','','checkbox-row'); const achieved=document.createElement('input'); achieved.type='checkbox'; achieved.className='item-achieved'; achieved.checked=Boolean(item.achieved);
  completion.append(achieved,document.createTextNode('该要点已实际完成'));
  const updateLock=()=>{area.disabled=check.checked;remove.disabled=check.checked;confirmed.disabled=check.checked;achieved.disabled=check.checked || !confirmed.checked};
  check.addEventListener('change',updateLock); updateLock();
  confirmed.addEventListener('change',()=>{row.dataset.confirmChanged='true';updateLock();if(!confirmed.checked)achieved.checked=false});
  area.addEventListener('input',()=>{if(confirmed.checked)row.dataset.confirmChanged='true';confirmed.checked=false;achieved.checked=false;achieved.disabled=true});
  row.append(label,area,lock,confirmation,completion,remove); editItems.append(row);
}
document.querySelector('#add-item').addEventListener('click',()=>addEditorItem());
document.querySelector('#cancel-edit').addEventListener('click',()=>{editor.hidden=true;editSnapshot=null});
editor.addEventListener('submit', async event => {
  event.preventDefault(); if(!editSnapshot)return; const snapshot=editSnapshot; const resume = snapshot.resume;
  const items = [...editItems.children].map(row => ({id:row.dataset.id || undefined,text:row.querySelector('textarea').value.trim(),locked:row.querySelector('.checkbox-row input').checked,confirm:row.dataset.confirmChanged || row.querySelector('.item-confirm').checked ? row.querySelector('.item-confirm').checked : undefined,achieved:row.querySelector('.item-achieved').checked}));
  const plans=[...editPlans.children].map(section=>{const plan=structuredClone(resume.plans.find(p=>p.key===section.dataset.key));for(const area of section.querySelectorAll('textarea'))plan[area.dataset.field]=area.dataset.field==='actions'?area.value.split('\n').map(v=>v.trim()).filter(Boolean):area.value.trim();return plan});
  const header=Object.fromEntries([...editor.querySelectorAll('[data-header]')].map(field=>[field.dataset.header,field.value.trim()]));
  const body = {revision:resume.revision,title:document.querySelector('#edit-title').value,summary:document.querySelector('#edit-summary').value,items,plans,header};
  try {
    const response = await fetch(`/api/batches/${snapshot.batchId}/resumes/${snapshot.kind}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.detail || '保存失败');
    editSnapshot=null; render(result); setStatus(result.guides[snapshot.kind]?.status==='stale'?'简历新版已保存。请更新对应面试指南。':'简历新版已保存。','ready'); await loadRevisionHistory();
  } catch (error) { document.querySelector('#edit-status').textContent=error.message; }
});
document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
  currentKind = tab.dataset.kind;
  editSnapshot=null; editor.hidden=true;
  document.querySelectorAll('.tab').forEach(item => {item.classList.toggle('active',item===tab);item.setAttribute('aria-selected',item===tab?'true':'false')});
  if (currentBatch) render(currentBatch);
  loadRevisionHistory();
}));
retryGuideEl.addEventListener('click', async () => {
  if (!currentBatch) return;
  retryGuideEl.disabled = true;
  try {
    const response = await fetch(`/api/batches/${currentBatch.id}/guides/${currentKind}/retry`,{method:'POST'});
    if (!response.ok) throw Error();
    await loadBatch(currentBatch.id);
  } catch {setStatus('指南重试未提交成功，请稍后再试。','error')} finally {retryGuideEl.disabled=false}
});
for (const [input,count] of [[jdInput,document.querySelector('#jd-count')],[materialInput,document.querySelector('#material-count')]]) input.addEventListener('input',()=>count.textContent=`${input.value.length} / ${input.maxLength}`);
async function loadHistory() {
  try {
    const response = await fetch('/api/batches'); if (!response.ok) return;
    const rows = await response.json();
    historyEl.replaceChildren(new Option('选择记录',''));
    rows.forEach(row => historyEl.add(new Option(`${new Date(row.created_at).toLocaleString('zh-CN')} · ${row.jd_excerpt}`,row.id)));
    if (currentBatch) historyEl.value = currentBatch.id;
  } catch { /* 主流程不依赖历史列表 */ }
}
historyEl.addEventListener('change',()=>{if (historyEl.value) {editSnapshot=null;sessionStorage.setItem('coach_batch_id',historyEl.value); loadBatch(historyEl.value).then(loadRevisionHistory)}});
retryBatchEl.addEventListener('click',async()=>{
  if(!currentBatch)return;retryBatchEl.disabled=true;
  try { const response=await fetch(`/api/batches/${currentBatch.id}/retry`,{method:'POST'});if(!response.ok)throw Error();await loadBatch(currentBatch.id); }
  catch {setStatus('重试未提交成功，请稍后再试。','error');}finally{retryBatchEl.disabled=false;}
});
async function loadRevisionHistory() {
  const container=document.querySelector('#revision-history');container.replaceChildren();if(!currentBatch)return;
  const id=currentBatch.id,kind=currentKind;
  try {
    const responses=await Promise.all([fetch(`/api/batches/${id}/resumes/${kind}/revisions`),fetch(`/api/batches/${id}/guides/${kind}/revisions`)]);
    if(responses.some(response=>!response.ok))return;
    const [resumes,guides]=await Promise.all(responses.map(response=>response.json()));
    if(currentBatch.id!==id || currentKind!==kind)return;
    for(const row of resumes){const line=textElement('p',`简历第 ${row.revision} 版 · `);for(const format of ['html','docx','pdf']){const link=textElement('a',format.toUpperCase());link.href=`/api/batches/${id}/resumes/${kind}/download/${format}?revision=${row.revision}`;line.append(link,document.createTextNode(' '));}container.append(line);}
    for(const row of guides){const line=textElement('p',`指南第 ${row.revision} 版 · 对应简历第 ${row.resume_revision} 版 · `);if(row.status==='ready'){const link=textElement('a','下载历史指南');link.href=`/api/batches/${id}/guides/${kind}/revisions/${row.revision}/download`;line.append(link);}else line.append(document.createTextNode({failed:'生成失败',queued:'排队中',running:'生成中',superseded:'生成时内容已变化'}[row.status] || row.status));container.append(line);}
  }catch{container.append(textElement('p','暂时无法读取历史，请稍后刷新。'));}
}
document.querySelector('#revision-history').parentElement.addEventListener('toggle',event=>{if(event.target.open)loadRevisionHistory()});
document.querySelector('#delete-batch').addEventListener('click',()=>document.querySelector('#delete-options').hidden=false);
document.querySelector('#cancel-delete').addEventListener('click',()=>document.querySelector('#delete-options').hidden=true);
document.querySelector('#confirm-delete').addEventListener('click',async()=>{
  if(!currentBatch)return;
  try{const response=await fetch(`/api/batches/${currentBatch.id}`,{method:'DELETE'});if(!response.ok)throw Error();clearTimeout(pollTimer);sessionStorage.removeItem('coach_batch_id');currentBatch=null;editSnapshot=null;resultsEl.hidden=true;document.querySelector('#delete-options').hidden=true;retryBatchEl.hidden=true;generateButton.disabled=false;await loadHistory();setStatus('本次资料已删除。恢复旧备份也会保留这次删除。','ready');}
  catch{setStatus('删除未完成，请检查服务后重试。','error');}
});
(async () => {
  try {const config=await fetch('/api/config');const data=await config.json();document.querySelector('#provider-note').textContent=data.provider==='local'?'离线演示 · 未调用真实模型':'已配置真实模型';}catch{document.querySelector('#provider-note').textContent='暂时无法检查模型配置';}
  await loadHistory();
  const known = sessionStorage.getItem('coach_batch_id');
  if (known) { await loadBatch(known);await loadRevisionHistory();return; }
  try {const response = await fetch('/api/batches/latest');if (response.ok) {render(await response.json());await loadRevisionHistory();}}catch{setStatus('无法连接本机服务，请确认Docker已启动。','error');}
})();
