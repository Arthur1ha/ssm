/* Shared companion projection and actual browser motion; all device data is simulated.
 * NODE_PATH: sucrase/puppeteer. SSM_CHECK_REACT and SSM_CHECK_REACT_DOM: local React 18 UMD.
 * Optional PUPPETEER_EXECUTABLE_PATH and SCREENSHOT_DIR. No live API or hardware access.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = file => sucrase.transform(fs.readFileSync(file,'utf8'),{transforms:['jsx'],production:true}).code;

function projectionChecks() {
  const context=vm.createContext({window:{},Date,Map,Set,Object,Number,Array,Boolean,Math,setTimeout,clearTimeout});
  for(const file of ['app/src/utils/stateSnapshot.js','app/src/utils/agentMeta.js','app/src/avatars/registry.js','app/src/avatars/presentation.js'])
    vm.runInContext(compile(file),context,{filename:file});
  let time=1000000,nextTimer=0;
  const jobs=new Map();
  const store=context.window.createCompanionPresentation({now:()=>time,
    setTimeout:(fn,delay)=>{const id=++nextTimer;jobs.set(id,{fn,at:time+delay});return id;},
    clearTimeout:id=>jobs.delete(id)});
  const advance=ms=>{time+=ms;for(const [id,job] of [...jobs])if(job.at<=time){jobs.delete(id);job.fn();}};
  const cards=[{unit_id:'go2',agent_type:'robot',_online:true},{unit_id:'esp32_desk_led',tags:['lighting'],_online:true}];
  const state={connected:true,connection_state:'connected',activity_state:'active',life_state:'explore',epoch:'first',revision:1,ts:time/1000};
  store.syncDevices(cards,{go2:{state},esp32_desk_led:{state:{ism:'ON',r:20,g:40,b:80,brightness:120,ts:time/1000}}},true);
  assert.equal(store.getSnapshot('go2').behavior,'explore');
  store.setActivity({activity_id:'current',status:'active',revision:1});
  const emit=event=>store.ingest({ts:time/1000,...event});
  store.beginRequest('go2','first-run');
  assert.equal(store.getSnapshot('go2').behavior,'thinking');
  emit({type:'agent_reply',agent_id:'go2',target_unit_id:'esp32_desk_led',run_id:'first-run',event_id:'sse-copy',source_event_id:'original',emotion:'curious'});
  const began=store.getSnapshot('go2').animationStartedAt;
  assert.equal(store.getSnapshot('go2').emotion,'curious');
  assert.equal(store.getSnapshot('go2').behavior,'explore');
  assert.equal(store.getSnapshot('esp32_desk_led').emotion,'neutral','Reply emotion belongs to its actor, not its target');
  advance(300);
  store.finishRequest('go2','first-run',{result:'ok',reply:'done',event_id:'http-copy',source_event_id:'original',emotion:'angry'});
  assert.equal(store.getSnapshot('go2').animationStartedAt,began,'HTTP/SSE duplication must not restart a response');
  assert.equal(store.getSnapshot('go2').emotion,'curious');
  emit({type:'activity',activity_id:'current',stage:'rps_result',event_id:'round',game_round:{choices:{go2:'scissors',esp32_desk_led:'rock'},round:1}});
  emit({type:'activity',activity_id:'current',event_type:'message',agent_id:'go2',event_id:'later-emotion',emotion:'happy'});
  assert.equal(store.getSnapshot('go2').shape,'scissors');
  assert.equal(store.getSnapshot('go2').emotion,'curious','Public gesture retains its original expression while playing');
  advance(1700);
  assert.equal(store.getSnapshot('go2').shape,'idle');
  assert.equal(store.getSnapshot('go2').emotion,'happy','Gesture expiration restores the unexpired model emotion');
  assert.equal(store.getSnapshot('go2').behavior,'explore');
  advance(4500);
  assert.equal(store.getSnapshot('go2').emotion,'neutral');
  assert.equal(store.getSnapshot('go2').behavior,'explore','Emotion expiration must preserve persistent exploration');
  store.beginRequest('go2','new-run');
  emit({type:'agent_reply',agent_id:'go2',run_id:'first-run',event_id:'late-old-run',emotion:'angry'});
  assert.equal(store.getSnapshot('go2').emotion,'neutral');
  store.finishRequest('go2','new-run',{result:'ok',reply:'done',emotion:'relaxed'});
  assert.equal(store.getSnapshot('go2').emotion,'relaxed');
  emit({type:'agent_reply',agent_id:'go2',event_id:'stale',emotion:'sad',ts:1});
  assert.equal(store.getSnapshot('go2').emotion,'relaxed');
  store.setActivity({activity_id:'current',status:'ended',revision:2});
  emit({type:'activity',activity_id:'current',event_type:'message',agent_id:'go2',event_id:'ended-reply',emotion:'angry'});
  store.setActivity({activity_id:'current',status:'active',revision:1});
  emit({type:'activity',activity_id:'current',event_type:'message',agent_id:'go2',event_id:'revived-reply',emotion:'angry'});
  assert.equal(store.getSnapshot('go2').emotion,'relaxed','Ended activity cannot be revived by stale snapshots or messages');
  store.updateDevice('go2',{state:{...state,epoch:'second',revision:1,activity_state:'rest',ts:time/1000}});
  assert.equal(store.getSnapshot('go2').behavior,'rest');
  store.updateDevice('go2',{state:{...state,revision:99,ts:time/1000}});
  assert.equal(store.getSnapshot('go2').behavior,'rest','A retired epoch cannot revive exploration');
  advance(61000);
  assert.equal(store.getSnapshot('go2').online,null,'An old Go2 telemetry snapshot must become uncertain');
  assert.equal(store.getSnapshot('esp32_desk_led').online,true,'A change-only lamp does not require Go2 telemetry heartbeats');
  // Explicit turns, transport recovery and physical task aliases all share the
  // same lifecycle even when their original page has been unmounted.
  const lifecycle=context.window.createCompanionPresentation({now:()=>time,setTimeout:()=>0,clearTimeout(){}});
  const snapshot={go2:{state:{...state,ts:time/1000}},esp32_desk_led:{state:{ism:'ON'}}};
  lifecycle.syncDevices(cards,snapshot,true);
  const reply=(run_id,event_id,more={})=>lifecycle.ingest({type:'agent_reply',agent_id:'go2',run_id,event_id,ts:time/1000,...more});
  lifecycle.beginRequest('go2','resumed-turn');
  lifecycle.finishRequest('go2','resumed-turn',{result:'needs_clarification',reply:'Which one?',event_id:'question',emotion:'curious'});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'curious');
  const questionAt=time/1000;
  advance(1500);
  lifecycle.beginRequest('go2','resumed-turn');
  reply('resumed-turn','old-unseen-question',{emotion:'angry',ts:questionAt});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'neutral','Resume must reject a prior turn even with an unseen event ID');
  lifecycle.finishRequest('go2','resumed-turn',{result:'ok',reply:'Resumed',event_id:'resumed-answer',emotion:'happy'});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'happy','A resumed run may supply a new emotional reply');
  lifecycle.beginRequest('go2','without-emotion');
  reply('without-emotion','plain-answer');
  assert.equal(lifecycle.getSnapshot('go2').behavior,'explore','An optional missing emotion must not leave a request busy');
  reply('fresh-untracked-run','fresh-untracked-answer',{emotion:'relaxed'});
  reply('without-emotion','late-known-answer',{emotion:'angry'});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'relaxed','A new reply without preceding progress becomes the latest run');
  lifecycle.ingest({type:'run_stage',agent_id:'go2',run_id:'autonomous-round',event_id:'round-planning',stage:'planned',ts:time/1000});
  assert.equal(lifecycle.getSnapshot('go2').behavior,'explore','Autonomous planning retains continuous exploration');
  lifecycle.ingest({type:'agent_progress',agent_id:'go2',run_id:'autonomous-round',event_id:'round-executing',stage:'executing',ts:time/1000});
  assert.equal(lifecycle.getSnapshot('go2').behavior,'explore','Autonomous execution retains continuous exploration');
  lifecycle.beginRequest('go2','control-alias',{behavior:'working',kind:'command'});
  lifecycle.finishRequest('go2','control-alias',{result:'executing',task_id:'physical-task'});
  assert.equal(lifecycle.getSnapshot('go2').behavior,'working');
  lifecycle.ingest({type:'run_stage',agent_id:'go2',task_id:'physical-task',event_id:'physical-finished',stage:'finished',ts:time/1000});
  assert.equal(lifecycle.getSnapshot('go2').behavior,'explore','Physical task feedback terminates its originating control request');
  lifecycle.beginRequest('go2','inactive-unknown');
  lifecycle.finishRequest('go2','inactive-unknown',{result:'unknown',error:'receipt_unavailable',execution_active:false});
  assert.equal(lifecycle.getSnapshot('go2').behavior,'explore','Known inactive work must stop busy presentation even if its result is unknown');
  lifecycle.ingest({type:'peer_message',sender:'esp32_desk_led',target:'go2',event_id:'peer-emotion',emotion:'curious',ts:time/1000});
  assert.equal(lifecycle.getSnapshot('esp32_desk_led').emotion,'curious','Peer expressions belong to their explicit sender');
  assert.equal(lifecycle.getSnapshot('go2').emotion,'neutral');
  lifecycle.updateDevice('go2',{health:'reconnecting'});
  assert.equal(lifecycle.getSnapshot('go2').online,null);
  lifecycle.syncDevices(cards,snapshot,true);
  assert.equal(lifecycle.getSnapshot('go2').online,true,'A fresh global source recovers page-local stream health');
  const client=new EventTarget();client.eventsConnected=true;client.devicesSynced=true;
  const detach=lifecycle.attach(client),detachDuplicate=lifecycle.attach(client);
  lifecycle.beginRequest('go2','pending-http',{behavior:'working',kind:'command'});
  const beforeReconnect=time/1000;
  client.eventsConnected=false;client.dispatchEvent(new Event('events-error'));
  assert.equal(lifecycle.getSnapshot('go2').online,null);
  advance(2000);
  client.eventsConnected=true;client.devicesSynced=false;client.eventsOpenedAt=time;
  client.dispatchEvent(new Event('events-open'));
  client.devicesSynced=true;client.dispatchEvent(new Event('devices-synced'));
  assert.equal(lifecycle.getSnapshot('go2').behavior,'working','Reconnect preserves an explicit HTTP request still awaiting its receipt');
  lifecycle.ingest({type:'agent_reply',agent_id:'go2',run_id:'pending-http',event_id:'unseen-before-reconnect',emotion:'angry',ts:beforeReconnect});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'neutral','Unseen pre-reconnect events cannot restart an old reaction');
  lifecycle.ingest({type:'agent_reply',payload:{agent_id:'go2',run_id:'pending-http',event_id:'wrapped-reply',emotion:'happy',ts:time/1000}});
  assert.equal(lifecycle.getSnapshot('go2').emotion,'happy','Known event envelopes preserve their outer type');
  detach();detachDuplicate();lifecycle.reset();
  store.reset();
}

async function commandProjectionChecks() {
  const context=vm.createContext({window:{},Date,Map,Set,Object,Number,Array,Boolean,Math,setTimeout,clearTimeout});
  for(const file of ['app/src/utils/stateSnapshot.js','app/src/utils/agentMeta.js','app/src/avatars/registry.js','app/src/avatars/presentation.js'])
    vm.runInContext(compile(file),context,{filename:file});
  const store=context.window.createCompanionPresentation();
  store.syncDevices([{unit_id:'go2',agent_type:'robot',_online:true}],{go2:{state:{connected:true,
    activity_state:'active',life_state:'explore',ts:Date.now()/1000}}},true);
  const requests=[];
  let resolveReply;
  const client={request:(_path,options)=>{requests.push(options);return new Promise(resolve=>{resolveReply=resolve;});}};
  const adapter=vm.createContext({companionPresentation:store,apiClient:client,AbortController,setTimeout,clearTimeout,
    crypto:require('node:crypto')});
  vm.runInContext(fs.readFileSync('app/src/CommandBus.js','utf8')+'\nthis.bus=commandBus;',adapter);
  const pending=adapter.bus.command('go2','chat',{message:'看看前方'});
  assert.equal(store.getSnapshot('go2').behavior,'thinking');
  const runId=requests[0].body.params.task_id;
  assert.match(runId,/^go2_/,'Presentation and transport must share the generated chat run');
  store.ingest({type:'agent_reply',agent_id:'go2',run_id:runId,event_id:'reply',emotion:'curious',ts:Date.now()/1000});
  const result={result:'ok',reply:'看到了',event_id:'reply',emotion:'angry'};
  resolveReply(result);
  assert.strictEqual(await pending,result,'Presentation may not change the device result');
  assert.equal(store.getSnapshot('go2').emotion,'curious','HTTP must not replace the same SSE reply');
  assert.equal(store.getSnapshot('go2').behavior,'explore');
  const physical=adapter.bus.command('go2','SPORT',{cmd:'Hello'});
  assert.equal(store.getSnapshot('go2').behavior,'working');
  resolveReply({result:'accepted'});
  await physical;
  assert.equal(store.getSnapshot('go2').behavior,'explore','Accepted results retain their existing success semantics');
  store.beginRequest('go2','ongoing-chat');
  const velocity=adapter.bus.command('go2','move',{vx:.1,vy:0,vyaw:0});
  resolveReply({result:'ok'});
  await velocity;
  assert.equal(store.getSnapshot('go2').behavior,'thinking','Velocity frames must not replace an ongoing conversation');
  store.reset();
}

async function browserChecks() {
  const browser=await puppeteer.launch({headless:true,executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page=await browser.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request',request=>request.url().startsWith('data:')||request.url()==='about:blank'?request.continue():request.abort());
    await page.setViewport({width:390,height:1000});
    await page.setContent('<div id="root"></div>');
    for(const file of ['app/styles/app.css','app/styles/avatars.css'])await page.addStyleTag({path:file});
    for(const file of [process.env.SSM_CHECK_REACT,process.env.SSM_CHECK_REACT_DOM])await page.addScriptTag({path:file});
    for(const file of ['utils/stateMachine.js','utils/stateSnapshot.js','utils/color.js','utils/agentMeta.js','utils/activityView.js',
      'avatars/registry.js','avatars/presentation.js','avatars/Avatar.jsx','avatars/CompanionAvatar.jsx','components/Icon.jsx','components/DeviceCard.jsx','components/ActivityMembers.jsx'])
      await page.addScriptTag({content:compile('app/src/'+file)});
    await page.evaluate(()=>{
      window.apiClient=new EventTarget();apiClient.eventsConnected=true;apiClient.devicesSynced=true;
      window.navigate=()=>{};
      window.cards=[{unit_id:'go2',name:'Go2',agent_type:'robot',_online:true},
        {unit_id:'esp32_desk_led',name:'灯',agent_type:'actuator',tags:['lighting'],_online:true}];
      window.live={go2:{state:{connected:true,connection_state:'connected',activity_state:'active',life_state:'explore',epoch:'browser',revision:1,ts:Date.now()/1000}},
        esp32_desk_led:{state:{ism:'ON',r:30,g:80,b:200,brightness:170,ts:Date.now()/1000}}};
      window.activity={activity_id:'browser-activity',status:'active',devices:{go2:{status:'joined'}}};
      companionPresentation.syncDevices(cards,live,true);companionPresentation.setActivity(activity);
      window.root=ReactDOM.createRoot(document.getElementById('root'));
      window.mountKey=0;
      window.renderSurfaces=()=>ReactDOM.flushSync(()=>root.render(React.createElement('div',null,
        React.createElement('section',{id:'home'},React.createElement(HomeDeviceRow,{agent:cards[0],unitData:live,connected:true})),
        React.createElement('section',{id:'device'},React.createElement(CompanionAvatar,{key:mountKey,unitId:'go2',size:48})),
        React.createElement(ActivityMembers,{activity,events:[],agents:cards,unitData:live,connected:true}),
        React.createElement('section',{id:'history'},React.createElement(CompanionAvatar,{unitId:'go2',mode:'history',snapshot:{emotion:'sad',shape:'paper'},size:36})),
        React.createElement('section',{id:'lamp'},React.createElement(CompanionAvatar,{unitId:'esp32_desk_led',size:48})))));
      window.emit=event=>apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{ts:Date.now()/1000,...event}}));
      renderSurfaces();
    });
    const liveFaces=['#home .shape-face','#device .shape-face','.activity-members .shape-face'];
    await page.waitForSelector('#device .shape-face[data-behavior="explore"]');
    assert.deepEqual(await Promise.all(liveFaces.map(selector=>page.$eval(selector,node=>Number(node.getAttribute('width'))))),[36,48,96]);
    for(const selector of liveFaces){
      assert.equal(await page.$eval(selector,node=>node.dataset.behavior),'explore');
      assert.equal(await page.$eval(selector+' .shape-face__behavior',node=>getComputedStyle(node).animationName),'avatar-explore');
    }
    await page.evaluate(()=>{
      companionPresentation.beginRequest('go2','browser-run');
      emit({type:'agent_reply',agent_id:'go2',run_id:'browser-run',event_id:'emotion',emotion:'curious'});
    });
    await page.waitForSelector('#device .shape-face[data-emotion="curious"]');
    for(const selector of liveFaces)assert.equal(await page.$eval(selector,node=>node.dataset.emotion),'curious');
    assert.equal(await page.$eval('#history .shape-face',node=>node.dataset.emotion),'sad');
    assert.equal(await page.$eval('#history .shape-face',node=>node.dataset.animated),'false');
    await page.evaluate(()=>{mountKey++;renderSurfaces();});
    await page.waitForSelector('#device .shape-face[data-emotion="curious"]');
    assert.equal(await page.$eval('#device .shape-face',node=>node.dataset.behavior),'explore');
    await page.evaluate(()=>companionPresentation.updateDevice('go2',{state:{...live.go2.state,activity_state:'rest',revision:2,ts:Date.now()/1000}}));
    await page.waitForSelector('#device .shape-face[data-behavior="rest"]');
    assert.equal(await page.$eval('#device .shape-face__expression[data-current="true"] .shape-face__eyes',node=>node.dataset.closed),'true');
    const identityColor=await page.$eval('#lamp .shape-face',node=>node.style.getPropertyValue('--avatar-color'));
    assert.equal(await page.$eval('#lamp .shape-face__light',node=>getComputedStyle(node).fill),'rgb(30, 80, 200)');
    await page.evaluate(()=>companionPresentation.updateDevice('esp32_desk_led',{state:{ism:'OFF',ts:Date.now()/1000+1}}));
    await page.waitForFunction(()=>!document.querySelector('#lamp .shape-face__light'));
    assert.equal(await page.$eval('#lamp .shape-face',node=>node.dataset.animated),'true');
    assert.equal(await page.$eval('#lamp .shape-face',node=>node.style.getPropertyValue('--avatar-color')),identityColor);

    // Measure painted movement, not just animation-name attributes, at all actual sizes.
    for(const size of [36,48,96]){
      await page.evaluate(size=>ReactDOM.flushSync(()=>root.render(React.createElement(ShapeFace,{unitId:'go2',size,animated:true,online:true}))),size);
      const delta=await page.$eval('.shape-face',node=>{
        const ambient=node.querySelector('.shape-face__ambient');
        const animation=ambient.getAnimations()[0];animation.pause();animation.currentTime=0;
        const first=node.querySelector('.shape-face__body').getBoundingClientRect();
        animation.currentTime=2000;
        const second=node.querySelector('.shape-face__body').getBoundingClientRect();
        return Math.abs(first.top-second.top);
      });
      assert(delta>1,`Idle motion should be perceptible at ${size}px; got ${delta}px`);
    }
    // The same event remounted midway seeks into its motion instead of starting again.
    await page.evaluate(()=>{
      window.motionStart=Date.now()-350;
      window.renderTimed=key=>ReactDOM.flushSync(()=>root.render(React.createElement(ShapeFace,{key,unitId:'go2',size:48,animated:true,online:true,
        emotion:'happy',animationKey:'timed',animationStartedAt:motionStart,motionDurationMs:1200,motionEpoch:motionStart})));
      renderTimed('first');
    });
    await page.waitForSelector('[data-motion="pop"]');
    await page.evaluate(()=>renderTimed('second'));
    await page.waitForSelector('[data-motion="pop"]');
    const delay=await page.$eval('.shape-face__form',node=>parseFloat(getComputedStyle(node).animationDelay));
    assert(delay<=-.35,'Remounted response motion must seek to elapsed time');
    await page.evaluate(()=>{motionStart=Date.now()-2000;renderTimed('expired');});
    await page.waitForSelector('[data-motion="none"]');
    // A paused avatar rejoins the unit's ongoing loop rather than lagging behind it.
    await page.$eval('#root',node=>node.style.marginTop='2000px');
    await page.waitForSelector('.shape-face[data-paused="true"]');
    await new Promise(resolve=>setTimeout(resolve,150));
    await page.$eval('#root',node=>node.style.marginTop='0');
    await page.waitForSelector('.shape-face:not([data-paused])');
    const phaseError=await page.$eval('.shape-face__ambient',node=>{
      const animation=node.getAnimations()[0];
      const elapsed=Date.now()-motionStart;
      return Math.abs((animation.currentTime-Number(animation.effect.getTiming().delay))-elapsed);
    });
    assert(phaseError<100,`Resumed loop diverged from shared clock by ${phaseError}ms`);
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);
    assert.equal(await page.$eval('.shape-face__ambient',node=>getComputedStyle(node).animationName),'none');
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'no-preference'}]);
    await page.evaluate(()=>ReactDOM.flushSync(()=>root.render(React.createElement(ShapeFace,{unitId:'go2',size:48,animated:true,online:false}))));
    assert.equal(await page.$eval('.shape-face',node=>node.dataset.animated),'false');
    assert.match(await page.$eval('.shape-face',node=>getComputedStyle(node).transitionProperty),/filter/);
    for(const width of [320,390]){
      await page.setViewport({width,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    if(process.env.SCREENSHOT_DIR){
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.evaluate(()=>renderSurfaces());
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'companion-surfaces.png'),fullPage:true});
    }
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
}

(async()=>{projectionChecks();await commandProjectionChecks();await browserChecks();console.log('PASS: shared home/device/activity state and emotion, command transport lifecycle, actor identity, event expiry and deduplication, epoch retirement, activity invalidation, history, lighting, visible small-size motion, remount seeking, reduced motion and offline behavior.');})()
  .catch(error=>{console.error(error);process.exitCode=1;});
