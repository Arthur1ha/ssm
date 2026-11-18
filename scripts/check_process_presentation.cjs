/* Public-fact replay fixtures only; no API, model, or device connections. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const context=vm.createContext({window:{},Date,Event,EventTarget,console,setTimeout,clearTimeout});
for(const file of ['utils/agentMeta.js','utils/color.js','utils/activityView.js','utils/runProcess.js'])
  vm.runInContext(fs.readFileSync('app/src/'+file,'utf8'),context);
context.ssmColor=context.window.ssmColor;
const steps=context.processSteps, merge=context.mergeProcessFact;
assert.equal(context.processPendingLabel({stage:'considering'}),'正在规划下一步');
assert.equal(context.processPendingLabel({stage:'dispatching',tool:'go2_push_cargo',arguments:{operation:'assess'}}),'正在观察货物与目的地');
assert.equal(context.processPendingLabel({stage:'dispatching',tool:'go2_push_cargo',arguments:{operation:'approach'}}),'正在接近货物');
assert.equal(context.processActionDetail({tool:'go2_move',arguments:{direction:'turn_left',angle_deg:30}}).text,'向左转动 30°');
const call={event_id:'call-1',agent_id:'cloud_orchestrator',target_unit_id:'go2',run_id:'run-1',
  step_id:'step-1',tool_call_id:'tool-1',tool:'go2_observe',fact_kind:'call',stage:'dispatching',status:'executing',sequence:1};
assert.equal(steps([{stage:'planning'},{stage:'narrated',text:'旧解说'}]).length,0,'Ordinary chat acquired empty process details');
assert.equal(steps([{public_result:{task_id:'task-hidden'},observation:{frame_id:'frame-hidden'}}]).length,0,
  'Correlation metadata acquired empty process details');
const partial={...call,tool:'start_activity',fact_kind:'result',status:'ok',sequence:2,result_kind:'activity',public_result:{
  members:{esp32_desk_led:{joined:true,status:'joined'},go2:{joined:false,status:'offline',reason:'device_offline'}}}};
assert.equal(context.processNeedsAttention(partial),true,'Outer ok hid a member failure');
assert.equal(context.processNeedsAttention({...partial,public_result:{members:{go2:{ok:false}}}}),true);
assert.equal(context.processNeedsAttention({...partial,public_result:{failed:true}}),true);
const actualActivity={...partial,public_result:{activity:{activity_id:'activity-1',revision:3,members:[
  {unit_id:'esp32_desk_led',status:'joined',mode:'reactive'},{unit_id:'go2',status:'offline',reason:'device_offline'}]}}};
assert.equal(context.processNeedsAttention(actualActivity),true);
assert.equal(context.processOutcome(actualActivity),'部分完成');
assert(context.processResultNotes(actualActivity).some(note=>note.text==='呆呆狗当前离线，未能加入活动。' && note.attention));
assert(context.processResultNotes(actualActivity).some(note=>note.text==='皮皮灯已加入，进入自主调光。'));
assert(!JSON.stringify(context.processResultNotes(actualActivity)).includes('esp32_desk_led'));
assert.equal(context.processRecord({...call,observation:{frame_id:'frame-1',as_of:100,proximity:'near'}}).observation.frame_id,'frame-1');
assert(!JSON.stringify(context.processResultNotes({...call,observation:{frame_id:'frame-1',as_of:100,proximity:'near'}})).includes('frame-1'));
const reading={...call,agent_id:'esp32_desk_led',tool:'set_led_color',status:'ok',fact_kind:'result',result_kind:'action',
  arguments:{r:255,g:234,b:198,brightness:170,task_id:'task_hidden'},
  decision_basis:{reason:'阅读需要偏暖的中性白和适合看字的亮度。',evidence:[]},
  public_result:{result:'ok',task_id:'task_hidden',request_id:'request_hidden',future_field:'must-not-render'}};
assert.equal(context.processFactTitle(reading),'调整灯光');
assert.equal(context.processActionDetail(reading).text,'设为暖白，亮度设为 67%');
assert.equal(context.processResultNotes(reading).length,0,'A successful receipt acquired a redundant success paragraph');
assert.doesNotMatch(JSON.stringify(context.processResultNotes(reading)),/task_hidden|request_hidden|future_field|must-not-render/);
assert.equal(context.processUserText('task_id=task_hidden'),'');
// Provider diagnostics stay in logs, including historical serialized SDK errors.
for (const [status,code,detail] of [
  [403,'AllocationQuota.FreeTierOnly','Free quota exhausted.'],
  [400,'invalid_parameter_error','The current model only supports text modality and does not support image input.'],
  [404,'model_not_found','The model `Qwen3.8-Omni-Flash` does not exist or you do not have access to it.'],
]) {
  const legacy=`Error code: ${status} - {'error': {'message': '${detail}', 'code': '${code}'}, 'request_id': 'provider-hidden', 'id': 'chatcmpl-hidden'}`;
  const receipt={...call,status:'error',fact_kind:'result',result_kind:'query',reason:legacy,
    public_result:{result:'error',error:'APIStatusError',message:legacy}};
  const notes=JSON.stringify(context.processResultNotes(receipt));
  assert(notes.includes('这一步暂时未完成。'));
  assert(!notes.includes(detail) && !notes.includes(code) && !notes.includes(String(status)));
  assert.doesNotMatch(notes,/request_id|provider-hidden|chatcmpl-hidden/);
  const structured={...receipt,reason:detail,public_result:{result:'error',status_code:status,error_code:code,
    error_type:'APIStatusError',message:detail,request_id:'provider-hidden',task_id:'physical-hidden'}};
  assert.doesNotMatch(JSON.stringify(context.processResultNotes(structured)),/provider-hidden|physical-hidden|request_id|task_id/);
  assert(context.processResultNotes(structured).every(note=>!note.text.includes(code) && !note.text.includes(`HTTP ${status}`)));
}
assert.equal(context.processUserText('视觉失败 task_id=task_hidden; 请检查模型。'),'视觉失败 请检查模型。');
assert.equal(context.processErrorText('HTTP 404\nRequest ID: abc\ntaskId=def; model_not_found'),'这一步暂时未完成。');
assert.equal(context.processErrorText(JSON.stringify({error:{code:'model_not_found',message:'Model unavailable',request_id:'hidden'}})),
  '这一步暂时未完成。');
assert(context.processResultNotes({...call,status:'error',public_result:{result:'error',error_type:'TypeError'}})
  .some(note=>note.text==='这一步暂时未完成。'));
const longError='The provider rejected the image input. '.repeat(40);
const longNotes=context.processResultNotes({...call,status:'error',reason:longError.slice(0,400),text:longError.slice(0,600),
  public_result:{result:'error',message:longError}});
assert.equal(longNotes.length,1,'Truncated reason/text duplicated the full error message');
assert.equal(longNotes[0].text,'这一步暂时未完成。');
vm.runInContext(fs.readFileSync('app/src/CommandBus.js','utf8')+'\nthis.bus=commandBus;',context);
assert(context.bus.describe({result:'error',error:'NotFoundError',message:'Model unavailable',status_code:404,error_code:'model_not_found'})
  .includes('这一步暂时未完成。'));
assert.doesNotMatch(context.bus.describe({result:'error',error:'NotFoundError',message:'Model unavailable request_id=hidden'}),/request_id|hidden/);
for (const [reason,readable] of [
  ['localization_unavailable','暂时未取得可用的位置数据'],
  ['posture_not_confirmed','暂时还不能确认站立姿态'],
  ['motion_not_observed','本次没有确认到实际移动'],
  ['cargo_search_unconfirmed:localization_unavailable','暂时未取得可用的位置数据'],
]) {
  const notes=context.processResultNotes({...call,status:'blocked',reason,public_result:{result:'blocked',reason}});
  assert(notes.some(note=>note.text===readable));
  assert.doesNotMatch(JSON.stringify(notes),/localization_unavailable|posture_not_confirmed|motion_not_observed/);
}
assert.equal(context.processUserText('定位反馈是 localization_unavailable，先等一下。'),'定位反馈是 暂时未取得可用的位置数据，先等一下。');
assert.equal(context.processUserText('执行反馈 unknown_internal_code'),'执行反馈 相关处理暂未完成');
assert.equal(context.processErrorText('{"error":{"message":"服务暂不可用"}}'),'服务暂不可用');
for (const diagnostic of ['InvalidParameterValue.NoFaceInPhoto','APIStatusError','HTTP/1.1 400 Bad Request','400 Bad Request','{"error":{"code":"unknown_code"}}']) {
  const text='已移动约 0.4 米，识别暂不可用 '+diagnostic+'，尚未确认送达。';
  const displayed=context.processUserText(text);
  assert(displayed.includes('已移动约 0.4 米') && displayed.includes('尚未确认送达。'));
  assert(!displayed.includes(diagnostic));
  assert(!context.activityReasonText({reason:text}).includes(diagnostic));
  assert.equal(context.processErrorText(diagnostic),'这一步暂时未完成。');
}
assert.equal(context.processUserText('查看 https://example.com/photos，位置还需确认。'),'查看 https://example.com/photos，位置还需确认。');
assert.equal(context.processUserText('配置为{"亮度":120}，{暖白}，已移动约 0.4 米。'),'配置为{"亮度":120}，{暖白}，已移动约 0.4 米。');
assert.equal(context.processOutcome({result_kind:'message',status:'ok'}),'已处理消息');
assert.equal(context.processOutcome({result_kind:'message',status:'received'}),'消息已送达');
assert.equal(context.bus.describe({result:'ok',result_kind:'message'}),'已处理消息');
const failedAttempt={...call,event_id:'failed-attempt',status:'blocked',public_result:{result:'blocked',reason:'posture_not_confirmed'}};
const retryAttempt={...call,event_id:'retry-attempt',step_id:'retry-step',status:'executing',sequence:3};
assert.equal(steps([failedAttempt,retryAttempt]).length,2,'A restarted attempt erased the original blocked result');
assert.equal(steps([failedAttempt,retryAttempt])[0].event.status,'blocked');
const serial=steps([call,{...call,event_id:'result-1',fact_kind:'result',status:'ok',sequence:2},
  {...call,event_id:'call-2',step_id:'step-2',tool_call_id:'tool-2',sequence:3}]);
assert.equal(serial.length,2,'Two explicit tool calls collapsed into one');
assert.equal(serial[0].event.status,'ok');assert.equal(serial[1].event.status,'executing');
assert.equal(steps([{...call,status:'ok',sequence:5},{...call,status:'executing',sequence:2}])[0].event.status,'ok');
for (const status of ['planning','planned','decision','dispatching','considering','executing','summarizing']) {
  const merged=merge({...call,status:'ok',ts:500,public_result:{result:'ok'}},
    {status,stage:status,ts:100,public_result:{result:status}});
  assert.equal(merged.status,'ok',`A stale ${status} regressed a terminal receipt`);
  assert.equal(merged.ts,500);assert.equal(merged.public_result.result,'ok');
}
const submitted=merge({...call,status:'accepted',dispatch_only:true},{status:'executing'});
assert.equal(submitted.status,'executing','Submission acceptance blocked actual execution');
assert.equal(submitted.dispatch_only,false);
assert.equal(merge(submitted,{status:'ok'}).status,'ok');
assert.equal(context.processOutcome(merge({...call,status:'accepted',dispatch_only:true},
  {status:'accepted',fact_kind:'result'})),'已完成');
assert.equal(steps([{...call,status:'unknown',sequence:5},{...call,status:'accepted',sequence:6}])[0].event.status,'accepted');
assert.equal(merge({...call,arguments:{r:1,g:2,b:3},decision_basis:{reason:'真实说明',evidence:[{ref:'obs'}]}},
  {arguments:{},decision_basis:null,text:'',reason:null}).arguments.b,3);
assert.equal(context.processGroupKey(call),'run:cloud_orchestrator:run-1','Target replaced actor');
assert.equal(context.processRecord({...call,parent_run_id:'parent',model_turn:2,public_result:{found:true}}).model_turn,2);
assert.equal(context.processOutcome({status:'accepted',result_kind:'action'}),'已完成');
assert.equal(context.processOutcome({status:'accepted',dispatch_only:true}),'已提交');
assert.equal(context.processOutcome({status:'accepted',result_kind:'message'}),'已发出');
assert.equal(context.processOutcome({tool:'send_message',status:'rejected',result_kind:'message'}),'消息未发出');
assert.equal(context.processOutcome({tool:'start_activity',status:'rejected',result_kind:'activity'}),'本次未执行');
const observation={...call,event_id:'obs-event',observation_event_id:'obs-1',fact_kind:'observation',media_ref:'/api/photo',text:'前方有纸箱。'};
assert.equal(steps([observation,{...observation,event_id:'new-projection',text:'该观察补充'}]).length,1);
assert.equal(steps([observation,{...observation,event_id:'obs-event-2',observation_event_id:'obs-2'}]).length,2,
  'Independent observations sharing a URL collapsed');
assert.equal(steps([observation,{...observation,run_id:'other-run'}]).length,2);
assert.equal(steps([observation,{...observation,agent_id:'esp32_desk_led'}]).length,2);
const teaching={...call,tool:'apply_teaching',result_kind:'training',public_result:{teaching_id:'reading',revision:3,training_revision:3,
  name:'阅读灯光',step_number:2,loaded:true,completed:false}};
assert(context.processResultNotes(teaching).some(note=>note.text.includes('「阅读灯光」') && note.text.includes('第 2 步')));
assert(!JSON.stringify(context.processResultNotes(teaching)).includes('reading'));
const message={event_type:'message',agent_id:'go2',unit_id:'go2',message_id:'msg-1',event_id:'speech-1',text:'再来一次！'};
assert.equal(context.activityEventKind({...message,fact_kind:'observation'}),'message',
  'A real peer message was rendered as a non-persona observation');
const received={event_type:'response',event_id:'received-1',message_id:'msg-1',status:'received',
  result_kind:'message',public_result:{delivery_status:'received'}};
assert.equal(context.activityMessageReceipt({...message,target_unit_id:'esp32_desk_led'},[message,received]),'对方已收到');
assert.equal(context.activityProcessRows([message,received]).length,1,'A receipt duplicated the personality message');
const publicRules={...message,public_result:{game_rules:{rule_id:'rps_siege',name:'猜拳围城',
  description:'双方约定映射后再出拳',rules:'一把定胜负，平局重来。',status:'confirmed',
  mapping:[{choice:'rock',label:'红色'}]}}};
assert.equal(context.processHasContent({...publicRules,text:undefined}),true);
assert.equal(context.processFactTitle(publicRules),'猜拳围城');
const ruleFields=context.processResultNotes(publicRules);
assert(!ruleFields.some(note=>note.text===publicRules.public_result.game_rules.rules
  || note.text===publicRules.public_result.game_rules.description),'Static game instructions escaped into process feedback');
assert(ruleFields.some(note=>note.text==='石头用红色表示。'));
assert(ruleFields.some(note=>note.text==='双方已确认玩法。'));
assert(!JSON.stringify(ruleFields).includes('rps_siege'),'Internal rule ID escaped into the displayed rule facts');
const historicalRules={event_type:'action_result',event_id:'old-rules',agent_id:'go2',stage:'game_rules',
  fact_kind:'result',result_kind:'query',text:'',public_result:{game_rules:{rule_id:'push_delivery',name:'指挥官与送货员',
    description:'静态玩法介绍',rules:'完整的内部游戏规则和流程'.repeat(200),status:'proposed'}}};
assert.equal(context.processHasContent(historicalRules),false,'Historical static rules created an empty process entry');
assert.equal(context.activityProcessRows([historicalRules]).length,0);
assert.equal(context.activityProcessRows([message,{...message,event_id:'speech-2',message_id:'msg-2'}]).length,2,
  'Independent identical speech was deduplicated by text');
const game={event_type:'game_result',event_id:'game-1',game_round:{round:1,choices:{go2:'rock'},winner:'go2'}};
assert.equal(context.activityProcessRows([game])[0].kind,'result');
// Typed lifecycle events with no visible content still release the input and retain run resume identity.
(async()=>{
  const effects=[],states=[],bodies=[];let nextTrace=0;const client=new EventTarget();
  client.request=async(_path,options)=>{bodies.push(options.body);
    return {trace_id:options.body.resume_run_id ? 'trace-3' : `trace-${++nextTrace}`};};
  const emit=detail=>client.dispatchEvent(Object.assign(new Event('server-event'),{detail}));
  Object.assign(context,{apiClient:client,apiErrorText:e=>e.message,
    React:{useState(initial){const index=states.length;states.push(initial);return [initial,value=>states[index]=value];},
      useRef:current=>({current}),useCallback:fn=>fn,useEffect:fn=>effects.push(fn)}});
  vm.runInContext(fs.readFileSync('app/src/hooks/useSendIntent.js','utf8'),context);
  const hook=context.useSendIntent();effects.forEach(fn=>fn());
  const first=[],second=[],statuses=[];
  await hook.send('你好',{onMessage:text=>first.push(text)});assert.equal(states[0],true);
  client.dispatchEvent(Object.assign(new Event('server-event'),{detail:{type:'agent_progress',trace_id:'trace-1',
    agent_id:'cloud_orchestrator',stage:'planning',event_id:'lifecycle-1'}}));
  assert.equal(states[0],true);
  client.dispatchEvent(Object.assign(new Event('server-event'),{detail:{type:'run_stage',trace_id:'trace-1',
    agent_id:'cloud_orchestrator',stage:'finished',status:'ok',event_id:'lifecycle-2'}}));
  assert.equal(states[0],false,'Empty terminal lifecycle left the input busy');
  emit({type:'agent_reply',trace_id:'trace-1',agent_id:'cloud_orchestrator',text:'原回复',event_id:'reply-1'});
  assert.deepEqual(first,['原回复'],'Terminal lifecycle swallowed the later original reply');
  await hook.send('新请求',{onMessage:text=>second.push(text),onStatus:text=>statuses.push(text)});
  emit({type:'agent_reply',trace_id:'trace-1',agent_id:'cloud_orchestrator',text:'此前的补充',event_id:'reply-1-late'});
  assert.deepEqual(first,['原回复','此前的补充']);assert.deepEqual(second,[]);
  assert.equal(states[0],true,'An old trace released the new request input');
  emit({type:'reply_error',trace_id:'trace-2',agent_id:'cloud_orchestrator',event_id:'error-2',text:'回复生成失败'});
  emit({type:'run_stage',trace_id:'trace-2',agent_id:'cloud_orchestrator',event_id:'finish-2',stage:'finished'});
  assert.deepEqual(statuses,['回复生成失败']);assert.deepEqual(second,[]);assert.equal(states[0],false);
  const firstQuestion=[],secondQuestion=[],finalReply=[];
  await hook.send('帮我找东西',{onMessage:text=>firstQuestion.push(text)});
  emit({type:'agent_reply',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'question-3',
    status:'needs_clarification',run_id:'resume-same-run',text:'要找什么？'});
  emit({type:'run_stage',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'control-question-3',
    stage:'question',status:'needs_clarification',run_id:'resume-same-run'});
  await hook.send('纸箱',{onMessage:text=>secondQuestion.push(text)});
  assert.equal(bodies.at(-1).resume_run_id,'resume-same-run');
  // The API reuses the trace and run on every resume. Replayed old questions
  // must not attach themselves to the new user answer.
  emit({type:'agent_reply',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'question-3',
    status:'needs_clarification',run_id:'resume-same-run',text:'要找什么？'});
  assert.deepEqual(secondQuestion,[]);
  emit({type:'agent_reply',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'question-4',
    status:'needs_clarification',run_id:'resume-same-run',text:'在哪找？'});
  assert.deepEqual(firstQuestion,['要找什么？']);assert.deepEqual(secondQuestion,['在哪找？']);
  await hook.send('客厅',{onMessage:text=>finalReply.push(text)});
  assert.equal(bodies.at(-1).resume_run_id,'resume-same-run');
  emit({type:'run_stage',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'finish-5',stage:'done'});
  emit({type:'agent_reply',trace_id:'trace-3',agent_id:'cloud_orchestrator',event_id:'final-5',text:'已经找到纸箱。'});
  assert.deepEqual(finalReply,['已经找到纸箱。']);
  assert.deepEqual(secondQuestion,['在哪找？']);
  assert.equal(states[0],false);
  console.log('PASS: seven public-fact fixtures, actor/target, partial membership, serial calls, late receipts, observation identity, training and empty lifecycle control.');
})().catch(error=>{console.error(error);process.exitCode=1;});
