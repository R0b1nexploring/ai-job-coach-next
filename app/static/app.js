'use strict';
const $ = selector => document.querySelector(selector);
const form = $('#generator-form');
const jdInput = $('#jd');
const materialInput = $('#material');
const statusEl = $('#status');
const resultsEl = $('#results');
const resumeEl = $('#resume');
const guideEl = $('#guide');
const guideTitleEl = $('#guide-title');
const guideStatusEl = $('#guide-status');
const guideDownloadEl = $('#download-guide');
const retryGuideEl = $('#retry-guide');
const generateButton = $('#generate');
const historyEl = $('#batch-history');
const editor = $('#editor');
const editItems = $('#edit-items');
const editPlans = $('#edit-plans');
const retryBatchEl = $('#retry-batch');
const HEADER_FIELDS = ['name','phone','email','location','education'];
const SOURCE_BADGES = {user_confirmed:['已确认素材','ok'],user_unconfirmed:['未核对素材','warn'],none:['AI 建议','warn']};
const EMPTY_EDUCATION = '学校：________________    专业：____________\n学历：________    就读时间：________________';
const PLAN_FIELDS = [['title','项目名称'],['background','背景与限制'],['task','本人任务'],['actions','方法与步骤'],['outcome','成果或预期产物'],['verification','核验方法']];
const FACETS = {question:'面试问题',answer_outline:'回答思路',contribution_boundary:'贡献边界',verification:'验证方式',practice_step:'准备动作'};
const STEPS = [
  ['先放入你想申请的岗位','粘贴完整岗位描述，保留职责与任职要求。'],
  ['带上你已有的经历','上传或粘贴个人素材；没有素材也可以继续。'],
  ['填好简历上的个人信息','这一页可以留空，也可以在生成后分别修改两版。'],
  ['确认输入，开始生成','核对三项输入，再生成双版本简历与各自的指南。'],
  ['逐项核对你的简历','每次只看一个章节；修改与来源状态都在当前页。'],
  ['围绕每条要点准备面试','选一个要点，再查看问题、回答与准备动作。'],
  ['准备好后，导出使用','选择所需格式，带走当前版本与对应指南。']
];
let currentBatch = null;
let currentKind = 'job';
let currentStep = 0;
let pollTimer = null;
let editSnapshot = null;
let requestEpoch = 0;
let submitting = false;
let uploading = false;
let historyRows = {id:null,kind:null,resumes:[],guides:[]};
let historyEpoch = 0;
let historyKind = 'job';
const dialogFocus = new WeakMap();
const pageReaders = new Set();

