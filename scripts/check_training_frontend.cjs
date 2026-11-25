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
const context = vm.createContext({window:{},React,getAgentDisplayName:id=>id==='go2' ? 'Go2' : id});
for (const name of ['app/src/utils/inputAttributes.js','app/src/components/Icon.jsx','app/src/utils/stateMachine.js','app/src/hooks/useTraining.js','app/src/pages/TransitionInspector.jsx','app/src/components/TrainingPreview.jsx']) {
  const source = fs.readFileSync(path.join(root,name),'utf8');
  vm.runInContext(transform(source,{transforms:['jsx'],jsxRuntime:'classic',production:true}).code,context);
  Object.assign(context, context.window);
}
const inspect = (transition,records,catalog) => renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{transition,projection:records,catalog}));
const nativeSentence = '机器狗已站稳、没有待确认动作时，管理员可开启自由探索。';
const explainedEvent = inspect({action:'LIFE',label:'开启探索',trigger_condition:nativeSentence,
  event_label:'旧事件说明'}, [], {});
assert.ok(explainedEvent.includes(nativeSentence), 'A complete event sentence must be shown as declared by the Card');
assert.doesNotMatch(explainedEvent,/旧事件说明|时，开启探索/, 'The complete explanation must not be combined with another description or action label');
assert.match(inspect({action:'Sit',label:'坐下',trigger_condition:'收到坐下指令'}, [], {}),
  /收到坐下指令<\/span>/, 'Existing condition fragments remain intact without an invented action suffix');
assert.match(inspect({trigger:'done',event_label:'有限闪烁完成',guard_label:'闪烁前为熄灭'}, [], {}),
  /闪烁前为熄灭/, 'A separately declared event guard must remain visible');
const lampCatalog={hash:'lamp-current',events:[],limits:{cooldown_s:{minimum:1,maximum:3600}},
  presentation:{actions:{SET_COLOR:'调色',BLINK:'闪烁'},parameters:{SET_COLOR:{r:'红色',g:'绿色',b:'蓝色',brightness:'亮度'},BLINK:{count:'次数'}}},
  transitions:[{id:'color',action:'SET_COLOR',src:'ON',dst:'ON',source_label:'常亮',destination_label:'常亮',fixed_params:[],
    params_schema:{properties:{r:{type:'integer',minimum:0,maximum:255},g:{type:'integer',minimum:0,maximum:255},b:{type:'integer',minimum:0,maximum:255},brightness:{type:'integer',minimum:0,maximum:255}}}},
    {id:'blink',action:'BLINK',src:'OFF',dst:'BLINKING',source_label:'熄灭',destination_label:'闪烁',fixed_params:[],
      params_schema:{properties:{count:{type:'integer',minimum:0,maximum:20},
        r:{type:'integer',minimum:0,maximum:255},g:{type:'integer',minimum:0,maximum:255},b:{type:'integer',minimum:0,maximum:255}}}}]};
const reading = {teaching_id:'reading',desired_status:'enabled',name:'阅读灯光',load_status:'awaiting_load',
  definition:{situation:'准备看书时',steps:[{transition_ids:['color'],defaults:{r:255,g:236,b:210,brightness:200}}]}};
