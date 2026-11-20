/* Run the real React browser harness against simulated network and device state. */
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {transform} = require('sucrase');
const root = path.resolve(__dirname,'..');
const context = vm.createContext({window:{},React});
for (const name of ['app/src/components/Icon.jsx','app/src/utils/stateMachine.js','app/src/hooks/useTraining.js','app/src/pages/TransitionInspector.jsx','app/src/components/TrainingPreview.jsx']) {
  const source = fs.readFileSync(path.join(root,name),'utf8');
  vm.runInContext(transform(source,{transforms:['jsx'],jsxRuntime:'classic',production:true}).code,context);
}
const inspect = (transition,records) => renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{transition,projection:records}));
const reading = {teaching_id:'reading',desired_status:'enabled',name:'阅读灯光',load_status:'awaiting_load',
  stepSummaries:[{step_index:0,label:'调色'}],definition:{situation:'准备看书时',steps:[{transition_ids:['color'],defaults:{r:255,g:236,b:210,brightness:200}}]}};
const colorTransition = {action:'SET_COLOR',label:'调色',trigger:'CMD_COLOR'};
const light = inspect(colorTransition,[reading]);
assert.match(light,/准备看书时/);
assert.match(light,/调色/);
assert.match(light,/>255</);
assert.match(light,/brightness/);
assert.match(light,/>200</);
assert.match(light,/training-rule-parameters/);
assert.doesNotMatch(light,/training-rule-group__caption|设备原有规则/);
assert.doesNotMatch(light,/常亮 → 常亮/);
assert.doesNotMatch(light,/已保存|已加载|已应用|等待加载|awaiting_load/);
assert.doesNotMatch(inspect(colorTransition,[{...reading,desired_status:'removed'}]),/准备看书时/);
assert.doesNotMatch(inspect(colorTransition,[]),/动作参数|training-inspector__rule/);
const hello = inspect({action:'Hello',label:'打招呼'},[{teaching_id:'hello',desired_status:'enabled',name:'看到人就打招呼',
  stepSummaries:[{step_index:0,label:'打招呼'}],definition:{steps:[{transition_ids:['hello'],defaults:{}}]}}]);
assert.match(hello,/打招呼/);
assert.doesNotMatch(hello,/动作参数/);
const blink = inspect({action:'BLINK',label:'闪烁'},[{teaching_id:'sound',desired_status:'enabled',name:'声音提醒',
  definition:{steps:[{transition_ids:['blink'],defaults:{count:2}}],cooldown_s:20}}]);
assert.match(blink,/>2</);
assert.match(blink,/>20</);
assert.match(blink,/冷却时间/);
assert.doesNotMatch(blink,/持续闪烁/);
console.log('PASS: teaching lines show situation, action and exact saved parameters without load/application labels.');
const catalog = {hash:'current',events:[{id:'vision.owner_recognized',name:'识别到主人'}],
  transitions:[{id:'sit',src:'standing',dst:'sitting',source_label:'站立',destination_label:'坐下',state_field:'body_state'}],
  state_fields:{body_state:{label:'身体',labels:{standing:'站立',sitting:'坐下'}}}};
const old = {teaching_id:'owner-sit',revision:1,name:'主人出现就坐下',desired_status:'enabled',capability_hash:'current',
  definition:{trigger:{event_id:'vision.owner_recognized',where:{recognized:true}},steps:[{transition_ids:['sit'],conditions:[{field:'body_state',op:'eq',value:'standing'}],defaults:{},goal_state:'sitting'}]}};
const current = {...old,revision:2,name:'主人出现时坐好',history:[old]};
const before = context.window.trainingComparisonRecords([current],'owner-sit',true);
assert.equal(before[0].revision,1,'Comparison must use the exact saved previous version');
assert.equal(context.window.trainingComparisonRecords([old],'owner-sit',true).length,0,'First teaching has no previous association');
assert.equal(context.window.trainingComparisonRecords([current],'owner-sit',false)[0],current);
const projected = context.window.trainingRecordProjection(old,catalog);
assert.equal(projected.stepSummaries[0].conditionText,'身体为站立');
assert.equal(projected.stepSummaries[0].goalText,'已经坐下时跳过');
assert.equal(context.window.trainingAssociationLabel(old,catalog),'识别到主人');
assert.equal(context.window.trainingNeedsConfirmation({...old,capability_hash:'old'},catalog),true);
const baseEvent = {type:'agent_progress',result_kind:'training',agent_id:'go2',teaching_id:'owner-sit',revision:2,version:2,
  run_id:'run-1',step_id:'run-1:0',step_index:0,transition_id:'sit',device_epoch:'connection-a',ts:100,
  sequence:0,fact_kind:'observation',observation_id:'vision.owner_recognized',observation_event_id:'appearance-1',as_of:99};
