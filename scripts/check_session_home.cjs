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
  const browser = await puppeteer.launch({headless:true, executablePath:process.env.PUPPETEER_EXECUTABLE_PATH, args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', req => req.respond({status:200,contentType:'text/html',body:'<div id="root"></div>'}));
    await page.goto('http://ssm.test/');
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT});
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT_DOM});
    await page.addScriptTag({content:compile('app/src/ApiClient.js')});
    await page.addScriptTag({content:compile('app/src/components/SessionGate.jsx')});
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
    await waitText('空间伙伴');
    assert((await body()).includes('正在听听伙伴们的近况'));
    assert(!(await body()).includes('添加设备'));
    await page.evaluate(() => root.render(React.createElement(DevicesScreen,{agents:[{unit_id:'lamp',name:'皮皮灯',agent_type:'actuator',_online:true}],unitData:{},connected:true,loading:true})));
    await waitText('皮皮灯');
    assert((await body()).includes('正在同步'));
    await page.evaluate(() => root.render(React.createElement(DevicesScreen,{agents:[],unitData:{},connected:true,loading:false,isAdmin:true})));
    await waitText('添加设备');
    // Exercise the actual App + device hook together, with delayed snapshots.
    await page.addScriptTag({content:compile('app/src/utils/stateSnapshot.js')});
    await page.addScriptTag({content:compile('app/src/hooks/useSsmCore.js')});
    await page.addScriptTag({content:`
      window.useSpaceActivity = () => [];
      window.useActivityTimeline = () => ({events:[]});
      window.useSendIntent = () => ({thinking:false,send(){}});
      window.setAgentDisplayNames = () => {};
      window.SessionBadge = () => null;
      window.ActivityFeed = () => React.createElement('div', null, '对话内容');
      window.MainInputBar = () => React.createElement('input', {'aria-label':'消息'});
      window.snapshots = [];
      apiClient.request = path => path === '/api/devices'
        ? new Promise((resolve,reject) => snapshots.push({resolve,reject}))
        : Promise.resolve({});
    `});
    await page.addScriptTag({content:compile('app/src/app.jsx')});
    await page.evaluate(() => root.render(React.createElement(App,{key:'home',session:{actor:'visitor'}})));
    await waitText('正在准备你的空间');
    assert(!(await body()).includes('空间伙伴'));
    assert(!(await body()).includes('对话内容'));
    assert.equal(await page.$('input'), null);
    await page.evaluate(() => snapshots.shift().reject(new Error('模拟设备服务异常')));
    await waitText('重新加载');
    assert(!(await body()).includes('空间伙伴'));
    assert(!(await body()).includes('对话内容'));
    await page.evaluate(() => document.querySelector('.home-loading button').click());
    await waitText('正在准备你的空间');
    await page.evaluate(() => snapshots.shift().resolve([{unit_id:'lamp',name:'皮皮灯',agent_type:'actuator',online:true}]));
    await waitText('皮皮灯');
    assert((await body()).includes('空间伙伴'));
    assert((await body()).includes('对话内容'));
    await page.type('input', '尚未发送');
    await page.evaluate(() => apiClient.dispatchEvent(new Event('events-open')));
    await waitText('正在同步');
    assert((await body()).includes('皮皮灯'));
    assert((await body()).includes('对话内容'));
    assert.equal(await page.$eval('input', input=>input.value), '尚未发送');
    await page.evaluate(() => snapshots.shift().reject(new Error('模拟刷新失败')));
    await waitText('重试');
    assert((await body()).includes('皮皮灯'));
    assert.equal(await page.$eval('input', input=>input.value), '尚未发送');
    // An empty, successfully loaded space also reveals chat (no endless loading).
    await page.evaluate(() => root.render(React.createElement(App,{key:'empty',session:{actor:'visitor'}})));
    await waitText('正在准备你的空间');
    await page.evaluate(() => snapshots.shift().resolve([]));
    await waitText('空间里还没有设备');
    assert((await body()).includes('对话内容'));
    assert.deepEqual(errors, []);
    console.log('PASS: real-browser session network/503/401, retry, invite preservation, session end, initial overview, refresh keeps partner rows, empty state; App initial loading/error/retry, simultaneous home reveal, refresh retains input, empty snapshot; JSX syntax.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
