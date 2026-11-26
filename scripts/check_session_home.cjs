/* Browser regression checks with simulated APIs; never contacts devices.
 * NODE_PATH must expose sucrase and puppeteer. Set SSM_CHECK_REACT and
 * SSM_CHECK_REACT_DOM to local React 18 UMD scripts, and optionally
 * PUPPETEER_EXECUTABLE_PATH to a local Chrome executable. Run from repo root.
 */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = file => sucrase.transform(fs.readFileSync(file, 'utf8'), {transforms:['jsx'], production:true}).code;
(async () => {
  for (const file of ['app/src/app.jsx','app/src/components/SessionGate.jsx','app/src/pages/DevicesPage.jsx']) compile(file);
  const browser = await puppeteer.launch({headless:true, protocolTimeout:15000, executablePath:process.env.PUPPETEER_EXECUTABLE_PATH, args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:',error.message); });
    await page.setRequestInterception(true);
    page.on('request', req => req.respond({status:200,contentType:'text/html',body:'<div id="root"></div>'}));
    await page.goto('http://ssm.test/');
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT});
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT_DOM});
    await page.addScriptTag({content:compile('app/src/utils/inputAttributes.js')});
    await page.addScriptTag({content:compile('app/src/ApiClient.js')});
    await page.addScriptTag({content:compile('app/src/components/SessionGate.jsx')});
    for (const file of ['app/src/avatars/registry.js', 'app/src/avatars/presentation.js', 'app/src/avatars/Avatar.jsx', 'app/src/avatars/CompanionAvatar.jsx'])
      await page.addScriptTag({content:compile(file)});
    await page.addScriptTag({content:compile('app/src/pages/DevicesPage.jsx')});
    await page.addScriptTag({content:`
      window.Icon = () => null;
      window.HomeDeviceRow = ({agent}) => React.createElement('div', null, agent.name);
      window.isDeviceOnline = agent => agent._online;
      apiClient.openEvents = () => {};
      window.replies = []; window.requests = [];
      apiClient.request = async path => {
        requests.push(path);
        const reply = replies.shift();
        if (reply?.status) throw Object.assign(new Error('request failed'), reply);
        if (!reply) throw new TypeError('Failed to fetch');
        return reply;
      };
      window.root = ReactDOM.createRoot(document.getElementById('root'));
      window.mount = (key, responses) => {
        replies = responses; requests = [];
        root.render(React.createElement(SessionGate, {key}, () => React.createElement('p', null, 'HOME')));
      };
    `});
    const body = () => page.evaluate(() => document.body.innerText);
    const waitText = text => page.waitForFunction(text => document.body.innerText.includes(text), {}, text);
    const valid = {actor:'admin',csrf_token:'test',space_name:'测试空间'};
    await page.evaluate(() => mount('network', [null]));
    await waitText('重新连接');
    assert(!(await body()).includes('扫码加入'));
    await page.evaluate(value => { replies = [value]; document.querySelector('button').click(); }, valid);
    await waitText('HOME');
    await page.evaluate(() => mount('server', [{status:503}]));
    await waitText('重新连接');
    assert(!(await body()).includes('扫码加入'));
    await page.evaluate(() => mount('unauthorized', [{status:401}]));
    await waitText('扫码加入');
    await page.evaluate(() => { location.hash='invite=test-token'; mount('invite', [{status:401},{status:503}]); });
    await waitText('重新连接');
    assert.equal(await page.evaluate(() => location.hash), '');
    await page.evaluate(value => { replies = [{status:401},value]; document.querySelector('button').click(); }, valid);
    await waitText('HOME');
    assert.equal(await page.evaluate(() => requests.filter(path=>path==='/api/visitor/session').length), 2);
    await page.evaluate(() => apiClient.dispatchEvent(new Event('unauthorized')));
    await waitText('会话已结束');
    await page.evaluate(() => root.render(React.createElement(DevicesScreen,{agents:[],unitData:{},connected:false,loading:true})));
    await page.waitForFunction(() => !document.getElementById('root').hasChildNodes());
    await page.evaluate(() => root.render(React.createElement(DevicesScreen,{agents:[{unit_id:'lamp',name:'皮皮灯',agent_type:'actuator',_online:true}],unitData:{},connected:true,loading:true})));
    await waitText('皮皮灯');
    assert((await body()).includes('正在同步'));
    await page.evaluate(() => root.render(React.createElement(DevicesScreen,{agents:[],unitData:{},connected:true,loading:false})));
    await page.waitForFunction(() => !document.getElementById('root').hasChildNodes());
    // Exercise the actual App + device hook together, with delayed snapshots.
    await page.addScriptTag({content:compile('app/src/utils/stateSnapshot.js')});
    await page.addScriptTag({content:compile('app/src/hooks/useSsmCore.js')});
    await page.addScriptTag({content:`
      window.useSpaceActivity = () => [];
      window.useActivityTimeline = () => ({events:[]});
      window.useSendIntent = () => ({thinking:false,send(){}});
      window.SessionBadge = () => null;

      window.snapshots = [];
      apiClient.request = path => path === '/api/devices'
        ? new Promise((resolve,reject) => snapshots.push({resolve,reject}))
        : Promise.resolve({});
    `});
    for (const file of ['utils/agentMeta.js','utils/activityView.js','utils/runProcess.js',
      'components/RunProcess.jsx','components/ChatMessages.jsx','components/ActivityFeed.jsx'])
      await page.addScriptTag({content:compile('app/src/'+file)});
    await page.addScriptTag({content:compile('app/src/hooks/useTraining.js')});
    await page.addScriptTag({content:compile('app/src/components/ChatPanel.jsx')});
    await page.addScriptTag({content:compile('app/src/components/Toast.jsx')});
    await page.addScriptTag({content:compile('app/src/app.jsx')});
    await page.evaluate(() => root.render(React.createElement(App,{key:'home',session:{actor:'visitor'}})));
    await waitText('正在准备你的空间');
    assert(!(await body()).includes('空间伙伴'));
    assert.equal(await page.$('.chat-messages'), null);
    assert.equal(await page.$('[aria-label="聊天消息"]'), null);
    await page.evaluate(() => snapshots.shift().reject(new Error('模拟设备服务异常')));
    await waitText('重新加载');
    assert(!(await body()).includes('空间伙伴'));
    assert.equal(await page.$('.chat-messages'), null);
    await page.evaluate(() => document.querySelector('.home-loading button').click());
    await waitText('正在准备你的空间');
    await page.evaluate(() => snapshots.shift().resolve([{unit_id:'lamp',name:'皮皮灯',agent_type:'actuator',online:true}]));
    await waitText('皮皮灯');
    assert((await body()).includes('空间伙伴'));
    assert(await page.$('.chat-messages'));
    await page.type('[aria-label="聊天消息"]', '尚未发送');
    await page.evaluate(() => apiClient.dispatchEvent(new Event('events-open')));
    await waitText('正在同步');
    assert((await body()).includes('皮皮灯'));
    assert(await page.$('.chat-messages'));
    assert.equal(await page.$eval('[aria-label="聊天消息"]', input=>input.value), '尚未发送');
    await page.evaluate(() => snapshots.shift().reject(new Error('模拟刷新失败')));
    await waitText('重试');
    assert((await body()).includes('皮皮灯'));
    assert.equal(await page.$eval('[aria-label="聊天消息"]', input=>input.value), '尚未发送');
    // An empty, successfully loaded space also reveals chat (no endless loading).
    await page.evaluate(() => root.render(React.createElement(App,{key:'empty',session:{actor:'visitor'}})));
    await waitText('正在准备你的空间');
    await page.evaluate(() => snapshots.shift().resolve([]));
    await waitText('你好，我是总管家。');
    assert.equal(await page.$eval('.agent-speech__name', node=>node.textContent), '总管家');
    assert.equal(await page.$eval('.agent-speech__bubble', node=>node.textContent),
      '你好，我是总管家。可以先和我聊聊，也可以扫码添加新伙伴。');
    assert.equal(await page.$('.home-space'), null);
    assert.equal(await page.$('.chat-panel button:not([aria-label="发送消息"])'), null);
    assert.equal(await page.$('.shape-face'), null);
    assert.equal(await page.$eval('[aria-label="聊天消息"]', input=>input.placeholder), '和总管家说点什么…');
    await page.type('[aria-label="聊天消息"]', '先聊聊今天的安排');
    await page.keyboard.press('Enter');
    await waitText('先聊聊今天的安排');
    assert(!(await body()).includes('你好，我是总管家。'));

    // Exercise the actual App scan result, reusable toast and undo together.
    if (process.env.SSM_CHECK_VERBOSE) console.log('Checking App scan toast and undo');
    await page.addStyleTag({path:'app/styles/app.css'});
    await page.evaluate(() => {
      for (const name of ['SpaceMenu','AdminInviteDrawer','AdminSpaceDrawer','OrchestratorSettingsDrawer','SpacePeopleDrawer'])
        window[name] = () => null;
      window.useTraining = () => ({proposals:[],resetVersion:0,loadAll(){}});
      window.QrScanner = ({onScan}) => { window.emitScan=onScan; return React.createElement('p',null,'模拟摄像头'); };
      window.fixtureDevice = {unit_id:'lamp',device_id:'esp32_desk',name:'皮皮灯',agent_type:'actuator',online:true};
      window.members=[]; window.undoRequests=[]; window.bindCount=0;
      window.bindReply={device_id:'esp32_desk',result:'added'};
      apiClient.request=async (path,options={}) => {
        if (path==='/api/devices') return [...members];
        if (path==='/api/admin/devices/bind') {
          bindCount++;
          if (window.holdBind) await new Promise(resolve => {window.finishBind=resolve;});
          if (window.bindError) throw Object.assign(new Error('Simulated binding error'),{status:bindError});
          if (!members.length) members=[fixtureDevice];
          return {...bindReply};
        }
        if (path==='/api/admin/devices/esp32_desk' && options.method==='DELETE') {
          undoRequests.push({path,method:options.method});
          if (window.undoError) throw new Error('Simulated removal failure');
          if (window.holdUndo) await new Promise(resolve => {window.finishUndo=resolve;});
          members=[];
          return {device_id:'esp32_desk',result:'removed'};
        }
        return {};
      };
      root.render(React.createElement(App,{key:'admin-toast',session:{actor:'admin',session_id:'test'}}));
    });
    await page.setViewport({width:390,height:844});
    await waitText('你好，我是总管家。');
    const headerHeight = await page.$eval('.device-scan-trigger',button=>button.parentElement.parentElement.getBoundingClientRect().height);
    const scan = async () => {
      await page.click('.device-scan-trigger');
      await waitText('模拟摄像头');
      await page.evaluate(() => { emitScan('ssm-device:v1:esp32_desk:test-token'); });
    };
    const toastText = () => page.$eval('.toast',element=>element.innerText);
    await page.evaluate(()=>{window.holdBind=true;});
    await page.click('.device-scan-trigger');
    await waitText('模拟摄像头');
    assert.equal(await page.$$eval('.device-scan-dialog button,.device-scan-dialog input,.device-scan-dialog form,.device-scan-dialog h2',elements=>elements.length),0);
    assert.equal(await page.$eval('.device-scan-dialog',element=>element.innerText),'模拟摄像头');
    await page.evaluate(()=>{emitScan('ssm-device:v1:esp32_desk:test-token');});
    await page.waitForFunction(()=>window.finishBind);
    await page.waitForSelector('.device-scan-dialog',{hidden:true,timeout:1000});
    assert.equal(await page.$('.toast'),null,'Do not report success before the binding response');
    await page.evaluate(()=>{window.holdBind=false;finishBind();});
    await waitText('添加成功');
    if (process.env.SSM_CHECK_VERBOSE) console.log('Toast rendered');
    assert.equal(await page.$('.device-scan-dialog'),null);
    assert.equal(await page.$eval('.toast__action',button=>button.textContent.trim()),'撤销');
    assert.equal(await page.$eval('.agent-speech__bubble',node=>node.textContent),
      '你好，我是总管家。有什么想聊的，或想让伙伴们做的，告诉我就好。');
    assert.equal(await page.evaluate(()=>document.querySelector('.toast-region').parentElement===document.body),true);
    assert.equal(await page.$eval('.device-scan-trigger',button=>button.parentElement.parentElement.getBoundingClientRect().height),headerHeight);
    await page.focus('.toast__action');
    await page.$eval('.toast', element => Promise.all(element.getAnimations().map(animation => animation.finished)));
    for (const [width,height] of [[320,568],[390,844],[390,520],[1280,800]]) {
      await page.setViewport({width,height});
      await page.waitForFunction(()=>{
        const toast=document.querySelector('.toast').getBoundingClientRect();
        const footer=document.querySelector('.chat-panel__footer').getBoundingClientRect();
        return Math.abs(footer.top-toast.bottom-12)<1;
      });
      const rect=await page.$eval('.toast',element=>{
        const r=element.getBoundingClientRect();return {left:r.left,right:r.right,bottom:r.bottom,height:r.height,
          footerTop:document.querySelector('.chat-panel__footer').getBoundingClientRect().top};
      });
      assert(rect.left>=15 && rect.right<=width-15 && Math.abs(rect.footerTop-rect.bottom-12)<1,
        JSON.stringify({width,height,...rect}));
      assert(rect.height>=44 && rect.height<80);
    }
    await page.setViewport({width:390,height:844});
    await page.evaluate(()=>{window.holdUndo=true;document.querySelector('.toast__action').click();document.querySelector('.toast__action').click();});
    await page.waitForFunction(()=>window.finishUndo);
    assert.equal(await page.evaluate(()=>undoRequests.length),1,'Prevent duplicate undo requests');
    assert.equal(await page.$eval('.toast__action',button=>button.disabled),true);
    await page.evaluate(()=>{window.holdUndo=false;finishUndo();});
    await waitText('已撤销添加');
    if (process.env.SSM_CHECK_VERBOSE) console.log('Undo completed');
    assert.equal(await page.$('.toast__action'),null);
    await waitText('你好，我是总管家。');
    await page.hover('.toast');
    await page.waitForSelector('.toast',{hidden:true,timeout:3500});
    if (process.env.SSM_CHECK_VERBOSE) console.log('Undo feedback expired');

    await page.evaluate(()=>{bindReply.result='already_added';});
    await scan();
    await waitText('空间已有，无需重复添加');
    assert.equal(await page.$('.toast__action'),null);
    const count=await page.evaluate(()=>bindCount);
    await new Promise(resolve=>setTimeout(resolve,1800));
    await scan();
    await page.waitForFunction(count=>bindCount>count,{},count);
    await new Promise(resolve=>setTimeout(resolve,1500));
    assert((await toastText()).includes('空间已有，无需重复添加'),'Repeated text gets a fresh duration');
    await page.waitForSelector('.toast',{hidden:true,timeout:4000});
    if (process.env.SSM_CHECK_VERBOSE) console.log('Repeated feedback timer checked');

    // An actionable toast uses the same 3s duration and never auto-runs undo.
    await page.evaluate(()=>{bindReply.result='added';});
    await scan();
    await waitText('添加成功');
    await page.focus('.toast__action');
    await page.hover('.toast__action');
    await page.waitForSelector('.toast',{hidden:true,timeout:3500});
    if (process.env.SSM_CHECK_VERBOSE) console.log('Actionable toast timer checked');
    assert.equal(await page.evaluate(()=>undoRequests.length),1);

    await page.evaluate(()=>{window.bindError=404;});
    await scan();
    await waitText('未发现设备，请确认已启动');
    assert.equal(await page.$('.device-scan-dialog'),null);
    assert.equal(await page.$('.toast__action'),null);

    // Undo failure remains retryable; actual completion refreshes membership.
    await page.evaluate(()=>{window.bindError=0;window.undoError=true;});
    await scan();
    await waitText('添加成功');
    await page.click('.toast__action');
    await waitText('撤销失败，请重试');
    assert.equal(await page.$eval('.toast__action',button=>button.textContent.trim()),'重试');
    await page.evaluate(()=>{window.undoError=false;});
    await page.click('.toast__action');
    await waitText('已撤销添加');

    // A late action result cannot replace newer feedback.
    await scan();
    await waitText('添加成功');
    await page.evaluate(()=>{window.holdUndo=true;window.finishUndo=null;});
    await page.click('.toast__action');
    await page.waitForFunction(()=>window.finishUndo);
    await page.evaluate(()=>{bindReply.result='already_added';});
    await scan();
    await waitText('空间已有，无需重复添加');
    await page.evaluate(()=>{window.holdUndo=false;finishUndo();});
    await page.waitForFunction(()=>members.length===0);
    assert((await toastText()).includes('空间已有，无需重复添加'));

    // Pending actions do not extend a toast; their eventual result gets its own 3s.
    await page.evaluate(()=>{bindReply.result='added';});
    await scan();
    await waitText('添加成功');
    await page.evaluate(()=>{window.holdUndo=true;window.finishUndo=null;});
    await page.click('.toast__action');
    await page.waitForFunction(()=>window.finishUndo);
    await page.waitForSelector('.toast',{hidden:true,timeout:3500});
    await page.evaluate(()=>{window.holdUndo=false;finishUndo();});
    await waitText('已撤销添加');
    await page.hover('.toast');
    await page.waitForSelector('.toast',{hidden:true,timeout:3500});
    // Resizing keeps the same conversation/composer, with a readable desktop width.
    await page.type('.space-page [aria-label="聊天消息"]', '保留这段草稿');
    await page.evaluate(() => { window.responsiveInput=document.querySelector('.space-page [aria-label="聊天消息"]'); });
    for (const [width,height] of [[320,568],[360,740],[390,844],[430,932],[932,430],[1023,768],[1024,768],[1366,768],[1440,900],[1920,1080]]) {
      await page.setViewport({width,height});
      await page.waitForFunction(() => Math.abs(document.querySelector('.space-page').getBoundingClientRect().height-visualViewport.height)<1);
      const layout=await page.evaluate(() => {
        const panel=document.querySelector('.space-page > .chat-panel').getBoundingClientRect();
        const input=document.querySelector('.space-page [aria-label="聊天消息"]');
        return {left:panel.left,right:panel.right,width:panel.width,bottom:input.getBoundingClientRect().bottom,
          documentWidth:document.documentElement.scrollWidth,same:input===responsiveInput,draft:input.value};
      });
      assert(layout.left>=0 && layout.right<=width && layout.documentWidth<=width, `Home overflow at ${width}×${height}`);
      assert(layout.width<=960 && Math.abs(layout.left-(width-layout.width)/2)<1, 'Home conversation must be centered and limited in width');
      assert(layout.bottom<=height && layout.same && layout.draft==='保留这段草稿', 'Resizing must keep the visible composer and its draft');
    }
    // Simulate the visual viewport shrinking/panning independently of the layout
    // viewport, as a mobile keyboard can do. This is not a real-device keyboard test.
    await page.setViewport({width:390,height:844});
    await page.evaluate(() => {
      Object.defineProperty(visualViewport,'height',{configurable:true,value:320});
      Object.defineProperty(visualViewport,'offsetTop',{configurable:true,value:40});
      visualViewport.dispatchEvent(new Event('resize'));
    });
    const keyboard=await page.$eval('.space-page', node => {
      const rect=node.getBoundingClientRect();
      const input=node.querySelector('[aria-label="聊天消息"]').getBoundingClientRect();
      return {top:rect.top,height:rect.height,inputBottom:input.bottom};
    });
    assert.equal(keyboard.top,40);
    assert.equal(keyboard.height,320);
    assert(keyboard.inputBottom<=360, 'The composer must stay within the simulated keyboard viewport');
    await page.evaluate(() => {
      delete visualViewport.height; delete visualViewport.offsetTop;
      visualViewport.dispatchEvent(new Event('resize'));
    });
    console.log('PASS: responsive home width, landscape, breakpoints, draft preservation and simulated keyboard viewport.');
    assert.deepEqual(errors, []);
    console.log('PASS: session/home regressions; actual App bottom toast, no header growth, mobile/desktop layout, uniform 3s expiry, repeated feedback resets duration; undo dispatch/deduplication/failure/retry; stale action result preserves newer toast; JSX syntax.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
