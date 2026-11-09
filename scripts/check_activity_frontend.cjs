/* Real activity card, mocked API/device feedback. Never contacts live devices.
 * NODE_PATH must expose sucrase and puppeteer. Set PUPPETEER_EXECUTABLE_PATH,
 * SSM_CHECK_REACT and SSM_CHECK_REACT_DOM; SCREENSHOT_DIR is optional. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const compile = name => sucrase.transform(fs.readFileSync(name,'utf8'),{transforms:['jsx'],production:true}).code;
const files = ['utils/stateMachine.js','utils/agentMeta.js','utils/color.js','utils/activityView.js',
  'components/Icon.jsx','components/ActivityDetails.jsx','components/ActivityFeed.jsx'];
const context = vm.createContext({window:{}});
for (const file of files) {
  vm.runInContext(compile('app/src/'+file),context);
  Object.assign(context,context.window);
}
const {activityMemberView,activityTaskRows,activityRelatedEvents,activityMessageReceipt,activityFeedbackChange,
  activityDecisionBasis,activityDecisionText,activityDecisionEvidence,projectActivityEvents} = context;
const compactEvents=context.compactActiveActivityEvents;
const replay=process.env.SSM_ACTIVITY_REPLAY ? JSON.parse(fs.readFileSync(process.env.SSM_ACTIVITY_REPLAY,'utf8')) : null;
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
assert.equal(activityDecisionText(groundedInvite,invite.text),decisionBasis.reason+'\n'+invite.text);
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
assert.equal(activityDecisionBasis(activityTaskRows([
  {...planned,decision_basis:decisionBasis},result])[0]).reason,decisionBasis.reason,
  'An explicitly linked receipt erased the original selection basis');
assert.equal(activityMessageReceipt(invite,[invite]),'等待对方决定');
assert.equal(activityMessageReceipt(invite,events),'对方已接受');
assert.equal(activityMemberView(go2,states.go2.state,{status:'joined'},true,now).label,'玩耍 · 站立');
assert.equal(activityMemberView(lamp,states.esp32_desk_led.state,{status:'joined'},true,now).label,'常亮 · 蓝色');
assert.equal(activityMemberView(lamp,{...states.esp32_desk_led.state,_received_at:now-7200},{status:'joined'},true,now).label,
  '常亮 · 蓝色','An unchanged lamp setting was replaced by a freshness warning');
assert.equal(activityMemberView(lamp,{ism:'OFF'},{status:'joined'},true,now).label,'熄灭');
assert.equal(activityMemberView(go2,{...states.go2.state,_received_at:now-70},{status:'joined'},true,now).label,'状态待确认');
assert.equal(activityMemberView(go2,states.go2.state,{status:'joined'},false,now).label,'状态待确认');
assert.equal(activityMemberView(go2,{...states.go2.state,body_telemetry_fresh:false},{status:'joined'},true,now).label,'玩耍 · 身体待确认');
assert.equal(activityTaskRows([planned,running,result]).length,1);
assert.equal(activityTaskRows([result,running])[0].status,'timeout','A progress replay downgraded a receipt');
assert.equal(activityTaskRows([result,{...result,event_id:'other',action:'go2_observe'}]).length,2,'Different actions in a run were merged');
assert.equal(activityTaskRows([result,{...result,event_id:'repeated'}]).length,2,'Repeated receipts were erased');
assert.equal(activityRelatedEvents(result,events).messages.length,2);
assert.equal(activityRelatedEvents(invite,events).task.length,3);
assert.equal(activityRelatedEvents({...result,task_id:'unlinked',trigger_message_id:null},events).messages.length,0,
  'Nearby conversation was falsely presented as the cause');
assert.equal(projectActivityEvents([planned,{...result,action:'go2_observe'}]).length,2,
  'Another action in the same task hid the plan');
const lampPlan=event('lamp-plan','action',{unit_id:lamp.unit_id,task_id:'light-task',action:'set_led_color',
  state_at_event:{status:'current',as_of:now-10,values:{ism:'ON',r:255,g:200,b:80}}});
const lampResult=event('lamp-result','action_result',{unit_id:lamp.unit_id,task_id:'light-task',action:'set_led_color',status:'ok',
  state_at_event:{status:'current',as_of:now-9,values:{ism:'ON',r:0,g:0,b:255}}});
assert.equal(activityFeedbackChange(lampResult,[lampPlan,lampResult],lamp),'设备反馈：常亮 · 暖白 → 常亮 · 蓝色');
assert.equal(activityFeedbackChange({...lampResult,status:'timeout'},[lampPlan],lamp),'');
const lightingReceipts=[{...lampResult,trigger_message_id:'msg_reply'},
  {...lampResult,event_id:'game-confirmation',text:'游戏出题颜色已确认'},
  {...lampResult,event_id:'peer-ack',event_type:'action_progress',action:undefined,status:'accepted',text:'设备已接收动作请求'},
  {...lampResult,event_id:'peer-result',action:undefined,text:'ok'}];
const dedupedLighting=activityTaskRows(projectActivityEvents(lightingReceipts));
assert.equal(dedupedLighting.length,1,'Graph, game and peer receipts became separate lamp actions');
assert.equal(dedupedLighting[0].trigger_message_id,'msg_reply','Game confirmation erased the causal link');
const powerOn={...lampResult,event_id:'power-on',action:'set_led_state',task_id:'power-task',trigger_message_id:'msg_reply'};
const oneLight=compactEvents(activityTaskRows(projectActivityEvents([powerOn,...lightingReceipts])));
assert.equal(oneLight.length,1,'Power-on and color for one request were not summarized');
assert.equal(oneLight[0].related_task_ids.length,2,'The summarized light lost its physical execution history');
assert.equal(activityRelatedEvents(oneLight[0],[powerOn,...lightingReceipts]).task.length,5);
if(replay){
  const visible=compactEvents(activityTaskRows(projectActivityEvents(replay.events)));
  const lighting=visible.filter(item=>item.event_type==='action_result' && context.isActivityLightingAction(item.action));
  assert.equal(lighting.length,1,'Latest real activity still repeats the lamp setup');
  assert.equal(lighting[0].related_task_ids.length,2,'Latest real setup should retain power and color tasks');
  assert.equal(visible.filter(item=>item.event_type==='action_progress' && !item.action && item.status==='accepted').length,0);
  assert.equal(visible.filter(item=>item.event_type==='action_result' && !item.action).length,0);
  console.log(`PASS: latest recorded activity replay (${replay.events.length} raw events, one lamp setup summary).`);
}
console.log('PASS: explicit message/task relationships, physical freshness, action grouping and unconfirmed results.');

async function browserChecks() {
  const puppeteer = require('puppeteer');
  if(process.env.SCREENSHOT_DIR) fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
  const browser = await puppeteer.launch({headless:true,executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,
    protocolTimeout:45000,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
  try {
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
    await page.addStyleTag({path:'app/styles/app.css'});
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
    assert.equal(await page.$$('.space-activity__action').then(rows=>rows.length),1);
    assert(await page.$eval('.space-activity__action',e=>e.textContent.includes('结果还没有确认')));
    assert.equal(await page.$eval('.space-activity__action',e=>getComputedStyle(e).borderTopWidth),'0px');
    assert.equal(await page.$eval('.space-activity__action',e=>getComputedStyle(e).backgroundColor),'rgba(0, 0, 0, 0)');
    assert(await page.$eval('.space-activity',e=>e.textContent.includes('对方已接受')));
    for(const width of [320,390,1280]) {
      console.log('Browser: checking width',width);
      await page.setViewport({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Horizontal overflow');
      const open = await page.$('.space-activity__action .space-activity__evidence-link');
      await open.click();
      await page.waitForSelector('[role="dialog"]');
      assert(await page.$eval('.activity-evidence',e=>e.textContent.includes('停止结果尚未确认')));
      assert.equal(await page.$$('.activity-evidence blockquote').then(nodes=>nodes.length),2);
      assert.equal(await page.$eval('.activity-evidence',e=>e.scrollWidth<=e.clientWidth),true);
      assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('.activity-evidence'))),true);
      await page.keyboard.down('Shift');
      await page.keyboard.press('Tab');
      await page.keyboard.up('Shift');
      assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('.activity-evidence'))),true);
      if(process.env.SCREENSHOT_DIR && width===390) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'activity-evidence-390.png'),fullPage:true});
      }
      await page.keyboard.press('Escape');
      await page.waitForFunction(()=>!document.querySelector('[role="dialog"]'));
      assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('space-activity__evidence-link')),true,
        'Focus was not restored to the evidence button');
      if(process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`activity-card-${width}.png`),fullPage:true});
    }
    // A reasoned invitation has evidence before any task or acceptance exists.
    await page.evaluate(({invite,basis})=>__render({events:[{...invite,decision_basis:basis}]}),
      {invite:groundedInvite,basis:decisionBasis});
    await page.waitForSelector('[data-decision-reason]');
    assert.equal(await page.$eval('[data-decision-reason]',el=>el.textContent),decisionBasis.reason+'\n'+invite.text);
    assert(await page.$eval('[data-decision-reason]',el=>el.classList.contains('space-activity__bubble')),
      'The main decision was reduced to a caption instead of agent dialogue');
    assert.equal(await page.$eval('[data-decision-reason]',el=>
      el.parentElement.querySelector('.space-activity__speaker').textContent.includes('打算')),false,
      'A new intention heading changed the existing speaker hierarchy');
    assert.equal(await page.$$('.space-activity__bubble').then(els=>els.length),1,
      'A separate reason bubble changed the number of dialogue entries');
    assert.equal(await page.$$('.space-activity__item').then(els=>els.length),1);
    assert.equal(await page.$$eval('[data-decision-reason]',els=>els.length),1);
    await page.click('.space-activity__message-meta .space-activity__evidence-link');
    await page.waitForSelector('[data-decision-evidence]');
    assert(await page.$eval('[data-decision-evidence]',el=>el.textContent.includes('白色猫头鹰')));
    assert(await page.$eval('[data-decision-evidence]',el=>el.textContent.includes('来源原记录未保留')));
    assert(await page.$eval('.activity-evidence',el=>el.textContent.includes('不能据此确认设备已经行动')));
    if(process.env.SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'decision-evidence.png'),fullPage:true});
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('[role="dialog"]'));
    // Color and candidate reasoning stays in the existing action row with its real receipt.
    const colorBasis={...decisionBasis,reason:'刚才的观察里有白色玩偶，我想用白色出题，让呆呆狗自己挑一个同色目标。'};
    const candidateBasis={...decisionBasis,reason:'我刚看到了地上的白色猫头鹰，想先检查能不能安全靠近它。'};
    await page.evaluate(({lampResult,result,colorBasis,candidateBasis})=>__render({events:[
      {...lampResult,decision_basis:colorBasis},{...result,status:'executing',decision_basis:candidateBasis}]}),
      {lampResult,result,colorBasis,candidateBasis});
    await page.waitForFunction(()=>document.querySelectorAll('[data-decision-reason]').length===2);
    assert(await page.$$eval('[data-decision-reason]',els=>els.every(el=>el.tagName==='P')));
    assert(await page.$eval('[data-decision-reason]',el=>el.textContent.startsWith('刚才的观察里有白色玩偶')));
    assert.equal(await page.$$('.space-activity__bubble').then(els=>els.length),0,
      'Action reasoning added dialogue layers instead of using the original action line');
    assert.equal(await page.$$('.space-activity__item').then(els=>els.length),2);
    assert.equal(await page.$$('.space-activity__action').then(els=>els.length),2);
    assert(await page.$$eval('[data-decision-reason]',els=>els.every(el=>
      el.parentElement.classList.contains('space-activity__action'))));
    // A message that already speaks the reason should not duplicate the bubble.
    await page.evaluate(({invite,basis})=>__render({events:[{...invite,text:basis.reason,decision_basis:basis}]}),
      {invite:groundedInvite,basis:decisionBasis});
    await page.waitForFunction(()=>document.querySelectorAll('[data-decision-reason]').length===1);
    assert.equal(await page.$$('.space-activity__bubble').then(els=>els.length),1);
    // Restored historical records retain their evidence, including stale notices.
    await page.evaluate(({invite,basis})=>__render({activity:{...__props.activity,status:'ended'},
      events:[{...invite,decision_basis:{...basis,evidence:basis.evidence.map(row=>({...row,as_of:1}))}}]}),
      {invite:groundedInvite,basis:decisionBasis});
    await page.click('.space-activity__toggle');
    await page.waitForSelector('[data-decision-reason]');
    await page.click('.space-activity__message-meta .space-activity__evidence-link');
    await page.waitForSelector('[data-decision-evidence]');
    assert(await page.$eval('[data-decision-evidence]',el=>el.textContent.includes('决策时已过期')));
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('[role="dialog"]'));
    await page.click('.space-activity__toggle');
    await page.evaluate(({activity,events})=>__render({activity,events}),{activity,events});
    await page.waitForSelector('.space-activity__action');
    if(replay){
      await page.setViewport({width:390,height:900});
      await page.evaluate(replay=>{
        window.__beforeReplay=__props;
        const lampRows=replay.events.filter(item=>item.unit_id==='esp32_desk_led' && item.state_at_event?.values?.ism);
        const last=lampRows[lampRows.length-1].state_at_event;
        __render({activity:replay.activity,events:replay.events,connected:true,
          unitData:{...__props.unitData,esp32_desk_led:{state:{...last.values,_received_at:last.as_of}}}});
        document.querySelector('.test-shell>h1').textContent='最新活动日志回放';
      },replay);
      if(replay.activity.status==='ended'){
        await page.waitForSelector('.space-activity--ended');
        await page.click('.space-activity__toggle');
      }
      await page.waitForFunction(()=>[...document.querySelectorAll('.space-activity__action p')]
        .filter(e=>e.textContent.includes('已调为常亮 · 白光')).length===1);
      assert.equal(await page.$$('.space-activity__task').then(rows=>rows.length),0);
      assert.equal(await page.$eval('.space-activity',e=>e.textContent.includes('设备已接收动作请求')),false);
      assert.equal(await page.$$eval('.space-activity__action',rows=>rows.every(e=>
        getComputedStyle(e).borderTopWidth==='0px' && getComputedStyle(e).backgroundColor==='rgba(0, 0, 0, 0)')),true);
      assert(await page.$eval('.activity-members',e=>e.textContent.includes('常亮 · 白光')));
      if(process.env.SCREENSHOT_DIR){
        await page.$eval('.space-activity__timeline',e=>{e.scrollTop=0;});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'latest-log-top-390.png'),fullPage:true});
        await page.evaluate(()=>{
          const timeline=document.querySelector('.space-activity__timeline');
          const setup=[...timeline.querySelectorAll('.space-activity__action p')].find(e=>e.textContent.includes('已调为常亮 · 白光'));
          const position=setup.getBoundingClientRect().top-timeline.getBoundingClientRect().top+timeline.scrollTop;
          timeline.scrollTop=Math.max(0,position-180);
        });
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'latest-log-setup-390.png'),fullPage:true});
        await page.$eval('.space-activity__timeline',e=>{e.scrollTop=e.scrollHeight;});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'latest-log-results-390.png'),fullPage:true});
      }
      await page.evaluate(()=>__render(__beforeReplay));
      await page.waitForFunction(()=>document.querySelector('.activity-member__state')?.textContent.includes('玩耍'));
      console.log('Browser: latest real log renders one lamp setup, no transport acknowledgements or action boxes.');
    }
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
      else if(request.url().startsWith('data:'))request.continue();
      else if(request.url().endsWith('/favicon.ico'))request.respond({status:204,body:''});
      else {errors.push('Unexpected home network: '+request.url());request.abort();}
    });
    await home.setViewport({width:390,height:844});await home.goto('http://ssm.test/');
    await home.addStyleTag({path:'app/styles/app.css'});
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
        throw Error('Unexpected API: '+endpoint);
      };
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App,{
        session:{actor:'admin',space_name:'工作空间'},logout(){},refreshSession:async()=>{}}));
    },{activity,events,go2,lamp,states});
    await home.waitForSelector('.space-activity .activity-member');
    assert.equal(await home.$$('.space-activity').then(nodes=>nodes.length),1);
    assert.equal(await home.evaluate(()=>location.hash),'');
    assert(await home.$eval('.home-activity-input-note',e=>e.textContent.includes('发送新指令')));
    assert.equal(await home.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    if(process.env.SCREENSHOT_DIR)await home.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'home-activity-390.png')});
    assert.deepEqual(errors,[]);
    console.log('PASS: real activity card at 320/390/1280px, explicit evidence, focus/escape, reading position, stale feedback and ended metadata.');
  } finally {await browser.close();}
}
if(process.argv.includes('--browser')) browserChecks().catch(error=>{console.error(error);process.exitCode=1;});
