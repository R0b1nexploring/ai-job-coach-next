'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {step: 0, kind: 'job', id: null, batch: null, candidates: [], extractedText: null, sourceType: 'manual', busy: false, editing: false, key: null, timer: null, guideExplicit: false, guideChoices: {job:null,blueprint:null}, libraryItems: [], selectedMaterialId: null, materialRevisionId: null, deletePreview: null};
  let runtimeConfig = null;
  const kinds = {job: '求职版', blueprint: '蓝图 Plus'};
  const titles = ['设定目标岗位', '整理个人素材', '核对职业事实', '生成双版简历', '核对与编辑简历', '准备面试', '导出申请材料'];
  const descriptions = ['两版简历和面试指南都将围绕这个岗位准备。', '选用与岗位相关的经历，也可以跳过本步。', '对照原文，决定哪些经历可以由你本人确认。', '选择语言并核对资料，然后开始生成。', '切换版本，核对内容，或把修改保存为新修订。', '结合当前简历，准备回答思路与具体例子。', '选择要使用的版本及文件格式。'];
  const types = {employer:'公司',job_title:'职位',employment_date:'任职时间',school:'学校',major:'专业',degree:'学历',education_date:'就读时间',skill:'技能',project:'项目',responsibility:'职责',action:'行动',result:'结果',qualification:'资格',work_sample:'作品',resource_constraint:'资源限制'};
  const completions = {unknown:'未确定',completed:'已完成',in_progress:'进行中',planned:'计划中',not_applicable:'不适用'};
  const statuses = {queued:'排队中',running:'生成中',guides_pending:'简历已保存，指南处理中',guides_partial:'部分指南待更新',ready:'结果已保存',failed:'生成失败',review_blocked:'内容检查发现问题',stale:'需要更新',not_run:'尚未检查',passed:'内容检查已通过',suggestions:'内容检查有建议',unavailable:'内容检查不可用',blocked:'内容检查发现问题'};
  const statusText = s => statuses[s] || s || '尚无状态';
  const failureAdvice = code => ({MODEL_ACCESS_DENIED:'模型服务拒绝了访问。请更新服务密钥或检查模型权限，重启服务后可在这里重试；已核对的资料仍然保留。',MODEL_RATE_LIMITED:'模型服务已限流，请稍后在这里重试。',MODEL_TIMED_OUT:'模型没有在时限内返回，请稍后重试。',MODEL_UNAVAILABLE:'暂时无法连接模型服务，请确认服务可用后重试。'})[code] || '';
  const modelLabel = config => config?.provider === 'local' ? '本地演示（不调用模型）' : config?.provider === 'compatible' ? '真实模型服务' + (config.model ? ' · ' + config.model : '') : '未取得服务信息';
  const exclusionLabel = reason => ({unsupported_type:'类型未支持',sensitive_value:'含私密或疑似联系方式',sensitive_identifier:'标识含私密信息',empty_value:'内容为空',value_too_long:'内容过长'})[reason] || '无法纳入模型输入';
  const el = (tag, text, className) => {const node = document.createElement(tag); if (text != null) node.textContent = String(text); if (className) node.className = className; return node;};
  const msg = (text, error=false) => {const node=$('message'); node.textContent=text || ''; node.hidden=!text; node.setAttribute('data-error', String(error));};
  const safeId = id => typeof id === 'string' && /^[a-zA-Z0-9_-]+$/.test(id);
  const base = () => `/api/v2/batches/${encodeURIComponent(state.id)}`;
  function lock(value) {
    state.busy = value;
    for (const id of ['new-task', 'open-history', 'save-edit', 'cancel-edit']) $(id).disabled = value;
    for (const id of ['generate', 'extract', 'reextract']) $(id).disabled = value || !!state.id;
    syncActions();
    updateKind();
  }
  async function request(url, options={}) {
    const response = await fetch(url, options);
    let data;
    const contentType=response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {try {data=await response.json();} catch (_) {data={};}}
    if (!response.ok) {
      const detail=typeof data?.detail === 'string' ? data.detail : response.status === 409 ? '内容已变化，请刷新后重新核对。' : `请求未完成（${response.status}）`;
      throw new Error(detail);
    }
    return data;
  }
  const json = (method, data, extra={}) => ({method,headers:{'Content-Type':'application/json',...extra},body:JSON.stringify(data)});
  const header = prefix => Object.fromEntries(['name','phone','email','location','education'].map(k => [k, $(`${prefix}-${k}`).value.trim()]));
  const invalidatesRequest = () => {state.key=null;};
  const resultViews = {resume:4, guide:5, export:6};
  function syncResultLink() {
    const view = Object.keys(resultViews).find(key => resultViews[key] === state.step);
    const hash = state.id && view ? '#' + new URLSearchParams({batch:state.id, kind:state.kind, view}) : '';
    if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash);
  }
  function readResultLink() {
    const hash = new URLSearchParams(location.hash.slice(1));
    const id = hash.get('batch'), kind = hash.get('kind'), view = hash.get('view');
    return /^[0-9a-f]{32}$/.test(id || '') && Object.hasOwn(kinds, kind) && Object.hasOwn(resultViews, view)
      ? {id, kind, step:resultViews[view]} : null;
  }
  function openStep(step, focus=false) {
    if (state.editing && step !== 4) {msg('请先保存或取消当前修改。',true); return;}
    if (step > 3 && !state.id) {msg('请先创建本次生成。',true); return;}
    state.step = Math.max(0, Math.min(6, step));
    document.querySelectorAll('[data-panel]').forEach(panel => {
      panel.hidden = Number(panel.dataset.panel) !== state.step;
    });
    document.querySelectorAll('#steps [data-step]').forEach(button => {
      const index = Number(button.dataset.step);
      if (index === state.step) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      button.disabled = (index > 3 && !state.id) || state.busy;
    });
    $('toggle-steps').setAttribute('aria-expanded', 'false');
    $('step-label').textContent = (state.step < 3 ? '准备资料' : '制作与交付') + '  /  ' + String(state.step + 1).padStart(2, '0') + ' / 07';
    $('mobile-progress').textContent = (state.step + 1) + ' / 7';
    $('main-title').textContent = titles[state.step];
    $('step-description').textContent = descriptions[state.step];
    $('kind-switch').hidden = state.step < 4;
    if (state.id && state.step < 4) $('step-description').textContent = '这里显示本次生成时使用的资料。新建任务可更换输入。';
    document.querySelector('.stage-body').scrollTop = 0;
    if (focus) msg('');
    updateContext();
    updateKind();
    if (state.step === 2 && !state.id) renderCandidates();
    if (state.step >= 4) renderResults();
    syncActions();
    syncResultLink();
    if (focus) $('main-title').focus({preventScroll: true});
  }
  function syncActions() {
    const material = $('material').value.trim();
    const count = Array.from($('jd').value.trim()).length;
    const saved = !!state.id;
    const canSkip = !saved && !material;
    const nextLabels = ['继续：个人素材', canSkip ? '跳过素材，去生成' : '继续：核对事实', '继续：生成设置', '查看简历', '查看面试指南', '去导出', ''];
    $('next').textContent = nextLabels[state.step] + ' →';
    $('next').hidden = state.editing || state.step === 6 || (state.step === 3 && !saved);
    $('next').disabled = state.busy || (!saved && state.step === 0 && count < 20);
    $('generate').hidden = state.step !== 3 || saved;
    $('download-package').hidden = state.step !== 6;
    $('previous').hidden = state.step === 0 || state.editing;
    $('previous').disabled = state.busy;
    $('footer-hint').textContent = state.step === 0 && !saved
      ? (count < 20 ? '至少还需 ' + (20 - count) + ' 字' : '可以继续')
      : state.editing ? '正在编辑' : '';
    $('jd-count').textContent = Array.from($('jd').value).length.toLocaleString('en-US') + ' / 15,000';
    document.querySelectorAll('#steps [data-step]').forEach(button => {
      button.disabled = state.busy || (!saved && Number(button.dataset.step) > 3);
    });
  }
  function updateKind() {
    document.querySelectorAll('[data-kind]').forEach(button => {
      const active = button.dataset.kind === state.kind;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
      button.disabled = state.busy || state.editing;
    });
  }
  function appendSavedFacts(container) {
    for (const fact of state.batch?.facts || []) {
      const card = el('article', null, 'v2-card');
      card.append(el('strong', types[fact.fact_type] || '职业事实'), el('p', fact.value || '未填写'));
      const confirmed = fact.confirmation === 'user_confirmed' ? '本人已确认' : '待核对';
      card.append(el('p', confirmed + ' · ' + (completions[fact.completion] || '未确定'), 'field-note'));
      for (const evidence of fact.evidence || []) {
        const source = state.batch.material_sources.find(item => item.revision_id === evidence.source_revision_id);
        if (!source || !Number.isInteger(evidence.start) || !Number.isInteger(evidence.end)) continue;
        const quote = Array.from(source.extracted_text || '').slice(evidence.start, evidence.end).join('');
        if (quote) card.append(el('p', '原文依据：' + quote, 'field-note'));
      }
      container.append(card);
    }
  }
  function updateContext() {
    const batch = state.batch;
    const saved = !!state.id;
    const sources = batch?.material_sources || [];
    if (saved) {
      $('jd').value = batch?.jd_text || '';
      $('material').value = sources.map(source => source.extracted_text || '').join('\n\n');
      if (batch) {
        $('resume-locale').value = batch.resume_locale;
        $('guide-locale').value = batch.guide_locale;
      }
    }
    $('model-service').textContent = '生成服务：' + modelLabel(saved ? batch : runtimeConfig);
    $('fact-help').hidden = saved;
    $('context-jd').textContent = $('jd').value || '尚未填写岗位。';
    $('context-material').textContent = $('material').value || '没有个人素材。';
    const facts = saved ? batch?.facts || [] : state.candidates;
    const confirmed = facts.filter(item => saved ? item.confirmation === 'user_confirmed' : item.confirmed).length;
    $('context-state').textContent = facts.length ? '已确认 ' + confirmed + ' / ' + facts.length + ' 条事实'
      : saved ? '本次记录未使用职业事实' : '本次任务的原文可在这里核对';
    $('result-state').hidden = !saved;
    $('delivery-card').hidden = saved || state.step > 0;
    $('preparation-summary').hidden = saved || state.step === 0;
    $('check-job').textContent = Array.from($('jd').value.trim()).length >= 20 ? '已填写' : '待填写';
    $('check-material').textContent = $('material').value.trim() ? '已添加' : '可跳过';
    $('check-facts').textContent = facts.length ? confirmed + ' / ' + facts.length + ' 已确认' : '无候选';
    $('paste-jd').hidden = saved || !navigator.clipboard?.readText;
    $('support-state').textContent = statusText(batch?.status);
    const support = $('support-detail');
    support.replaceChildren();
    if (batch) {
      support.append(el('p', '内容评审：' + statusText(batch.review?.status)), el('p', '文件检查：尚未验收'));
      if (batch.provider === 'local') support.append(el('p', '本地演示按原文呈现事实。所选语言和内容需自行核对。'));
      for (const item of batch.projection_exclusions || []) support.append(el('p', '事实 ' + (item.ordinal + 1) + ' 未发送模型：' + exclusionLabel(item.reason)));
    }
    $('jd').readOnly = $('material').readOnly = saved;
    $('resume-locale').disabled = $('guide-locale').disabled = saved;
    $('material-file').closest('label').hidden = saved;
    $('extract').hidden = saved || !$('material').value.trim();
    $('reextract').hidden = saved || !$('material').value.trim();
    $('profile-options').hidden = saved;
    $('upload-note').hidden = saved;
    if (saved && state.step === 2) renderCandidates();
    if (saved && state.step === 3) $('generation-status').textContent = statusText(batch?.status);
    syncActions();
  }
  async function extract() {
    const text=$('material').value;
    if(!text.trim()){state.candidates=[];state.extractedText='';renderCandidates();msg('没有素材，可以继续生成草稿。');return true;}
    lock(true);msg('正在提取事实候选…');
    try {const result=await request('/api/v2/fact-candidates',json('POST',{text})); if(!result || !Array.isArray(result.candidates) || typeof result.normalized_text!=='string') throw new Error('事实提取结果格式无效。');
      if ($('material').value !== text) throw new Error('提取期间素材已改变，请重新提取。');
      $('material').value=result.normalized_text; state.extractedText=result.normalized_text;
      state.candidates=result.candidates.map(c=>({...c,confirmed:false})); invalidatesRequest();updateContext();renderCandidates();msg(`已提取 ${state.candidates.length} 条候选，请逐条核对。`);return true;
    } catch(e){msg(e.message,true);return false;} finally {lock(false);updateContext();}
  }
  function renderCandidates(){
    const list = $('candidate-list');
    list.replaceChildren();
    if (state.id) {
      appendSavedFacts(list);
      populateNavigator('fact', [...list.children]);
      $('candidate-empty').hidden = list.childElementCount > 0;
      return;
    }
    $('candidate-empty').hidden = state.candidates.length > 0;
    state.candidates.forEach((candidate,index)=>{
      const card=el('article',null,'v2-card');const heading=el('div',null,'v2-row');heading.append(el('strong',`事实 ${index+1}`));const del=el('button','删除','quiet');del.type='button';del.addEventListener('click',()=>{state.candidates.splice(index,1);invalidatesRequest();updateContext();renderCandidates();});heading.append(del);card.append(heading);
      const grid=el('div',null,'v2-grid');const typeWrap=el('div');typeWrap.append(el('label','事实类型'));const select=el('select');select.setAttribute('aria-label',`事实 ${index+1} 的类型`); const options={...types}; if(candidate.fact_type&&!options[candidate.fact_type]) options[candidate.fact_type]='请选择事实类型';for(const [value,label] of Object.entries(options)){const o=el('option',label);o.value=value;select.append(o);}select.value=candidate.fact_type || 'action';select.addEventListener('change',()=>{candidate.fact_type=select.value;candidate.confirmed=false;check.checked=false;invalidatesRequest();updateContext();});typeWrap.append(select);grid.append(typeWrap);
      const compWrap=el('div');compWrap.append(el('label','完成情况'));const comp=el('select');comp.setAttribute('aria-label',`事实 ${index+1} 的完成情况`);for(const [value,label] of Object.entries(completions)){const o=el('option',label);o.value=value;comp.append(o);}comp.value=candidate.completion || 'unknown';comp.addEventListener('change',()=>{candidate.completion=comp.value;candidate.confirmed=false;check.checked=false;invalidatesRequest();updateContext();});compWrap.append(comp);grid.append(compWrap);card.append(grid);
      const valueLabel=el('label',`事实 ${index+1} 的内容`);const value=el('textarea');value.setAttribute('aria-label',`事实 ${index+1} 的内容`);value.value=candidate.value || '';value.addEventListener('input',()=>{candidate.value=value.value;candidate.confirmed=false;check.checked=false;invalidatesRequest();updateContext();});card.append(valueLabel,value);
      const source=Array.from(state.extractedText || '').slice(candidate.start,candidate.end).join('');card.append(el('p',`来源原文：${source || '未提供'}`,'field-note'));
      const row=el('label',null,'checkbox-row');const check=el('input');check.type='checkbox';check.checked=!!candidate.confirmed;check.addEventListener('change',()=>{candidate.confirmed=check.checked;invalidatesRequest();updateContext();});row.append(check,el('span','我确认这条事实的内容与完成情况'));card.append(row);list.append(card);
    });
    populateNavigator('fact', [...list.children]);
  }
  function populateNavigator(name, targets) {
    const select = $(name + '-picker');
    const previous = select.value;
    select.replaceChildren(new Option('请选择…', ''));
    targets.forEach((target, index) => {
      const id = name + '-item-' + index;
      target.id = id;
      const heading = name === 'fact'
        ? (index + 1) + ' · ' + (state.id ? state.batch?.facts[index]?.value : state.candidates[index]?.value)
        : target.textContent;
      select.add(new Option(heading, id));
    });
    select.value = previous;
    $(name + '-navigation').hidden = targets.length < 2 || (name === 'resume' && state.editing);
  }
  for (const name of ['fact', 'resume', 'guide']) {
    $(name + '-picker').addEventListener('change', event => {
      const target = $(event.target.value);
      if (!target) return;
      target.scrollIntoView({block: 'start'});
      target.tabIndex = -1;
      target.focus({preventScroll: true});
    });
  }
  async function generate(){
    if(state.id || state.busy)return;
    const jd=$('jd').value.trim(), material=$('material').value;
    if(Array.from(jd).length<20){openStep(0);msg('请填写至少 20 字的岗位描述。',true);return;}
    if(material.trim() && state.extractedText!==material){openStep(1);msg('素材已变化，请重新提取事实。',true);return;}
    if(state.candidates.some(c=>!c.value?.trim())){openStep(2);msg('请补充或删除空事实。',true);return;}
    if(state.candidates.some(c=>!Object.hasOwn(types,c.fact_type))){openStep(2);msg('请为每条事实选择明确类型，或删除该事实。',true);return;}
    const candidates=state.candidates.map(c=>({fact_type:c.fact_type,value:c.value.trim(),start:c.start,end:c.end,quote_hash:c.quote_hash,confirmed:!!c.confirmed,completion:c.completion || 'unknown'}));
    const payload={jd_text:jd,material_text:material,candidates,header:header('header'),resume_locale:$('resume-locale').value,guide_locale:$('guide-locale').value,source_type:state.sourceType};
    if(state.materialRevisionId)payload.material_revision_id=state.materialRevisionId;
    state.key ||= crypto.randomUUID(); lock(true);msg('正在检查哪些事实可以发送给模型…');
    try{const preview=await request('/api/v2/projection-preview',json('POST',{candidates,header:payload.header}));
      if(!preview || !Array.isArray(preview.excluded))throw new Error('无法确认模型输入，请稍后重试。');
      if(preview.excluded.length){const lines=preview.excluded.map(e=>`事实 ${Number(e.ordinal)+1}：${exclusionLabel(e.reason)}`);if(!confirm(`以下事实不会发送给模型：\n${lines.join('\n')}\n\n可以返回修改，或选择继续生成。`)){msg('已取消生成，可返回修改事实。');return;}}
      msg('正在创建任务…');const result=await request('/api/v2/batches',json('POST',payload,{'Idempotency-Key':state.key}));if(!safeId(result?.id))throw new Error('创建结果缺少有效记录编号。');state.id=result.id;localStorage.setItem('jobcoach-v2-current-id',state.id);await refresh();openStep(4,true);msg('任务已创建，离开页面后可通过本浏览器恢复。');}
    catch(e){msg(e.message,true);}finally{lock(false);}
  }
  async function refresh(){
    if(!state.id)return;
    const requestedId=state.id;
    try{const batch=await request(base());if(state.id!==requestedId)return;state.batch=batch;if(!state.batch || state.batch.id!==requestedId)throw new Error('未找到本次记录。');if(!state.editing)renderResults();updateContext();$('generation-status').textContent=statusText(state.batch.status); schedule();}
    catch(e){if(state.id!==requestedId)return;clearTimeout(state.timer);msg(`读取状态失败：${e.message}`,true);}
  }
  function schedule(){clearTimeout(state.timer);const b=state.batch;if(!b)return;const pending=['queued','running','guides_pending'].includes(b.status)||Object.values(b.guides || {}).some(g=>['queued','running'].includes(g.status));if(pending)state.timer=setTimeout(refresh,3000);}
  function addUnit(container, unit){if(unit?.text)container.append(el('li',unit.text));}
  function renderResults(){
    if(state.editing)return;
    const b=state.batch, resume=b?.resumes?.[state.kind], guide=b?.guides?.[state.kind];
    $('resume-heading').textContent=kinds[state.kind];$('resume-status').textContent=resume?`修订 ${resume.revision} · 内容评审：${statusText(resume.review?.status)} · 文件检查未完成`:`${statusText(b?.status)}，暂无可查看的简历。`;
    $('edit').disabled=!resume?.document;$('retry-batch').hidden=b?.status!=='failed';const reader=$('resume-reader');reader.replaceChildren();
    if(!resume && ['queued','running'].includes(b?.status)) {
      const pending = el('div', null, 'pending-work');
      pending.append(el('strong', statusText(b.status)), el('p', '简历完成后会自动显示。可以离开，稍后从历史记录继续。'));
      reader.append(pending);
    }
    else if(b?.provider==='local')reader.append(el('p','本地演示只原样呈现事实；不代表所选语言已完整翻译或通过质量检查。','field-note'));else if(b?.provider)reader.append(el('p','所选语言尚未自动验收，请核对实际正文。','field-note'));
    for(const exclusion of b?.projection_exclusions || [])reader.append(el('p',`事实 ${Number(exclusion.ordinal)+1} 未发送模型：${exclusionLabel(exclusion.reason)}`,'field-note'));
    if(resume?.document){
      const doc=resume.document, savedHeader=resume.header || {}, refs=Object.fromEntries((b.facts || []).map(c=>[c.claim_id,c.value]));
      const displayValue=value=>typeof value==='string' && value.trim() ? value : '待填写';
      reader.append(el('h3',`姓名：${displayValue(savedHeader.name)}`));
      reader.append(el('p',`电话：${displayValue(savedHeader.phone)} · 邮箱：${displayValue(savedHeader.email)} · 所在城市：${displayValue(savedHeader.location)}`));
      reader.append(el('h3',doc.title),el('p',doc.summary?.text || ''));
      const fields=['entity_ref','organization_ref','role_ref','school_ref','major_ref','degree_ref','date_ref'];
      const visibleText=value=>typeof value==='string' && !!value.trim();
      const hasEducationFact=(doc.sections || []).filter(section=>section.type==='education').some(section=>(section.entries || []).some(entry=>['school_ref','major_ref','degree_ref','date_ref'].some(field=>visibleText(refs[entry[field]]))));
      if(visibleText(savedHeader.education) && !(resume.education_fact_adopted===true && hasEducationFact)){
        reader.append(el('h3','教育背景（个人信息）'),el('p',savedHeader.education));
      }else if(!visibleText(savedHeader.education) && !hasEducationFact){reader.append(el('h3','教育背景'),el('p','学校、专业、学历和就读时间：待填写','field-note'));}
      for(const section of doc.sections || []){
        const entries=(section.entries || []).map(entry=>{
          const parts=[entry.name,entry.category,...fields.map(field=>{const value=refs[entry[field]],alias=entry.display_aliases?.[field];return visibleText(value) && alias?.locale===doc.locale && visibleText(alias.text)?alias.text:value;})].filter(visibleText);
          const units=(entry.units || []).filter(unit=>visibleText(unit.text));return {parts:[...new Set(parts)],units};
        }).filter(entry=>entry.parts.length || entry.units.length);
        if(!entries.length)continue;
        reader.append(el('h3',({experience:'工作经历',projects:'项目经历',skills:'技能',education:'教育背景'})[section.type]||section.type));
        for(const entry of entries){if(entry.parts.length)reader.append(el('p',entry.parts.join(' · ')));const ul=el('ul');for(const unit of entry.units)addUnit(ul,unit);if(ul.childNodes.length)reader.append(ul);}
      }
    }
    else if(b?.status==='review_blocked'){
      reader.append(el('p','内容检查发现问题，以下候选仅供诊断，未作为正式简历保存或提供正式下载。'));
      const candidate=b.diagnostic_documents?.[state.kind];
      const location = id => {if(!candidate)return '';if(candidate.summary?.unit_id===id)return candidate.summary.text;for(const section of candidate.sections || [])for(const entry of section.entries || [])for(const unit of entry.units || [])if(unit.unit_id===id)return unit.text;return '';};
      for(const issue of b.review?.issues || []){if(issue.scope!==state.kind && issue.scope!=='plan')continue;const box=el('div',null,'v2-diagnostic');box.append(el('h3',`问题定位：${issue.scope==='plan'?'方案':kinds[issue.scope]} · ${issue.target_id || '未提供定位'}`));const target=location(issue.target_id);if(target)box.append(el('p',`对应段落：${target}`));box.append(el('p',`问题：${issue.diagnosis || '请核对内容。'}`),el('p',`建议：${issue.recommendation || '请核对来源。'}`));reader.append(box);}
      if(candidate){reader.append(el('h3',`诊断候选 · ${candidate.title || kinds[state.kind]}`));if(candidate.summary?.text)reader.append(el('p',`简介 [${candidate.summary.unit_id || '候选'}]：${candidate.summary.text}`));for(const section of candidate.sections || []){reader.append(el('h3',({experience:'工作经历',projects:'项目经历',skills:'技能',education:'教育背景'})[section.type] || '章节'));for(const entry of section.entries || []){if(entry.name || entry.category)reader.append(el('p',entry.name || entry.category));for(const unit of entry.units || [])reader.append(el('p',`[${unit.unit_id || '候选'}] ${unit.text || ''}`));}}}
    }
    else if (!['queued','running'].includes(b?.status)) reader.append(el('p','正文生成后会显示在这里。','field-note'));
    if(b?.status==='failed') reader.replaceChildren(el('p',failureAdvice(b.error_code) || '本次生成没有完成。已核对的资料仍保留，可重试生成。','v2-diagnostic'));
    const gstatus=guide?`${statusText(guide.status)} · 指南修订 ${guide.revision} · 原始生成自简历修订 ${guide.resume_revision} · 适用于修订 ${guide.applies_to_revision ?? '未确认'} · ${guide.locale==='en'?'英文':'中文'}`:'指南尚未生成。';$('guide-status').textContent=[gstatus,failureAdvice(guide?.error_code)].filter(Boolean).join('。');
    const gr=$('guide-reader');gr.replaceChildren();for(const entry of guide?.content?.entries || []){gr.append(el('h3',entry.question));for(const [key,label] of Object.entries({answer_outline:'回答思路',contribution_boundary:'贡献边界',verification:'验证方式',practice_step:'准备动作'}))if(entry[key])gr.append(el('p',`${label}：${entry[key]}`));}for(const note of guide?.content?.context_notes || [])gr.append(el('p',note.text || ''));if(!gr.childNodes.length)gr.append(el('p','指南内容保存后会显示在这里。','field-note'));
    const localeChoice=state.guideChoices[state.kind] || guide?.locale || b?.guide_locale || 'zh-CN';
    $('guide-new-locale').value=localeChoice;$('guide-new-locale').disabled=!guide || ['queued','running'].includes(guide.status);
    if (guide && ['failed','stale'].includes(guide.status)) $('guide-options').open = true;
    const canRetry=!!guide && (['failed','stale'].includes(guide.status) || (guide.status==='ready' && localeChoice!==guide.locale));
    $('retry-guide').hidden=!canRetry;$('retry-guide').textContent=guide?.status==='ready'?'以所选语言生成新指南':'更新本版指南';
    $('export-status').textContent=resume?`${kinds[state.kind]} · 简历修订 ${resume.revision} · ${statusText(resume.review?.status)}；文件尚未验收。`:'简历尚未保存，暂不能下载。';
    const canGuide=!!guide && guide.status==='ready' && guide.applies_to_revision===resume?.revision;
    $('guide-export-note').textContent=canGuide?`指南修订 ${guide.revision}，原始生成自简历修订 ${guide.resume_revision}，适用于当前修订 ${guide.applies_to_revision}。`:'对应当前修订的指南尚未就绪，指南文件暂不可用。';
    populateNavigator('resume', [...reader.querySelectorAll('h3')]);
    populateNavigator('guide', [...gr.querySelectorAll('h3')]);
    downloads('resume-downloads','resume',!!resume); downloads('guide-downloads','guide',canGuide);$('download-package').disabled=!state.id;
  }
  function downloads(id,category,enabled){const target=$(id);target.replaceChildren();for(const format of ['html','docx','pdf']){const b=el('button',format.toUpperCase(),'secondary');b.type='button';b.disabled=!enabled;b.addEventListener('click',()=>download(category,format));target.append(b);}}
  async function download(category,format, options={}){
    const id=options.id || state.id, kind=options.kind || state.kind, revision=options.revision;
    if(!safeId(id) || !['job','blueprint'].includes(kind) || (revision !== undefined && (!Number.isInteger(revision) || revision<1))){msg('历史文件绑定无效。',true);return;}
    const query=new URLSearchParams({paper:$('export-paper').value,page_target:$('export-pages').value});if(category==='guide')query.set('allow_overflow','true');if(revision!==undefined)query.set('revision',String(revision));
    const urlPath=`/api/v2/batches/${encodeURIComponent(id)}/documents/${kind}/${category}/${format}?${query}`;
    try{const response=await fetch(urlPath);if(!response.ok){let info;try{info=await response.json();}catch(_){}let detail=typeof info?.detail==='string'?info.detail:`下载未完成（${response.status}）`;if(info?.page_count && Array.isArray(info.section_lengths)){const lengths=info.section_lengths.map(([heading,count])=>`${heading || '开头'}：${count} 字`).join('；');detail+=` 实际 ${info.page_count} 页。${lengths}`;}throw new Error(detail);}const blob=await response.blob();const url=URL.createObjectURL(blob);const a=el('a');a.href=url;a.download=`${kind}-${category}${revision===undefined?'':`-v${revision}`}-draft.${format}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    catch(e){if($('history-dialog').open)$('history-note').textContent=e.message;else msg(e.message,true);}
  }
  async function downloadPackage(){
    if(!safeId(state.id))return;
    const batchId=state.id, button=$('download-package'), status=$('package-status');button.disabled=true;status.textContent='正在检查并生成完整草稿包…';
    const query=new URLSearchParams({paper:$('export-paper').value,page_target:$('export-pages').value,guide_pagination:'flow'});
    try{
      const response=await fetch(`/api/v2/batches/${encodeURIComponent(batchId)}/package?${query}`);
      if(!response.ok){
        let info;try{info=await response.json();}catch(_){}
        const docLabel = key => {const [kind, type] = String(key).split(':');return (kinds[kind] || '当前版本') + (type==='guide'?'指南':'简历');};
        let detail = typeof info?.detail==='string' ? info.detail : '草稿包未完成，请稍后重试。';
        if (Array.isArray(info?.missing) && info.missing.length) detail += ' 待完成：' + info.missing.map(docLabel).join('、') + '。';
        for (const item of info?.overflows || []) {
          detail += ' ' + docLabel(item.document) + '实际 ' + item.page_count + ' 页，目标 ' + item.page_target + ' 页。';
          detail += item.page_target===1 ? '可在下载设置中选择两页，或缩短简历。' : '请在简历编辑中缩短正文，再导出。';
        }
        throw new Error(detail);
      }
      const blob=await response.blob();if(state.id!==batchId)throw new Error('当前记录已切换，请在目标记录中重新下载草稿包。');const url=URL.createObjectURL(blob);const a=el('a');a.href=url;a.download=`jobcoach-${batchId.slice(0,8)}-complete-draft.zip`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='草稿包已生成；内容和文件质量仍需核对。';
    }catch(e){status.textContent=e.message;msg('完整草稿包尚未生成，请查看具体原因。',true);}finally{button.disabled=!state.id;}
  }
  function edit(){
    const r=state.batch?.resumes?.[state.kind];if(!r?.document)return;
    state.editing=true;$('resume-navigation').hidden=true;document.querySelector('.stage-body').scrollTop=0;syncActions();updateKind();$('resume-reader').hidden=true;$('edit').hidden=true;$('editor').hidden=false;$('editor-actions').hidden=false;
    const editor=$('editor');editor.replaceChildren();
    const field=(target,label,value,attribute,isArea=false)=>{const wrap=el('div',null,'v2-editor-field');const caption=el('label',label);const input=el(isArea?'textarea':'input');input.value=value || '';input.dataset.edit=attribute;input.setAttribute('aria-label',label);wrap.append(caption,input);target.append(wrap);return {input,wrap};};
    field(editor,'简历标题',r.document.title,'title');for(const k of ['name','phone','email','location','education'])field(editor,({name:'姓名',phone:'电话',email:'邮箱',location:'所在城市',education:'教育背景'})[k],r.header?.[k],`header:${k}`,k==='education');
    const summary=field(editor,'简介',r.document.summary.text,`unit:${r.document.summary.unit_id}`,true);if(r.document.summary.locked){summary.input.disabled=true;summary.wrap.append(el('p','此内容已锁定，不能修改。','field-note'));}
    editor.append(el('p','可调整已有章节顺序，在已有条目中新增或删除正文。新增内容不会自动成为已确认事实。','field-note'));
    const sections=el('div');sections.id='edit-sections';editor.append(sections);
    const sectionNames={experience:'工作经历',projects:'项目经历',skills:'技能',education:'教育背景'};
    const updateMoves=()=>{const cards=[...sections.children];cards.forEach((card,index)=>{const up=card.querySelector('[data-move=up]'),down=card.querySelector('[data-move=down]');up.disabled=index===0||card.dataset.locked==='true'||cards[index-1]?.dataset.locked==='true';down.disabled=index===cards.length-1||card.dataset.locked==='true'||cards[index+1]?.dataset.locked==='true';});};
    for(const section of r.document.sections || []){
      const card=el('section',null,'v2-card v2-edit-section');card.dataset.sectionId=section.section_id;card.dataset.locked=String((section.entries || []).some(entry=>(entry.units || []).some(unit=>unit.locked)));
      const bar=el('div',null,'v2-row');bar.append(el('strong',sectionNames[section.type] || '章节'));const moves=el('div',null,'v2-row');
      for(const [direction,label] of [['up','上移章节'],['down','下移章节']]){const button=el('button',label,'secondary');button.type='button';button.dataset.move=direction;button.setAttribute('aria-label',`${label}：${sectionNames[section.type] || '章节'}`);button.addEventListener('click',()=>{const sibling=direction==='up'?card.previousElementSibling:card.nextElementSibling;if(!sibling)return;if(direction==='up')sections.insertBefore(card,sibling);else sections.insertBefore(sibling,card);updateMoves();});moves.append(button);}bar.append(moves);card.append(bar);
      if(card.dataset.locked==='true')card.append(el('p','本章节含锁定内容，不能调整其位置。','field-note'));
      if(!(section.entries || []).length)card.append(el('p','此章节没有可添加正文的现有条目。','field-note'));
      for(const [entryIndex,entry] of (section.entries || []).entries()){
        const entryBox=el('div',null,'v2-entry-edit');entryBox.append(el('h3',entry.name || entry.category || `条目 ${entryIndex+1}`));const entryHasLocked=(entry.units || []).some(unit=>unit.locked);
        if(entryHasLocked)entryBox.append(el('p','此条目含锁定要点，为保留其位置，暂不能删除条目内要点。','field-note'));
        for(const [unitIndex,unit] of (entry.units || []).entries()){
          const label=`${sectionNames[section.type] || '正文'} · 要点 ${unitIndex+1}`;const f=field(entryBox,label,unit.text,`unit:${unit.unit_id}`,true);
          if(unit.locked){f.input.disabled=true;f.wrap.append(el('p','此内容已锁定，不能修改或删除。','field-note'));continue;}
          if(entryHasLocked)continue;
          const remove=el('button','删除要点','quiet');remove.type='button';remove.setAttribute('aria-label',`删除${label}`);remove.addEventListener('click',()=>{const deleted=f.wrap.dataset.deleted==='true';f.wrap.dataset.deleted=String(!deleted);f.input.disabled=!deleted;remove.textContent=deleted?'删除要点':'撤销删除';remove.setAttribute('aria-label',`${deleted?'删除':'撤销删除'}${label}`);f.wrap.classList.toggle('v2-deleted',!deleted);});f.wrap.append(remove);
        }
        const additions=el('div');entryBox.append(additions);const add=el('button','增加正文要点','secondary');add.type='button';add.setAttribute('aria-label',`在${sectionNames[section.type] || '章节'}条目 ${entryIndex+1} 增加正文要点`);add.addEventListener('click',()=>{const wrap=el('div',null,'v2-editor-field v2-addition');const label=el('label','新增要点（待核对）');const input=el('textarea');input.dataset.addEntry=entry.entry_id;input.setAttribute('aria-label','新增要点内容');input.maxLength=2000;const remove=el('button','移除此新增要点','quiet');remove.type='button';remove.addEventListener('click',()=>wrap.remove());wrap.append(label,input,remove);additions.append(wrap);input.focus();});entryBox.append(add);card.append(entryBox);
      }
      sections.append(card);
    }
    updateMoves();
    const planRefs=new Map();
    for(const section of r.document.sections || [])for(const [index,entry] of (section.entries || []).entries()){
      const label=entry.name || entry.category || `${sectionNames[section.type] || '章节'}条目 ${index+1}`;
      const refs=new Set([section.type==='projects'?entry.plan_ref:null,...(entry.units || []).map(unit=>unit.plan_ref)].filter(Boolean));
      for(const ref of refs){if(!planRefs.has(ref))planRefs.set(ref,[]);if(!planRefs.get(ref).includes(label))planRefs.get(ref).push(label);}
    }
    if(!Array.isArray(r.plans))editor.append(el('p','本版方案信息暂不可用；本次编辑不会提交方案修改。','field-note'));
    else for(const plan of r.plans){
      if(!planRefs.has(plan.key))continue;
      const box=el('section',null,'v2-card v2-plan-edit');box.dataset.planKey=plan.key;
      box.append(el('strong','项目方案 · 待核对'),el('p',`关联项目：${planRefs.get(plan.key).join('、')}`,'field-note'));
      box.append(el('p','以下为已保存的方案行动；修改关联项目正文后会按当前要点同步，背景等仍需核对。修改方案不会自动确认事实。','field-note'));
      const actions=el('ul');for(const action of plan.actions || [])actions.append(el('li',action));
      if(actions.children.length)box.append(actions);else box.append(el('p','当前没有已保存的行动要点。','field-note'));
      for(const [key,label] of Object.entries({title:'方案名称',background:'背景与限制',task:'任务',outcome:'结果或预期产物',verification:'核验方法',resource_constraints:'资源限制（每行一项）'})){
        const wrap=el('div',null,'v2-editor-field');const caption=el('label',label);const input=el(key==='title'?'input':'textarea');
        input.value=key==='resource_constraints'?(plan[key] || []).join('\n'):(plan[key] || '');input.dataset.planField=key;input.setAttribute('aria-label',`${plan.title || '项目方案'} · ${label}`);input.maxLength=key==='title'?200:2000;wrap.append(caption,input);box.append(wrap);
      }
      editor.append(box);
    }
  }
  function cancelEdit(){state.editing=false;syncActions();updateKind();$('editor').hidden=true;$('editor-actions').hidden=true;$('resume-reader').hidden=false;$('edit').hidden=false;renderResults();}
  async function saveEdit(){
    const r=state.batch?.resumes?.[state.kind];if(!r)return;
    const fields=[...$('editor').querySelectorAll('[data-edit]')];const data={expected_revision:r.revision,title:'',unit_texts:{},header:{},section_order:[...$('edit-sections').children].map(card=>card.dataset.sectionId),delete_unit_ids:[],add_units:[],plan_updates:{}};
    for(const input of fields){const key=input.dataset.edit;if(key==='title')data.title=input.value;else if(key.startsWith('header:'))data.header[key.slice(7)]=input.value;else if(key.startsWith('unit:')){if(input.parentElement.dataset.deleted==='true')data.delete_unit_ids.push(key.slice(5));else if(!input.disabled)data.unit_texts[key.slice(5)]=input.value;}}
    for(const input of $('editor').querySelectorAll('[data-add-entry]')){if(!input.value.trim()){msg('请填写新增要点或移除空白要点。',true);input.focus();return;}data.add_units.push({entry_id:input.dataset.addEntry,text:input.value.trim()});}
    for(const box of $('editor').querySelectorAll('[data-plan-key]')){
      const original=(r.plans || []).find(plan=>plan.key===box.dataset.planKey);if(!original){msg('方案引用已变化，请刷新后重新编辑。',true);return;}
      const update={};for(const input of box.querySelectorAll('[data-plan-field]')){const key=input.dataset.planField;update[key]=key==='resource_constraints'?input.value.split('\n').map(line=>line.trim()).filter(Boolean):input.value;}
      const keys=['title','background','task','outcome','verification','resource_constraints'];
      if(keys.some(key=>JSON.stringify(update[key])!==JSON.stringify(key==='resource_constraints'?(original[key] || []):(original[key] || '')))){
        for(const key of keys.filter(key=>key!=='resource_constraints'))if(!update[key]?.trim()){msg('请填写完整的方案名称、背景、任务、结果和核验方法。',true);return;}
        data.plan_updates[box.dataset.planKey]=update;
      }
    }
    lock(true);try{await request(`${base()}/resumes/${state.kind}`,json('PUT',data));cancelEdit();await refresh();msg('已保存本版修订。');}catch(e){msg(e.message,true);}finally{lock(false);}
  }
  async function retryBatch(){lock(true);try{await request(`${base()}/retry`,{method:'POST'});await refresh();msg('已请求重试本次生成。');}catch(e){msg(e.message,true);}finally{lock(false);}}
  async function retryGuide(){const guideLocale=$('guide-new-locale').value;lock(true);$('retry-guide').disabled=true;try{await request(`${base()}/guides/${state.kind}/retry`,json('POST',{guide_locale:guideLocale}));await refresh();msg('已请求生成本版新指南，原指南保留在历史记录。');}catch(e){msg(e.message,true);}finally{lock(false);$('retry-guide').disabled=false;}}
  async function removeBatch(){if(!state.id || !confirm('将永久删除本次资料、两版简历和对应指南。此操作无法撤销。继续吗？'))return;lock(true);try{await request(base(),{method:'DELETE'});resetTask();msg('本次资料已删除。');}catch(e){msg(e.message,true);}finally{lock(false);}}
  $('paste-jd').addEventListener('click', async () => {
    const field = $('jd');
    try {
      const text = await navigator.clipboard.readText();
      if (state.id || state.step !== 0) return;
      if (!text.trim()) { msg('剪贴板里没有文字。可以直接输入岗位描述。', true); return; }
      if (text.length > field.maxLength) { msg('岗位描述超过 15,000 字，请选取岗位职责和任职要求。', true); return; }
      if (field.value.trim() && !confirm('用剪贴板内容替换当前岗位描述？')) return;
      field.value = text;
      field.dispatchEvent(new Event('input'));
      msg('');
    } catch {
      msg('请在输入框中使用系统粘贴。', true);
    }
    field.focus();
  });
  $('jd').addEventListener('input',()=>{invalidatesRequest();updateContext();});$('material').addEventListener('input',()=>{state.extractedText=null;state.candidates=[];state.materialRevisionId=null;state.sourceType='manual';$('library-status').textContent='文字已修改，当前不再绑定已保存修订；可另存为新修订。';invalidatesRequest();updateContext();});
  document.querySelectorAll('[id^=header-]').forEach(node=>node.addEventListener('input',invalidatesRequest));
  $('resume-locale').addEventListener('change',()=>{if(!state.guideExplicit)$('guide-locale').value=$('resume-locale').value;invalidatesRequest();});
  $('guide-locale').addEventListener('change',()=>{state.guideExplicit=true;invalidatesRequest();});
  $('material-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;lock(true);$('upload-note').textContent='正在提取文件文字…';try{const data=new FormData();data.append('file',file);const result=await request('/api/materials/extract',{method:'POST',body:data});if(typeof result?.text!=='string')throw new Error('未提取到文字。');$('material').value=result.text;state.extractedText=null;state.candidates=[];state.materialRevisionId=null;state.sourceType=file.name.toLowerCase().endsWith('.pdf')?'pdf':file.name.toLowerCase().endsWith('.docx')?'docx':'text';invalidatesRequest();$('upload-note').textContent='已提取文字，请核对后提取事实。';updateContext();}catch(e){$('upload-note').textContent=`提取失败：${e.message}；可以直接手填。`;}finally{lock(false);}});
  $('guide-new-locale').addEventListener('change',event=>{state.guideChoices[state.kind]=event.target.value;renderResults();});
  $('extract').addEventListener('click',extract);$('reextract').addEventListener('click',extract);$('generate').addEventListener('click',generate);$('edit').addEventListener('click',edit);$('cancel-edit').addEventListener('click',cancelEdit);$('save-edit').addEventListener('click',saveEdit);$('retry-guide').addEventListener('click',retryGuide);$('retry-batch').addEventListener('click',retryBatch);$('download-package').addEventListener('click',downloadPackage);$('delete-batch').addEventListener('click',removeBatch);
  async function moveForward() {
    if (state.busy) return;
    if (state.step === 1 && !state.id) {
      if (!$('material').value.trim()) {
        openStep(3, true);
        return;
      }
      if (state.extractedText !== $('material').value && !await extract()) return;
    }
    if (state.step === 2 && !state.id) {
      const index = state.candidates.findIndex(item => !Object.hasOwn(types, item.fact_type) || !item.value.trim());
      if (index >= 0) {
        msg('请核对第 ' + (index + 1) + ' 条事实，选择类型并填写内容，或删除该条。', true);
        const card = $('fact-item-' + index);
        const field = card.querySelector(Object.hasOwn(types, state.candidates[index].fact_type) ? 'textarea' : 'select');
        field.focus();
        return;
      }
    }
    openStep(state.step + 1, true);
  }
  $('next').addEventListener('click', moveForward);
  $('previous').addEventListener('click', () => openStep(
    state.step === 3 && !state.id && !$('material').value.trim() ? 1 : state.step - 1, true));
  $('toggle-steps').addEventListener('click', () => {
    const button = $('toggle-steps');
    button.setAttribute('aria-expanded', String(button.getAttribute('aria-expanded') !== 'true'));
  });
  function closeMenu() { $('toggle-steps').setAttribute('aria-expanded', 'false'); }
  document.addEventListener('click', event => {
    if (!event.target.closest('.workflow-rail')) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('toggle-steps').getAttribute('aria-expanded') === 'true') {
      closeMenu();
      $('toggle-steps').focus();
    }
  });
  $('open-context').addEventListener('click', () => {
    $('context-dialog-body').append($('context-content'));
    $('context-dialog').showModal();
  });
  $('close-context').addEventListener('click', () => $('context-dialog').close());
  $('context-dialog').addEventListener('close', () => $('info-rail').append($('context-content')));
  document.querySelectorAll('#steps [data-step]').forEach(button => button.addEventListener('click', () => openStep(Number(button.dataset.step), true)));
  document.querySelectorAll('[data-kind]').forEach(button => button.addEventListener('click', () => {
    if (state.editing || state.busy) return;
    state.kind = button.dataset.kind;
    syncResultLink();
    updateKind();
    renderResults();
    document.querySelector('.stage-body').scrollTop = 0;
  }));
  function canLeaveWork() {
    const hasDraft = !state.id && ($('jd').value.trim() || $('material').value.trim() || Object.values(header('header')).some(Boolean));
    if (state.editing || hasDraft) return confirm('离开后，未保存的输入或编辑将丢失。继续吗？');
    return true;
  }
  function resetTask() {
    clearTimeout(state.timer);
    Object.assign(state, {id: null, batch: null, kind: 'job', key: null, candidates: [],
      extractedText: null, selectedMaterialId: null, materialRevisionId: null, sourceType: 'manual',
      editing: false, deletePreview: null, guideExplicit: false, guideChoices: {job:null, blueprint:null}});
    localStorage.removeItem('jobcoach-v2-current-id');
    for (const id of ['jd','material','material-file','header-name','header-phone','header-email','header-location','header-education']) $(id).value = '';
    for (const id of ['resume-locale','guide-locale']) $(id).value = 'zh-CN';
    for (const id of ['profile-options', 'guide-options']) $(id).open = false;
    $('upload-note').textContent = '支持 Word、文本 PDF 和 TXT。';
    for (const id of ['editor','generation-status','library-status','package-status']) $(id).replaceChildren();
    cancelEdit();
    openStep(0, true);
    lock(false);
  }
  $('new-task').addEventListener('click', () => {
    if (canLeaveWork()) resetTask();
  });

  async function loadMaterials(selectedId=null){
    const status=$('library-status');status.textContent='正在读取素材库…';
    try{const result=await request('/api/v2/materials');if(!Array.isArray(result?.materials))throw new Error('素材列表格式无效。');state.libraryItems=result.materials.filter(item=>safeId(item.material_id) && safeId(item.revision_id));const select=$('library-select');select.replaceChildren();const prompt=el('option','请选择素材');prompt.value='';select.append(prompt);for(const item of state.libraryItems){const option=el('option',`素材 ${item.material_id.slice(0,8)} · 修订 ${item.revision} · ${formatTime(item.created_at)}`);option.value=item.material_id;select.append(option);}if(selectedId && state.libraryItems.some(item=>item.material_id===selectedId))select.value=selectedId;libraryActions();void previewLibrarySelection();status.textContent=state.libraryItems.length?`已有 ${state.libraryItems.length} 份素材。`:'素材库为空；可保存当前文字。';}
    catch(e){status.textContent=`读取素材库失败：${e.message}`;}
  }
  function selectedLibraryItem(){return state.libraryItems.find(item=>item.material_id===$('library-select').value);}
  async function previewLibrarySelection() {
    const item = selectedLibraryItem();
    const preview = $('library-preview');
    preview.hidden = !item;
    if (!item) return;
    preview.textContent = '正在读取素材…';
    try {
      const data = await request('/api/v2/materials/' + encodeURIComponent(item.material_id)
        + '/revisions/' + encodeURIComponent(item.revision_id));
      if (selectedLibraryItem()?.revision_id !== item.revision_id) return;
      preview.textContent = data.extracted_text;
    } catch (error) {
      if (selectedLibraryItem()?.revision_id === item.revision_id) preview.textContent = error.message;
    }
  }
  async function loadSelectedMaterial(){
    const item=selectedLibraryItem();if(!item){$('library-status').textContent='请先选择素材。';return;}
    $('library-status').textContent='正在读取所选修订…';
    try{const data=await request(`/api/v2/materials/${encodeURIComponent(item.material_id)}/revisions/${encodeURIComponent(item.revision_id)}`);if(data.material_id!==item.material_id || data.revision_id!==item.revision_id || typeof data.extracted_text!=='string')throw new Error('素材修订不匹配。');if($('material').value.trim() && $('material').value!==data.extracted_text && !confirm('用所选素材替换当前输入的文字？'))return;$('material').value=data.extracted_text;state.selectedMaterialId=data.material_id;state.materialRevisionId=data.revision_id;state.sourceType=data.source_type;state.extractedText=null;state.candidates=[];invalidatesRequest();$('library-status').textContent=`已读取修订 ${data.revision}；请重新提取并核对事实。`;$('library-dialog').close();msg('已导入所选素材，继续即可核对事实。');updateContext();}
    catch(e){$('library-status').textContent=`读取素材失败：${e.message}`;}
  }
  async function saveLibraryMaterial(asRevision){
    const text=$('material').value.replace(/\r\n?/g,'\n'), item=selectedLibraryItem();
    if(!text.trim()){$('library-status').textContent='请先填写或上传素材文字。';return;}
    if(asRevision&&!item){$('library-status').textContent='请先选择要追加修订的素材。';return;}
    const url=asRevision?`/api/v2/materials/${encodeURIComponent(item.material_id)}/revisions`:'/api/v2/materials';
    $('library-save-new').disabled=$('library-save-revision').disabled=true;$('library-status').textContent='正在保存素材文字…';
    try{const result=await request(url,json('POST',{extracted_text:text,source_type:state.sourceType}));if(!safeId(result?.material_id)||!safeId(result?.revision_id)||(asRevision&&result.material_id!==item.material_id))throw new Error('保存结果无效。');$('material').value=text;state.selectedMaterialId=result.material_id;state.materialRevisionId=result.revision_id;state.extractedText=null;state.candidates=[];invalidatesRequest();await loadMaterials(result.material_id);$('library-status').textContent=`已保存素材修订 ${result.revision}；请重新提取并核对事实。`;updateContext();}
    catch(e){$('library-status').textContent=`保存素材失败：${e.message}`;}
    finally{libraryActions();}
  }
  function clearMaterialPreview(message='请选择方式后预览影响。'){state.deletePreview=null;$('material-delete-confirm').disabled=true;$('material-delete-impact').replaceChildren(el('p',message,'field-note'));}
  function openMaterialDelete(){const item=selectedLibraryItem();if(!item){$('library-status').textContent='请先选择素材。';return;}state.selectedDeleteMaterial=item.material_id;$('material-delete-mode').value='preserve_existing';clearMaterialPreview();$('material-delete-dialog').showModal();}
  async function previewMaterialDelete(){
    const id=state.selectedDeleteMaterial,mode=$('material-delete-mode').value;if(!safeId(id))return;clearMaterialPreview('正在检查影响…');$('material-delete-preview').disabled=true;
    try{const data=await request(`/api/v2/materials/${encodeURIComponent(id)}/deletion-preview?${new URLSearchParams({mode})}`);if(data?.material_id!==id||data?.mode!==mode||!Array.isArray(data.affected_batch_ids)||typeof data.impact_token!=='string'||!data.impact_token)throw new Error('影响预览格式无效。');if($('material-delete-mode').value!==mode || state.selectedDeleteMaterial!==id)throw new Error('选择已变化，请重新预览。');state.deletePreview={id,mode,token:data.impact_token,affectedBatchIds:[...data.affected_batch_ids]};const impact=$('material-delete-impact');impact.replaceChildren();impact.append(el('p',`素材修订：${data.revision_count} 个；受影响批次：${data.affected_batch_ids.length} 个。`));impact.append(el('p',mode==='cascade'?'确认后这些批次及其资料将被级联删除。':'确认后不再允许复用该素材；这些批次保留各自冻结副本。'));if(data.affected_batch_ids.length){const list=el('ul');for(const batchId of data.affected_batch_ids)list.append(el('li',String(batchId)));impact.append(list);}$('material-delete-confirm').disabled=false;}
    catch(e){clearMaterialPreview(`预览失败：${e.message}`);}finally{$('material-delete-preview').disabled=false;}
  }
  async function confirmMaterialDelete(){
    const preview=state.deletePreview;if(!preview || preview.id!==state.selectedDeleteMaterial || preview.mode!==$('material-delete-mode').value)return;
    $('material-delete-confirm').disabled=true;$('material-delete-preview').disabled=true;$('material-delete-mode').disabled=true;
    try{await request(`/api/v2/materials/${encodeURIComponent(preview.id)}`,json('DELETE',{mode:preview.mode,impact_token:preview.token}));$('material-delete-dialog').close();if(state.selectedMaterialId===preview.id){state.selectedMaterialId=null;state.materialRevisionId=null;}state.deletePreview=null;await loadMaterials();$('library-status').textContent=preview.mode==='cascade'?'素材和预览中的受影响批次已删除。':'素材已停止复用，旧批次冻结副本保留。';if(state.id && preview.mode==='cascade' && preview.affectedBatchIds.includes(state.id)){resetTask();msg('当前批次已随素材删除。');}}
    catch(e){clearMaterialPreview(`删除未完成：${e.message}。请重新预览影响后再试。`);}finally{$('material-delete-preview').disabled=false;$('material-delete-mode').disabled=false;}
  }
  function libraryActions() {
    const selected = !!selectedLibraryItem();
    $('library-load').disabled = !selected || !!state.id;
    $('library-save-new').disabled = !!state.id || !$('material').value.trim();
    $('library-save-revision').disabled = !selected || $('library-save-new').disabled;
    $('library-delete').disabled = !selected;
  }
  $('open-library').addEventListener('click', async () => {
    $('library-dialog').showModal();
    await loadMaterials(state.selectedMaterialId);
    libraryActions();
  });
  $('close-library').addEventListener('click', () => $('library-dialog').close());
  $('library-select').addEventListener('change', () => { libraryActions(); void previewLibrarySelection(); });
  $('library-load').addEventListener('click',loadSelectedMaterial);$('library-save-new').addEventListener('click',()=>saveLibraryMaterial(false));$('library-save-revision').addEventListener('click',()=>saveLibraryMaterial(true));$('library-delete').addEventListener('click',openMaterialDelete);
  $('material-delete-close').addEventListener('click',()=>$('material-delete-dialog').close());$('material-delete-cancel').addEventListener('click',()=>$('material-delete-dialog').close());$('material-delete-mode').addEventListener('change',()=>clearMaterialPreview());$('material-delete-preview').addEventListener('click',previewMaterialDelete);$('material-delete-confirm').addEventListener('click',confirmMaterialDelete);

  function formatTime(value) {
    if (!value) return '时间未知';
    const date = new Date(/[zZ]|[+-]\d{2}:\d{2}$/.test(value) ? value : value + 'Z');
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', {hour12:false});
  }
  async function showHistory(){
    const dialog=$('history-dialog');$('history-kind').value=state.kind;$('history-open').disabled=true;dialog.showModal();$('history-note').textContent='正在读取历史记录…';$('history-list').replaceChildren();
    try{const rows=await request('/api/v2/batches');if(!Array.isArray(rows))throw new Error('历史记录格式无效。');const select=$('history-batch');select.replaceChildren();for(const row of rows){if(!safeId(row.id))continue;const option=el('option',`${row.job_label || '生成记录'} · ${formatTime(row.created_at)} · ${statusText(row.status)}`);option.value=row.id;select.append(option);}if(state.id && [...select.options].some(o=>o.value===state.id))select.value=state.id;$('history-open').disabled=!select.value;if(select.value)await loadHistory();else $('history-note').textContent='还没有已保存的记录。';}
    catch(e){$('history-note').textContent=`历史记录读取失败：${e.message}`;$('history-open').disabled=true;}
  }
  async function loadHistory(){
    const id=$('history-batch').value, kind=$('history-kind').value;if(!safeId(id))return;$('history-note').textContent='正在读取修订…';const list=$('history-list');list.replaceChildren();
    try{const result=await request(`/api/v2/batches/${encodeURIComponent(id)}/history/${kind}`);if(id!==$('history-batch').value || kind!==$('history-kind').value)return;$('history-note').textContent=`${kinds[kind]}历史；下载明确绑定所选历史修订，不会切换当前稿。`;
      for(const category of ['resume','guide']){const title=el('h3',category==='resume'?'简历修订':'指南修订');list.append(title);const rows=category==='resume'?result.resumes:result.guides;if(!Array.isArray(rows)||!rows.length){list.append(el('p','暂无记录。','field-note'));continue;}for(const row of rows){if(!Number.isInteger(row.revision)||row.revision<1)continue;const card=el('div',null,'v2-card');const binding=category==='guide'?` · 原始绑定简历修订 ${row.resume_revision} · ${statusText(row.status)} · ${row.locale==='en'?'英文':'中文'}`:'';card.append(el('p',`修订 ${row.revision} · ${formatTime(row.created_at)}${binding}`));const buttons=el('div',null,'v2-downloads');for(const format of ['html','docx','pdf']){const button=el('button',format.toUpperCase(),'secondary');button.type='button';button.disabled=category==='guide'&&row.status!=='ready';button.addEventListener('click',()=>download(category,format,{id,kind,revision:row.revision}));buttons.append(button);}card.append(buttons);list.append(card);}}
    }catch(e){$('history-note').textContent=`历史修订读取失败：${e.message}`;}
  }
  $('open-history').addEventListener('click',showHistory);$('close-history').addEventListener('click',()=>$('history-dialog').close());$('history-batch').addEventListener('change',loadHistory);$('history-kind').addEventListener('change',loadHistory);$('history-open').addEventListener('click',async()=>{const id=$('history-batch').value;if(!safeId(id) || !canLeaveWork())return;clearTimeout(state.timer);cancelEdit();state.kind=$('history-kind').value;state.id=id;state.batch=null;state.editing=false;state.guideChoices={job:null,blueprint:null};localStorage.setItem('jobcoach-v2-current-id',id);$('history-dialog').close();openStep(4,true);await refresh();});
  request('/api/config').then(config=>{runtimeConfig=config;updateContext();}).catch(()=>{updateContext();});
  function followResultLink(link) {
    clearTimeout(state.timer);
    if (state.editing) cancelEdit();
    Object.assign(state, {id:link.id, kind:link.kind, batch:null, editing:false, guideChoices:{job:null,blueprint:null}});
    openStep(link.step);
    refresh().then(()=>{if(state.batch?.id===link.id) localStorage.setItem('jobcoach-v2-current-id',link.id);});
  }
  window.addEventListener('hashchange', () => {
    const link=readResultLink();
    if (link && !state.busy && canLeaveWork()) followResultLink(link);
    else syncResultLink();
  });
  const link=readResultLink(), stored=localStorage.getItem('jobcoach-v2-current-id');
  if(link) followResultLink(link);
  else if(safeId(stored)){state.id=stored;openStep(4);refresh();}else openStep(0);
})();