const colorTransition = {action:'SET_COLOR',label:'调色',trigger:'CMD_COLOR'};
const light = inspect(colorTransition,[reading],lampCatalog);
assert.match(light,/准备看书/);
assert.match(light,/调色/);
for(const value of ['255','236','210','200'])assert.ok(light.includes(value),'Non-default color parameters must remain visible');
assert.doesNotMatch(light,/设备原有规则|已保存|已加载|已应用|awaiting_load/);
assert.doesNotMatch(inspect(colorTransition,[{...reading,desired_status:'removed'}],lampCatalog),/准备看书/);
assert.doesNotMatch(inspect(colorTransition,[{...reading,desired_status:'removed'}],lampCatalog),/training-custom-events|还没有自定义事件/);
const disabled=inspect(colorTransition,[{...reading,desired_status:'disabled'}],lampCatalog);
assert.match(disabled,/准备看书/,'Disabled custom events remain available for management');
assert.match(disabled,/未启用/);
assert.match(disabled,/training-custom-events/);
const happyBlink={teaching_id:'sound',desired_status:'enabled',definition:{emotion:'happy',steps:[{transition_ids:['blink'],defaults:{count:3}}],cooldown_s:20}};
const blink=inspect({action:'BLINK',label:'闪烁'},[happyBlink],lampCatalog);
assert.match(blink,/开心/);
assert.match(blink,/闪烁/);
assert.match(blink,/3/);
assert.match(blink,/20/,'Non-default cooldown remains visible with the sentence');
console.log('PASS: sentence rules retain non-default action parameters and inactive custom events.');
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
assert.equal(context.window.trainingAssociationLabel(old,catalog),'看到（主人）');
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
  before:old,after:current,stepSummaries:projected.stepSummaries},catalog}));
assert.doesNotMatch(proposalMarkup,/修改前|修改后|主人出现就坐下|规则草案/);
assert.match(proposalMarkup,/确认并生效/);
assert.match(proposalMarkup,/先不添加/);
assert.match(proposalMarkup,/待确认/);
assert.match(proposalMarkup,/站立/);
assert.match(proposalMarkup,/看到主人时/);
assert.match(proposalMarkup,/切换到坐下/);
assert.doesNotMatch(proposalMarkup,/training-event-details|training-event-chain__event/);
assert.doesNotMatch(proposalMarkup,/识别对象|recognized|冷却时间|修改参数/);
const blinkProposal = renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{proposal:{
  operation:'update',summaryText:'开心时闪烁',before:{name:'开心时闪烁',definition:{steps:[{defaults:{count:3}}]}},
  after:{name:'开心时闪烁',definition:{steps:[{defaults:{count:5}}]}}}}));
assert.match(blinkProposal,/源状态待同步|目标状态待同步/);
assert.doesNotMatch(blinkProposal,/training-rule-changes|count|修改参数/);
const createProposal = renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{proposal:{
  operation:'create',summaryText:'模型生成的标题',changeText:'当我自己觉得开心时，不论原本是什么状态都闪烁',
  before:{name:'先前未保存的版本',definition:{steps:[{defaults:{count:2}}]}},
  after:{definition:{emotion:'happy',steps:[{defaults:{count:3}}]}}}}));
assert.doesNotMatch(createProposal,/修改前|修改后|规则草案|未添加|模型生成|当我自己|不论原本|training-rule-changes/);
assert.match(createProposal,/开心/);
assert.match(createProposal,/3/);
const combined=renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{transitions:[
  {src:'BLINKING',dst:'OFF',trigger:'CMD_OFF',action:'SET_STATE',label:'关灯',event_label:'请求关灯'},
  {src:'BLINKING',dst:'OFF',trigger:'BLINK_DONE',label:'闪烁结束',event_label:'有限闪烁完成',guard_label:'闪烁前为熄灭'}
]}));
assert.equal((combined.match(/aria-label="预置事件"/g) || []).length,1);
assert.doesNotMatch(combined,/training-custom-events|还没有自定义事件/,'Native-only inspectors omit the entire empty custom-event section');
assert.match(combined,/请求关灯/);assert.match(combined,/有限闪烁完成/);assert.match(combined,/闪烁前为熄灭/);
assert.doesNotMatch(combined,/rule-or|设备原有/);
const selfTransition = inspect({src:'BLINKING',dst:'BLINKING',action:'BLINK',label:'闪烁'},[]);
assert.match(selfTransition,/源状态与目标状态相同/);
assert.doesNotMatch(inspect({src:'OFF',dst:'BLINKING',action:'BLINK',label:'闪烁'},[]),/自循环表示/);
console.log('PASS: exact-version and parameter comparison, self-transition explanation, execution identity and stale observation rejection.');
const view=context.window.trainingTransitionView(old,catalog);
assert.equal(view.chain,'站立 → 看到（主人） → 坐下');
assert.equal(view.eventParams,'主人');
const flower={...old,definition:{...old.definition,trigger:{event_id:'vision.target_presence',where:{target:'花',detected:true}}}};
const flowerCatalog={...catalog,events:[...catalog.events,{id:'vision.target_presence',name:'看到目标物体',fields:{target:'string',detected:'boolean'}}]};
assert.equal(context.window.trainingTransitionView(flower,flowerCatalog).chain,'站立 → 看到（花） → 坐下');
assert.equal(context.window.trainingParameterPatch(old,catalog,{'event:target':'花'}).event_target,'花');
assert.deepEqual(Object.keys(context.window.trainingParameterPatch(old,catalog,{'event:target':'主人'}).event_params),[]);
const multiCatalog={...catalog,transitions:[...catalog.transitions,{...catalog.transitions[0],id:'lie-sit',src:'lying',source_label:'趴下'}]};
const multi={...old,definition:{...old.definition,steps:[{transition_ids:['sit','lie-sit'],defaults:{}}]}};
assert.equal(context.window.trainingTransitionView(multi,multiCatalog).paths.length,2);
assert.equal(context.window.trainingTransitionView(multi,multiCatalog).paths[1].chain,'趴下 → 看到（主人） → 坐下');
assert.equal(context.window.trainingRuleSentence(old,catalog),'看到主人时，切换到坐下。');
assert.equal(context.window.trainingRuleSentence({...happyBlink,definition:{...happyBlink.definition,cooldown_s:undefined}},lampCatalog),'开心时，闪烁 3 次。');
const continuous={...happyBlink,definition:{...happyBlink.definition,cooldown_s:undefined,
  steps:[{transition_ids:['blink'],defaults:{count:0}}]}};
