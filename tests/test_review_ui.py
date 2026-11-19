"""Execute the UI's review presentation logic without a browser or real data."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_ui_assertions(assertions: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for UI logic checks")
    script = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const context = {};
vm.runInNewContext(source.slice(source.indexOf('function describeStatus('), source.indexOf('function hasResumes(')), context);
const {contentReviewView: view, currentReviewNotice: notice, describeStatus} = context;
""" + assertions
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_offline_or_unknown_provider_cannot_claim_model_review_success():
    run_ui_assertions("""
for (const source of ['local', 'degraded_local', 'unexpected']) {
  for (const status of ['passed', 'repaired', 'suggestions']) {
    const result = view({status, source, repair_count: 0, issues: []});
    assert.equal(result.status, 'unavailable');
    assert.equal(result.badges.some(badge => badge[1] === 'ok'), false);
    assert.ok(notice({content_review: {status, source}}));
  }
}
assert.equal(view({status:'passed', source:'compatible'}).status, 'passed');
assert.equal(view({status:'unavailable', source:'compatible', reason:'invalid_review'}).status, 'unavailable');
""")


def test_repaired_report_keeps_suggestions_locations_and_literal_quotes():
    run_ui_assertions("""
const report = {status:'repaired', source:'compatible', repair_count:2, issues:[{
  severity:'suggestion', diagnosis:'可补充检查方法', instruction:'自行决定是否展开',
  targets:[{scope:'job', target_id:'item-b', field:'text', quote:'<img src=x onerror=alert(1)>'}],
  jd_quote:'输出装配检查记录'
}]};
const before = JSON.stringify(report);
const result = view(report, {items:[{id:'item-a'}, {id:'item-b'}]});
assert.equal(result.status, 'repaired');
assert.ok(result.text.includes('2 处定向修复；1 条表达建议'));
assert.ok(result.text.includes('求职版 · 要点 2 · 要点正文'));
assert.ok(result.text.includes('可选建议：自行决定是否展开'));
assert.ok(result.text.includes('岗位依据：输出装配检查记录'));
assert.ok(result.text.includes('<img src=x onerror=alert(1)>'));
assert.equal(JSON.stringify(report), before);
""")


def test_stale_and_old_batches_do_not_get_current_success_conclusions():
    run_ui_assertions("""
assert.equal(view(undefined), null);
assert.equal(view({}), null);
assert.equal(notice({}), '');
const report = {status:'stale', source:'compatible', repair_count:1, issues:[]};
const result = view(report);
assert.equal(result.status, 'stale');
assert.equal(result.badges.some(badge=>badge[1]==='ok'), false);
assert.ok(result.text.includes('不能视为当前版已通过'));
assert.ok(result.text.includes('生成时的记录：1 处定向修复'));
assert.ok(notice({content_review:report}).includes('已过期'));
assert.equal(notice({content_review:{status:'passed', source:'compatible'}}), '');
""")


def test_blocked_generation_explains_retained_input_and_retry():
    run_ui_assertions("""
const report = {status:'blocked', source:'compatible', reason:'repair_rejected', issues:[{
  severity:'error', diagnosis:'方案与正文的结果不一致', instruction:'统一结果范围',
  targets:[{scope:'plan', target_id:'plan-a', field:'outcome', quote:'未完成的测试'}]
}]};
const result = view(report, {plans:[{key:'plan-a'}]});
assert.equal(result.status, 'blocked');
assert.ok(result.text.includes('已拒绝采用'));
assert.ok(result.text.includes('项目方案 · 方案 1 · 成果或预期产物'));
assert.ok(result.text.includes('修改方向：统一结果范围'));
const [message, tone] = describeStatus({status:'failed', error_code:'CONTENT_REVIEW_FAILED'});
assert.equal(tone, 'error');
assert.ok(message.includes('未交付简历'));
assert.ok(message.includes('重试') && message.includes('输入仍然保留'));
assert.ok(describeStatus({status:'running'})[0].includes('内容评审'));
""")


