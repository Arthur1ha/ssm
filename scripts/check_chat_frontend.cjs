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
    for (const file of ['utils/stateMachine.js','utils/stateSnapshot.js','utils/agentMeta.js','utils/activityView.js','utils/runProcess.js','avatars/registry.js',
      'components/Icon.jsx','avatars/Avatar.jsx','hooks/useTraining.js','components/TrainingPreview.jsx','components/ActivityMembers.jsx','components/RunProcess.jsx',
      'components/ChatMessages.jsx','components/ChatPanel.jsx','components/ActivityFeed.jsx','pages/FsmDevicePage.jsx']) {
      await page.addScriptTag({content:compile('app/src/'+file)});
    }
    await page.evaluate(() => {
      window.apiClient = new EventTarget();
      apiClient.request = async () => ({records:[],proposals:[],edge_annotations:[]});
      window.apiErrorText = error => error.message;
      window.sent = [];
      window.commandBus = {describe: result => result.result, command: async (_unit, _action, params) => {
        sent.push(params);
        return new Promise(resolve => { window.finishCommand = resolve; });
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
    assert.equal(await page.$eval('.chat-messages', node => node.textContent.includes('旧回复')), false,
      'A fresh page must not restore the old sessionStorage transcript');
    // Send through the real device page and observe actual SSE projection.
    await page.type('.chat-panel input', '调成暖白色');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 1);
    assert.equal(await page.$eval('input', node => node.disabled), true);
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
    await page.waitForSelector('.device-page__header .shape-face[data-emotion="happy"]');
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.behavior), 'idle');
    assert.equal(await page.$$eval('.chat-messages img', nodes => nodes.length), 1);
    assert.equal(await page.$eval('.chat-messages', node => node.textContent.split('已经调好了').length - 1), 1);
    assert.equal(await page.$$eval('details', nodes => nodes.every(node => !node.open)), true);
    await page.click('details summary');
    assert.equal(await page.$eval('details', node => node.open), true);
    await page.evaluate(() => device(false));
    await page.waitForFunction(() => document.querySelector('input').disabled);
    assert.match(await page.$eval('input', node => node.placeholder), /离线/);
    await page.evaluate(() => device(true, 'remount'));
    await page.waitForFunction(() => !document.querySelector('input').disabled);
    assert.match(await page.$eval('.chat-messages', node => node.textContent), /已经调好了/);
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.emotion), 'neutral', 'Remount must not replay a previous reply');
    // Long device tools publish their own source. Keep their in-flight
    // observations, decisions and screenshot in the same request transcript.
    await page.type('.chat-panel input', '去找猫头鹰');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 2);
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.behavior), 'thinking');
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
    assert.equal(searching.open, true, 'An executing concrete call should remain visible');
    assert.match(searching.text, /可达的边界观察点/);
    assert.doesNotMatch(searching.text, /停止搜索/);
    assert.equal(searching.photos, 1);
    assert.doesNotMatch(await latestProcess.$eval('.run-process__body',node=>node.textContent), /上下文|去找猫头鹰/,
      'Process details should explain the agent actions without repeating the user request');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_progress',
      agent_id:lamp.unit_id,run_id:sent[1].run_id,source:'object_search',stage:'search_observe',
      event_id:'search-copy',text:'同一张照片的新说明',media_ref:'/api/search-photo',observation_event_id:'search-observation'}})));
    await page.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('同一张照片的新说明'));
    assert.equal(await latestProcess.evaluate(node=>node.open),true,'A new progress record closed the user-opened process');
    assert.equal(await latestProcess.$$eval('img',nodes=>nodes.length),1,'Repeated media duplicated an observation');
    await page.evaluate(() => finishCommand({result:'blocked',reply:'本次搜索结束，遮挡区域仍未确认。'}));
    await page.waitForFunction(() => !document.querySelector('input').disabled);
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/device-chat.png'});
    }
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:{
      type:'agent_reply',agent_id:lamp.unit_id,run_id:'expired-auto',source:'autonomous',scope:'space',
      activity_id:'ended-activity',event_id:'old-activity-emotion',text:'历史活动的迟到回复',emotion:'angry',ts:Date.now()/1000,
    }})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('历史活动的迟到回复'));
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.emotion), 'neutral', 'An ended activity reply must remain static');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:{
      type:'agent_reply',agent_id:lamp.unit_id,run_id:'stale-auto',source:'autonomous',scope:'space',
      event_id:'stale-emotion',text:'过期的回复',emotion:'angry',ts:1,
    }})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('过期的回复'));
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.emotion), 'neutral', 'Old server timestamps cannot be converted into fresh animation');
    await page.type('.chat-panel input', '再来一次');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.length === 3);
    await page.evaluate(() => {
      const run_id=sent[2].run_id;
      apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_reply',agent_id:lamp.unit_id,
        source:'device_chat',run_id,event_id:'latest-reply',text:'新的请求已经完成',emotion:'relaxed',ts:Date.now()/1000}}));
      finishCommand({result:'ok',reply:'新的请求已经完成',event_id:'latest-reply',emotion:'relaxed'});
    });
    await page.waitForSelector('.device-page__header .shape-face[data-emotion="relaxed"]');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event',{detail:{type:'agent_reply',
      agent_id:lamp.unit_id,source:'device_chat',run_id:sent[1].run_id,event_id:'late-prior-run',
      text:'此前搜索的补充回复',emotion:'angry',ts:Date.now()/1000}})));
    await page.waitForFunction(() => document.querySelector('.chat-messages').textContent.includes('此前搜索的补充回复'));
    assert.equal(await page.$eval('.device-page__header .shape-face', el => el.dataset.emotion), 'relaxed',
      'A retained prior run must not override the latest request expression');
    // Ordinary chat has only original role speech, without a process or avatar.
    await page.evaluate(()=>root.render(React.createElement('div',{style:{display:'flex',height:'100dvh'}},React.createElement(ChatPanel,{
      messages:[{type:'user',text:'今天陪我聊聊吧',requestId:'ordinary'},
        {type:'ai',agent:'go2',text:'汪，我在这儿。今天过得怎么样？',requestId:'ordinary',final:true}],agents:[lamp],onSend(){},
    }))));
    await page.waitForFunction(()=>document.querySelector('.chat-messages')?.textContent.includes('今天过得怎么样'));
    assert.equal(await page.$$('.chat-messages details').then(rows=>rows.length),0);
    assert.equal(await page.$$('.chat-messages .shape-face').then(rows=>rows.length),0);
    if(process.env.SCREENSHOT_DIR){await page.setViewport({width:390,height:844});await page.screenshot({path:process.env.SCREENSHOT_DIR+'/ordinary-chat-390.png'});}
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
      assert.match(await page.$eval('.run-process-summary__status[data-attention]',node=>node.textContent),/待确认/);
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
    await page.type('input', '组合输入');
    await page.$eval('input', input => input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',isComposing:true,bubbles:true})));
    assert.equal(await page.$eval('input', input => input.value), '组合输入', 'IME Enter prematurely sent the draft');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => sent.includes('组合输入'));
    assert.equal(await page.$eval('input', input => input.value), '');
    await page.click('.chat-suggestions button');
    await page.waitForFunction(() => sent.includes('快捷测试'));
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
    console.log('PASS: shared home/device styles, collapsed progress, device send/SSE/HTTP deduplication, media, fresh page history, navigation history, offline input, IME, suggestions, mobile layout and actual reload with a new API/SSE nonce while retaining login.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