assert.equal(context.window.trainingRuleSentence(continuous,lampCatalog),'开心时，持续闪烁。','Zero remains the declared continuous-blink value');
const detailedBlink={...happyBlink,definition:{...happyBlink.definition,
  steps:[{transition_ids:['blink'],defaults:{count:2,r:120,g:40,b:210}}]}};
const detailedSentence=context.window.trainingRuleSentence(detailedBlink,lampCatalog);
for(const value of ['2','120','40','210','20'])assert.ok(detailedSentence.includes(value),'Sentence formatting retains all saved numeric parameters');
const multiStepCatalog={...catalog,presentation:{actions:{Sit:'坐下',StandUp:'站立'}},transitions:[
  {...catalog.transitions[0],action:'Sit'},
  {id:'stand-again',src:'sitting',dst:'standing',source_label:'坐下',destination_label:'站立',action:'StandUp',state_field:'body_state'}]};
const sequence={...old,definition:{...old.definition,steps:[...old.definition.steps,
  {transition_ids:['stand-again'],defaults:{},conditions:[],goal_state:'standing'}]}};
const sequenceSentence=context.window.trainingRuleSentence(sequence,multiStepCatalog);
assert.equal(sequenceSentence,'看到主人时，切换到坐下，然后切换到站立。');
assert.equal(sequenceSentence.split('看到主人').length-1,1,'Later steps must not manufacture another observation trigger');
const chainMarkup=renderToStaticMarkup(React.createElement(context.window.TrainingEventChain,{
  view:context.window.trainingTransitionView(sequence,multiStepCatalog)}));
assert.match(chainMarkup,/规则执行顺序/);
assert.equal((chainMarkup.match(/看到（主人）/g)||[]).length,1,'An ordered chain has one trigger');
assert.equal((chainMarkup.match(/完成后/g)||[]).length,1,'Actions are connected by completion, not a new observation');
let orderedActivity=context.window.trainingActivityEvent(undefined,{
  ...baseEvent,fact_kind:'result',status:'ok',sequence:2,public_result:{result:'ok'}},'go2',100);
orderedActivity=context.window.trainingActivityEvent(orderedActivity,{
  ...baseEvent,step_id:'run-1:1',step_index:1,transition_id:'stand-again',sequence:5,
  fact_kind:'result',status:'blocked',public_result:{result:'blocked',error:'device_busy'}},'go2',100);