function textElement(tag, value, className) {
  const element = document.createElement(tag); element.textContent = value;
  if (className) element.className = className;
  return element;
}
function setStatus(message, kind = '') {
  statusEl.textContent = message; statusEl.className = `status ${kind}`; statusEl.hidden = !message;
  refreshReaders();
}
function describeStatus(batch) {
  if (batch.status === 'queued') return ['任务已排队。离开页面后回来也能继续查看。', ''];
  if (batch.status === 'running') return ['正在生成双版本简历并进行内容评审…', ''];
  if (batch.status === 'guides_pending') return ['双版本已生成，面试指南正在准备。可以先核对简历。', ''];
  if (batch.status === 'guides_partial') return ['简历已生成；部分指南未完成，可在面试准备步骤重试。', 'error'];
  if (batch.status === 'failed' && batch.error_code === 'CONTENT_REVIEW_FAILED') return ['内容评审发现的问题尚未解决，本次未交付简历。请在右侧查看“内容评审”，再重试这次任务；输入仍然保留。', 'error'];
  if (batch.status === 'failed') return ['生成没有完成，请重试这次任务。输入仍然保留。', 'error'];
  if (batch.source === 'degraded_local') return ['本次是离线兜底草稿，请逐条核对后使用。', 'error'];
  if (batch.unverified_targets?.length) return [`已生成；${batch.unverified_targets.length} 项岗位目标为 AI 推断，请对照 JD 核对。`, 'error'];
  return ['双版本与两份指南已生成，请核对内容。', 'ready'];
}
function contentReviewView(review, resume = null) {
  if (!review || typeof review !== 'object' || !review.status) return null;
  const model = review.source === 'compatible';
  const offline = ['local','degraded_local'].includes(review.source);
  const knownStatus = ['passed','repaired','suggestions','unavailable','stale','blocked'].includes(review.status);
  let status = knownStatus ? review.status : 'unavailable';
  // A local substitute must never acquire a model-review success badge.
  if (!model && ['passed','repaired','suggestions'].includes(status)) status = 'unavailable';
  const labels = {passed:'已审稿',repaired:'已定向修复',suggestions:'有表达建议',unavailable:'尚未完成审稿',stale:'报告已过期',blocked:'未通过审稿'};
  const notes = {
    passed:'模型未发现需要定向修复的实质内容问题。请继续核对事实、来源与实际完成情况。',
    repaired:'已按问题位置修改，并完成再次审稿。表达建议保留供你决定是否采用。',
    suggestions:'未发现需要自动修复的实质内容问题。以下表达建议未自动改写正文，可按需要采用。',
    unavailable:offline?'本次使用离线草稿，没有完成模型内容审稿。请逐项核对后使用。':'本次没有取得有效的内容评审结论。已保留草稿，请逐项核对后使用。',
    stale:'当前版的正文或语义状态已改变，生成时的评审不再适用。下面保留历史诊断，不能视为当前版已通过审稿。',
    blocked:'定向修复后仍有实质内容问题，或修复未能可靠完成。本次不交付简历；请查看问题后重试。'
  };
  const reasons = {
    local_provider:'使用本地演示生成，未调用模型审稿。',unsupported_provider:'当前生成服务不支持内容审稿。',
    model_unavailable:'审稿服务暂时不可用。',invalid_review:'未取得可用的审稿结果。',
    repair_failed:'定向修改没有完成。',repair_rejected:'修改超出允许范围或未满足内容约束，已拒绝采用。',
    no_progress:'定向修改没有解决已指出的问题。',recheck_unavailable:'定向修改后的再次审稿未能完成。',
    unresolved_issues:'再次审稿仍发现未解决的实质问题。'
  };
  const source = offline ? '离线未审' : model ? (status === 'unavailable' ? '模型审稿未完成' : '模型审稿') : '未取得有效审稿';
  const repairs = Number.isInteger(review.repair_count) && review.repair_count > 0 ? review.repair_count : 0;
  const issues = Array.isArray(review.issues) ? review.issues.filter(issue=>issue && typeof issue === 'object') : [];
  const suggestions = issues.filter(issue=>issue.severity === 'suggestion').length;
  const heading = status === 'stale' ? '生成时的记录' : '本次记录';
  let text = `${notes[status]}\n\n来源：${source}\n${heading}：${repairs} 处定向修复；${suggestions} 条表达建议。`;
  if (reasons[review.reason]) text += `\n说明：${reasons[review.reason]}`;
  const fields = {text:'要点正文',title:'标题',summary:'简介',background:'背景与限制',task:'本人任务',actions:'方法与步骤',outcome:'成果或预期产物',verification:'核验方法'};
  issues.forEach((issue,index)=>{
    text += `\n\n${index+1}. ${issue.severity === 'error' ? '实质问题' : '表达建议'}`;
    if (issue.diagnosis) text += `\n${issue.diagnosis}`;
    for (const target of Array.isArray(issue.targets) ? issue.targets : []) {
      if (!target || typeof target !== 'object') continue;
      const isPlan = target.scope === 'plan';
      const rows = isPlan ? resume?.plans : resume?.items;
      const position = Array.isArray(rows) ? rows.findIndex(row=>(isPlan ? row.key : row.id) === target.target_id) : -1;
      const version = target.scope === 'job' ? '求职版' : target.scope === 'blueprint' ? '蓝图 Plus' : '项目方案';
      const location = position >= 0 ? `${isPlan?'方案':'要点'} ${position+1} · ` : '';
      text += `\n位置：${version} · ${location}${fields[target.field] || '相关内容'}`;
      if (target.quote) text += `\n原文：${target.quote}`;
    }
    if (issue.instruction) text += `\n${issue.severity === 'error' ? '修改方向' : '可选建议'}：${issue.instruction}`;
    if (issue.jd_quote) text += `\n岗位依据：${issue.jd_quote}`;
  });
  const tone = ['passed','repaired'].includes(status) ? 'ok' : status === 'blocked' ? 'danger' : 'warn';
  return {status,label:labels[status],text,badges:[[labels[status],tone],[source,model&&status!=='unavailable'?'':'warn']]};
}
function currentReviewNotice(resume) {
  const review = contentReviewView(resume?.content_review,resume);
  if (!review) return '';
  if (review.status === 'unavailable') return '内容审稿尚未完成，请在右侧“内容评审”查看说明，并逐项核对草稿。';
  if (review.status === 'stale') return '内容评审报告已过期，不能作为当前版的审稿结论。';
  if (review.status === 'suggestions') return '本版有表达建议，可在右侧“内容评审”查看。';
  return '';
}
function hasResumes() { return Boolean(currentBatch?.resumes?.[currentKind]); }
function inputsMatchBatch() {
  if(!currentBatch)return false;
  return jdInput.value.trim()===(currentBatch.jd_text||'').trim()
    && materialInput.value.trim()===(currentBatch.material_text||'').trim()
    && $('#confirmed').checked===Boolean(currentBatch.material_confirmed)
    && HEADER_FIELDS.every(field=>$(`#pf-${field}`).value.trim()===String(currentBatch.profile?.[field]||'').trim());
}
function readyForInputs(){return hasResumes()&&inputsMatchBatch()&&!submitting;}
function isGenerating() { return submitting || (inputsMatchBatch()&&['queued','running','guides_pending'].includes(currentBatch?.status)); }
function collectProfile(prefix) {
  return Object.fromEntries(HEADER_FIELDS.map(field=>[field,$(`#${prefix}-${field}`).value.trim()]).filter(([,value])=>value));
}
function saveDraft() {
  try { sessionStorage.setItem('coach_draft',JSON.stringify({batchId:currentBatch?.id,jd:jdInput.value,material:materialInput.value,confirmed:$('#confirmed').checked,profile:collectProfile('pf'),step:Math.min(currentStep,3)})); } catch { /* Storage availability does not block editing. */ }
}
function persistView() {
  try { sessionStorage.setItem('coach_view',JSON.stringify({batchId:currentBatch?.id,step:currentStep,kind:currentKind,section:$('#resume-section').value,entry:$('#guide-entry').value,facet:currentFacet()})); } catch { /* View state is optional. */ }
}
function showStep(step, focus = true) {
  if (step > 3 && !hasResumes()) step = 3;
  currentStep = Math.max(0,Math.min(6,step));
  document.querySelectorAll('[data-screen]').forEach(screen=>screen.hidden=Number(screen.dataset.screen)!==currentStep);
  form.hidden = currentStep > 3;
  resultsEl.hidden = currentStep < 4 || !hasResumes();
  $('#version-tabs').hidden = currentStep < 4;
  $('#step-label').textContent = `第 ${currentStep + 1} 步 / 共 7 步`;
  $('#screen-title').textContent = STEPS[currentStep][0];
  $('#screen-description').textContent = STEPS[currentStep][1];
  document.querySelectorAll('[data-step]').forEach(button=>{
    const n=Number(button.dataset.step); button.disabled=n>3&&!hasResumes();
    if(n===currentStep)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
    button.classList.toggle('complete',n<currentStep);
  });
  $('#step-prev').disabled=currentStep===0;
  const next=$('#step-next');
  next.disabled = uploading || (currentStep===3 && !readyForInputs());
  next.replaceChildren(document.createTextNode(currentStep===6?'新建下一份':'下一步'),textElement('span','→'));
  const hints=['填写这一页后，点击下一步。','素材可以留空。','信息可以留空。','生成完成后，点击下一步。','逐项核对后，进入面试准备。','准备好后，进入导出。','本次资料已保存，可随时返回。'];
  $('#footer-hint').textContent=hints[currentStep];
  statusEl.hidden = currentStep < 3 || !statusEl.textContent;
  if (currentStep===3) renderInputReview();
  if (currentStep===4) renderResume(currentBatch.resumes[currentKind]);
  if (currentStep===5) renderGuide(currentBatch.guides[currentKind]);
  if (currentStep===6) renderExports();
  renderColumns();
  persistView();
  if (!currentBatch || !inputsMatchBatch()) saveDraft();
  refreshReaders();updateFooterHint();
  if(focus)$('#screen-title').focus({preventScroll:true});
}
function validateJD() {
  if(jdInput.value.trim().length<20){showStep(0,false);setStatus('请粘贴更完整的岗位描述（至少 20 字）。','error');statusEl.hidden=false;jdInput.setAttribute('aria-invalid','true');jdInput.focus();return false;}
  jdInput.removeAttribute('aria-invalid');return true;
}
function advanceReading(direction) {
  const reader=(currentStep===4?resumeEl:guideEl)._reader;
  if(!reader)return false;
  if(direction>0&&reader.index<reader.pages.length-1){reader.index++;reader.paint();updateFooterHint();return true;}
  if(direction<0&&reader.index>0){reader.index--;reader.paint();updateFooterHint();return true;}
  if(currentStep===4){
    const select=$('#resume-section'),index=select.selectedIndex+direction;
    if(index<0||index>=select.options.length)return false;
    select.selectedIndex=index;renderResume(currentBatch.resumes[currentKind]);
    if(direction<0)requestAnimationFrame(()=>{const previous=resumeEl._reader;previous.index=previous.pages.length-1;previous.paint();updateFooterHint();});
    renderColumns();persistView();updateFooterHint();return true;
  }
  const guide=currentBatch?.guides[currentKind];if(guide?.status!=='ready')return false;
  const facets=Object.keys(FACETS),facet=facets.indexOf(currentFacet());let nextFacet=facet+direction,entry=Number($('#guide-entry').value);
  if(nextFacet<0){entry--;nextFacet=facets.length-1;}else if(nextFacet>=facets.length){entry++;nextFacet=0;}
  if(entry<0||entry>=guide.entries.length)return false;
  $('#guide-entry').value=String(entry);$('#guide-facets').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.facet===facets[nextFacet])));renderGuide(guide);
  if(direction<0)requestAnimationFrame(()=>{const previous=guideEl._reader;previous.index=previous.pages.length-1;previous.paint();updateFooterHint();});
  renderColumns();persistView();updateFooterHint();return true;
}
function updateFooterHint(){
  if(currentStep===4){const reader=resumeEl._reader;$('#footer-hint').textContent=reader&&reader.index<reader.pages.length-1?'下一步：继续当前章节。':$('#resume-section').selectedIndex<$('#resume-section').options.length-1?'下一步：核对下一项。':'下一步：进入面试准备。';}
  if(currentStep===5){const reader=guideEl._reader,guide=currentBatch?.guides[currentKind];const finalFacet=currentFacet()==='practice_step'&&Number($('#guide-entry').value)===(guide?.entries?.length||0)-1;$('#footer-hint').textContent=reader&&reader.index<reader.pages.length-1?'下一步：继续当前内容。':guide?.status!=='ready'||finalFacet?'下一步：进入导出。':'下一步：查看下一项准备内容。';}
}
$('#step-prev').addEventListener('click',()=>{if([4,5].includes(currentStep)&&advanceReading(-1))return;showStep(currentStep-1);});
$('#step-next').addEventListener('click',()=>{
  if(currentStep===6){newTask();return;}
  if(currentStep===0&&!validateJD())return;
  if(currentStep===3&&!readyForInputs())return;
  if([4,5].includes(currentStep)&&advanceReading(1))return;
  showStep(currentStep+1);
});
document.querySelectorAll('[data-step]').forEach(button=>button.addEventListener('click',()=>{
  const target=Number(button.dataset.step);
  if(target>0&&target<4&&!validateJD())return;
  showStep(target);
}));
function renderInputReview() {
  const container=$('#input-review');container.replaceChildren();
  const profile=collectProfile('pf');
  for(const [title,value,step] of [
    ['岗位描述',`${jdInput.value.trim().length} 字 · ${jdInput.value.trim().length>=20?'已填写':'还需补充'}`,0],
    ['个人素材',materialInput.value.trim()?`${materialInput.value.trim().length} 字 · ${$('#confirmed').checked?'已确认真实经历':'尚未核对'}`:'未填写 · 将生成待核验的规划草稿',1],
    ['个人信息',Object.keys(profile).length?`已填写 ${Object.keys(profile).length} 项 · 仅作为两版初始信息`:'暂留空白填写区',2]
  ]){
    const card=textElement('div','','review-card'),copy=textElement('div','');copy.append(textElement('h3',title),textElement('p',value));
    const button=textElement('button','修改','quiet');button.type='button';button.addEventListener('click',()=>showStep(step));card.append(copy,button);container.append(card);
  }
  const ready=readyForInputs();
  $('#generation-title').textContent=ready?'双版本已准备好':isGenerating()?'正在准备你的双版本':'生成两份可核对的草稿';
  $('#generation-note').textContent=ready?'点击下方“下一步”，逐项核对求职版与蓝图 Plus；随后准备面试并导出。':'先生成双版草稿，再独立审稿；实质问题最多定向修复一次。每版指南按最终正文准备。';
  generateButton.hidden=ready;
  generateButton.disabled=isGenerating()||uploading;
  generateButton.textContent=isGenerating()?'正在生成…':hasResumes()?'按新输入生成双版本':'开始生成双版本';
  retryBatchEl.hidden=currentBatch?.status!=='failed'||!inputsMatchBatch();
  if(currentBatch&&!inputsMatchBatch()&&!submitting)setStatus('输入已修改，请按新输入生成。上次成果仍保存在历史记录中。','');
}

