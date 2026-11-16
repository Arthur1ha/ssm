/* Real activity card, mocked API/device feedback. Never contacts live devices.
 * NODE_PATH must expose sucrase and puppeteer. Set PUPPETEER_EXECUTABLE_PATH,
 * SSM_CHECK_REACT and SSM_CHECK_REACT_DOM; SCREENSHOT_DIR is optional. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const compile = name => sucrase.transform(fs.readFileSync(name,'utf8'),{transforms:['jsx'],production:true}).code;
const files = ['utils/stateMachine.js','utils/agentMeta.js','utils/color.js','utils/activityView.js','utils/runProcess.js','avatars/registry.js',
  'components/Icon.jsx','avatars/Avatar.jsx','components/RunProcess.jsx','components/ActivityMembers.jsx','components/ActivityFeed.jsx'];
const context = vm.createContext({window:{}});
for (const file of files) {
  vm.runInContext(compile('app/src/'+file),context);
  Object.assign(context,context.window);
}
const {activityMemberView,processSteps,activityProcessRows,activityMessageReceipt,
  activityDecisionBasis,activityDecisionText,activityDecisionEvidence,projectActivityEvents} = context;
const sseReplay=process.env.SSM_ACTIVITY_SSE_REPLAY ? JSON.parse(fs.readFileSync(process.env.SSM_ACTIVITY_SSE_REPLAY,'utf8')) : null;
const now = Date.now()/1000;
const go2 = {unit_id:'go2',name:'呆呆狗',agent_type:'robot',_online:true,
  state_machine:{state_field:'body_state',regions:[
    {id:'activity',state_field:'activity_state',states:['active','rest'],views:{active:{label:'活跃'},rest:{label:'休息'}}},
    {id:'life',state_field:'life_state',states:['idle','play'],views:{idle:{label:'空闲'},play:{label:'玩耍'}}},
    {id:'body',state_field:'body_state',states:['standing','moving'],views:{standing:{label:'站立'},moving:{label:'移动中'}}},
  ]}};
const lamp = {unit_id:'esp32_desk_led',name:'皮皮灯',agent_type:'actuator',tags:['lighting'],_online:true};
const states = {go2:{state:{connected:true,activity_state:'active',life_state:'play',body_state:'standing',
  body_confirmation:'observed',body_telemetry_fresh:true,autonomy_mode:'free_explore',_received_at:now}},
  esp32_desk_led:{state:{ism:'ON',r:0,g:0,b:255,brightness:120,autonomy_mode:'reactive',_received_at:now}}};
const activity = {activity_id:'act_example',context:'让狗和灯自己玩吧',status:'active',revision:1,
  devices:{go2:{status:'joined',mode:'free_explore'},esp32_desk_led:{status:'joined',mode:'reactive'}}};
let nextEventTime=now-20;
const event = (id,type,extra={}) => ({event_id:id,activity_id:activity.activity_id,event_type:type,
  ts:nextEventTime++,unit_id:'go2',...extra});
const invite = event('invite','message',{unit_id:'esp32_desk_led',message_id:'msg_invite',target_unit_id:'go2',
  interaction_kind:'game_invite',text:'来玩找颜色吧？我亮一种颜色，你在周围找同色的东西。'});
const accepted = event('accepted','message',{message_id:'msg_reply',reply_to:'msg_invite',reply_kind:'accepted',
  target_unit_id:'esp32_desk_led',text:'好呀，你出颜色，我来找！'});
const planned = event('plan','action',{task_id:'task_game',action:'go2_navigate_to_visible_target',
  trigger_message_id:'msg_invite',text:'准备安全接近蓝色物品',state_at_event:{status:'current',as_of:now-22,values:states.go2.state}});
const running = event('run','action_progress',{task_id:'task_game',action:planned.action,status:'executing'});
const result = event('result','action_result',{task_id:'task_game',action:planned.action,status:'timeout',
  reason:'stop_unconfirmed',text:'停止结果尚未确认',trigger_message_id:'msg_invite',
  state_at_event:{status:'current',as_of:now-16,values:states.go2.state}});
const events = [invite,accepted,planned,running,result];
const decisionBasis = {reason:'呆呆狗现在也没任务，我也有空；刚才它看到了白色玩偶，我想邀请它玩找颜色。',
  evidence:[{ref:'observation-one',kind:'observation',source:'go2',as_of:now-30,summary:'地上有一个白色猫头鹰玩偶。'}]};
const groundedInvite={...invite,decision_basis:decisionBasis};
assert.equal(activityDecisionBasis(groundedInvite).reason,decisionBasis.reason);
assert.equal(activityDecisionText(groundedInvite,invite.text),invite.text);
for (const type of ['question', 'user_question', 'peer_message', 'proposal', 'reply', 'PEER_REPLY']) {
  assert.equal(activityDecisionText({...groundedInvite,event_type:type},invite.text),invite.text,
    'A conversation bubble acquired decision reasoning: '+type);
}
assert.equal(activityDecisionText(groundedInvite,''),'','A missing message was replaced by private decision reasoning');
assert.equal(activityDecisionText(groundedInvite,decisionBasis.reason),decisionBasis.reason);
assert.equal(activityDecisionText(invite,invite.text),invite.text);
assert.equal(activityDecisionBasis(invite),null,'Old history acquired a fabricated explanation');
assert.equal(activityDecisionEvidence(decisionBasis.evidence[0],groundedInvite,[],now).missingOriginal,true);
assert.equal(activityDecisionEvidence(decisionBasis.evidence[0],groundedInvite,
  [{event_id:'observation-one',unit_id:'go2'}],now).missingOriginal,false);
const oldEvidence={...decisionBasis.evidence[0],as_of:now-700};
assert.match(activityDecisionEvidence(oldEvidence,groundedInvite,[],now).notice,/决策时已过期/);
assert.match(activityDecisionEvidence({...decisionBasis.evidence[0],as_of:now-500},
  {...groundedInvite,ts:now-400},[],now+200).notice,/当前情况需要重新确认/);
assert.match(activityDecisionEvidence({...decisionBasis.evidence[0],as_of:null},groundedInvite,[],now).notice,/记录时间未提供/);
assert.equal(activityDecisionBasis(processSteps([
  {...planned,decision_basis:decisionBasis},result])[0].event).reason,decisionBasis.reason,
  'An explicitly linked receipt erased the original selection basis');
assert.equal(activityMessageReceipt(invite,[invite]),'等待对方决定');
assert.equal(activityMessageReceipt(invite,events),'对方已接受');
assert.equal(activityMemberView(go2,states.go2.state,{status:'joined'},true,now).label,'玩耍 · 站立');
assert.equal(activityMemberView(go2,states.go2.state,{status:'joined'},true,now).detail,'',
  'Normal participation belongs in the shared stage footer');
assert.equal(activityMemberView(lamp,{...states.esp32_desk_led.state,control_state:'holding'},
  {status:'joined'},true,now).detail,'暂时保留你的设置 · 亮度适中');
assert.equal(activityMemberView(go2,states.go2.state,{status:'offline'},true,now).detail,'未加入 · 设备离线');
assert.equal(activityMemberView(lamp,states.esp32_desk_led.state,{status:'joined'},true,now).label,'常亮 · 蓝色');
assert.equal(activityMemberView(lamp,{...states.esp32_desk_led.state,_received_at:now-7200},{status:'joined'},true,now).label,
  '常亮 · 蓝色','An unchanged lamp setting was replaced by a freshness warning');
assert.equal(activityMemberView(lamp,{ism:'OFF'},{status:'joined'},true,now).label,'熄灭');
assert.equal(activityMemberView(go2,{...states.go2.state,_received_at:now-70},{status:'joined'},true,now).label,'状态待确认');
assert.equal(activityMemberView(go2,states.go2.state,{status:'joined'},false,now).label,'状态待确认');
assert.equal(activityMemberView(go2,{...states.go2.state,body_telemetry_fresh:false},{status:'joined'},true,now).label,'玩耍 · 身体待确认');
assert.equal(processSteps([planned,running,result]).length,1);
assert.equal(processSteps([result,running])[0].event.status,'timeout','A progress replay downgraded a receipt');
assert.equal(processSteps([result,{...result,event_id:'other',action:'go2_observe'}]).length,2,'Different capabilities were merged');
assert.equal(processSteps([result,{...result,event_id:'repeated'}]).length,1,'A repeated terminal receipt duplicated one task step');
const grouped=activityProcessRows(events);
assert.equal(grouped.length,3,'Explicit steps should collapse once without swallowing personality replies');
assert.equal(grouped.filter(row=>row.kind==='process').length,1);
assert.equal(grouped.filter(row=>row.kind==='message').every(row=>!row.process.length),true,
  'A shared invitation attached every action to every message');
const searchSteps = ['search_observing','search_observe','search_selected','search_observe'].map((stage,index) =>
  event('search-step-'+index,'search_progress',{stage,task_id:'activity-search',source:'object_search',
    fact_kind:stage === 'search_observe' ? 'observation' : stage === 'search_selected' ? 'decision' : undefined,
    observation_event_id:stage === 'search_observe' ? 'search-observation-'+index : undefined,
    text:stage === 'search_observe' ? '前方有纸箱。' : '搜索步骤 '+stage,
    media_ref:stage === 'search_observe' ? '/api/activity-photo-'+index : null}));
assert.equal(processSteps(projectActivityEvents([...searchSteps,
  {...result,task_id:'activity-search',status:'ok'}])).length,4,
  'Search completion erased its individual observations and steps');
assert.equal(projectActivityEvents([...searchSteps,event('scene-copy','observation',{
  source:'go2.camera',text:'前方有纸箱。',media_ref:'/api/activity-photo-1',observation_event_id:'search-observation-1'})]).length,4,
  'A shared scene duplicated a search photo');
const lampPlan=event('lamp-plan','action',{unit_id:lamp.unit_id,task_id:'light-task',action:'set_led_color',
  state_at_event:{status:'current',as_of:now-10,values:{ism:'ON',r:255,g:200,b:80}}});
const lampResult=event('lamp-result','action_result',{unit_id:lamp.unit_id,task_id:'light-task',action:'set_led_color',status:'ok',
  state_at_event:{status:'current',as_of:now-9,values:{ism:'ON',r:0,g:0,b:255}}});
// Public state rendering reads only the saved, current snapshot.
assert.equal(context.activityPhysicalLabel(lamp,lampResult.state_at_event.values),'常亮 · 蓝色');
assert.equal(processSteps([lampPlan,lampResult]).length,1);
assert.equal(processSteps([{...lampPlan,arguments:{r:0,g:0,b:255}},
  {...lampResult,arguments:{}}])[0].event.arguments.b,255,'A receipt without parameters erased the selected color');
const explicitRound=event('round','message',{stage:'rps_result',game_id:'rps_siege',game_round:{round:1,
  choices:{go2:'scissors',esp32_desk_led:'rock'},mapping:{go2:{label:'打招呼'},esp32_desk_led:{label:'橙色'}},winner:'esp32_desk_led'}});
assert.equal(context.avatarEventPresentation(explicitRound,'go2').shape,'scissors');
assert.equal(context.avatarEventPresentation({...explicitRound,stage:'throw_announcement'},'go2'),null);
assert.equal(context.avatarEventPresentation({...explicitRound,game_round:undefined,text:'狗出剪刀'},'go2'),null);
console.log('PASS: explicit message/task relationships, physical freshness, action grouping and unconfirmed results.');

async function browserChecks() {
  const puppeteer = require('puppeteer');
  if(process.env.SCREENSHOT_DIR) fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
  const browser = await puppeteer.launch({headless:true,executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,
    protocolTimeout:45000,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
  try {
    // Native EventSource -> real ApiClient -> hook -> activity bubbles. The
    // history endpoint deliberately starts empty, so REST cannot mask dropped
    // named SSE events (the previous mapping_proposal transport bug).
    const streamPage = await browser.newPage();
    const streamErrors = [];
    streamPage.on('pageerror', error=>{streamErrors.push(error.message);console.error('SSE browser error:',error.message);});
    const streamActivity = sseReplay?.activity || {...activity,activity_id:'act_sse'};
    const mappingRows = sseReplay?.events.filter(row=>row.stage==='mapping_proposal') || [
      {type:'activity',event_type:'message',stage:'mapping_proposal',game_id:'rps_siege',
        event_id:'map-lamp',activity_id:streamActivity.activity_id,unit_id:'esp32_desk_led',
        ts:now,text:'石头＝正红；剪刀＝亮绿；布＝明黄。'},
      {type:'activity',event_type:'message',stage:'mapping_proposal',game_id:'rps_siege',
        event_id:'map-dog',activity_id:streamActivity.activity_id,unit_id:'go2',
        ts:now+.1,text:'石头＝开心；剪刀＝比心；布＝伸展。'},
    ];
    const streamRows = sseReplay?.events || [...mappingRows, ...Array.from({length:18},(_,index)=>({
      type:'activity',event_type:'message',stage:'agreement',event_id:`later-${index}`,
      activity_id:streamActivity.activity_id,unit_id:'go2',ts:now+index+1,text:`后续进展 ${index}`,
    }))];
    const wire = sseReplay?.wire || streamRows.map(row=>
      `id: ${row.event_id}\nevent: activity\ndata: ${JSON.stringify(row)}\n\n`).join('');
    let restoreHistory = false;
    await streamPage.setRequestInterception(true);
    streamPage.on('request', request=>{
      const url = new URL(request.url());
      if(url.pathname==='/') request.respond({status:200,contentType:'text/html',
        body:'<html><body><div id="root"></div></body></html>'});
      else if(url.pathname==='/api/events') request.respond({status:200,contentType:'text/event-stream',body:wire});
      else if(url.pathname==='/api/activity/current') request.respond({status:200,contentType:'application/json',
        body:JSON.stringify({activity:streamActivity,latest_activity_id:streamActivity.activity_id})});
      else if(url.pathname===`/api/activity/${streamActivity.activity_id}/events`) request.respond({status:200,
        contentType:'application/json',body:JSON.stringify({activity:streamActivity,events:restoreHistory ? streamRows : []})});
      else if(url.pathname==='/favicon.ico') request.respond({status:204,body:''});
      else {streamErrors.push('Unexpected stream request: '+request.url());request.abort();}
    });
    await streamPage.goto('http://ssm.test/');
    for(const file of [process.env.SSM_CHECK_REACT,process.env.SSM_CHECK_REACT_DOM])
      await streamPage.addScriptTag({path:file});
    for(const file of ['ApiClient.js',...files,'hooks/useActivityTimeline.js'])
      await streamPage.addScriptTag({content:compile('app/src/'+file)});
    for (const style of ['app','process','avatars']) await streamPage.addStyleTag({path:`app/styles/${style}.css`});
    await streamPage.evaluate(({go2,lamp,states,expected})=>{
      window.__received=[];
      apiClient.addEventListener('server-event',message=>{
        if(!__received.some(row=>row.event_id===message.detail.event_id)) __received.push(message.detail);
        if(__received.length>=expected) apiClient.closeEvents();
      });
      function Harness(){
        const view=useActivityTimeline();window.__timeline=view;
        return React.createElement(SpaceActivityFeed,{activity:view.displayActivity,events:view.events,
          agents:[go2,lamp],unitData:states,connected:true});
      }
      window.__root=ReactDOM.createRoot(document.getElementById('root'));
      window.__mount=()=>__root.render(React.createElement(Harness));__mount();
    },{go2,lamp,states,expected:streamRows.length});
    await streamPage.waitForFunction(()=>Boolean(window.__timeline?.displayActivity));
    await streamPage.evaluate(()=>apiClient.openEvents());
    try {
      await streamPage.waitForFunction(count=>window.__received.length===count,{timeout:5000},streamRows.length);
    } catch(error) {
      console.error('Received SSE records:',await streamPage.evaluate(()=>({count:__received.length,
        mappings:__received.filter(row=>row.stage==='mapping_proposal').length})));
      throw error;
    }
    await streamPage.waitForFunction(()=>document.querySelector('.space-activity__toggle'));
    await streamPage.click('.space-activity__toggle');
    await streamPage.waitForFunction(texts=>texts.every(text=>[...document.querySelectorAll('.agent-speech__bubble')]
      .some(bubble=>bubble.textContent.trim()===text.trim())),{timeout:5000},mappingRows.map(row=>row.text));
    // Reconnect/snapshot recovery must restore the same bubbles without
    // inventing a different mapping or duplicating records.
    restoreHistory = true;
    await streamPage.evaluate(()=>apiClient.dispatchEvent(new Event('events-open')));
    await streamPage.waitForFunction(count=>__timeline.events.length===count,{},streamRows.length);
    assert.equal(await streamPage.evaluate(()=>__timeline.events.filter(row=>row.stage==='mapping_proposal').length),mappingRows.length);
    assert.deepEqual(streamErrors,[]);
    await streamPage.close();
    console.log('PASS: native named SSE -> ApiClient -> activity hook -> both mapping bubbles; history recovery deduplicates.');
    const page = await browser.newPage();
    const errors=[];
    page.on('pageerror',error=>{errors.push(error.message);console.error('Browser error:',error.message);});
    await page.setRequestInterception(true);
    page.on('request',request=>{
      if(request.isNavigationRequest() && request.url()==='http://ssm.test/') request.respond({status:200,
        contentType:'text/html',body:'<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>'});
      else if(request.url().startsWith('data:')) request.continue();
      else if(request.url().endsWith('/favicon.ico')) request.respond({status:204,body:''});
      else {errors.push('Unexpected network: '+request.url());request.abort();}
    });
    await page.goto('http://ssm.test/');
    console.log('Browser: loading real activity components.');
    for (const style of ['app','process','avatars']) await page.addStyleTag({path:`app/styles/${style}.css`});
    await page.addStyleTag({content:'html,body{margin:0;background:var(--color-bg);color:var(--color-text);font-family:var(--font-sans)} .test-shell{max-width:560px;margin:auto;padding:18px 12px}.test-shell>h1{font-size:16px;margin:0 4px 16px}'});
    for(const file of [process.env.SSM_CHECK_REACT,process.env.SSM_CHECK_REACT_DOM]) {
      assert(file,'Set SSM_CHECK_REACT and SSM_CHECK_REACT_DOM');await page.addScriptTag({path:file});
    }
    for(const file of files) await page.addScriptTag({content:compile('app/src/'+file)});
    await page.addScriptTag({content:compile('app/src/hooks/useActivityTimeline.js')});
    console.log('Browser: scripts loaded; preparing local font.');
    if(process.env.SSM_CHECK_FONT) {
      await page.evaluate(async data=>{const font=new FontFace('ActivityCJK',`url(data:font/otf;base64,${data})`);await font.load();document.fonts.add(font);},fs.readFileSync(process.env.SSM_CHECK_FONT).toString('base64'));
      await page.addStyleTag({content:':root{--font-sans:ActivityCJK,sans-serif;--font-mono:ActivityCJK,monospace}button{font-family:inherit}'});
    }
    console.log('Browser: rendering the activity fixture.');
    await page.evaluate(({activity,events,go2,lamp,states})=>{
      window.apiClient = new EventTarget();apiClient.devicesSynced=true;
      window.navigate = hash=>window.location.hash=hash;
      window.__props={activity,events,agents:[go2,lamp],unitData:states,connected:true};
      window.__endCalls=0;
      window.__root=ReactDOM.createRoot(document.getElementById('root'));
      window.__render = patch=>{
        __props={...__props,...patch};
        __root.render(React.createElement('main',{className:'test-shell'},React.createElement('h1',null,'我的空间'),
          React.createElement(SpaceActivityFeed,{...__props,onEnd:()=>window.__endCalls++})));
      };__render({});
    },{activity,events,go2,lamp,states});
    await page.waitForSelector('.activity-member');
    console.log('Browser: activity card mounted.');
    assert.match(await page.$eval('.activity-members__footer',el=>el.textContent),/2 位伙伴参与活动/);
    const activityHash = await page.evaluate(()=>location.hash);
    for (const member of await page.$$('.activity-member')) await member.click();
    assert.equal(await page.evaluate(()=>location.hash),activityHash,'Companion presentation must not navigate');
    await page.waitForSelector('.run-process-summary');
    assert.equal(await page.$$eval('.run-process-summary', rows=>rows.every(row=>row.open)),true,
      'An unconfirmed result must expose its cause by default');
    assert.match(await page.$eval('.run-process-summary summary',el=>el.innerText),/确认/);
    assert(await page.$eval('.space-activity',e=>e.textContent.includes('对方已接受')));
    for(const width of [320,390,1280]) {
      await page.setViewport({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Horizontal overflow');
      assert.equal(await page.$$eval('.activity-member',members=>members.every(member=>{
        const style=getComputedStyle(member), portrait=member.querySelector('.activity-member__portrait').getBoundingClientRect();
        const name=member.querySelector('.activity-member__heading').getBoundingClientRect();
        return style.backgroundColor==='rgba(0, 0, 0, 0)' && parseFloat(style.borderTopWidth)===0
          && portrait.bottom<=name.top && member.scrollWidth<=member.clientWidth;
      })),true,'Shared stage avatars, names and states must fit without individual card chrome');
      await page.$eval('.run-process-summary',el=>{el.open=true;});
      assert.equal(await page.$eval('.run-process-summary',el=>el.open),true);
      assert.match(await page.$eval('.run-process__body',el=>el.innerText),/停止结果尚未确认/);
      assert.equal(await page.$eval('.run-process__body',el=>el.scrollWidth<=el.clientWidth),true);
      if(process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`activity-process-${width}.png`),fullPage:true});
      await page.click('.run-process-summary summary');
    }
    // Public rationale belongs inside the same disclosure, never a second bubble.
    await page.evaluate(({invite,basis})=>__render({events:[{...invite,decision_basis:basis}]}),
      {invite:groundedInvite,basis:decisionBasis});
    await page.waitForSelector('.run-process-summary');
    assert.equal(await page.$$eval('.agent-speech__bubble',els=>els.length),1);
    assert.equal(await page.$eval('.agent-speech__bubble',el=>el.textContent),invite.text);
    assert.equal(await page.$eval('.run-process-summary',el=>el.open),false);
    await page.click('.run-process-summary summary');
    assert.match(await page.$eval('[data-decision-reason]',el=>el.innerText),/刚才它看到了白色玩偶/);
    assert.match(await page.$eval('[data-decision-evidence]',el=>el.innerText),/白色猫头鹰/);
    assert.match(await page.$eval('[data-decision-evidence]',el=>el.innerText),/原记录未保留/);
    assert.equal(await page.$eval('.run-process__body',el=>el.textContent.includes('来玩找颜色吧')),false,
      'Decision details repeated the message already displayed in its bubble');
    // A new event must preserve the user's explicit open state.
    await page.evaluate(()=>__render({events:[...__props.events,{event_id:'unrelated-new',event_type:'message',
      unit_id:'go2',text:'另一个独立消息',ts:Date.now()/1000}]}));
    assert.equal(await page.$eval('.run-process-summary',el=>el.open),true);
    await page.evaluate(({invite,basis})=>__render({events:[{...invite,
      decision_basis:{...basis,evidence:basis.evidence.map(row=>({...row,as_of:1}))}}]}),
      {invite:groundedInvite,basis:decisionBasis});
    await page.waitForFunction(()=>document.querySelector('[data-decision-evidence]')?.textContent.includes('决策时已过期'));
    // Keep the deciding role distinct from its target, and keep receipt text
    // visible even when the action has both arguments and a saved rationale.
    await page.evaluate(({planned,result,basis})=>__render({events:[
      {...planned,unit_id:'cloud_orchestrator',target_unit_id:'go2',arguments:{distance_m:0.2},decision_basis:basis},
      {...result,unit_id:'cloud_orchestrator',target_unit_id:'go2'},
    ]}),{planned,result, basis:decisionBasis});
    await page.waitForSelector('.run-process__execution');
    assert.equal(await page.$eval('.run-process__actor',el=>el.textContent),
      await page.evaluate(()=>getAgentDisplayName('cloud_orchestrator')));
    assert.match(await page.$eval('.run-process__target',el=>el.textContent),/呆呆狗/);
    assert.match(await page.$eval('.run-process__notes',el=>el.textContent),/停止结果尚未确认/);
    assert.deepEqual(await page.$$eval('.run-process__step-content > div',els=>els.map(el=>el.className)),
      ['run-process__decision','run-process__execution','run-process__notes']);
    if(process.env.SCREENSHOT_DIR) {
      await page.setViewport({width:390,height:900});
      await page.$eval('.run-process-summary',el=>{el.open=true;});
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'activity-decision-390.png'),fullPage:true});
    }
    await page.evaluate(round=>__render({events:[round]}),explicitRound);
    await page.waitForSelector('.activity-game-round');
    assert.equal(await page.$$eval('.activity-game-round .shape-face',rows=>rows.length),2);
    assert.match(await page.$eval('.activity-game-round',el=>el.innerText),/打招呼/);
    assert.match(await page.$eval('.activity-game-round',el=>el.innerText),/橙色/);
    assert.equal(await page.$$eval('.activity-game-round [data-motion]',rows=>rows.every(row=>row.dataset.motion==='none')),true,
      'History replay animated a revealed result');
    for (const width of [320,390,1280]) {
      await page.setViewport({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Round result overflow');
      if(process.env.SCREENSHOT_DIR && width===390) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'activity-round-390.png'),fullPage:true});
    }
    await page.evaluate(({activity,events})=>__render({activity,events}),{activity,events});
    await page.waitForSelector('.run-process-summary');
    // Append while reading old messages: badge appears and scroll is not forced down.
    await page.evaluate(()=>{
      const lots=Array.from({length:25},(_,i)=>({event_id:'chat'+i,event_type:'message',unit_id:'go2',
        text:'第'+i+'条观察后的交流。',ts:Date.now()/1000+i}));
      __render({events:lots});
    });
    await page.waitForFunction(()=>document.querySelector('.space-activity__timeline').scrollHeight>500);
    await page.$eval('.space-activity__timeline',el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll',{bubbles:true}));});
    const oldFirst=await page.$eval('.space-activity__timeline',el=>el.firstElementChild.textContent);
    await page.evaluate(()=>__render({events:[...__props.events,{event_id:'new',event_type:'message',unit_id:'go2',text:'这里有一条新发现。',ts:Date.now()/1000+30}]}));
    await page.waitForSelector('.space-activity__new');
    assert.equal(await page.$eval('.space-activity__timeline',e=>e.scrollTop),0);
    assert.equal(await page.$eval('.space-activity__timeline',el=>el.firstElementChild.textContent),oldFirst,
      'Incoming messages replaced the text being read');
    await page.click('.space-activity__new');
    await page.waitForFunction(()=>!document.querySelector('.space-activity__new'));
    // Unknown feedback must not display old physical facts as current.
    await page.evaluate(()=>__render({connected:false}));
    await page.waitForFunction(()=>document.querySelector('.activity-member__state')?.textContent==='状态待确认');
    assert(await page.$eval('.activity-members',e=>e.textContent.includes('常亮 · 蓝色')));
    await page.evaluate(()=>__render({connected:true,activity:{...__props.activity,status:'ended'},events:[]}));
    await page.waitForFunction(()=>!document.querySelector('.activity-members'));
    if(process.env.SCREENSHOT_DIR) {
      await page.setViewport({width:390,height:900});
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'activity-ended-390.png'),fullPage:true});
    }
    // Hook restores the server's ended metadata rather than retaining joined modes.
    await page.evaluate(()=>{
      const ended={...__props.activity,status:'ended',revision:8,devices:{go2:{status:'left',physical_stop:'unknown'}}};
      window.__serverActivity={...ended,status:'active',revision:7};
      apiClient.request=async endpoint=>endpoint==='/api/activity/current'
        ? {activity:__serverActivity,latest_activity_id:ended.activity_id}
        : {activity:__serverActivity || ended,events:[]};
      function HookHarness(){const view=useActivityTimeline();return React.createElement('pre',null,JSON.stringify(view.displayActivity));}
      __root.render(React.createElement(HookHarness));
    });
    await page.waitForFunction(()=>document.querySelector('pre')?.textContent.includes('"active"'));
    await page.evaluate(()=>{
      __serverActivity=null;
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'activity',activity_id:'act_example',event_id:'ended',event_type:'ended'}}));
    });
    await page.waitForFunction(()=>document.querySelector('pre')?.textContent.includes('"physical_stop":"unknown"'));
    // Mount the unchanged home route with its real components and the same
    // simulated API, verifying that the enhancement stays inside its old card.
    const home=await browser.newPage();
    home.on('pageerror',error=>errors.push(error.message));
    await home.setRequestInterception(true);
    home.on('request',request=>{
      if(request.isNavigationRequest() && request.url()==='http://ssm.test/') request.respond({status:200,
        contentType:'text/html',body:'<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>'});
      else if(new URL(request.url()).pathname.startsWith('/api/activity-photo-'))request.respond({status:200,
        contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#475569"/></svg>'});
      else if(request.url().startsWith('data:'))request.continue();
      else if(request.url().endsWith('/favicon.ico'))request.respond({status:204,body:''});
      else {errors.push('Unexpected home network: '+request.url());request.abort();}
    });
    await home.setViewport({width:390,height:844});await home.goto('http://ssm.test/');
    for (const style of ['app','process','avatars']) await home.addStyleTag({path:`app/styles/${style}.css`});
    await home.addStyleTag({content:'html,body,#root{height:100%;overflow:hidden}'});
    for(const file of [process.env.SSM_CHECK_REACT,process.env.SSM_CHECK_REACT_DOM])await home.addScriptTag({path:file});
    const scripts=[...fs.readFileSync('app/index.html','utf8').matchAll(/<script[^>]+src="(src\/[^\"]+)"/g)]
      .map(match=>match[1]).filter(file=>file!=='src/boot.js');
    for(const file of scripts)await home.addScriptTag({content:compile('app/'+file)});
    if(process.env.SSM_CHECK_FONT){
      await home.evaluate(async data=>{const f=new FontFace('ActivityCJK',`url(data:font/otf;base64,${data})`);await f.load();document.fonts.add(f);},fs.readFileSync(process.env.SSM_CHECK_FONT).toString('base64'));
      await home.addStyleTag({content:':root{--font-sans:ActivityCJK,sans-serif;--font-mono:ActivityCJK,monospace}'});
    }
    await home.evaluate(({activity,events,go2,lamp,states})=>{
      const devices=[go2,lamp].map(card=>({...card,online:true,state:states[card.unit_id].state}));
      apiClient.eventsConnected=true;apiClient.devicesSynced=true;apiClient.openEvents=()=>{};
      apiClient.request=async endpoint=>{
        if(endpoint==='/api/devices')return devices;
        if(endpoint==='/api/activity/current')return {activity,latest_activity_id:activity.activity_id};
        if(endpoint==='/api/activity/'+activity.activity_id+'/events')return {activity,events};
        if(endpoint==='/api/agents/names')return {};
        if(endpoint==='/api/intents'){window.__homeIntentCount=(window.__homeIntentCount || 0)+1;
          return {trace_id:'tr_home',event_id:'home-request'};}
        throw Error('Unexpected API: '+endpoint);
      };
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App,{
        session:{actor:'admin',space_name:'工作空间'},logout(){},refreshSession:async()=>{}}));
    },{activity,events,go2,lamp,states});
    await home.waitForSelector('.space-activity .activity-member');
    assert.equal(await home.$$('.space-activity').then(nodes=>nodes.length),1);
    assert.equal(await home.evaluate(()=>location.hash),'');
    assert.equal(await home.$('.home-activity-input-note'),null);
    // Activity progress must not enter either the global search feed or an
    // unrelated request that is currently waiting for its own SSE feedback.
    await home.type('.chat-panel input','查看当前情况');
    await home.keyboard.press('Enter');
    await home.waitForFunction(()=>window.__homeIntentCount === 1);
    await home.evaluate(({activity,searchSteps})=>{
      const emit = detail => apiClient.dispatchEvent(new CustomEvent('server-event',{detail}));
      searchSteps.forEach(step=>{
        emit({type:'agent_progress',scope:'space',source:'object_search',agent_id:'go2',
          run_id:'activity-search',task_id:'activity-search',event_id:step.event_id,
          activity_id:activity.activity_id,stage:step.stage,text:step.text,media_ref:step.media_ref});
        emit({...step,type:'activity',activity_id:activity.activity_id});
      });
      emit({type:'agent_reply',scope:'session',source:'orchestrator',agent_id:'cloud_orchestrator',
        trace_id:'tr_home',event_id:'home-final',text:'当前情况已反馈。'});
    },{activity,searchSteps});
    await home.waitForFunction(()=>document.querySelectorAll('.space-activity img').length === 2);
    await home.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('当前情况已反馈。'));
    assert.equal(await home.$$eval('.run-process-summary',rows=>rows.some(row=>
      !row.closest('.space-activity') && /前方有纸箱|搜索步骤/.test(row.textContent))),false,'Activity steps escaped into the conversation');
    assert.equal(await home.$$eval('.chat-messages img',rows=>rows.every(row=>row.closest('.space-activity'))),true,
      'An activity photo escaped its card');
    assert.equal(await home.$$eval('.space-activity__timeline img',rows=>rows.length),2);
    assert.match(await home.$eval('.space-activity__timeline',el=>el.textContent),/搜索步骤 search_selected/);
    for(const width of [320,390,1280]){
      await home.setViewport({width,height:844});
      assert.equal(await home.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,
        'Activity photos overflow the viewport');
    }
    // Autonomous searches outside an activity still retain their own feed.
    await home.evaluate(()=>apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{
      type:'agent_progress',scope:'space',source:'object_search',agent_id:'go2',
      run_id:'standalone-search',event_id:'standalone-step',fact_kind:'decision',stage:'search_selected',text:'独立搜索的下一步。'}})));
    await home.waitForFunction(()=>[...document.querySelectorAll('.run-process__body')]
      .some(row=>!row.closest('.space-activity') && row.textContent.includes('独立搜索的下一步。')));
    console.log('PASS: activity photos and search steps stay inside their card during a pending chat; standalone search stays in chat.');
    assert.equal(await home.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    if(process.env.SCREENSHOT_DIR)await home.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'home-activity-390.png')});
    assert.deepEqual(errors,[]);
    // The production activity result shape keeps partial joins visible even
    // though the start_activity tool itself returned ok.
    await page.evaluate(({activity,lamp})=>__render({connected:true,activity:{...activity,
      context:'我要干活了，你们自己玩吧',devices:{esp32_desk_led:{status:'joined',mode:'reactive'},go2:{status:'offline'}}},
      events:[{event_id:'partial-call',agent_id:'cloud_orchestrator',unit_id:'cloud_orchestrator',run_id:'partial-run',step_id:'partial-step',
        event_type:'action_result',tool:'start_activity',status:'ok',result_kind:'activity',ts:Date.now()/1000,
        public_result:{activity:{revision:1,members:[{unit_id:lamp.unit_id,status:'joined',mode:'reactive'},
          {unit_id:'go2',status:'offline',reason:'device_offline'}]}}}]}),{activity,lamp});
    await page.waitForFunction(()=>document.querySelector('.run-process-summary')?.textContent.includes('部分完成'));
    assert.equal(await page.$eval('.run-process-summary',el=>el.open),true);
    assert.match(await page.$eval('.run-process__notes',el=>el.textContent),/呆呆狗当前离线，未能加入活动/);
    assert.match(await page.$eval('.activity-members__footer',el=>el.textContent),/1 位伙伴参与活动/);
    assert.match(await page.$eval('.activity-members',el=>el.textContent),/未加入 · 设备离线/);
    if(process.env.SCREENSHOT_DIR){await page.setViewport({width:390,height:900});await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'activity-partial-390.png'),fullPage:true});}
    console.log('PASS: real activity card at 320/390/1280px, shared disclosure/evidence, reading position, stale feedback and ended metadata.');
  } finally {await browser.close();}
}
if(process.argv.includes('--browser')) browserChecks().catch(error=>{console.error(error);process.exitCode=1;});