assert.equal(orderedActivity.steps[0].result.status,'ok','Later steps retain the completed first action');
assert.equal(context.window.trainingActivitySummary(sequence,orderedActivity).text,'本次未完成 · 1/2');
const terminalEvent={...baseEvent,step_id:'run-1:1',step_index:1,transition_id:null,sequence:7,
  fact_kind:'result',status:'blocked',public_result:{result:'blocked',error:'device_busy',
    training_run_finished:true,step_count:1,total_steps:2}};
orderedActivity=context.window.trainingActivityEvent(orderedActivity,terminalEvent,'go2',100);
assert.equal(context.window.trainingActivitySummary(sequence,orderedActivity).text,'本次部分完成 · 1/2');
assert.ok(context.window.trainingActivityFor({...sequence,revision:2},{'owner-sit':orderedActivity},'connection-a',100),
  'A blocked-before-dispatch terminal needs no fabricated physical transition');
const partialMarkup=renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{
  transition:multiStepCatalog.transitions[0],projection:[sequence],catalog:multiStepCatalog,
  activityFor:()=>orderedActivity}));
assert.match(partialMarkup,/本次部分完成 · 1\/2/);
assert.match(partialMarkup,/设备正在执行其他动作/);
assert.match(partialMarkup,/已完成/);assert.match(partialMarkup,/未执行/);
assert.doesNotMatch(partialMarkup,/已响应/,'First-step success cannot stand in for the complete rule');
assert.equal(context.window.trainingActivityEvent(orderedActivity,{...baseEvent,sequence:8,
  step_id:'run-1:1',step_index:1,transition_id:'stand-again',fact_kind:'call'},'go2',100),orderedActivity,
  'A finished run cannot be revived by a late call');
const skippedActivity=context.window.trainingActivityEvent(undefined,{...baseEvent,
  transition_id:null,fact_kind:'result',status:'ok',sequence:2,public_result:{result:'ok',skipped:true}},'go2',100);
assert.ok(context.window.trainingActivityFor(current,{'owner-sit':skippedActivity},'connection-a',100));
assert.match(renderToStaticMarkup(React.createElement(context.window.TrainingRunProgress,{
  record:old,catalog,activity:skippedActivity})),/已满足，跳过/);
assert.equal(context.window.trainingParameterPatch(happyBlink,lampCatalog,{'cooldown_s':''}).cooldown_s,null,
  'Clearing a saved cooldown removes it without inventing another interval');
const additionalGuard={...old,definition:{...old.definition,steps:[{...old.definition.steps[0],conditions:[
  ...old.definition.steps[0].conditions,{field:'requester_is_owner',op:'eq',value:true}]}]}};
assert.match(context.window.trainingRuleSentence(additionalGuard,catalog),/请求者是主人/,'Additional guards remain visible');
assert.doesNotMatch(context.window.trainingRuleSentence(old,catalog),/身体为站立|已经坐下|跳过/,'The state pair replaces duplicate posture labels');
const fields=context.window.trainingRuleView(happyBlink,lampCatalog).flatMap(group=>group.fields);
const emotionField=fields.find(field=>field.path==='emotion');
assert.equal(context.window.trainingFieldInputValue(emotionField),'开心');
assert.equal(context.window.trainingParameterPatch(happyBlink,lampCatalog,{emotion:'好奇'}).emotion,'curious');
assert.equal(context.window.trainingParameterPatch(happyBlink,lampCatalog,{emotion:'"happy"'}).emotion,undefined,'Legacy enum values remain compatible without dirtying an unchanged field');
for(const value of ['兴奋','not-an-emotion'])assert.throws(()=>context.window.trainingParameterPatch(happyBlink,lampCatalog,{emotion:value}));
for(const value of ['21','1.5','','-1'])assert.throws(()=>context.window.trainingParameterPatch(happyBlink,lampCatalog,{'step:0:count':value}));
assert.equal(context.window.trainingParameterPatch(happyBlink,lampCatalog,{'step:0:count':'0'}).steps[0].params.count,0);
const renderedInput=renderToStaticMarkup(React.createElement(context.TrainingCompactRuleEditor,{record:happyBlink,catalog:lampCatalog,
  values:Object.fromEntries(fields.map(field=>[field.path,context.window.trainingFieldInputValue(field)])),onChange(){}}));