// Each reader divides the complete source by its measured available height.
// The source remains untouched; no output is shortened for display or saving.
function refreshReaders() { requestAnimationFrame(()=>pageReaders.forEach(reader=>reader.layout())); }
function makeReader(container, title, text, badges = []) {
  let reader=container._reader;
  if(!reader){
    container.replaceChildren();
    const heading=textElement('h3','','reader-title'),badgeRow=textElement('div','','badges'),body=textElement('div','','reading-text');
    const pager=textElement('div','','reader-pager'),prev=textElement('button','上一段'),count=textElement('span',''),next=textElement('button','下一段');
    count.setAttribute('aria-live','polite');count.setAttribute('aria-atomic','true');prev.type=next.type='button';prev.setAttribute('aria-label','查看上一段内容');next.setAttribute('aria-label','查看下一段内容');
    pager.append(prev,count,next);container.append(heading,badgeRow,body,pager);
    reader={container,heading,badgeRow,body,prev,next,count,source:'',pages:[],index:0,width:0,height:0,
      layout(){
        if(!body.clientHeight||!body.clientWidth)return;
        if(this.width===body.clientWidth&&this.height===body.clientHeight&&this.pages.length){this.paint();return;}
        const oldOffset=this.pages.slice(0,this.index).reduce((sum,page)=>sum+page.length,0);
        this.width=body.clientWidth;this.height=body.clientHeight;this.pages=[];
        let offset=0;
        while(offset<this.source.length){
          let low=1,high=Math.min(this.source.length-offset,10000),fit=1;
          while(low<=high){const middle=Math.floor((low+high)/2);body.textContent=this.source.slice(offset,offset+middle);if(body.scrollHeight<=body.clientHeight+1){fit=middle;low=middle+1;}else high=middle-1;}
          // Prefer complete sentences, but do not waste most of a page.
          let chunk=this.source.slice(offset,offset+fit);
          if(offset+fit<this.source.length){
            const matches=[...chunk.matchAll(/[\n。！？；]/g)];const boundary=matches.at(-1)?.index;
            if(boundary!==undefined&&boundary>fit*.62)fit=boundary+1;
            const last=this.source.charCodeAt(offset+fit-1);if(last>=0xD800&&last<=0xDBFF&&fit>1)fit--;
          }
          this.pages.push(this.source.slice(offset,offset+fit));offset+=fit;
        }
        if(!this.pages.length)this.pages=['暂无内容。'];
        let accumulated=0;this.index=0;
        for(let i=0;i<this.pages.length;i++){if(accumulated+this.pages[i].length>oldOffset){this.index=i;break;}accumulated+=this.pages[i].length;}
        this.paint();
      },
      paint(){this.index=Math.max(0,Math.min(this.index,this.pages.length-1));body.textContent=this.pages[this.index]||'';prev.disabled=this.index===0;next.disabled=this.index===this.pages.length-1;count.textContent=this.pages.length>1?(container.classList.contains('sidebar-reader')?`${this.index+1} / ${this.pages.length} 段`:`第 ${this.index+1} / ${this.pages.length} 段 · 全文可查看`):'完整内容 · 1 / 1';updateFooterHint();}
    };
    prev.addEventListener('click',()=>{reader.index--;reader.paint();});next.addEventListener('click',()=>{reader.index++;reader.paint();});
    new ResizeObserver(()=>reader.layout()).observe(body);pageReaders.add(reader);container._reader=reader;
  }
  reader.heading.textContent=title;reader.badgeRow.replaceChildren();reader.badgeRow.hidden=!badges.length;
  badges.forEach(([label,tone])=>reader.badgeRow.append(textElement('span',label,`badge ${tone||''}`)));
  if(reader.source!==text){reader.source=String(text||'暂无内容。');reader.pages=[];reader.index=0;reader.width=reader.height=0;}
  reader.layout();refreshReaders();return reader;
}
function headerText(header = {}) {
  return `姓名：${header.name||'________________'}\n电话：${header.phone||'____________'}\n邮箱：${header.email||'________________'}\n所在城市：${header.location||'________'}`;
}
function resumeSections(resume) {
  if(!resume)return [];
  return [{value:'header',label:'个人信息',text:headerText(resume.header)}, {value:'summary',label:'标题与简介',text:`${resume.title}\n\n${resume.summary}`}, {value:'education',label:'教育背景',text:resume.header?.education||EMPTY_EDUCATION},
    ...resume.items.map((item,index)=>({value:`item:${index}`,label:`项目要点 ${index+1}`,text:item.text,item,signals:resume.signals?.[item.id]})),
    ...(resume.plans||[]).map((plan,index)=>({value:`plan:${index}`,label:`项目方案 ${index+1}`,text:PLAN_FIELDS.map(([key,label])=>`${label}\n${Array.isArray(plan[key])?plan[key].join('\n'):plan[key]||'暂无内容'}`).join('\n\n')}))];
}
function setOptions(select, rows, preferred) {
  const old=preferred??select.value;
  const signature=JSON.stringify(rows.map(row=>[row.value,row.label]));
  if(select.dataset.signature!==signature){select.replaceChildren(...rows.map(row=>new Option(row.label,row.value)));select.dataset.signature=signature;}
  if(rows.some(row=>String(row.value)===String(old)))select.value=old;
}
function itemDetails(section) {
  const item=section.item,badges=[];let text=section.text;
  if(!item)return {text,badges};
  badges.push(SOURCE_BADGES[item.source_status]||SOURCE_BADGES.none,[item.achieved?'已实际完成':'未确认完成',item.achieved?'ok':'warn']);
  if(item.locked)badges.push(['已锁定','ok']);
  const notes=[];
  if((item.flags||[]).includes('dropped_source_quote'))notes.push('AI 原引用未通过核对，已移除不可靠的来源标注。');
  if((item.flags||[]).includes('achievement_unverified'))notes.push('整条完成情况仍需核对，不能把部分真实经历视为整条已完成。');
  if(section.signals?.unverified_numbers?.length)notes.push(`数字未见于素材：${section.signals.unverified_numbers.join('、')}`);
  if(section.signals?.gap_phrases?.length)notes.push(`含差距评语：${section.signals.gap_phrases.join('、')}`);
  if(notes.length){badges.push(['有内容待核对','danger']);text+=`\n\n核对提示\n${notes.join('\n')}`;}
  if(item.source_quote)text+=`\n\n素材原文\n${item.source_quote}`;
  return {text,badges};
}
function renderResume(resume) {
  if(!resume){makeReader(resumeEl,'简历正在生成','生成完成后即可逐项核对。');return;}
  const sections=resumeSections(resume),select=$('#resume-section');setOptions(select,sections);
  const section=sections.find(row=>row.value===select.value)||sections[0];const details=itemDetails(section);
  makeReader(resumeEl,`${section.label} · 第 ${resume.revision} 次修订`,details.text,details.badges);
}
$('#resume-section').addEventListener('change',()=>{renderResume(currentBatch?.resumes[currentKind]);renderColumns();persistView();});
function currentFacet() { return $('#guide-facets button[aria-pressed=true]')?.dataset.facet||'question'; }
function renderGuide(guide) {
  retryGuideEl.hidden=true;$('#guide-facets').hidden=!guide||guide.status!=='ready';$('#guide-entry').disabled=!guide||guide.status!=='ready';
  guideStatusEl.textContent={queued:'排队中',running:'生成中',ready:'已完成',failed:'生成失败',stale:'需更新'}[guide?.status]||'等待简历';
  if(!guide){makeReader(guideEl,'面试指南','简历生成后会自动准备对应指南。');return;}
  if(guide.status==='stale'){makeReader(guideEl,'指南需要更新','简历正文已修改，当前指南对应旧内容。点击“更新这份指南”，会保留历史指南并准备新的对应内容。');retryGuideEl.textContent='更新这份指南';retryGuideEl.hidden=false;return;}
  if(guide.status==='failed'){makeReader(guideEl,'指南生成未完成','简历仍可正常查看与导出。可以重试这份指南，当前内容与修订会保留。');retryGuideEl.textContent='重试这份指南';retryGuideEl.hidden=false;return;}
  if(guide.status!=='ready'){makeReader(guideEl,'正在准备面试内容','正在为这份简历逐条准备问题、回答思路与核验动作。可以返回核对简历，稍后再查看。');return;}
  const entries=guide.entries||[];setOptions($('#guide-entry'),entries.map((entry,index)=>({value:String(index),label:`项目要点 ${index+1}`})));
  const index=Number($('#guide-entry').value)||0,entry=entries[index];
  if(!entry){makeReader(guideEl,'暂无指南内容','请重试指南或稍后查看。');return;}
  const facet=currentFacet();const badges=guide.source==='degraded_local'?[['离线兜底指南 · 请核对','warn']]:[];
  makeReader(guideEl,`${FACETS[facet]} · 要点 ${index+1}`,entry[facet]||'暂无内容。',badges);
}
$('#guide-entry').addEventListener('change',()=>{renderGuide(currentBatch?.guides[currentKind]);renderColumns();persistView();});
$('#guide-facets').addEventListener('click',event=>{const button=event.target.closest('[data-facet]');if(!button)return;$('#guide-facets').querySelectorAll('button').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));renderGuide(currentBatch?.guides[currentKind]);renderColumns();persistView();});
function renderExports() {
  if(!currentBatch)return;
  const guide=currentBatch.guides[currentKind];
  $('#export-kind').textContent=currentKind==='job'?'求职版':'蓝图 Plus · 项目规划';
  $('#export-description').textContent=currentKind==='job'?'导出当前版本的简历，保留个人信息、要点与当前修订。':'包含尚未完成的项目规划。仅供学习与准备参考，不能作为已核实简历投递。';
  for(const format of ['html','docx','pdf'])$(`#download-${format}`).href=`/api/batches/${currentBatch.id}/resumes/${currentKind}/download/${format}`;
  guideTitleEl.textContent=currentKind==='job'?'求职版面试指南':'蓝图 Plus 面试指南';
  guideDownloadEl.hidden=guide?.status!=='ready';guideDownloadEl.href=`/api/batches/${currentBatch.id}/guides/${currentKind}/download`;
  $('#export-guide-note').textContent=guide?.status==='ready'?'与当前简历正文对应。':'指南尚未就绪，请返回面试准备步骤查看或重试。';
  $('#download-data').href=`/api/batches/${currentBatch.id}/archive`;
}
function linkedItem(resume) {
  if(!resume)return null;
  if(currentStep===5){const entry=currentBatch?.guides[currentKind]?.entries?.[Number($('#guide-entry').value)];return resume.items.find(item=>item.id===entry?.item_id)||null;}
  const [type,value]=$('#resume-section').value.split(':');
  if(type==='item')return resume.items[Number(value)]||null;
  if(type==='plan'){const plan=resume.plans?.[Number(value)];return resume.items.find(item=>item.plan_key===plan?.key)||null;}
  return null;
}
let supportContext='';
function renderColumns() {
  if(!$('#context-reader'))return;
  const viewingResult=currentStep>=4&&hasResumes(),resume=viewingResult?currentBatch.resumes[currentKind]:null;
  const jd=viewingResult?currentBatch.jd_text||'':jdInput.value;
  const material=viewingResult?currentBatch.material_text||'':materialInput.value;
  const confirmed=viewingResult?Boolean(currentBatch.material_confirmed):$('#confirmed').checked;
  const profile=viewingResult?resume.header||{}:collectProfile('pf');
  const facts=textElement('dl','','summary-facts');
  for(const [label,value] of [['岗位描述',`${jd.length} 字`],['个人素材',material?`${material.length} 字 · ${confirmed?'已核对':'未核对'}`:'未填写'],[viewingResult?'本版个人信息':'初始个人信息',Object.values(profile).filter(value=>String(value).trim()).length+' 项']])facts.append(textElement('dt',label),textElement('dd',value));
  $('#context-summary').replaceChildren(textElement('p',viewingResult?'本次生成时的资料':'正在填写的资料','field-note'),facts);
  const source=$('#context-source-select').value;
  makeReader($('#context-reader'),source==='material'?'个人素材原文':'目标岗位原文',source==='material'?(material||'尚未提供个人素材。可在素材步骤上传或填写，也可以无素材起步。'):(jd||'尚未填写岗位描述。请在中间填写职责与任职要求。'),source==='material'&&material?[[confirmed?'本人已核对素材':'素材尚未核对',confirmed?'ok':'warn']]:[]);
  const topics=[];let action=null,actionLabel='';
  const add=(value,label,text,badges=[])=>topics.push({value,label,text,badges});
  const version=currentKind==='job'?'求职版':'蓝图 Plus';
  const guide=viewingResult?currentBatch.guides[currentKind]:null;
  const item=linkedItem(resume);
  if(currentStep<4){
    $('#support-heading').textContent='准备与说明';
    if(currentStep===3&&inputsMatchBatch()){
      const review=contentReviewView(currentBatch?.quality?.content_review);
      if(review)add('content-review',`内容评审 · ${review.label}`,review.text,review.badges);
    }
    add('overview','当前准备情况',`岗位描述：${jd.length>=20?'已填写，可继续':'至少 20 字，保留完整岗位要求'}\n个人素材：${material?(confirmed?'本人已核对':'尚未核对，请检查真实性'):'暂未提供，可生成待核验的规划草稿'}\n个人信息：${Object.values(profile).filter(value=>String(value).trim()).length} 项，可留空。\n\n每一步的输入会保留；完成当前操作后点击“下一步”。`);
    if(currentStep===0)add('requirements','岗位描述需要什么','保留岗位职责、任职要求、工具与项目方向的原文。\n\n生成内容会围绕这份岗位描述整理；不要只写岗位名称。');
    if(currentStep===1)add('material','素材与事实边界','可填写课程项目、实习、科研、竞赛或技能。上传后的文字需要你核对。\n\n来源已经核对，不代表 AI 扩写后的整条要点已经实际完成。生成后仍需逐项检查。');
    if(currentStep===2)add('personal','个人信息如何保存','这些信息仅作为两版简历的初始填写内容，不会发送给模型。\n\n生成后可分别修改求职版和蓝图 Plus；历史修订保留各自信息。未填姓名、联系方式和教育信息会保留空白填写区。');
    add('versions','两版简历与指南','求职版：围绕已提供素材整理简历表达；没有素材时保留待核验的规划草稿。\n\n蓝图 Plus：提供尚未完成的项目规划，仅供准备参考。\n\n每版各有一份对应面试指南。AI 补充需要本人核对。');
  }else if(currentStep===4){
    $('#support-heading').textContent=item?'对应面试准备':'本版信息';
    if(item&&guide?.status==='ready'){
      const entry=guide.entries.find(entry=>entry.item_id===item.id);
      if(entry)for(const [key,label] of Object.entries(FACETS))add(key,label,entry[key]||'暂无内容。',guide.source==='degraded_local'?[['离线兜底指南 · 请核对','warn']]:[]);
      if(entry){actionLabel='展开面试准备';action=()=>{const id=item.id,facet=$('#support-topic').value;showStep(5);const index=currentBatch.guides[currentKind].entries.findIndex(entry=>entry.item_id===id);$('#guide-entry').value=String(index);if(FACETS[facet])$('#guide-facets').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.facet===facet)));renderGuide(currentBatch.guides[currentKind]);renderColumns();persistView();};}
    }
    if(item&&!topics.length){
      add('guide-state','指南状态',guide?.status==='stale'?'正文已修改，原指南需要更新。请进入面试准备步骤查看或更新。':guide?.status==='failed'?'指南生成没有完成，可以单独重试。简历仍可查看与导出。':'对应指南尚未就绪；可以先核对简历，稍后再查看。');
      actionLabel='查看指南状态';action=()=>showStep(5);
    }
    const review=contentReviewView(resume.content_review,resume);
    if(review)add('content-review',`内容评审 · ${review.label}`,review.text,review.badges);
    add('version','当前版本',`${version} · 简历第 ${resume.revision} 次修订\n共 ${resume.items.length} 条要点、${resume.plans?.length||0} 个项目方案。\n\n${currentKind==='blueprint'?'蓝图包含未完成的项目规划，仅供准备参考。':'逐条核对来源、表述与实际完成情况。'}\n\n只改本版个人信息不会让对应指南过期；正文或完成状态变化需要更新指南。`);
  }else if(currentStep===5){
    $('#support-heading').textContent='对应简历要点';
    if(guide?.status==='ready'&&item){
      const details=itemDetails({item,text:item.text,signals:resume.signals?.[item.id]});add('item','简历原文',details.text,details.badges);
      const plan=resume.plans?.find(plan=>plan.key===item.plan_key);
      if(plan)add('plan','关联项目方案',PLAN_FIELDS.map(([key,label])=>`${label}\n${Array.isArray(plan[key])?plan[key].join('\n'):plan[key]||'暂无内容'}`).join('\n\n'));
      actionLabel='编辑这条要点';action=()=>{const id=item.id;showStep(4);const index=currentBatch.resumes[currentKind].items.findIndex(item=>item.id===id);$('#resume-section').value=`item:${index}`;renderResume(currentBatch.resumes[currentKind]);renderColumns();persistView();$('#edit-resume').click();$('#edit-section').value='items';$('#edit-item-select').value=String(index);showEditPane();const row=editItems.children[index],area=row.querySelector('textarea');(area.disabled?row.querySelector('.item-lock'):area).focus({preventScroll:true});};
    }else add('guide-state','对应关系',guide?.status==='stale'?'本版正文已变更。更新指南后，才会显示与当前要点对应的回答和项目方案。':'指南就绪后，这里显示与当前问题对应的简历要点和项目方案。');
    add('boundaries','回答与事实边界','已确认素材、AI 建议与实际完成是不同状态。\n\n只描述本人做过且可以核对的部分；未完成的步骤按计划说明，不作为过去经历。');
  }else{
    $('#support-heading').textContent='导出信息';
    add('saved','保存与对应关系',`${version} · 当前简历第 ${resume.revision} 次修订\n\n个人信息与正文读取当前保存修订。\n面试指南：${{ready:'已就绪，与当前正文对应',stale:'需要更新，请返回面试准备',failed:'生成失败，可单独重试',queued:'排队中',running:'生成中'}[guide?.status]||'尚未就绪'}。\n\n历史修订可从右上方“历史记录”查看与下载。`);
    add('formats','格式与用途','PDF：阅读与打印。\nWord：继续修改排版。\nHTML：浏览器阅读。\n\n“导出全部资料”包含本次资料。蓝图 Plus 导出前会确认准备用途。');
    actionLabel='返回核对简历';action=()=>showStep(4);
  }
  const nextContext=[currentBatch?.id,currentStep,currentKind,item?.id,guide?.status].join(':');
  const contextChanged=nextContext!==supportContext;supportContext=nextContext;
  setOptions($('#support-topic'),topics,contextChanged?topics[0]?.value:undefined);
  const topic=topics.find(topic=>topic.value===$('#support-topic').value)||topics[0];
  makeReader($('#support-reader'),topic.label,topic.text,topic.badges);
  if(topic.value==='content-review'){action=null;actionLabel='';}
  $('#support-action').hidden=!action;$('#support-action').textContent=actionLabel;$('#support-action').onclick=action?()=>{if($('#support-dialog').open)closeDialog($('#support-dialog'));action();}:null;
  refreshReaders();
}
let columnUpdate;
function scheduleColumns(){clearTimeout(columnUpdate);columnUpdate=setTimeout(renderColumns,100);}
form.addEventListener('input',scheduleColumns);form.addEventListener('change',scheduleColumns);
$('#context-source-select').addEventListener('change',renderColumns);
$('#support-topic').addEventListener('change',renderColumns);
function arrangeColumns(){
  const compact=matchMedia('(max-width:1000px)').matches||document.body.dataset.layout==='two';
  const narrow=matchMedia('(max-width:700px)').matches;
  for(const [name,inDialog] of [['context',narrow],['support',compact]]){
    const dialog=$(`#${name}-dialog`),column=$(`#${name}-column`);
    const parent=inDialog?$(`#${name}-slot`):$('.workspace-columns');
    if(column.parentElement!==parent){if(dialog.open)closeDialog(dialog);if(name==='context'&&!inDialog)parent.prepend(column);else parent.append(column);}
    $(`#open-${name}`).hidden=!inDialog;
  }
  refreshReaders();
}
for(const name of ['context','support']){
  $(`#open-${name}`).addEventListener('click',()=>{renderColumns();openDialog($(`#${name}-dialog`));});
  $(`#close-${name}`).addEventListener('click',()=>closeDialog($(`#${name}-dialog`)));
}
matchMedia('(max-width:1000px)').addEventListener('change',arrangeColumns);
matchMedia('(max-width:700px)').addEventListener('change',arrangeColumns);
arrangeColumns();

