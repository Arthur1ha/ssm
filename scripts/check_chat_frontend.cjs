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
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    const apiHeaders = [];
    page.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/')) {
        apiHeaders.push(request.headers());
        return request.respond({status:200,contentType:'application/json',body:JSON.stringify({session_id:'login-kept'})});
      }
      return request.respond({status:200, contentType:'text/html',
      body:'<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>'});
    });
    await page.goto('http://localhost/');
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM]) await page.addScriptTag({path:file});
    await page.addStyleTag({path:'app/styles/app.css'});
    for (const file of ['utils/stateMachine.js','utils/stateSnapshot.js','utils/agentMeta.js','utils/activityView.js',
      'components/Icon.jsx','hooks/useTraining.js','components/TrainingPreview.jsx','components/ActivityDetails.jsx',
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
      event({type:'agent_reply',agent_id:lamp.unit_id,run_id,source:'device_chat',event_id:'r1',text:'已经调好了'});
      // HTTP repeats the same final reply; it must not produce another bubble.
      finishCommand({result:'ok',reply:'已经调好了',event_id:'r1'});
    });
    await page.waitForFunction(() => !document.querySelector('input').disabled);
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
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/device-chat.png'});
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