assert.match(renderedInput,/type="text"/);
assert.match(renderedInput,/aria-label="事件参数：情绪"/);
assert.match(renderedInput,/aria-label="事件参数：次数"/);
assert.doesNotMatch(renderedInput,/<select|type="number"/);
const repeatedValues={...happyBlink,definition:{...happyBlink.definition,situation:'在3号房间',cooldown_s:3,
  steps:[{transition_ids:['blink'],defaults:{count:3,r:3,g:3,b:3}},
    {transition_ids:['blink'],defaults:{count:3}}]}};
const repeatedFields=context.window.trainingRuleView(repeatedValues,lampCatalog).flatMap(group=>group.fields);
const repeatedEditor=renderToStaticMarkup(React.createElement(context.TrainingCompactRuleEditor,{
  record:repeatedValues,catalog:lampCatalog,values:Object.fromEntries(repeatedFields.map(field=>[field.path,context.window.trainingFieldInputValue(field)])),onChange(){}}));
for(const path of ['step:0:count','step:0:r','step:0:g','step:0:b','step:1:count','cooldown_s']){
  assert.equal((repeatedEditor.match(new RegExp(`data-path="${path}"`,'g')) || []).length,1,`The exact ${path} field is bound once even when values repeat`);
}
assert.match(repeatedEditor,/在3号房间/,'Numbers inside immutable situation text stay as text');
const repeatedParts=context.window.trainingRuleParts(repeatedValues,lampCatalog);
assert.equal(repeatedParts.filter(part=>part.field?.path==='step:0:count').length,1);
assert.ok(repeatedParts.some(part=>part.text?.includes('在3号房间')));
const successProposal={proposal_id:'p-success',unit_id:'go2',teaching_id:'owner-sit',operation:'create',confirmed:true,expected_revision:0,after:old};
const successProps={proposal:successProposal,catalog,onLocate:()=>{},record:{...old,load_status:'loaded',loaded_revision:1}};
const success=renderToStaticMarkup(React.createElement(context.window.TrainingPreview,successProps));
assert.match(success,/事件已生效/);
assert.match(success,/看到主人时/);
assert.match(success,/切换到坐下/);
assert.doesNotMatch(success,/training-event-details|training-event-chain__event|现在，Go2/);
assert.equal((success.match(/<button /g)||[]).length,1);
assert.match(success,/aria-label="查看状态转移"/);
const locateMarkup=success.match(/<button[^>]*aria-label="查看状态转移"[\s\S]*?<\/button>/)?.[0] || '';
assert.ok(locateMarkup);
assert.doesNotMatch(locateMarkup,/>查看状态转移</,'The locate entry uses an accessible icon without a visible label');
assert.doesNotMatch(success,/先不添加|确认并生效|事件参数<\/dt>/);
const awaiting=renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{...successProps,record:{...old,load_status:'awaiting_load',loaded_revision:null}}));
assert.doesNotMatch(awaiting,/事件已生效|就会自动切换/);
assert.match(awaiting,/等待设备加载/);
assert.doesNotMatch(awaiting,/<button /,'Waiting for an acknowledgement does not offer the active-event entry');
const newerRecord=renderToStaticMarkup(React.createElement(context.window.TrainingPreview,{...successProps,record:{...current,load_status:'loaded',loaded_revision:2}}));
assert.doesNotMatch(newerRecord,/事件已生效|就会自动切换/);
console.log('PASS: source/target state pairs, concise rule sentences, exact loaded-version success and a single accessible transition icon.');
if(process.argv.includes('--presentation-only')) process.exit(0);
const args = [path.join(__dirname,'check_go2_frontend.cjs'), ...process.argv.slice(2), '--training-only'];
const result = spawnSync(process.execPath,args,{stdio:'inherit',env:process.env});
process.exit(result.status ?? 1);