function render(batch) {
  const changedBatch=currentBatch?.id!==batch.id;currentBatch=batch;
  if(changedBatch){
    editSnapshot=null;if($('#editor-dialog').open)closeDialog($('#editor-dialog'));
    jdInput.value=batch.jd_text||'';materialInput.value=batch.material_text||'';$('#confirmed').checked=Boolean(batch.material_confirmed);
    HEADER_FIELDS.forEach(field=>$(`#pf-${field}`).value=batch.profile?.[field]||'');updateCounts();
    $('#resume-section').value='header';$('#guide-entry').value='0';
  }
  if(!$('#history-dialog').open)historyEl.value=batch.id;
  let [message,tone]=describeStatus(batch);
  if(batch.quality?.dropped_source_quotes>0){message+=` ${batch.quality.dropped_source_quotes} 条 AI 引用未通过核对，已在要点标记。`;tone=tone||'error';}
  if(batch.status!=='failed'&&batch.resumes?.blueprint&&currentKind==='blueprint'&&currentStep>=4){message='蓝图 Plus 含尚未完成的项目规划，仅供准备参考。';tone='error';}
  if(currentStep===4){const notice=currentReviewNotice(batch.resumes?.[currentKind]);if(notice){message+=` ${notice}`;tone='error';}}
  setStatus(message,tone);retryBatchEl.hidden=batch.status!=='failed'||!inputsMatchBatch();
  showStep(currentStep,false);
  if(['queued','running','guides_pending'].includes(batch.status)||Object.values(batch.guides||{}).some(guide=>['queued','running'].includes(guide.status))){clearTimeout(pollTimer);const epoch=requestEpoch;pollTimer=setTimeout(()=>loadBatch(batch.id,epoch),1600);}else clearTimeout(pollTimer);
}
async function loadBatch(id, epoch=requestEpoch) {
  try{
    const response=await fetch(`/api/batches/${id}`,{cache:'no-store'});
    if(epoch!==requestEpoch)return;
    if(response.status===404){clearTimeout(pollTimer);sessionStorage.removeItem('coach_batch_id');currentBatch=null;showStep(0,false);setStatus('这份记录已删除，可以开始新任务。');await loadHistory();return;}
    if(!response.ok)throw Error();const batch=await response.json();if(epoch!==requestEpoch)return;render(batch);
  }catch{
    if(epoch!==requestEpoch)return;setStatus('暂时无法连接本机服务，正在等待连接恢复…','error');clearTimeout(pollTimer);pollTimer=setTimeout(()=>loadBatch(id,epoch),4000);
  }
}
form.addEventListener('submit',async event=>{
  event.preventDefault();if(currentStep!==3){if(currentStep<3)$('#step-next').click();return;}if(submitting||uploading||!validateJD())return;
  submitting=true;showStep(3,false);setStatus('正在保存输入…');renderInputReview();const epoch=++requestEpoch;clearTimeout(pollTimer);
  try{
    const body=JSON.stringify({jd_text:jdInput.value.trim(),material_text:materialInput.value.trim(),material_confirmed:$('#confirmed').checked,profile:collectProfile('pf')});
    const fingerprint=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body)))].map(value=>value.toString(16).padStart(2,'0')).join('');
    let pending;try{pending=JSON.parse(sessionStorage.getItem('coach_pending')||'null');}catch{pending=null;}
    if(!pending||pending.fingerprint!==fingerprint)pending={fingerprint,key:crypto.randomUUID()};sessionStorage.setItem('coach_pending',JSON.stringify(pending));
    const response=await fetch('/api/batches',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},body});
    if(!response.ok)throw Error('提交失败，请稍后重试');const batch=await response.json();if(epoch!==requestEpoch)return;
    sessionStorage.setItem('coach_batch_id',batch.id);sessionStorage.removeItem('coach_pending');sessionStorage.removeItem('coach_draft');submitting=false;editSnapshot=null;render({...JSON.parse(body),...batch,resumes:{},guides:{}});await loadHistory();await loadBatch(batch.id,epoch);
  }catch(error){if(epoch!==requestEpoch)return;submitting=false;setStatus(error.message||'提交失败，请重试。','error');renderInputReview();}
});
$('#material-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;uploading=true;$('#upload-status').textContent='正在提取文字…';showStep(currentStep,false);const epoch=requestEpoch;
  try{const payload=new FormData();payload.append('file',file);const response=await fetch('/api/materials/extract',{method:'POST',body:payload});const result=await response.json();if(epoch!==requestEpoch)return;if(!response.ok)throw Error(result.detail||'无法读取文件');materialInput.value=result.text;$('#confirmed').checked=false;updateCounts();saveDraft();$('#upload-status').textContent='文字已提取，请核对和修改后再勾选确认。';}
  catch(error){if(epoch===requestEpoch)$('#upload-status').textContent=error.message||'无法读取文件，请手工粘贴。';}
  finally{if(epoch===requestEpoch){uploading=false;showStep(currentStep,false);}}
});
function updateCounts(){ $('#jd-count').textContent=`${jdInput.value.length} / ${jdInput.maxLength}`;$('#material-count').textContent=`${materialInput.value.length} / ${materialInput.maxLength}`; }
form.addEventListener('input',event=>{updateCounts();if(event.target===materialInput)$('#confirmed').checked=false;if(event.target===jdInput){jdInput.removeAttribute('aria-invalid');if(currentStep===0)setStatus('');}saveDraft();});
form.addEventListener('change',saveDraft);
function newTask(){
  requestEpoch++;historyEpoch++;clearTimeout(pollTimer);currentBatch=null;editSnapshot=null;submitting=false;uploading=false;currentKind='job';
  sessionStorage.removeItem('coach_batch_id');sessionStorage.removeItem('coach_pending');sessionStorage.removeItem('coach_view');form.reset();updateCounts();
  $('#upload-status').textContent='支持 PDF、Word、TXT。提取后请核对，不会自动确认事实。';setStatus('');updateKindButtons();showStep(0);saveDraft();
}
$('#new-task').addEventListener('click',()=>{if(editSnapshot)return;newTask();});
function updateKindButtons(){document.querySelectorAll('.tab').forEach(button=>{const active=button.dataset.kind===currentKind;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});}
document.querySelectorAll('.tab').forEach(button=>button.addEventListener('click',()=>{currentKind=button.dataset.kind;$('#resume-section').value='header';$('#guide-entry').value='0';updateKindButtons();if(currentBatch)render(currentBatch);persistView();}));
function confirmBlueprint(event){event.preventDefault();$('#confirm-blueprint').href=event.currentTarget.href;openDialog($('#blueprint-dialog'));}
for(const format of ['html','docx','pdf'])$(`#download-${format}`).addEventListener('click',event=>{if(currentKind==='blueprint')confirmBlueprint(event);});
$('#cancel-blueprint').addEventListener('click',()=>closeDialog($('#blueprint-dialog')));
$('#confirm-blueprint').addEventListener('click',()=>closeDialog($('#blueprint-dialog')));
retryBatchEl.addEventListener('click',async()=>{
  if(!currentBatch)return;retryBatchEl.disabled=true;const epoch=requestEpoch,id=currentBatch.id;
  try{const response=await fetch(`/api/batches/${id}/retry`,{method:'POST'});if(!response.ok)throw Error();await loadBatch(id,epoch);}catch{if(epoch===requestEpoch)setStatus('重试未提交成功，请稍后再试。','error');}finally{retryBatchEl.disabled=false;}
});
retryGuideEl.addEventListener('click',async()=>{
  if(!currentBatch)return;retryGuideEl.disabled=true;const epoch=requestEpoch,id=currentBatch.id,kind=currentKind;
  try{const response=await fetch(`/api/batches/${id}/guides/${kind}/retry`,{method:'POST'});if(!response.ok)throw Error();await loadBatch(id,epoch);}catch{if(epoch===requestEpoch)setStatus('指南重试未提交成功，请稍后再试。','error');}finally{retryGuideEl.disabled=false;}
});

