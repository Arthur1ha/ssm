/* Shared conversation regression with simulated APIs. No live devices.
 * NODE_PATH: sucrase/puppeteer; SSM_CHECK_REACT and SSM_CHECK_REACT_DOM:
 * local React 18 UMD files. Optional PUPPETEER_EXECUTABLE_PATH. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = file => sucrase.transform(fs.readFileSync(file, 'utf8'), {transforms:['jsx'], production:true}).code;
(async () => {
  const browser = await puppeteer.launch({headless:true, executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,
    args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => {errors.push(error.message); console.error('Chat page error:', error.message);});
    await page.setRequestInterception(true);
    const apiHeaders = [];
    page.on('request', request => {
      if (/\/api\/.*photo/.test(request.url())) return request.respond({status:200,contentType:'image/svg+xml',
        body:'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#475569"/><text x="24" y="94" fill="white" font-size="20">SIMULATED OBSERVATION</text></svg>'});
      if (new URL(request.url()).pathname.startsWith('/api/')) {
        apiHeaders.push(request.headers());
        return request.respond({status:200,contentType:'application/json',body:JSON.stringify({session_id:'login-kept'})});
      }
      return request.respond({status:200, contentType:'text/html',
      body:'<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>'});
    });
    await page.goto('http://localhost/');
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM]) await page.addScriptTag({path:file});
    for (const style of ['app','process','avatars']) await page.addStyleTag({path:`app/styles/${style}.css`});
    if (process.env.SSM_CHECK_FONT) {
      await page.evaluate(async data=>{const f=new FontFace('PresentationCJK',`url(data:font/otf;base64,${data})`);await f.load();document.fonts.add(f);},fs.readFileSync(process.env.SSM_CHECK_FONT).toString('base64'));
      await page.addStyleTag({content:':root{--font-sans:PresentationCJK,sans-serif;--font-mono:PresentationCJK,monospace}input,button{font-family:inherit}'});
    }
    for (const file of ['utils/stateMachine.js','utils/stateSnapshot.js','utils/agentMeta.js','utils/activityView.js','utils/runProcess.js','avatars/registry.js','avatars/presentation.js',
      'components/Icon.jsx','avatars/Avatar.jsx','avatars/CompanionAvatar.jsx','hooks/useTraining.js','components/TrainingPreview.jsx','components/ActivityMembers.jsx','components/RunProcess.jsx',
      'components/ChatMessages.jsx','components/ChatPanel.jsx','components/ActivityFeed.jsx','pages/FsmDevicePage.jsx']) {
      await page.addScriptTag({content:compile('app/src/'+file)});
    }
    await page.evaluate(() => {
      window.apiClient = new EventTarget();
      apiClient.request = async () => ({records:[],proposals:[],edge_annotations:[]});
      window.apiErrorText = error => error.message;
      window.sent = [];
      window.finishCommands = new Map();
      window.commandBus = {describe: result => result.result, command: async (_unit, _action, params) => {
        sent.push(params);
        const runId = params.run_id || params.task_id;
        companionPresentation.beginRequest(_unit, runId, {kind:'chat',behavior:'thinking'});
        return new Promise(resolve => { window.finishCommand = result => {
          companionPresentation.finishRequest(_unit, runId, result); resolve(result);
        }; finishCommands.set(runId,window.finishCommand); });
      }};
      window.DeviceStateBoard = () => React.createElement('div', {style:{height:120}}, '设备状态');
      window.lamp = {unit_id:'esp32_desk_led', name:'皮皮灯', _online:true, transport:{},
        state_machine:{state_field:'ism',states:['OFF','ON','BLINKING'],transitions:[],views:{}},
        modes:[],widgets:[],skills:[],suggestions:['调成暖白色']};
      window.root = ReactDOM.createRoot(document.getElementById('root'));
      window.device = (online = true, key = 'device') => root.render(React.createElement(FsmDevicePage, {
        key, unitId:lamp.unit_id, device:{...lamp,_online:online}, liveState:{state:{ism:'ON',ts:Date.now()/1000}},
        actor:'visitor', sessionId:'chat-test', onBack() {},
      }));
      sessionStorage.setItem('ssm_chat:chat-test:device:esp32_desk_led', JSON.stringify([
        {role:'user',text:'旧请求',requestKey:'old'},
        {role:'process',text:'旧过程',requestKey:'old'},
        {role:'process',text:'处理结束',requestKey:'old',terminal:true},
        {role:'assistant',text:'旧回复',requestKey:'old',final:true,agent:lamp.unit_id},
      ]));
      device();
    });
    await page.waitForSelector('.chat-panel input');
    assert.equal(await page.$('.device-page__header .shape-face'), null, 'The device header must not duplicate the input avatar');
    assert.equal(await page.$$('.chat-input__avatar .shape-face').then(nodes => nodes.length), 1);
    let deviceInputHeight;
    for (const [width, height] of [[320,568],[390,844],[390,520]]) {
      await page.setViewport({width,height});
      await page.$eval('input', input => input.blur());
      await page.waitForFunction(() => document.querySelector('.chat-input__avatar .shape-face').getBoundingClientRect().width === 48);
      const before = await page.$eval('.chat-input', node => ({
        height:node.getBoundingClientRect().height,
        bottom:node.getBoundingClientRect().bottom,
        board:document.querySelector('.device-state-workspace').getBoundingClientRect().height,
      }));
      deviceInputHeight = before.height;
      assert(before.bottom <= height, 'The companion input must remain visible on a short viewport');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.focus('input');
      await page.waitForFunction(() => document.querySelector('.chat-input__avatar .shape-face').getBoundingClientRect().width === 36);
      assert.equal(await page.$eval('.chat-input', node => node.getBoundingClientRect().height), before.height,
        'Focusing the input must not increase the composer height');
      assert.equal(await page.$eval('.device-state-workspace', node => node.getBoundingClientRect().height), before.board,
        'The avatar must not take height from the whiteboard');
    }
    await page.$eval('input', input => input.blur());
    await page.waitForFunction(() => document.querySelector('.chat-input__avatar .shape-face').getBoundingClientRect().width === 48);
    assert.match(await page.$eval('.chat-input__avatar .shape-face', node => getComputedStyle(node).transitionProperty), /filter/,
      'Companion sizing must preserve the avatar offline fade');
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);
    assert.equal(await page.$eval('.chat-input__avatar', node => getComputedStyle(node).transitionDuration), '0s');
    assert.equal(await page.$eval('.chat-input__avatar .shape-face__ambient', node => getComputedStyle(node).animationName), 'none');
    await page.emulateMediaFeatures([]);
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.setViewport({width:390,height:844});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/device-companion-input-390.png'});
    }
    assert.equal(await page.$eval('.chat-messages', node => node.textContent.includes('旧回复')), false,
      'A fresh page must not restore the old sessionStorage transcript');
    // Send through the real device page and observe actual SSE projection.
    await page.type('.chat-panel input', '调成暖白色');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 1);
    assert.equal(await page.$eval('input', node => node.disabled), false,
      'A running goal must allow a new user request');
    await page.evaluate(() => {
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_progress',
        agent_id:lamp.unit_id,run_id:sent[0].run_id,source:'device_chat',event_id:'cargo-observing',
        stage:'dispatching',tool:'go2_push_cargo',arguments:{operation:'assess'},
        step_id:'cargo-assess',ts:Date.now()/1000-16}}));
    });
    await page.waitForFunction(() => document.querySelector('.run-process__waiting')?.textContent.includes('正在观察货物与目的地'));
    assert.match(await page.$eval('.run-process__waiting',node=>node.textContent),/已等待 1[6-9] 秒/);
    assert.equal(await page.$eval('details',node=>node.open),false,
      'The current step and elapsed wait must be visible with details closed');
    await page.setViewport({width:320,height:568});
    assert(await page.$eval('.run-process__waiting',node=>{
      const rect=node.getBoundingClientRect();
      return rect.left>=0 && rect.right<=innerWidth;
    }),'Current phase and elapsed time overflowed the mobile viewport');
    await page.evaluate(() => {
      const event = detail => apiClient.dispatchEvent(new CustomEvent('server-event',{detail}));
      event({type:'agent_progress',agent_id:lamp.unit_id,run_id:sent[0].run_id,source:'device_chat',
        event_id:'cargo-next-plan',stage:'considering',ts:Date.now()/1000});
      event({type:'agent_progress',agent_id:lamp.unit_id,run_id:'unrelated-run',source:'device_chat',
        event_id:'other-progress',stage:'summarizing',ts:Date.now()/1000});
    });
    await page.waitForFunction(() => document.querySelector('.run-process__waiting')?.textContent.includes('正在规划下一步'));
    await page.evaluate(() => {
      const event = detail => apiClient.dispatchEvent(new CustomEvent('server-event', {detail}));
      const run_id = sent[0].run_id;
      event({type:'agent_progress',agent_id:lamp.unit_id,run_id,source:'device_chat',event_id:'p1',
        stage:'observed',text:'已观察灯光',media_ref:'/api/test-photo'});
      event({type:'agent_reply',agent_id:lamp.unit_id,run_id,source:'device_chat',event_id:'r1',text:'已经调好了',emotion:'happy',ts:Date.now()/1000});
      // HTTP repeats the same final reply; it must not produce another bubble.
      finishCommand({result:'ok',reply:'已经调好了',event_id:'r1'});
    });
    await page.waitForFunction(() => !document.querySelector('input').disabled);
    await page.waitForSelector('.chat-input__avatar .shape-face[data-emotion="happy"]');
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.behavior), 'idle');
    assert.equal(await page.$$eval('.chat-messages img', nodes => nodes.length), 1);
    assert.equal(await page.$eval('.chat-messages', node => node.textContent.split('已经调好了').length - 1), 1);
    assert.equal(await page.$$eval('details', nodes => nodes.every(node => !node.open)), true);
    await page.click('details summary');
    assert.equal(await page.$eval('details', node => node.open), true);
    await page.evaluate(() => device(false));
    await page.waitForFunction(() => document.querySelector('input').disabled);
    assert.match(await page.$eval('input', node => node.placeholder), /离线/);
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', node => node.dataset.animated), 'false');
    await page.evaluate(() => device(true, 'remount'));
    await page.waitForFunction(() => !document.querySelector('input').disabled);
    assert.match(await page.$eval('.chat-messages', node => node.textContent), /已经调好了/);
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.emotion), 'happy', 'Remount must preserve the still-current reply expression');
    // Long device tools publish their own source. Keep their in-flight
    // observations, decisions and screenshot in the same request transcript.
    await page.type('.chat-panel input', '去找猫头鹰');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 2);
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.behavior), 'thinking');
    await page.evaluate(() => {
      const run_id = sent[1].run_id;
      const event = (stage, text, more = {}) => apiClient.dispatchEvent(new CustomEvent('server-event', {
        detail:{type:'agent_progress',agent_id:lamp.unit_id,run_id,task_id:run_id,
          source:'object_search',scope:'session',event_id:stage,stage,text,fact_kind:stage === 'search_selected' ? 'decision' : stage === 'search_observe' ? 'observation' : undefined,...more},
      }));
      event('search_observing', '正在观察当前位置，识别猫头鹰。');
      event('search_observe', '这次画面暂未识别到猫头鹰。画面观察：前方有椅子和纸箱。',
        {media_ref:'/api/search-photo',observation_event_id:'search-observation'});
      event('search_selected', '当前视野仍有未检查区域，已选定可达的边界观察点。');
      // A following outer-Graph update must not hide the search record.
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_progress',
        agent_id:lamp.unit_id,run_id,source:'device_chat',stage:'dispatching',event_id:'outer-progress',
        tool:'go2_find_object'}}));
    });
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('前方有椅子和纸箱'));
    const latestProcess = (await page.$$('.run-process-summary')).at(-1);
    const searching = await latestProcess.evaluate(node => ({
      open:node.open,text:node.textContent,photos:node.querySelectorAll('img').length,
    }));
    assert.equal(searching.open, false, 'An executing concrete call keeps details collapsed');
    assert.match(searching.text, /可达的边界观察点/);
    assert.doesNotMatch(searching.text, /停止搜索/);
    assert.equal(searching.photos, 1);
    assert.doesNotMatch(await latestProcess.$eval('.run-process__body',node=>node.textContent), /上下文|去找猫头鹰/,
      'Process details should explain the agent actions without repeating the user request');
    // Pause as a reader before Puppeteer scrolls a historic summary into view.
    await page.focus('.chat-panel__scroll');
    await page.keyboard.press('Home');
    await page.waitForSelector('.chat-panel__latest');
    await page.waitForFunction(()=>document.querySelector('.chat-panel__scroll').scrollTop<=2);
    await (await latestProcess.$('summary')).click();
    await page.waitForFunction(node=>node.querySelector('summary').getAttribute('aria-expanded')==='true',{},latestProcess);
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_progress',
      agent_id:lamp.unit_id,run_id:sent[1].run_id,source:'object_search',stage:'search_observe',
      event_id:'search-copy',text:'同一张照片的新说明',media_ref:'/api/search-photo',observation_event_id:'search-observation'}})));
    await page.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('同一张照片的新说明'));
    assert.equal(await latestProcess.evaluate(node=>node.open),true,'A new progress record closed the user-opened process');
    assert.equal(await latestProcess.$$eval('img',nodes=>nodes.length),1,'Repeated media duplicated an observation');
    await (await latestProcess.$('summary')).click();
    await page.waitForFunction(node=>node.querySelector('summary').getAttribute('aria-expanded')==='false',{},latestProcess);
    await page.evaluate(() => finishCommand({result:'blocked',reply:'本次搜索结束，遮挡区域仍未确认。'}));
    await page.waitForFunction(() => !document.querySelector('input').disabled);
    assert.equal(await latestProcess.evaluate(node=>node.open),false,'A failure must not reopen details the user closed');
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/device-chat.png'});
    }
    const expressionBeforeLate = await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.emotion);
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:{
      type:'agent_reply',agent_id:lamp.unit_id,run_id:'expired-auto',source:'autonomous',scope:'space',
      activity_id:'ended-activity',event_id:'old-activity-emotion',text:'历史活动的迟到回复',emotion:'angry',ts:Date.now()/1000,
    }})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('历史活动的迟到回复'));
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.emotion), expressionBeforeLate, 'An ended activity reply must not override the current expression');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:{
      type:'agent_reply',agent_id:lamp.unit_id,run_id:'stale-auto',source:'autonomous',scope:'space',
      event_id:'stale-emotion',text:'过期的回复',emotion:'angry',ts:1,
    }})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('过期的回复'));
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.emotion), expressionBeforeLate, 'Old server timestamps cannot be converted into fresh animation');
    await page.type('.chat-panel input', '再来一次');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 3);
    await page.evaluate(() => {
      const run_id=sent[2].run_id;
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_reply',agent_id:lamp.unit_id,
        source:'device_chat',run_id,event_id:'latest-reply',text:'新的请求已经完成',emotion:'relaxed',ts:Date.now()/1000}}));
      finishCommand({result:'ok',reply:'新的请求已经完成',event_id:'latest-reply',emotion:'relaxed'});
    });
    await page.waitForSelector('.chat-input__avatar .shape-face[data-emotion="relaxed"]');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_reply',
      agent_id:lamp.unit_id,source:'device_chat',run_id:sent[1].run_id,event_id:'late-prior-run',
      text:'此前搜索的补充回复',emotion:'angry',ts:Date.now()/1000}})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('此前搜索的补充回复'));
    assert.equal(await page.$eval('.chat-input__avatar .shape-face', el => el.dataset.emotion), 'relaxed',
      'A retained prior run must not override the latest request expression');
    // A new request replaces an in-flight device conversation. Old HTTP/SSE
    // replies cannot finish the new request; late physical receipts remain.
    await page.type('.chat-panel input','旧的长任务');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>sent.length===4);
    await page.type('.chat-panel input','改为新的任务');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>sent.length===5);
    await page.evaluate(()=>{
      const old=sent[3].run_id;
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_reply',agent_id:lamp.unit_id,
        source:'device_chat',run_id:old,event_id:'superseded-reply',text:'失效旧目标的回复'}}));
      finishCommands.get(old)({result:'ok',reply:'失效旧目标的HTTP回复',event_id:'superseded-http'});
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_progress',agent_id:lamp.unit_id,
        source:'device_chat',run_id:old,event_id:'superseded-result',fact_kind:'result',tool:'go2_move',
        arguments:{distance_m:0.3},status:'ok',public_result:{result:'ok',motion_performed:true}}}));
    });
    await page.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('0.3'));
    assert(!await page.$eval('.chat-messages',el=>el.textContent.includes('失效旧目标')));
    assert.equal(await page.$eval('.chat-panel',el=>el.getAttribute('aria-busy')),'true',
      'A superseded result ended the current device conversation');
    await page.evaluate(()=>finishCommands.get(sent[4].run_id)({result:'ok',reply:'替换目标完成',event_id:'replacement-result'}));
    await page.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('替换目标完成'));
    assert.equal(await page.$eval('.chat-panel',el=>el.getAttribute('aria-busy')),'false');
    // Long activity questions and teaching context must scroll with the device
    // conversation, rather than consuming the composer's fixed available space.
    await page.evaluate(()=>root.render(React.createElement(FsmDevicePage,{
      key:'long-context',unitId:lamp.unit_id,device:lamp,liveState:{state:{ism:'ON',ts:Date.now()/1000}},
      actor:'visitor',sessionId:'long-context',onBack(){},
      activityQuestion:{text:'这是需要用户补充的完整活动问题。'.repeat(100)},
      trainingLocation:{teaching_id:'long',summaryText:'这是完整的规则说明。'.repeat(80)},
    })));
    await page.waitForSelector('.device-activity-question');
    await page.waitForSelector('.training-selection');
    for(const [width,height] of [[320,568],[390,320],[844,390]]) {
      await page.setViewport({width,height});
      const view=await page.evaluate(()=>{
        const scroller=document.querySelector('.device-page__chat .chat-panel__scroll');
        const input=document.querySelector('.device-page__chat input').getBoundingClientRect();
        return {scrollable:scroller.scrollHeight>scroller.clientHeight,
          contained:[...document.querySelectorAll('.device-activity-question,.training-selection')].every(el=>scroller.contains(el)),
          inputVisible:input.top>=0 && input.bottom<=innerHeight,
          fits:document.documentElement.scrollWidth<=innerWidth};
      });
      assert.deepEqual(view,{scrollable:true,contained:true,inputVisible:true,fits:true},`Long device context clipped at ${width}×${height}`);
    }
    if(process.env.SCREENSHOT_DIR) {
      await page.setViewport({width:390,height:320});
      await page.$eval('.device-page__chat .chat-panel__scroll',el=>{el.scrollTop=0;});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/device-long-context-short.png'});
    }
    // Ordinary chat has only original role speech, without a process or avatar.
    await page.evaluate(()=>root.render(React.createElement('div',{style:{display:'flex',height:'100dvh'}},React.createElement(ChatPanel,{
      messages:[{type:'user',text:'今天陪我聊聊吧',requestId:'ordinary'},
        {type:'ai',agent:'go2',text:'汪，我在这儿。今天过得怎么样？',requestId:'ordinary',final:true}],agents:[lamp],onSend(){},
    }))));
    await page.waitForFunction(()=>document.querySelector('.chat-messages')?.textContent.includes('今天过得怎么样'));
    assert.equal(await page.$('.chat-input__avatar'), null, 'The shared home composer must not acquire a device avatar');
    assert.equal(await page.$eval('.chat-input', node => node.getBoundingClientRect().height), deviceInputHeight,
      'The device avatar must reuse the same composer height as ordinary chat');
    assert.equal(await page.$$('.chat-messages details').then(rows=>rows.length),0);
    assert.equal(await page.$$('.chat-messages .shape-face').then(rows=>rows.length),0);
    if(process.env.SCREENSHOT_DIR){await page.setViewport({width:390,height:844});await page.screenshot({path:process.env.SCREENSHOT_DIR+'/ordinary-chat-390.png'});}
    // One shared scrollport follows arrivals and late content sizing until the
    // user scrolls up; returning to latest restores that behavior.
    await page.setViewport({width:390,height:480});
    await page.evaluate(()=>{
      window.followRows=Array.from({length:24},(_,i)=>({type:'ai',agent:'go2',requestId:'follow-'+i,
        final:true,text:'第 '+i+' 条聊天。'.repeat(8)}));
      window.renderFollow=()=>ReactDOM.flushSync(()=>root.render(React.createElement('div',
        {style:{display:'flex',height:'100dvh'}},React.createElement(ChatPanel,{
          key:'following',messages:followRows,agents:[lamp],onSend(){}}))));
      renderFollow();
    });
    const atBottom=()=>page.waitForFunction(()=>{
      const el=document.querySelector('.chat-panel__scroll');return el.scrollHeight-el.clientHeight-el.scrollTop<=2;
    });
    await atBottom();
    await page.evaluate(()=>{followRows.push({type:'ai',text:'刚到的新消息。'.repeat(12)});renderFollow();});
    await atBottom();
    await page.evaluate(()=>{followRows.splice(-6);renderFollow();});
    await atBottom();
    await page.setViewport({width:390,height:640});
    await atBottom();
    await page.evaluate(()=>{followRows.push({type:'ai',text:'内容缩短和视口增高后继续跟随。'.repeat(12)});renderFollow();});
    await atBottom();
    await page.evaluate(()=>{
      const growing=document.createElement('div');growing.id='late-sized-content';growing.style.height='400px';
      document.querySelector('.chat-panel__content').append(growing);
    });
    await atBottom();
    // Browser layout/anchoring scroll events are not user intent and must not
    // disable following, even if the offset temporarily moves upward.
    await page.$eval('.chat-panel__scroll',el=>{el.scrollTop-=120;el.dispatchEvent(new Event('scroll',{bubbles:true}));});
    await atBottom();
    assert.equal(await page.$('.chat-panel__latest'),null);
    const wheelStart=await page.$eval('.chat-panel__scroll',el=>{
      const box=el.getBoundingClientRect();return {top:el.scrollTop,x:box.left+box.width/2,y:box.top+box.height/2};
    });
    await page.mouse.move(wheelStart.x,wheelStart.y);
    await page.mouse.wheel({deltaY:-400});
    await page.waitForSelector('.chat-panel__latest');
    await page.waitForFunction(top=>Math.abs(document.querySelector('.chat-panel__scroll').scrollTop-(top-400))<=2,{timeout:5000},wheelStart.top);
    const readingTop=await page.$eval('.chat-panel__scroll',el=>el.scrollTop);
    await page.evaluate(()=>{followRows.push({type:'ai',text:'阅读历史时到达的消息。'.repeat(10)});renderFollow();});
    assert.equal(await page.$eval('.chat-panel__scroll',el=>el.scrollTop),readingTop);
    await page.click('.chat-panel__latest');
    await atBottom();
    await page.evaluate(()=>{followRows.push({type:'ai',text:'恢复跟随后的消息。'.repeat(10)});renderFollow();});
    await atBottom();
    await page.focus('.chat-panel__scroll');
    await page.keyboard.press('PageUp');
    await page.waitForSelector('.chat-panel__latest');
    await page.click('.chat-panel__latest');
    await atBottom();
    // Waiting belongs to the current request's feed, then its concrete process.
    for (const home of [true,false]) {
      await page.evaluate(home=>{
        window.waitRows=[{type:'user',requestId:'previous',text:'此前请求'},
          {type:'process',requestId:'previous',tool:'go2_observe',status:'ok',step_id:'old-step'},
          {type:'user',requestId:'waiting',text:'看看前面有什么'}];
        window.renderWaiting=(busy=true)=>ReactDOM.flushSync(()=>root.render(React.createElement('div',
          {style:{display:'flex',height:'100dvh'}},React.createElement(ChatPanel,{
            key:home?'waiting-home':'waiting-device',messages:waitRows,agents:[lamp],busy,onSend(){},
            messageContent:home?React.createElement(ActivityFeed,{entries:waitRows,agents:[lamp],busy}):null,
          }))));
        renderWaiting();
      },home);
      assert.equal(await page.$$('.chat-messages__waiting .run-process__waiting').then(nodes=>nodes.length),1);
      assert.equal(await page.$('.run-process-summary .run-process__waiting'),null,'An old request acquired the spinner');
      assert.equal(await page.$('.chat-panel__waiting'),null,'Waiting floated above the input');
      assert(await page.$eval('.run-process__waiting',node=>Boolean(node.closest('.chat-panel__scroll'))));
      await page.evaluate(()=>{waitRows.push({type:'process',requestId:'waiting',tool:'go2_observe',
        status:'executing',step_id:'current-step'});renderWaiting();});
      assert.equal(await page.$('.chat-messages__waiting'),null);
      assert.equal(await page.$$('.run-process-summary .run-process__waiting').then(nodes=>nodes.length),1);
      assert.match(await page.$eval('.run-process-summary .run-process__waiting',node=>node.textContent),/执行中/);
      for (const width of [320,390]) {
        await page.setViewport({width,height:568});
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      }
      await page.evaluate(()=>{waitRows[waitRows.length-1].status='ok';renderWaiting();});
      assert.match(await page.$eval('.run-process-summary .run-process__waiting',node=>node.textContent),/等待回复/);
      await page.evaluate(()=>{waitRows.push({type:'ai',requestId:'waiting',text:'已经观察好了。',final:true});renderWaiting(false);});
      assert.equal(await page.$('.run-process__waiting'),null);
      assert.equal(await page.$('.run-process-summary__status[data-completed]'),null);
      assert.doesNotMatch(await page.$$eval('.run-process-summary summary',nodes=>nodes.map(node=>node.textContent).join('')),/✓/);
      await page.evaluate(()=>{waitRows[waitRows.length-2].status='unknown';renderWaiting();});
      assert.equal(await page.$('.run-process-summary__status'),null,'A prior attempt status must not label the still-running request');
      assert.equal(await page.$('.run-process__feedback-marker'),null);
      assert.equal(await page.$eval('.run-process__feedback[data-attention]',node=>getComputedStyle(node).color),'rgb(166, 181, 201)');
      assert.equal(await page.$$('.run-process-summary .run-process__waiting').then(nodes=>nodes.length),1);
    }
    // Compare home/device presentation using the same messages and both real feed paths.
    const styles = [];
    for (const home of [true, false]) {
      await page.evaluate(home => {
        window.rows = [{type:'user',text:'测试请求',requestId:'a'},
          {type:'process',agent:lamp.unit_id,text:'处理测试',requestId:'a'},
          {type:'ai',agent:lamp.unit_id,text:'测试回复',requestId:'a',final:true}];
        root.render(React.createElement('div', {style:{display:'flex',height:'100dvh'}}, React.createElement(ChatPanel, {
          key:home?'home':'direct',messages:rows,agents:[lamp],onSend:text=>sent.push(text),suggestions:['快捷测试'],
          messageContent:home?React.createElement(ActivityFeed,{entries:rows,agents:[lamp]}):null,
        })));
      }, home);
      await page.waitForFunction(() => document.querySelector('.chat-messages')?.textContent.includes('测试回复'));
      styles.push(await page.$eval('.chat-messages', node => {
        const bubble = [...node.querySelectorAll('div')].find(node => node.childNodes.length === 1 && node.textContent === '测试回复');
        const style = getComputedStyle(bubble);
        return [style.borderRadius, style.padding, style.fontSize, style.lineHeight, style.backgroundColor];
      }));
      for (const [width,height] of [[320,568],[390,844],[390,520]]) {
        await page.setViewport({width,height});
        const layout = await page.$eval('.chat-panel', node => {
          const input = node.querySelector('input').getBoundingClientRect();
          return {overflow:document.documentElement.scrollWidth > innerWidth, bottom:input.bottom};
        });
        assert.equal(layout.overflow, false);
        assert(layout.bottom <= height);
      }
    }
    assert.deepEqual(styles[0], styles[1]);
    await page.evaluate(()=>{
      rows.push({type:'user',requestId:'long-text',text:'https://example.test/'+ 'UnbrokenText'.repeat(100)});
      root.render(React.createElement('div',{style:{display:'flex',height:'100dvh'}},React.createElement(ChatPanel,{
        messages:rows,agents:[lamp],onSend:text=>sent.push(text),suggestions:['快捷测试'],
      })));
    });
    for(const width of [320,390]) {
      await page.setViewport({width,height:320});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'A long user URL overflowed the conversation');
    }
    await page.type('input', '组合输入');
    await page.$eval('input', input => input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',isComposing:true,bubbles:true})));
    assert.equal(await page.$eval('input', input => input.value), '组合输入', 'IME Enter prematurely sent the draft');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.includes('组合输入'));
    assert.equal(await page.$eval('input', input => input.value), '');
    await page.click('.chat-suggestions button');
    await page.waitForFunction(() => sent.includes('快捷测试'));
    // Exercise the real intent hook: replacement before/after HTTP acceptance,
    // late replies versus receipts, and an actual clarification continuation.
    await page.addScriptTag({content:compile('app/src/hooks/useSendIntent.js')});
    await page.evaluate(()=>{
      window.goalRequests=[]; window.goalReplies=[]; window.goalProgress=[]; window.goalReplaced=[];
      apiClient.request=async (_path,options)=>new Promise(resolve=>goalRequests.push({body:options.body,resolve}));
      function GoalProbe(){
        const {thinking,send}=useSendIntent();
        window.sendGoal=text=>send(text,{
          onMessage:message=>goalReplies.push({goal:text,message}),
          onProgress:event=>goalProgress.push({goal:text,event}),
          onSuperseded:()=>goalReplaced.push(text),
        });
        return React.createElement(ChatPanel,{busy:thinking,onSend:sendGoal,messages:[]});
      }
      ReactDOM.flushSync(()=>root.render(React.createElement(GoalProbe)));
      window.goalEvent=(trace_id,type,extra={})=>apiClient.dispatchEvent(new CustomEvent('server-event',{
        detail:{trace_id,type,agent_id:'cloud_orchestrator',event_id:crypto.randomUUID(),...extra}}));
    });
    await page.type('input','原来的目标');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>goalRequests.length===1);
    await page.evaluate(()=>goalRequests[0].resolve({trace_id:'goal-old'}));
    await page.type('input','新的目标');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>goalRequests.length===2);
    await page.evaluate(()=>{
      goalRequests[1].resolve({trace_id:'goal-current'});
      goalEvent('goal-old','agent_reply',{text:'不应出现的旧回复'});
      goalEvent('goal-old','agent_progress',{fact_kind:'result',public_result:{result:'ok'},task_id:'old-effect'});
    });
    await page.waitForFunction(()=>goalProgress.length===1);
    assert.deepEqual(await page.evaluate(()=>goalReplies),[]);
    assert.equal(await page.evaluate(()=>goalProgress[0].goal),'原来的目标');
    assert.equal(await page.$eval('.chat-panel',el=>el.getAttribute('aria-busy')),'true');
    await page.evaluate(()=>goalEvent('goal-current','agent_reply',{text:'当前目标的回复'}));
    await page.waitForFunction(()=>document.querySelector('.chat-panel').getAttribute('aria-busy')==='false');
    await page.evaluate(()=>{sendGoal('尚未应答的旧提交');sendGoal('最后的新提交');});
    await page.waitForFunction(()=>goalRequests.length===4);
    await page.evaluate(()=>goalRequests[3].resolve({trace_id:'goal-fast-new'}));
    await page.evaluate(()=>goalRequests[2].resolve({trace_id:'goal-slow-old'}));
    await page.evaluate(()=>{
      goalEvent('goal-slow-old','agent_reply',{text:'迟到提交的旧回复'});
      goalEvent('goal-slow-old','agent_progress',{fact_kind:'result',public_result:{result:'unknown'},task_id:'old-slow-effect'});
      goalEvent('goal-fast-new','run_stage',{stage:'question',status:'needs_clarification',run_id:'clarify-original'});
    });
    await page.waitForFunction(()=>goalProgress.some(item=>item.event.task_id==='old-slow-effect'));
    assert.equal(await page.evaluate(()=>goalReplies.length),1);
    assert.deepEqual(await page.evaluate(()=>goalReplaced),['原来的目标','尚未应答的旧提交']);
    await page.evaluate(()=>{sendGoal('原问题的补充答案');});
    await page.waitForFunction(()=>goalRequests.length===5);
    assert.equal(await page.evaluate(()=>goalRequests[4].body.resume_run_id),'clarify-original');
    await page.evaluate(()=>goalRequests[4].resolve({trace_id:'goal-fast-new'}));
    await page.evaluate(()=>goalEvent('goal-fast-new','agent_reply',{text:'补充后完成'}));
    await page.waitForFunction(()=>goalReplies.length===2);
    // Real reload: cookies survive; the nonce, SSE address and transcript do not.
    await page.addScriptTag({content:compile('app/src/ApiClient.js')});
    const before = await page.evaluate(async () => {
      document.cookie = 'ssm_session=login-kept; path=/';
      window.eventUrls = [];
      window.EventSource = function(url) { eventUrls.push(url); this.close = () => {}; this.addEventListener = () => {}; };
      const me = await apiClient.request('/api/me');
      apiClient.openEvents(); apiClient.closeEvents(); apiClient.openEvents();
      return {nonce:apiClient.chatId,urls:eventUrls,login:me.session_id};
    });
    assert.equal(before.urls[0], before.urls[1], 'Reconnect changed the conversation');
    assert.equal(new URL(before.urls[0], 'http://localhost').searchParams.get('chat_id'), before.nonce);
    await page.reload();
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM]) await page.addScriptTag({path:file});
    for (const file of ['app/src/ApiClient.js','app/src/hooks/useTraining.js']) await page.addScriptTag({content:compile(file)});
    const after = await page.evaluate(async () => {
      const me = await apiClient.request('/api/me');
      window.eventUrls = [];
      window.EventSource = function(url) { eventUrls.push(url); this.close = () => {}; this.addEventListener = () => {}; };
      apiClient.openEvents();
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(function Probe() {
        const [rows] = useSessionTranscript('chat-test','device:esp32_desk_led',[]);
        return React.createElement('div',{id:'transcript'},rows.map(row=>row.text).join(''));
      }));
      return {nonce:apiClient.chatId,url:eventUrls[0],login:me.session_id,cookie:document.cookie};
    });
    await page.waitForSelector('#transcript');
    assert.notEqual(before.nonce, after.nonce);
    assert.notEqual(before.urls[0], after.url);
    assert.equal(after.login, before.login);
    assert.match(after.cookie, /ssm_session=login-kept/);
    assert.equal(await page.$eval('#transcript', node=>node.textContent), '');
    assert.equal(apiHeaders.at(-2)['x-ssm-chat-id'], before.nonce);
    assert.equal(apiHeaders.at(-1)['x-ssm-chat-id'], after.nonce);
    assert.deepEqual(errors, []);
    console.log('PASS: shared home/device styles, goal replacement during execution and before HTTP acceptance, late reply/receipt isolation, clarification continuation, SSE/HTTP deduplication, media, fresh page history, offline input, IME, suggestions, mobile layout and reload with a new API/SSE nonce while retaining login.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