def test_sidebar_switches_versions_and_keeps_failed_batch_diagnostics_visible():
    run_ui_assertions("""
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {value:'', checked:false, append(){}, replaceChildren(){}});
  return nodes.get(selector);
};
Object.assign(context, {
  $:node, textElement:()=>({append(){}}), refreshReaders(){},
  jdInput:{value:'虚构岗位职责与任职要求'}, materialInput:{value:''},
  currentStep:4, currentKind:'job', collectProfile:()=>({}), linkedItem:()=>null,
  hasResumes:()=>Boolean(context.currentBatch?.resumes?.[context.currentKind]),
  inputsMatchBatch:()=>true,
  setOptions(select, rows, preferred) {
    select.rows=rows;
    if(preferred!==undefined || !rows.some(row=>row.value===select.value)) select.value=preferred??rows[0]?.value;
  },
  makeReader(element,title,text,badges) {Object.assign(element,{title,text,badges});}
});
vm.runInNewContext(source.slice(source.indexOf("let supportContext='';"), source.indexOf('let columnUpdate;')), context);
context.currentBatch={id:'fake-batch',jd_text:'虚构岗位',guides:{},resumes:{
  job:{revision:1,items:[],content_review:{status:'suggestions',source:'compatible',issues:[{severity:'suggestion',diagnosis:'求职版专属建议'}]}},
  blueprint:{revision:2,items:[],content_review:{status:'stale',source:'compatible',issues:[{severity:'suggestion',diagnosis:'蓝图历史建议'}]}}
}};
context.renderColumns();
assert.ok(node('#support-reader').text.includes('求职版专属建议'));
context.currentKind='blueprint';context.renderColumns();
assert.ok(node('#support-reader').text.includes('蓝图历史建议'));
assert.equal(node('#support-reader').text.includes('求职版专属建议'), false);
assert.ok(node('#support-reader').title.includes('报告已过期'));
assert.equal(node('#support-action').hidden, true);
delete context.currentBatch.resumes.blueprint.content_review;context.renderColumns();
assert.equal(node('#support-topic').rows.some(row=>row.value==='content-review'), false);
context.currentStep=3;
context.currentBatch={id:'failed-batch',resumes:{},guides:{},quality:{content_review:{status:'blocked',source:'compatible',reason:'unresolved_issues'}}};
context.renderColumns();
assert.ok(node('#support-reader').title.includes('未通过审稿'));
assert.ok(node('#support-reader').text.includes('重试'));
""")


def test_render_preserves_failed_review_message_after_switching_from_blueprint():
    run_ui_assertions("""
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {open:false, value:''});
  return nodes.get(selector);
};
Object.assign(context, {
  $:node, currentBatch:{id:'failed-batch'}, currentKind:'blueprint', currentStep:4,
  historyEl:{value:''}, retryBatchEl:{hidden:true}, inputsMatchBatch:()=>true,
  clearTimeout(){}, setTimeout(){}, pollTimer:null, requestEpoch:0,
  setStatus(message,tone){context.displayedStatus={message,tone};},
  showStep(step){context.currentStep=context.currentBatch.resumes?.blueprint?step:Math.min(step,3);}
});
vm.runInNewContext(source.slice(source.indexOf('function render(batch)'), source.indexOf('async function loadBatch(')), context);
const failed={id:'failed-batch',status:'failed',error_code:'CONTENT_REVIEW_FAILED',resumes:{},guides:{}};
context.render(failed);
assert.equal(context.displayedStatus.tone,'error');
assert.ok(context.displayedStatus.message.includes('未交付简历'));
assert.ok(context.displayedStatus.message.includes('重试'));
assert.equal(context.displayedStatus.message.includes('蓝图 Plus 含'),false);
assert.equal(context.currentStep,3);
assert.equal(context.retryBatchEl.hidden,false);
// Failure also wins if an unexpected stale resume is present in a response.
context.currentStep=4;
context.render({...failed,resumes:{blueprint:{items:[]}}});
assert.ok(context.displayedStatus.message.includes('未交付简历'));
context.currentStep=4;
context.render({...failed,status:'ready',resumes:{blueprint:{items:[]}}});
assert.ok(context.displayedStatus.message.includes('蓝图 Plus 含'));
context.currentStep=4;
context.render({...failed,status:'running'});
assert.equal(context.displayedStatus.message.includes('蓝图 Plus 含'),false);
""")