function openDialog(dialog){dialogFocus.set(dialog,document.activeElement);dialog.showModal();refreshReaders();}
function closeDialog(dialog){dialog.close();const target=dialogFocus.get(dialog);if(target?.isConnected)target.focus({preventScroll:true});refreshReaders();}
function cancelEdit(){if($('#save-edit').disabled)return;editSnapshot=null;closeDialog($('#editor-dialog'));}
$('#cancel-edit').addEventListener('click',cancelEdit);$('#cancel-edit-top').addEventListener('click',cancelEdit);
$('#editor-dialog').addEventListener('cancel',event=>{if($('#save-edit').disabled){event.preventDefault();return;}editSnapshot=null;});
$('#edit-resume').addEventListener('click',()=>{
  const resume=currentBatch?.resumes[currentKind];if(!resume)return;
  editSnapshot={batchId:currentBatch.id,kind:currentKind,resume:structuredClone(resume)};
  editor.querySelectorAll('[data-header]').forEach(field=>field.value=resume.header?.[field.dataset.header]||'');$('#edit-title').value=resume.title;$('#edit-summary').value=resume.summary;
  $('#edit-kind').textContent=`编辑${currentKind==='job'?'求职版':'蓝图 Plus'} · 第 ${resume.revision} 次修订`;
  $('#edit-status').textContent='全部章节一起保存。确认与完成状态需要你逐条核对。';
  editItems.replaceChildren();resume.items.forEach(item=>addEditorItem(item));editPlans.replaceChildren();
  (resume.plans||[]).forEach((plan,index)=>{
    const section=textElement('div','','editor-item');section.dataset.key=plan.key;
    PLAN_FIELDS.forEach(([key,label])=>{const wrapper=textElement('div','','plan-field');wrapper.dataset.field=key;const area=document.createElement('textarea');area.id=`edit-plan-${index}-${key}`;area.dataset.field=key;area.value=Array.isArray(plan[key])?plan[key].join('\n'):plan[key]||'';area.maxLength=2000;const fieldLabel=textElement('label',key==='actions'?`${label}（每行一项）`:label);fieldLabel.htmlFor=area.id;wrapper.append(fieldLabel,area);section.append(wrapper);});editPlans.append(section);
  });
  $('#edit-section').value='header';$('#edit-item-select').value='0';$('#edit-plan-select').value='0';$('#edit-plan-field').value='title';refreshEditorOptions();showEditPane();openDialog($('#editor-dialog'));$('#edit-name').focus({preventScroll:true});
});
function addEditorItem(item={id:'',text:'',locked:false}){
  const row=textElement('div','','editor-item');row.dataset.id=item.id||'';row.dataset.originalLocked=String(Boolean(item.locked));
  const label=textElement('label','项目要点'),area=document.createElement('textarea');area.value=item.text;area.maxLength=600;area.minLength=4;area.id=`edit-item-${crypto.randomUUID()}`;label.htmlFor=area.id;
  const controls=textElement('div','','item-controls');
  const lock=textElement('label','','checkbox-row'),check=document.createElement('input');check.type='checkbox';check.className='item-lock';check.checked=Boolean(item.locked);lock.append(check,document.createTextNode('锁定此要点'));
  const confirmation=textElement('label','','checkbox-row'),confirmed=document.createElement('input');confirmed.type='checkbox';confirmed.className='item-confirm';confirmed.checked=item.verification_status==='user_confirmed';confirmation.append(confirmed,document.createTextNode('我已核对这段文字'));
  const completion=textElement('label','','checkbox-row'),achieved=document.createElement('input');achieved.type='checkbox';achieved.className='item-achieved';achieved.checked=Boolean(item.achieved);completion.append(achieved,document.createTextNode('该要点已实际完成'));
  const remove=textElement('button','删除要点','secondary');remove.type='button';remove.className+=' remove-item';
  const note=textElement('p','','field-note');
  function updateLock(){const locked=row.dataset.originalLocked==='true'||check.checked;area.disabled=locked;confirmed.disabled=locked;achieved.disabled=locked||!confirmed.checked;refreshRemoval();}
  check.addEventListener('change',updateLock);confirmed.addEventListener('change',()=>{row.dataset.confirmChanged='true';if(!confirmed.checked)achieved.checked=false;updateLock();});
  area.addEventListener('input',()=>{if(confirmed.checked)row.dataset.confirmChanged='true';confirmed.checked=false;achieved.checked=false;achieved.disabled=true;});
  remove.addEventListener('click',()=>{const selected=Number($('#edit-item-select').value);row.remove();refreshEditorOptions();$('#edit-item-select').value=String(Math.min(selected,editItems.children.length-1));showEditPane();});
  controls.append(lock,confirmation,completion,remove);row.append(label,area,controls,note);editItems.append(row);updateLock();
}
function refreshRemoval(){
  const rows=[...editItems.children];rows.forEach((row,index)=>{const originalLocked=row.dataset.originalLocked==='true';const locked=originalLocked||row.querySelector('.item-lock').checked;const laterLocked=rows.slice(index+1).some(next=>next.dataset.originalLocked==='true'||next.querySelector('.item-lock').checked);row.querySelector('.remove-item').disabled=locked||laterLocked||rows.length<=1;row.querySelector('.field-note').textContent=originalLocked?'先取消锁定并保存，再重新打开修改文字。':locked?'已锁定：取消锁定后才能修改。':laterLocked?'后面的要点已锁定，此条不能删除，以保留锁定位置。':rows.length<=1?'至少保留一个要点。':'改写文字会清除这条的核对与完成状态。';});
}
function refreshEditorOptions(){
  setOptions($('#edit-item-select'),[...editItems.children].map((row,index)=>({value:String(index),label:`项目要点 ${index+1}${row.querySelector('.item-lock').checked?' · 已锁定':''}`})));
  setOptions($('#edit-plan-select'),[...editPlans.children].map((section,index)=>({value:String(index),label:`项目方案 ${index+1}`})));
  $('#add-item').disabled=editItems.children.length>=20;refreshRemoval();
}
function showEditPane(){
  const pane=$('#edit-section').value;document.querySelectorAll('[data-edit-pane]').forEach(section=>section.hidden=section.dataset.editPane!==pane);
  [...editItems.children].forEach((row,index)=>row.hidden=index!==Number($('#edit-item-select').value));
  [...editPlans.children].forEach((section,index)=>{section.hidden=index!==Number($('#edit-plan-select').value);section.querySelectorAll('.plan-field').forEach(field=>field.hidden=field.dataset.field!==$('#edit-plan-field').value);});
  const sections=['header','summary','items','plans'];const current=sections.indexOf(pane);let index=0,total=1;
  if(pane==='items'){index=Number($('#edit-item-select').value);total=editItems.children.length;}
  if(pane==='plans'){index=Number($('#edit-plan-select').value)*6+PLAN_FIELDS.findIndex(([key])=>key===$('#edit-plan-field').value);total=editPlans.children.length*6;}
  $('#edit-pane-prev').disabled=current===0&&index===0;$('#edit-pane-next').disabled=current===3&&index===total-1;
  if(pane==='plans'&&!editPlans.children.length){$('#edit-status').textContent='本版暂无项目方案，正文和个人信息仍可修改。';$('#edit-pane-next').disabled=true;}
}
['edit-section','edit-item-select','edit-plan-select','edit-plan-field'].forEach(id=>$(`#${id}`).addEventListener('change',showEditPane));
$('#add-item').addEventListener('click',()=>{addEditorItem();refreshEditorOptions();$('#edit-item-select').value=String(editItems.children.length-1);showEditPane();editItems.lastElementChild.querySelector('textarea').focus();});
function moveEditPane(direction){
  const sections=['header','summary','items','plans'],pane=$('#edit-section').value,current=sections.indexOf(pane);
  if(pane==='items'){const select=$('#edit-item-select'),next=Number(select.value)+direction;if(next>=0&&next<editItems.children.length){select.value=String(next);showEditPane();return;}}
  if(pane==='plans'){let field=PLAN_FIELDS.findIndex(([key])=>key===$('#edit-plan-field').value),plan=Number($('#edit-plan-select').value),next=plan*6+field+direction;if(next>=0&&next<editPlans.children.length*6){$('#edit-plan-select').value=String(Math.floor(next/6));$('#edit-plan-field').value=PLAN_FIELDS[next%6][0];showEditPane();return;}}
  const next=current+direction;if(next<0||next>=sections.length)return;$('#edit-section').value=sections[next];
  if(sections[next]==='items')$('#edit-item-select').value=String(direction>0?0:editItems.children.length-1);
  if(sections[next]==='plans'){ $('#edit-plan-select').value=String(direction>0?0:editPlans.children.length-1);$('#edit-plan-field').value=PLAN_FIELDS[direction>0?0:5][0]; }
  showEditPane();
}
$('#edit-pane-prev').addEventListener('click',()=>moveEditPane(-1));$('#edit-pane-next').addEventListener('click',()=>moveEditPane(1));
editor.addEventListener('submit',async event=>{
  event.preventDefault();if(!editSnapshot)return;const snapshot=editSnapshot,resume=snapshot.resume;
  if($('#edit-title').value.trim().length<2||$('#edit-summary').value.trim().length<4){$('#edit-section').value='summary';showEditPane();$('#edit-status').textContent='标题至少 2 字，简介至少 4 字。';($('#edit-title').value.trim().length<2?$('#edit-title'):$('#edit-summary')).focus();return;}
  const invalid=[...editItems.children].findIndex(row=>row.querySelector('textarea').value.trim().length<4);
  if(invalid>=0){$('#edit-section').value='items';$('#edit-item-select').value=String(invalid);showEditPane();$('#edit-status').textContent='项目要点至少 4 字，请补充后保存。';editItems.children[invalid].querySelector('textarea').focus();return;}
  for(const [index,section] of [...editPlans.children].entries()){
    const blank=[...section.querySelectorAll('textarea')].find(area=>!area.value.trim());
    if(blank){$('#edit-section').value='plans';$('#edit-plan-select').value=String(index);$('#edit-plan-field').value=blank.dataset.field;showEditPane();$('#edit-status').textContent='项目方案各字段需有内容，请补充当前字段后保存。';blank.focus();return;}
  }
  const items=[...editItems.children].map(row=>({id:row.dataset.id||undefined,text:row.querySelector('textarea').value.trim(),locked:row.querySelector('.item-lock').checked,confirm:(row.dataset.confirmChanged||row.querySelector('.item-confirm').checked)?row.querySelector('.item-confirm').checked:undefined,achieved:row.querySelector('.item-achieved').checked}));
  const plans=[...editPlans.children].map(section=>{const plan=structuredClone(resume.plans.find(value=>value.key===section.dataset.key));section.querySelectorAll('textarea').forEach(area=>plan[area.dataset.field]=area.dataset.field==='actions'?area.value.split('\n').map(value=>value.trim()).filter(Boolean):area.value.trim());return plan;});
  const header=Object.fromEntries([...editor.querySelectorAll('[data-header]')].map(field=>[field.dataset.header,field.value.trim()]));
  const body={revision:resume.revision,title:$('#edit-title').value,summary:$('#edit-summary').value,items,plans,header};const epoch=requestEpoch;$('#save-edit').disabled=true;$('#cancel-edit').disabled=true;$('#cancel-edit-top').disabled=true;
  try{const response=await fetch(`/api/batches/${snapshot.batchId}/resumes/${snapshot.kind}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(epoch!==requestEpoch||editSnapshot!==snapshot||currentBatch?.id!==snapshot.batchId||currentKind!==snapshot.kind)return;if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'保存失败，请检查输入。');editSnapshot=null;closeDialog($('#editor-dialog'));render(result);setStatus(result.guides[snapshot.kind]?.status==='stale'?'简历新版已保存，请在面试准备步骤更新指南。':'简历新版已保存，指南仍与当前正文对应。','ready');}
  catch(error){if(epoch===requestEpoch&&editSnapshot===snapshot)$('#edit-status').textContent=error.message;}finally{$('#save-edit').disabled=false;$('#cancel-edit').disabled=false;$('#cancel-edit-top').disabled=false;}
});

async function loadHistory(){
  try{const response=await fetch('/api/batches');if(!response.ok)return;const rows=await response.json();const selected=$('#history-dialog').open?historyEl.value:(currentBatch?.id||historyEl.value);historyEl.replaceChildren(new Option('选择记录',''));rows.forEach(row=>historyEl.add(new Option(`${new Date(row.created_at).toLocaleString('zh-CN')} · ${row.jd_excerpt}`,row.id)));historyEl.value=selected;}catch{ /* Primary flow remains usable without history. */ }
}
async function loadRevisionHistory(){
  const epoch=++historyEpoch,id=historyEl.value,kind=historyKind;
  historyRows={id:null,kind:null,resumes:[],guides:[]};$('#history-downloads').replaceChildren();setOptions($('#history-revision'),[]);setOptions($('#history-section'),[]);
  ['history-type','history-revision','history-section','return-record'].forEach(control=>$(`#${control}`).disabled=true);
  if(!id){renderRevisionHistory();return;}
  makeReader($('#revision-history'),'正在读取历史','请稍候…');
  try{const responses=await Promise.all([fetch(`/api/batches/${id}/resumes/${kind}/revisions`),fetch(`/api/batches/${id}/guides/${kind}/revisions`)]);if(responses.some(response=>!response.ok))throw Error();const [resumes,guides]=await Promise.all(responses.map(response=>response.json()));if(epoch!==historyEpoch)return;historyRows={id,kind,resumes,guides};['history-type','history-revision','history-section'].forEach(control=>$(`#${control}`).disabled=false);renderRevisionHistory();}
  catch{if(epoch===historyEpoch){makeReader($('#revision-history'),'历史暂时不可用','请重新选择记录，或稍后再试。');$('#return-record').disabled=false;}}
}
function historySections(row,type){
  if(type==='resume')return resumeSections(row);
  return (row?.entries||[]).flatMap((entry,index)=>Object.entries(FACETS).map(([key,label])=>({value:`${index}:${key}`,label:`要点 ${index+1} · ${label}`,text:entry[key]||'暂无内容。'})));
}
function renderRevisionHistory(){
  const type=$('#history-type').value,rows=type==='resume'?historyRows.resumes:historyRows.guides;
  setOptions($('#history-revision'),rows.map(row=>({value:String(row.revision),label:`第 ${row.revision} 次修订${type==='guide'?` · 对应简历 ${row.resume_revision}`:''}`})));
  const row=rows.find(value=>String(value.revision)===$('#history-revision').value);$('#history-downloads').replaceChildren();
  $('#return-record').disabled=!historyEl.value;
  if(!row){setOptions($('#history-section'),[]);makeReader($('#revision-history'),'暂无历史内容','选择一条生成记录，即可查看各版简历和指南修订。');return;}
  const sections=historySections(row,type);setOptions($('#history-section'),sections);
  const section=sections.find(value=>value.value===$('#history-section').value);
  if(section){const details=type==='resume'?itemDetails(section):{text:section.text,badges:row.source==='degraded_local'?[['离线兜底指南 · 请核对','warn']]:[]};makeReader($('#revision-history'),`${section.label} · 第 ${row.revision} 次修订`,details.text,details.badges);}else makeReader($('#revision-history'),'指南尚未就绪',{failed:'这次指南生成失败。',queued:'这次指南仍在排队。',running:'这次指南正在生成。',superseded:'生成时简历内容已变化，旧结果未覆盖当前内容。'}[row.status]||'此修订暂无可读内容。');
  const id=historyRows.id;
  if(type==='resume')for(const format of ['html','docx','pdf']){const link=textElement('a',format.toUpperCase());link.href=`/api/batches/${id}/resumes/${historyRows.kind}/download/${format}?revision=${row.revision}`;if(historyRows.kind==='blueprint')link.addEventListener('click',confirmBlueprint);$('#history-downloads').append(link);}
  else if(row.status==='ready'){const link=textElement('a','下载这份历史指南');link.href=`/api/batches/${id}/guides/${historyRows.kind}/revisions/${row.revision}/download`;$('#history-downloads').append(link);}
}
$('#open-history').addEventListener('click',async()=>{historyKind=currentKind;$('#history-kind').value=currentKind;openDialog($('#history-dialog'));await loadHistory();await loadRevisionHistory();});
$('#close-history').addEventListener('click',()=>closeDialog($('#history-dialog')));
historyEl.addEventListener('change',loadRevisionHistory);
$('#history-kind').addEventListener('change',()=>{historyKind=$('#history-kind').value;loadRevisionHistory();});
['history-type','history-revision','history-section'].forEach(id=>$(`#${id}`).addEventListener('change',renderRevisionHistory));
$('#return-record').addEventListener('click',async()=>{
  const id=historyEl.value;if(!id)return;const epoch=++requestEpoch;clearTimeout(pollTimer);currentKind=historyKind;updateKindButtons();sessionStorage.setItem('coach_batch_id',id);sessionStorage.removeItem('coach_draft');closeDialog($('#history-dialog'));currentStep=4;await loadBatch(id,epoch);
});
$('#delete-batch').addEventListener('click',()=>{$('#delete-status').textContent='';openDialog($('#delete-dialog'));});
$('#cancel-delete').addEventListener('click',()=>closeDialog($('#delete-dialog')));
$('#confirm-delete').addEventListener('click',async()=>{
  if(!currentBatch)return;$('#confirm-delete').disabled=true;
  try{const response=await fetch(`/api/batches/${currentBatch.id}`,{method:'DELETE'});if(!response.ok)throw Error();closeDialog($('#delete-dialog'));newTask();await loadHistory();setStatus('本次资料已删除。恢复旧备份也会保留这次删除。','ready');statusEl.hidden=false;}
  catch{$('#delete-status').textContent='删除未完成，请检查服务后重试。';}finally{$('#confirm-delete').disabled=false;}
});
(async()=>{
  const initialEpoch=requestEpoch;
  try{const response=await fetch('/api/config');const config=await response.json();$('#provider-note').textContent=config.provider==='local'?'离线演示 · 未调用真实模型':'已配置真实模型';}catch{$('#provider-note').textContent='暂时无法检查模型配置';}
  await loadHistory();
  if(initialEpoch!==requestEpoch)return;
  let view,draft;try{view=JSON.parse(sessionStorage.getItem('coach_view')||'null');draft=JSON.parse(sessionStorage.getItem('coach_draft')||'null');}catch{ /* Ignore damaged transient view state. */ }
  const known=sessionStorage.getItem('coach_batch_id');
  if(known){currentKind=view?.batchId===known?view.kind||'job':'job';currentStep=view?.batchId===known?view.step??4:4;updateKindButtons();await loadBatch(known);if(initialEpoch!==requestEpoch||currentBatch?.id!==known)return;if(currentBatch){if(draft?.batchId===known){jdInput.value=draft.jd||'';materialInput.value=draft.material||'';$('#confirmed').checked=Boolean(draft.confirmed);HEADER_FIELDS.forEach(field=>$(`#pf-${field}`).value=draft.profile?.[field]||'');updateCounts();}if(view?.section)$('#resume-section').value=view.section;if(view?.entry)$('#guide-entry').value=view.entry;if(view?.facet)$('#guide-facets').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.facet===view.facet)));showStep(currentStep,false);}return;}
  if(draft){jdInput.value=draft.jd||'';materialInput.value=draft.material||'';$('#confirmed').checked=Boolean(draft.confirmed);HEADER_FIELDS.forEach(field=>$(`#pf-${field}`).value=draft.profile?.[field]||'');updateCounts();showStep(draft.step||0,false);return;}
  try{const response=await fetch('/api/batches/latest');if(response.ok){const batch=await response.json();if(initialEpoch!==requestEpoch)return;sessionStorage.setItem('coach_batch_id',batch.id);currentStep=4;render(batch);return;}}catch{setStatus('无法连接本机服务，请确认本机应用已启动。','error');statusEl.hidden=false;}
  showStep(0,false);
})();