let activity = context.window.trainingActivityEvent(undefined,baseEvent,'go2',100);
assert.equal(activity.observation.observation_event_id,'appearance-1');
activity = context.window.trainingActivityEvent(activity,{...baseEvent,sequence:1,fact_kind:'call',stage:'executing'},'go2',100);
assert.equal(activity.call.stage,'executing');
assert.equal(context.window.trainingActivityEvent(activity,baseEvent,'go2',100),activity,'Late observation must not rewind execution');
assert.equal(context.window.trainingActivityEvent(activity,{...baseEvent,version:1},'go2',100),activity,'Conflicting rule versions must be rejected');
assert.equal(context.window.trainingActivityEvent(activity,{...baseEvent,agent_id:'lamp'},'go2',100),activity);
assert.equal(context.window.trainingActivityEvent(undefined,{...baseEvent,as_of:90},'go2',100),undefined,'Stale camera facts cannot light up a condition');
assert.equal(context.window.trainingActivityFor(current,{'owner-sit':activity},'connection-b',100),null,'Reconnection retires old execution highlights');
assert.equal(context.window.trainingActivityFor(old,{'owner-sit':activity},'connection-a',100),null,'Old rule versions cannot inherit execution');
assert.equal(context.window.trainingActivityFor(current,{'owner-sit':{...activity,transition_id:'other'}},'connection-a',100),null);
const completed = context.window.trainingActivityEvent(activity,{...baseEvent,sequence:2,fact_kind:'result',status:'ok',public_result:{result:'ok'}},'go2',100);
assert.equal(completed.observation.observation_event_id,'appearance-1','A receipt preserves its own observed cause');
assert.equal(context.window.trainingActivityFor(current,{'owner-sit':completed},'connection-a',113),null,'Result highlights expire');
const receiptOnly = context.window.trainingActivityEvent(undefined,{...baseEvent,sequence:2,fact_kind:'result',status:'ok'},'go2',100);
assert.equal(receiptOnly.observation,undefined,'A successful receipt must never invent an observation');
const newer = context.window.trainingActivityEvent(completed,{...baseEvent,run_id:'run-2',step_id:'run-2:0',
  ts:102,as_of:101,observation_event_id:'appearance-2',sequence:1,fact_kind:'call'},'go2',102);
assert.equal(newer.run_id,'run-2');
assert.equal(context.window.trainingActivityEvent(newer,{...baseEvent,ts:103,sequence:2,fact_kind:'result'},'go2',103),newer,
  'Late completion from a retired run must not take over a newer appearance');
assert.equal(context.window.trainingActivityEvent(newer,{...baseEvent,run_id:'unseen-old-run',step_id:'old:0',ts:103,
  as_of:99,sequence:2,fact_kind:'result'},'go2',103),newer,'Source observation time orders different appearances');
const proposalMarkup = renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{proposal:{
  proposal_id:'p1',operation:'update',summaryText:'主人出现时坐好',beforeSummaryText:'主人出现就坐下',
  before:old,after:current,stepSummaries:projected.stepSummaries}}));
assert.doesNotMatch(proposalMarkup,/修改前|修改后|主人出现就坐下|规则草案/);
assert.match(proposalMarkup,/保存规则/);
const blinkProposal = renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{proposal:{
  operation:'update',summaryText:'开心时闪烁',before:{name:'开心时闪烁',definition:{steps:[{defaults:{count:3}}]}},
  after:{name:'开心时闪烁',definition:{steps:[{defaults:{count:5}}]}}}}));
assert.match(blinkProposal,/>3</,'Same-title parameter edits must show the old count');
assert.match(blinkProposal,/>5</,'Same-title parameter edits must show the new count');
assert.match(blinkProposal,/training-rule-changes/);
const createProposal = renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{proposal:{
  operation:'create',summaryText:'模型生成的标题',changeText:'当我自己觉得开心时，不论原本是什么状态都闪烁',
  before:{name:'先前未保存的版本',definition:{steps:[{defaults:{count:2}}]}},
  after:{definition:{emotion:'happy',steps:[{defaults:{count:3}}]}}}}));
assert.doesNotMatch(createProposal,/修改前|修改后|规则草案|未添加|模型生成|当我自己|不论原本|training-rule-changes/);
assert.match(createProposal,/情绪表达（开心）/);
const combined=renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{transitions:[
  {src:'BLINKING',dst:'OFF',trigger:'CMD_OFF',action:'SET_STATE',label:'关灯',event_label:'请求关灯'},
  {src:'BLINKING',dst:'OFF',trigger:'BLINK_DONE',label:'闪烁结束',event_label:'有限闪烁完成',guard_label:'闪烁前为熄灭'}
]}));
assert.equal((combined.match(/aria-label="触发事件"/g) || []).length,1);
assert.match(combined,/请求关灯/);assert.match(combined,/有限闪烁完成/);assert.match(combined,/闪烁前为熄灭/);
assert.doesNotMatch(combined,/rule-or|设备原有/);
const selfTransition = inspect({src:'BLINKING',dst:'BLINKING',action:'BLINK',label:'闪烁'},[]);
assert.match(selfTransition,/不会沿箭头自动重复/);
assert.doesNotMatch(inspect({src:'OFF',dst:'BLINKING',action:'BLINK',label:'闪烁'},[]),/自循环表示/);
console.log('PASS: exact-version and parameter comparison, self-transition explanation, execution identity and stale observation rejection.');
if(process.argv.includes('--presentation-only')) process.exit(0);
const args = [path.join(__dirname,'check_go2_frontend.cjs'), ...process.argv.slice(2), '--training-only'];
const result = spawnSync(process.execPath,args,{stdio:'inherit',env:process.env});
process.exit(result.status ?? 1);
