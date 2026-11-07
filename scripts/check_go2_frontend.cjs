/* No build step or hardware. NODE_PATH must expose sucrase/react/react-dom;
 * --browser also needs puppeteer and a local Chrome (or PUPPETEER_EXECUTABLE_PATH).
 * Optional GO2_PREVIEW_REACT/GO2_PREVIEW_REACT_DOM select the product's cached UMD;
 * GO2_PREVIEW_FONT embeds a small local CJK font solely for readable screenshots.
 * Screenshots and controls use explicitly simulated state, never a live API. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const sucrase = require('sucrase');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const compile = name => sucrase.transform(read(name), { transforms: ['jsx'], jsxRuntime: 'classic', production: true }).code;
const context = vm.createContext({ window: {} });
vm.runInContext(read('app/src/utils/stateSnapshot.js'), context);
vm.runInContext(read('app/src/utils/stateMachine.js'), context);
const nodeControl = context.window.stateNodeControl;
const sittingNode = {id:'body:sitting',label:'坐姿',current:false};
const sitEdge = {dst:'body:sitting',active:true,reason:'',transition:{action:'Sit',params:{}}};
assert.equal(nodeControl(sittingNode,[sitEdge]).transition.action,'Sit','Target node must resolve its legal command');
assert.equal(nodeControl(sittingNode,[{...sitEdge,active:false}]).transition,null,'Node click must not invent a multi-step route');
assert.equal(nodeControl(sittingNode,[{...sitEdge,reason:'等待有效设备反馈'}]).reason,'等待有效设备反馈');
assert.equal(nodeControl({...sittingNode,current:true},[sitEdge]).transition,null,'Current state must not re-execute');
assert.equal(nodeControl(sittingNode,[sitEdge,{...sitEdge,transition:{action:'Other',params:{}}}]).transition,null,'Ambiguous targets need explicit action selection');
const merge = context.window.mergeStateSnapshot;
const retired = new Set();
let state = { epoch: 'old', revision: 8, body_state: 'standing' };
assert.equal(merge(state, { epoch: 'old', revision: 3 }, retired), state);
state = merge(state, { epoch: 'new', revision: 1, body_state: null }, retired);
assert.equal(state.epoch, 'new');
assert.equal(merge(state, { epoch: 'old', revision: 99 }, retired), state);
assert.equal(merge(state, { ts: Date.now() }, retired), state);
let lampOverlay = {ts:100,ism:'ON',control_ts:1000,autonomy_mode:'manual',control_state:'manual',control_hold_until:null};
lampOverlay=merge(lampOverlay,{ts:90,ism:'OFF',control_ts:1001,autonomy_mode:'reactive',control_state:'holding',control_hold_until:2000});
assert.equal(lampOverlay.ism,'ON','Control overlay replaced a newer physical fact');
assert.equal(lampOverlay.control_state,'holding','Control-only state was lost behind physical ts');
lampOverlay=merge(lampOverlay,{ts:101,ism:'BLINKING'});
assert.equal(lampOverlay.ism,'BLINKING');
assert.equal(lampOverlay.control_ts,1001,'Physical frame erased cloud control state');
lampOverlay=merge(lampOverlay,{ts:102,ism:'ON',control_ts:999,control_state:'manual',autonomy_mode:'manual'});
assert.equal(lampOverlay.control_state,'holding','Old control cursor replaced newer overlay');
const files = ['app/src/utils/color.js', 'app/src/utils/stateMachine.js', 'app/src/utils/agentMeta.js', 'app/src/utils/stateSnapshot.js',
  'app/src/components/DeviceWidgets.jsx', 'app/src/pages/fsmWidgets.jsx',
  'app/src/pages/StateVisual.jsx', 'app/src/pages/FsmGraph.jsx',
  'app/src/pages/HierarchicalFsmBoard.jsx', 'app/src/pages/FsmDevicePage.jsx'];
for (const name of files) compile(name);
compile('app/src/hooks/useSsmCore.js');
console.log('PASS: snapshot revision/epoch, independent lamp physical/control cursors, and modified JSX syntax.');

async function checkCommandResults() {
  let payload;
  const sandbox = vm.createContext({
    apiClient: {request: async () => payload}, AbortController, setTimeout, clearTimeout,
  });
  vm.runInContext(read('app/src/CommandBus.js') + '\nthis.bus=commandBus;', sandbox);
  for (const outcome of ['unknown', 'error', 'cancelled', 'rejected', 'blocked', 'ok', 'timeout', 'executing']) {
    payload = {accepted:true,status:'dispatched',result:outcome,task_id:'task_cached'};
    const result = await sandbox.bus.command('esp32_desk_led','BLINK',{count:2});
    assert.equal(result.result,outcome,`Compatibility accepted flag replaced ${outcome}`);
  }
  let waits=0;
  const waitForLampTask = sandbox.bus.waitForLampTask.bind(sandbox.bus);
  assert.equal((await waitForLampTask('esp32_desk_led','missing-receipt',0)).result,'timeout',
    'Missing physical receipt must not be reported as still executing after the deadline');
  sandbox.apiClient.request = (_path, {signal}) => new Promise((_resolve, reject) =>
    signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'}))));
  assert.equal((await waitForLampTask('esp32_desk_led','stalled-query',20)).result,'timeout',
    'A stalled receipt query must have a bounded wait');
  sandbox.apiClient.request = async () => payload;
  sandbox.bus.waitForLampTask = async () => { waits++; return {result:'ok'}; };
  payload = {accepted:true,status:'dispatched',result:'accepted',task_id:'task_new'};
  assert.equal((await sandbox.bus.command('esp32_desk_led','BLINK',{count:2})).result,'ok');
  assert.equal(waits,1,'New lamp dispatch stopped waiting for physical completion');
  payload = {accepted:true};
  assert.equal((await sandbox.bus.command('esp32_desk_led','OFF')).result,'accepted');
  assert.equal(sandbox.bus.describe({result:'accepted',dispatch_only:true}), '指令已发送');
  console.log('PASS: explicit command outcomes survive legacy accepted flags; new lamp dispatch still waits for its result.');
}

async function checkInteractions() {
  const hooks = [];
  let index = 0;
  let effects = [];
  const calls = [];
  const sources = [];
  const React = {
    Fragment: Symbol('Fragment'),
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useState(initial) {
      const slot = index++;
      if (!(slot in hooks)) hooks[slot] = typeof initial === 'function' ? initial() : initial;
      return [hooks[slot], next => { hooks[slot] = typeof next === 'function' ? next(hooks[slot]) : next; }];
    },
    useRef(initial) { const slot = index++; return hooks[slot] ||= { current: initial }; },
    useEffect(callback, deps) {
      const slot = index++;
      const previous = hooks[slot];
      if (!previous || !deps || deps.some((value, i) => value !== previous.deps?.[i])) {
        effects.push(() => { previous?.cleanup?.(); hooks[slot] = { deps, cleanup: callback() }; });
      }
    },
    useMemo: callback => callback(),
  };
  const ChatPanel = () => null;
  const ui = vm.createContext({ React, window: {}, console, setTimeout, clearTimeout, ChatPanel,
    apiClient: { addEventListener() {}, removeEventListener() {} },
    commandBus: { describe: result => result.error || result.result,
      async command(unitId, action, params) { calls.push({unitId, action, params}); return { result: 'accepted' }; } },
    EventSource: function() { sources.push(this); this.close = () => {}; },
    Go2AssetsDrawer: () => null, apiErrorText: error => error.message,
  });
  for (const file of files) vm.runInContext(compile(file), ui, { filename: file });
  const sm = { state_field: 'fsm_state', connection_field: 'connected', states: ['standing', 'sitting'],
    transitions: [], views: { standing: {label:'站立'}, sitting: {label:'坐下'} },
    regions: [{ id:'body', label:'身体', state_field:'body_state', states:['standing','sitting'], views:{} }] };
  const card = { unit_id:'go2',device_id:'go2',name:'Go2',_online:true,
    transport:{state_stream:'/api/go2/connection/stream'},state_machine:sm,widgets:[],modes:[],skills:[] };
  const initial = {epoch:'a',revision:2,connected:true,body_state:'standing',fsm_state:'standing'};
  const find = (tree, type) => !tree || typeof tree !== 'object' ? null
    : Array.isArray(tree) ? tree.map(item => find(item, type)).find(Boolean)
    : tree.type === type ? tree : find(tree.props?.children, type);
  const render = (device = card, state = initial) => {
    index = 0; effects = [];
    const tree = ui.window.FsmDevicePage({unitId:device.unit_id,device,liveState:{state},actor:'visitor',onBack(){}});
    effects.forEach(callback => callback());
    return tree;
  };
  let board = find(render(), ui.window.HierarchicalFsmBoard);
  const transition = { action:'Sit', trigger:'Sit', dst:'sitting' };
  await Promise.all([board.props.onTransition(transition), board.props.onTransition(transition)]);
  assert.equal(calls.length, 1, 'Repeated click sent two commands');
  ui.commandBus.command = async () => { throw Error('simulated request failure'); };
  await board.props.onTransition(transition);
  sources[0].onmessage({data:JSON.stringify({...initial,epoch:'b',revision:1,body_state:null})});
  board = find(render(), ui.window.HierarchicalFsmBoard);
  assert.equal(board.props.liveState.body_state, null);
  sources[0].onerror();
  assert.equal(find(render(), ui.window.HierarchicalFsmBoard).props.streamHealth, 'reconnecting');
  sources[0].onmessage({data:JSON.stringify({...initial,epoch:'a',revision:999})});
  board = find(render(), ui.window.HierarchicalFsmBoard);
  assert.equal(board.props.liveState.body_state, null);
  const lamp = {unit_id:'lamp',name:'灯',_online:true,transport:{},state_machine:{states:['OFF','ON'],state_field:'ism',initial:'OFF',transitions:[],views:{}},widgets:[],modes:[],skills:[]};
  const flat = find(render(lamp, {ism:'ON',ts:1}), ui.window.DeviceStateBoard);
  assert.equal(flat.props.current, 'ON', 'Old device stream contaminated the lamp');
  sources[0].onmessage({data:JSON.stringify({...initial,epoch:'late',revision:1})});
  assert.equal(find(render(lamp, {ism:'ON',ts:1}), ui.window.DeviceStateBoard).props.current, 'ON');
  let previewCount = 0, fireCount = 0;
  const graph = ui.window.FsmGraph({ states:['standing','sitting'], current:'standing', views:{}, layout:{},
    transitions:[{src:'standing',dst:'sitting',trigger:'Sit',action:'Sit',label:'坐下'}],
    onSelectState() { previewCount++; }, onInspectTransition() { fireCount++; } });
  const descendants = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree)
    ? tree.flatMap(descendants) : [tree, ...descendants(tree.props?.children)];
  const previewNode = descendants(graph).find(node => node.props?.['aria-label'] === 'sitting，查看说明');
  previewNode.props.onClick({type:'click'});
  assert.equal(previewCount, 1);
  assert.equal(fireCount, 0, 'Node selection opened edge details');
  descendants(graph).find(node => node.props?.['data-action'] === 'Sit').props.onClick({type:'click'});
  assert.equal(fireCount, 1, 'Edge must open transition details');
  hooks.forEach(hook => hook?.cleanup?.());
  console.log('PASS: command duplicate guard and errors, SSE old epoch rejection, device switch isolation, flat lamp fallback, separate node selection and edge inspection.');
}

(async () => {
  await checkCommandResults();
  await checkInteractions();
  if (!process.argv.includes('--browser') && !process.argv.includes('--preview')) return;
  const sm = JSON.parse(process.env.GO2_STATE_MACHINE_JSON ? fs.readFileSync(process.env.GO2_STATE_MACHINE_JSON, 'utf8') : (await promisify(execFile)('uv', ['run', 'python', '-c',
    'import json; from cloud.go2.card import _go2_state_machine; print(json.dumps(_go2_state_machine()))'],
  { cwd: root, env: { ...process.env, UV_CACHE_DIR: process.env.UV_CACHE_DIR || '/tmp/ssm-uv' }, encoding: 'utf8' })).stdout);
  const lampCard = JSON.parse((await promisify(execFile)('uv', ['run','python','-c',
    'import json; from cloud.cards.builder import build_card_from_manifest; print(json.dumps(build_card_from_manifest({"unit_id":"esp32_desk_led","name":"ws2812_ring","agent_type":"actuator","fsm_version":2,"capabilities":[{"action":"SET_COLOR"},{"action":"SET_STATE"},{"action":"BLINK"}]})))'],
    {cwd:root,env:{...process.env,UV_CACHE_DIR:process.env.UV_CACHE_DIR || '/tmp/ssm-uv-cache'},encoding:'utf8'})).stdout);
  const velocitySchema = JSON.parse((await promisify(execFile)('uv', ['run','python','-c',
    'import json; from cloud.go2.actions import velocity_schema; print(json.dumps(velocity_schema()))'],
    {cwd:root,env:{...process.env,UV_CACHE_DIR:process.env.UV_CACHE_DIR || '/tmp/ssm-uv-cache'},encoding:'utf8'})).stdout);
  const React = require('react');
  const ReactDOMServer = require('react-dom/server');
  const previewContext = vm.createContext({ React, window: {}, console });
  for (const name of ['app/src/utils/stateMachine.js', 'app/src/pages/StateVisual.jsx', 'app/src/pages/FsmGraph.jsx', 'app/src/pages/HierarchicalFsmBoard.jsx']) {
    vm.runInContext(compile(name), previewContext, {filename:name});
  }
  const initialState = {state_schema_version:2,epoch:'preview',revision:1,connected:true,
    connection_state:'online',activity_state:'active',life_state:'idle',body_state:'standing',
    body_confirmation:'observed',control_mode:'normal',autonomy_mode:'manual'};
  const previewCases = [initialState,
    {...initialState,activity_state:'rest',life_state:null,body_state:'lying',available_actions:[],available_life_actions:['wake']},
    {...initialState,life_state:'explore',body_state:'moving',control_mode:'remote',available_actions:['StopMove'],available_life_actions:['stop_explore']}];
  const previews = previewCases.map(state => ReactDOMServer.renderToStaticMarkup(React.createElement(
    previewContext.window.HierarchicalFsmBoard, {unitId:'go2',sm,current:state.body_state,
      liveState:state,widgets:[],connected:true,offline:false,actor:'admin',color:'#C8FF3E',
      onSelectState(){},onTransition(){},report(){}})));
  fs.writeFileSync('/tmp/go2-hierarchical-preview.html', '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Go2 模拟状态前端预览</title><style>*{box-sizing:border-box}body{margin:0;background:#0B0B0E;color:#ddd;font-family:sans-serif}h1{font-size:16px;padding:18px}main{display:flex;flex-wrap:wrap;gap:20px;padding:20px}.preview{width:390px;height:720px;border:1px solid #333}'+read('app/styles/app.css')+'</style></head><body><h1>浏览器验证用模拟状态 · 非真机截图 · 控制不可执行</h1><main>'+previews.map(html=>'<section class="preview">'+html+'</section>').join('')+'</main></body></html>');
  console.log('Rendered actual components with simulated states: /tmp/go2-hierarchical-preview.html');
  if (!process.argv.includes('--browser')) return;
  const puppeteer = require('puppeteer');
  const moduleFile = (module, file) => fs.readFileSync(path.join(path.dirname(require.resolve(module + '/package.json')), 'cjs', file), 'utf8');
  const modules = {
    react: moduleFile('react', 'react.production.js'),
    'react-dom': moduleFile('react-dom', 'react-dom.production.js'),
    'react-dom/client': moduleFile('react-dom', 'react-dom-client.production.js'),
    scheduler: moduleFile('scheduler', 'scheduler.production.js'),
  };
  const runtime = `const modules=${JSON.stringify(modules)}, loaded={};
    function require(id) { if (loaded[id]) return loaded[id].exports;
      const module={exports:{}}; loaded[id]=module;
      new Function('module','exports','require',modules[id])(module,module.exports,require); return module.exports; }
    window.React=require('react'); window.ReactDOM={...require('react-dom'),...require('react-dom/client')};`;
  const browser = await puppeteer.launch({ headless: true,
    ...(process.env.PUPPETEER_EXECUTABLE_PATH ? { executablePath: process.env.PUPPETEER_EXECUTABLE_PATH } : {}),
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:', error.message); });
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (/^(data:|blob:)/.test(request.url())) return request.continue();
      if (request.url().includes('/api/go2/map.png') || request.url().includes('/api/go2/video/snapshot')) {
        return request.respond({status:200,contentType:'image/svg+xml',headers:{'access-control-allow-origin':'*'},body:'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#14141f"/><path d="M30 30h260v120H30zM30 90h90V30" fill="none" stroke="#666" stroke-width="8"/><path d="M155 80l10 20-10-5-10 5z" fill="#c8ff3e"/><text x="100" y="165" fill="#999" font-size="10">SIMULATED</text></svg>'});
      }
      return request.abort();
    });
    await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1, isMobile: true, hasTouch: true });
    await page.setContent('<html lang="zh-CN"><head><base href="https://ssm-test.invalid/"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>');
    await page.addStyleTag({ content: '*{box-sizing:border-box;margin:0}html,body,#root{height:100%}body{background:#0B0B0E}' + read('app/styles/app.css') });
    if (process.env.GO2_PREVIEW_FONT) {
      const fontData = fs.readFileSync(process.env.GO2_PREVIEW_FONT).toString('base64');
      await page.evaluate(async data => {
        const font = new FontFace('PreviewCJK', `url(data:font/otf;base64,${data})`);
        await font.load(); document.fonts.add(font);
        document.body.style.setProperty('--font-sans', 'PreviewCJK, sans-serif');
        document.body.style.setProperty('--font-mono', 'PreviewCJK, monospace');
        document.body.style.fontFamily = 'PreviewCJK, sans-serif';
      }, fontData);
      await page.addStyleTag({content:'input,textarea,button{font-family:PreviewCJK,sans-serif}'});
    }
    if (process.env.GO2_PREVIEW_REACT && process.env.GO2_PREVIEW_REACT_DOM) {
      await page.addScriptTag({ content: fs.readFileSync(process.env.GO2_PREVIEW_REACT, 'utf8') });
      await page.addScriptTag({ content: fs.readFileSync(process.env.GO2_PREVIEW_REACT_DOM, 'utf8') });
    } else await page.addScriptTag({ content: runtime });
    console.log('Browser React runtime:', await page.evaluate(() => React.version));
    await page.addScriptTag({ content: `window.__calls=[]; window.__sources=[];window.__velocities=[];
      window.apiClient={addEventListener(){},removeEventListener(){}};
      window.apiErrorText=error=>error.message;
      window.commandBus={describe:r=>r.result==='accepted'?'指令已发送，尚未确认完成':'执行完成',
        command:async(unit,action,params)=>{__calls.push({unit,action,params}); await new Promise(r=>setTimeout(r,100));return {result:'accepted'};},
        velocity(unit,action,params){__velocities.push({unit,action,params});}};
      window.EventSource=function(){__sources.push(this);this.close=()=>{};};
      window.Icon=()=>null;window.Go2AssetsDrawer=()=>null;window.LampAssetsDrawer=()=>null;window.AgentPersonalitySettings=()=>null;
      window.ChatPanel=props=>React.createElement('div',{style:{padding:'18px',fontSize:'13px',color:'#999'}},
        React.createElement('p',null,'浏览器验证 · 模拟状态，不连接真机'),
        React.createElement('textarea',{placeholder:props.placeholder,disabled:props.disabled,'aria-label':'设备对话',style:{marginTop:18,width:'100%',height:60,background:'#17171b',color:'#ccc',border:'1px solid #333',borderRadius:10,padding:12}}));` });
    for (const name of files) await page.addScriptTag({ content: compile(name) });
    await page.addScriptTag({ content: `window.__velocitySchema=${JSON.stringify(velocitySchema)};window.__lampCard=${JSON.stringify(lampCard)};window.__sm=${JSON.stringify(sm)};
      window.__state={state_schema_version:2,epoch:'a',revision:1,connected:true,connection_state:'online',activity_state:'active',life_state:'idle',body_state:'standing',fsm_state:'standing',body_confirmation:'observed',control_mode:'normal',autonomy_mode:'manual',available_actions:['StandDown','Sit','Hello','Stretch','Content','Dance1','Dance2','FingerHeart','FrontPounce','FrontJump','EnterMoveControl','StopMove'],available_life_actions:['request_rest','start_explore']};
      window.__manualCommands=__sm.regions.find(region=>region.id==='body').transitions.filter(t=>t.dispatch_only).map(t=>t.action);
      __state.available_manual_actions=__manualCommands;
      window.__card={unit_id:'go2',device_id:'go2',name:'Go2 机器狗',agent_type:'robot',_online:true,state_machine:__sm,transport:{state_stream:'/api/go2/connection/stream'},widgets:[],modes:[],skills:[]};
      const appRoot=ReactDOM.createRoot(document.getElementById('root'));
      window.__renderState=(next,card=__card,actor='admin')=>{__state=next;appRoot.render(React.createElement(FsmDevicePage,{unitId:card.unit_id,device:card,liveState:{state:next},actor,onBack(){}}));};
      __renderState(__state);` });

    await page.waitForSelector('.device-state-board');
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.$$('.device-state-lane,.device-state-action-row,.device-state-topology,.device-region-diagram').then(nodes=>nodes.length),0);
    assert.equal(await page.$eval('.device-header__status',node=>node.textContent),'在线');
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="activity:active"],.fsm-whiteboard__node[data-state^="connection:"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current').then(nodes=>nodes.length),2,'Parallel current facts were lost');
    assert.equal(await page.$$('.fsm-whiteboard__node[data-group="行为"]').then(nodes=>nodes.length),4);
    assert.equal(await page.$$('.fsm-whiteboard__node[data-group="姿态与动作"]').then(nodes=>nodes.length),12);
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="body:unknown"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$eval('.fsm-whiteboard__node[data-group="行为"]',nodes=>nodes.every(node=>Number(node.dataset.radius)>40)),true);
    assert.equal(await page.$$eval('.fsm-whiteboard__node[data-group="姿态与动作"]',nodes=>nodes.every(node=>Number(node.dataset.radius)<40)),true);
    const closeExplanation = async () => {
      for (const label of ['关闭状态说明','关闭转换说明']) {
        if (await page.$(`[aria-label="${label}"]`)) await page.tap(`[aria-label="${label}"]`);
      }
    };
    const clickNode = async state => {
      await closeExplanation();
      const selector = `.fsm-whiteboard__node[data-state="${state}"]`;
      await page.waitForFunction(selector => document.querySelector(selector)?.dataset.executable === 'true', {}, selector);
      const before = await page.evaluate(()=>__calls.length);
      await page.tap(selector);
      await page.waitForFunction(before => __calls.length === before + 1, {}, before);
      await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
    };
    const clickAction = label => clickNode({'休息':'activity:rest','唤醒':'life:idle',
      '开启探索':'life:explore','停止探索':'life:idle','关灯':'device:OFF','关闭':'mechanism:closed'}[label]);
    await page.evaluate(()=>{
      __card.widgets=[{type:'video',endpoint:'/api/go2/video',states:['standing','unknown','lying','moving'],requires_connection:true},
        {type:'map',endpoint:'/api/go2/map.png',states:[],requires_connection:true},
        {type:'joystick',action:'velocity',params_schema:__velocitySchema,states:['standing','moving'],state_field:'body_state',
          node_state:'moving',enter_action:'EnterMoveControl',exit_action:'StopMove',
          enabled_when:{control_mode:'remote'},available_actions_field:'available_actions',availability_key:'Move'}];
      __renderState(__state);
    });
    await page.waitForSelector('.device-whiteboard__media');
    assert.equal(await page.$$('.device-remote-controls').then(nodes=>nodes.length),0,'Remote controls should be contextual');
    assert.equal(await page.$$('.fsm-whiteboard__edge[data-trigger="action_completed"]').then(nodes=>nodes.length)>0,true,'Completion links should be visible alongside outgoing actions');
    const assertAllStatesVisible = async () => {
      assert.equal(await page.$eval('.fsm-whiteboard__viewport',svg=>{
        const bounds=svg.getBoundingClientRect();
        return [...svg.querySelectorAll('.fsm-whiteboard__node')].every(node=>{
          const r=node.getBoundingClientRect();
          return r.left>=bounds.left-1 && r.right<=bounds.right+1 && r.top>=bounds.top-1 && r.bottom<=bounds.bottom+1;
        });
      }),true,'A state was clipped outside the initial whiteboard viewport');
    };
    await assertAllStatesVisible();
    const assertMediaWindow = async selector => {
      assert.equal(await page.$eval(selector,node=>{
        const r=node.getBoundingClientRect();
        return r.left>=8 && r.right<=innerWidth-8 && r.top>=8 && r.bottom<=innerHeight-8
          && Math.abs(r.left+r.width/2-innerWidth/2)<2 && Math.abs(r.top+r.height/2-innerHeight/2)<2
          && r.width>200 && node.getAttribute('aria-modal')==='true' && node.matches(':modal');
      }),true,'Both media viewers must be centered and contained in the viewport');
      assert.equal(await page.$eval(`${selector} .device-media-viewer__image`,node=>{
        const media=node.querySelector('img,canvas');
        return media && getComputedStyle(media).objectFit==='contain';
      }),true,'Expanded media must keep its aspect ratio');
    };
    assert.equal(await page.$$('.device-whiteboard__media-tile').then(nodes=>nodes.length),2);
    assert.equal(await page.$$eval('.device-whiteboard__media-tile',nodes=>nodes.every(node=>node.getBoundingClientRect().height<=90)),true,'Media too tall on a phone');
    await page.waitForSelector('[aria-label="放大地图"] img');
    for (const [width,height] of [[390,844],[1440,1000],[844,390]]) {
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      const thumbnailBounds=await page.$$eval('.device-whiteboard__media-tile',nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
      }));
      let viewerWidth;
      for (const [label,title,file] of [['地图','导航地图','map'],['视频','实时视频','video']]) {
        const trigger=`[aria-label="放大${label}"]`;
        const dialog=`.device-media-viewer[aria-label="${title}"]`;
        await page.tap(trigger);
        await page.waitForSelector(dialog);
        await assertMediaWindow(dialog);
        const actualWidth=await page.$eval(dialog,node=>node.getBoundingClientRect().width);
        if (viewerWidth) assert.equal(actualWidth,viewerWidth,'Video and map use different viewer widths');
        viewerWidth=actualWidth;
        await page.focus('[aria-label="设备对话"]');
        assert.equal(await page.$eval(dialog,node=>node.contains(document.activeElement)),true,'Media dialog lost focus to background');
        await page.click(`${dialog} .device-media-viewer__image`);
        assert.equal(await page.$(dialog)!==null,true,'Clicking media closed the viewer');
        if(width===390) await page.screenshot({path:`/tmp/ssm-${file}-centered.png`});
        await page.keyboard.press('Escape');
        await page.waitForFunction(()=>!document.querySelector('.device-media-viewer'));
        assert.equal(await page.$eval(trigger,node=>document.activeElement===node),true,'Closing did not restore thumbnail focus');
        await page.tap(trigger);
        await page.waitForSelector(dialog);
        await page.tap(`[aria-label="关闭${label}"]`);
        await page.waitForFunction(()=>!document.querySelector('.device-media-viewer'));
        await page.tap(trigger);
        await page.waitForSelector(dialog);
        await page.touchscreen.tap(4,4);
        await page.waitForFunction(()=>!document.querySelector('.device-media-viewer'));
      }
      assert.deepEqual(await page.$$eval('.device-whiteboard__media-tile',nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect();return [r.x,r.y,r.width,r.height];
      })),thumbnailBounds,'Opening media changed thumbnail layout');
    }
    await page.setViewport({width:390,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
    const connectorPoint=await page.$eval('.fsm-whiteboard__viewport',svg=>{
      const point=svg.createSVGPoint();point.x=195;point.y=398;
      const screen=point.matrixTransform(svg.getScreenCTM());return {x:screen.x,y:screen.y};
    });
    await page.touchscreen.tap(connectorPoint.x,connectorPoint.y);
    assert.equal(await page.evaluate(()=>__calls.length),0,'Shared background connector dispatched an arbitrary action');
    await closeExplanation();
    await clickNode('body:sitting');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Sit');
    await page.waitForSelector('.device-whiteboard__preview');
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current[data-state="body:standing"]').then(nodes=>nodes.length),1,'Command acceptance changed observed state');
    await page.tap('[aria-label="关闭状态说明"]');
    await clickNode('body:greeting');
    assert.equal(await page.$eval('.fsm-whiteboard__edge[data-trigger="Hello"] text',node=>node.textContent),'挥手');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Hello');
    await page.tap('[aria-label="关闭状态说明"]');
    await clickNode('body:lying');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'StandDown');
    await closeExplanation();
    const beforeInspect = await page.evaluate(()=>__calls.length);
    await page.$eval('.fsm-whiteboard__edge[data-trigger="Sit"]',node=>node.focus());
    await page.keyboard.press('Enter');
    await page.waitForSelector('[aria-label="转换说明"]');
    assert.equal(await page.evaluate(()=>__calls.length),beforeInspect,'Edge inspection sent a command');
    await page.click('[aria-label="转换说明"] .btn');
    await page.waitForFunction(before => __calls.length === before + 1, {}, beforeInspect);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Sit','Explicit edge action lost its declared command');
    await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
    await closeExplanation();
    await page.tap('.fsm-whiteboard__node[data-state="body:standing"]');
    assert.equal(await page.evaluate(()=>__calls.length),beforeInspect+1,'Current state click repeated a command');
    await closeExplanation();
    await page.evaluate(()=>{__calls=[];});
    const originalTransform=await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform'));
    // The renderer must also fit previously retained layouts, even if their
    // coordinates still contain an old horizontally overflowing action.
    await page.evaluate(()=>{
      const layout=__card.state_machine.regions.find(region=>region.id==='body').layout;
      window.__originalJumpX=layout.jumping.x;layout.jumping.x=2.1;__renderState(__state);
    });
    await page.waitForFunction(()=>{
      const node=document.querySelector('[data-state="body:jumping"]').getBoundingClientRect();
      const bounds=document.querySelector('.fsm-whiteboard__viewport').getBoundingClientRect();
      return node.right<=bounds.right+1;
    });
    await assertAllStatesVisible();
    await page.evaluate(()=>{
      __card.state_machine.regions.find(region=>region.id==='body').layout.jumping.x=__originalJumpX;
      __renderState(__state);
    });
    await page.waitForFunction(expected=>document.querySelector('.fsm-whiteboard__viewport > g').getAttribute('transform')===expected,{},originalTransform);
    await page.tap('[aria-label="放大白板"]');
    assert.notEqual(await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform')),originalTransform);
    await page.$$eval('.fsm-whiteboard__toolbar button',nodes=>nodes.find(node=>node.textContent==='适应').click());
    const box=await page.$eval('.fsm-whiteboard__viewport',node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
    const center={x:box.x+box.width/2,y:box.y+box.height/2};
    await page.mouse.move(center.x,center.y);
    await page.mouse.down();await page.mouse.move(center.x+45,center.y+25,{steps:6});await page.mouse.up();
    assert.notEqual(await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform')),originalTransform,'Canvas did not pan');
    assert.equal(await page.evaluate(()=>__calls.length),0,'Dragging sent a command');
    await page.$$eval('.fsm-whiteboard__toolbar button',nodes=>nodes.find(node=>node.textContent==='适应').click());
    const touchClient=await page.createCDPSession();
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:center.x-35,y:center.y,id:1},{x:center.x+35,y:center.y,id:2}]});
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:center.x-65,y:center.y,id:1},{x:center.x+65,y:center.y,id:2}]});
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    assert.notEqual(await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform')),originalTransform,'Pinch did not zoom');
    await page.$$eval('.fsm-whiteboard__toolbar button',nodes=>nodes.find(node=>node.textContent==='适应').click());
    await page.screenshot({path:'/tmp/go2-whiteboard-mobile.png',fullPage:true});
    await page.evaluate(()=>{
      const edge=document.querySelector('.fsm-whiteboard__node[data-state="body:sitting"]');
      edge.dispatchEvent(new MouseEvent('click',{bubbles:true}));edge.dispatchEvent(new MouseEvent('click',{bubbles:true}));
    });
    assert.equal(await page.evaluate(()=>__calls.length),1,'Duplicate request escaped guard');
    await page.waitForFunction(()=>document.body.textContent.includes('尚未确认完成'));
    await clickAction('休息');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'request_rest');
    await page.evaluate(()=>__renderState({...__state,revision:2,activity_state:'rest',life_state:null,body_state:'lying',fsm_state:'lying',available_actions:[],available_life_actions:['wake']}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="activity:rest"]');
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current[data-state^="body:"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$('.fsm-whiteboard__node.is-retained[data-state="body:lying"]').then(nodes=>nodes.length),1);
    await page.screenshot({path:'/tmp/go2-whiteboard-rest-mobile.png',fullPage:true});
    await clickAction('唤醒');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'wake');
    await page.evaluate(()=>__sources[0].onmessage({data:JSON.stringify({...__state,epoch:'b',revision:1,activity_state:'active',life_state:'idle',body_state:null,fsm_state:null,body_confirmation:'unknown'})}));
    await page.waitForFunction(()=>document.querySelectorAll('.fsm-whiteboard__node.is-current[data-state^="body:"]').length===0);
    assert.equal(await page.$('.device-header__stop'),null,'Go2 header should not show a stop button');
    await page.click('.device-header__settings');
    assert.equal(await page.$eval('.device-settings-safety',node=>node.disabled),false,'No feedback removed the settings stop action');
    await page.click('[aria-label="关闭设备设置"]');
    await page.evaluate(()=>__sources[0].onmessage({data:JSON.stringify({...__state,epoch:'a',revision:99,body_state:'standing'})}));
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="body:unknown"],.fsm-whiteboard__node.is-current[data-state^="body:"]').then(nodes=>nodes.length),0);
    await page.evaluate(()=>__renderState({...__state,epoch:'c',revision:1,activity_state:'active',life_state:'idle',body_state:'standing',fsm_state:'standing',available_actions:['EnterMoveControl','StopMove'],available_life_actions:['request_rest','start_explore','stop_explore']},__card,'visitor'));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="body:standing"]');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge',nodes=>nodes.find(node=>node.dataset.label==='开启探索').dataset.available),'false');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge',nodes=>nodes.some(node=>node.dataset.label==='停止探索')),true,'Idle exploration permission has no stop action');
    await page.evaluate(()=>__renderState(__state));
    await page.waitForFunction(()=>[...document.querySelectorAll('.fsm-whiteboard__edge')].find(node=>node.dataset.label==='开启探索')?.dataset.available==='true');
    await clickAction('开启探索');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'start_explore');
    await clickNode('body:moving');
    await page.waitForSelector('.device-remote-controls');
    assert.equal(await page.$eval('[aria-label="移动摇杆，方向键控制，松开停止"]',node=>node.getAttribute('aria-disabled')),'true');

    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'EnterMoveControl');
    assert.equal(await page.$eval('[aria-label="移动摇杆，方向键控制，松开停止"]',node=>node.getAttribute('aria-disabled')),'true','RPC acceptance enabled movement before state update');
    await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
    await page.evaluate(()=>__renderState({...__state,revision:2,control_mode:'remote',available_actions:['Move','StopMove']}));
    await page.waitForFunction(()=>document.querySelector('[aria-label="移动摇杆，方向键控制，松开停止"]').getAttribute('aria-disabled')==='false');
    // A Card limit change must affect the actual control without editing JSX.
    await page.evaluate(()=>{
      __card.widgets.find(widget=>widget.type==='joystick').params_schema.properties.x.maximum=.2;
      __renderState({...__state,revision:__state.revision+1});
    });
    await page.focus('[aria-label="移动摇杆，方向键控制，松开停止"]');
    await page.keyboard.down('ArrowUp');
    await page.waitForFunction(()=>__velocities.at(-1)?.params.vx===.2);
    await page.keyboard.up('ArrowUp');
    await page.waitForFunction(()=>__velocities.at(-1)?.params.vx===0);
    const pads=await page.$$eval('.device-joysticks [role="group"]',nodes=>nodes.map(node=>{
      const r=node.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
    }));
    const moveTouch=await page.touchscreen.touchStart(pads[0].x,pads[0].y-20);
    const turnTouch=await page.touchscreen.touchStart(pads[1].x+20,pads[1].y);
    await page.waitForFunction(()=>__velocities.some(item=>item.params.vx>0&&item.params.vyaw<0));
    await moveTouch.end();
    await page.waitForFunction(()=>__velocities.at(-1).params.vx===0&&__velocities.at(-1).params.vyaw<0);
    await turnTouch.end();
    await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
    await page.screenshot({path:'/tmp/go2-whiteboard-controls-mobile.png',fullPage:true});
    const beforeExit=await page.evaluate(()=>__calls.length);
    await page.$$eval('.device-remote-controls button',nodes=>nodes.find(node=>node.textContent==='退出遥控').click());
    await page.waitForFunction(before=>__calls.length>before,{},beforeExit);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'StopMove');
    await page.evaluate(()=>__renderState({...__state,revision:3,control_mode:'normal',available_actions:['EnterMoveControl','StopMove']}));
    await page.waitForFunction(()=>document.querySelector('[aria-label="移动摇杆，方向键控制，松开停止"]').getAttribute('aria-disabled')==='true');
    await page.tap('.fsm-whiteboard__node[data-state="body:standing"]');
    await page.waitForFunction(()=>!document.querySelector('.device-remote-controls'));
    await closeExplanation();
    await page.evaluate(()=>{
      __renderState({...__state,epoch:'d',revision:1,life_state:'explore',body_state:'moving',fsm_state:'moving',control_mode:'remote',available_actions:['Move','StopMove'],available_life_actions:['stop_explore']});
    });
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="body:moving"]');
    await clickAction('停止探索');
    await page.focus('[aria-label="移动摇杆，方向键控制，松开停止"]');
    await page.keyboard.down('ArrowUp');await page.waitForFunction(()=>__velocities.length>0);await page.keyboard.up('ArrowUp');
    assert.equal(await page.evaluate(()=>__velocities.at(-1).params.vx),0);
    await page.tap('[aria-label="白板全屏"]');
    await page.waitForSelector('.device-whiteboard.is-expanded');
    await page.tap('[aria-label="放大视频"]');
    await page.waitForSelector('.device-media-viewer');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('.device-media-viewer'));
    assert.equal(await page.$('.device-whiteboard.is-expanded')!==null,true,'Closing video also closed the board');
    assert.equal(await page.$eval('.device-whiteboard.is-expanded .device-whiteboard__feedback',
      node=>node.textContent.includes('指令已发送')),true,'Fullscreen hid command feedback');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('.device-whiteboard.is-expanded'));
    for(const [width,height] of [[1440,1000],[768,1024],[360,740]]){
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await assertAllStatesVisible();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Overflow at ${width}`);
      assert.equal(await page.$eval('[aria-label="设备对话"]',node=>{const r=node.getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0;}),true,'Chat left the phone viewport');
      if(width===1440) await page.screenshot({path:'/tmp/go2-whiteboard-desktop.png',fullPage:true});
    }
    await page.focus('[aria-label="移动摇杆，方向键控制，松开停止"]');
    await page.keyboard.down('ArrowUp');
    await page.waitForFunction(()=>__velocities.at(-1).params.vx>0);
    await page.evaluate(()=>__sources[0].onerror());
    await page.waitForFunction(()=>document.body.textContent.includes('普通操作等待同步'));
    await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
    await page.keyboard.up('ArrowUp');
    assert.equal(await page.$eval('.device-remote-controls button',node=>node.disabled),false,'Stale stream disabled remote exit');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge[data-action]',nodes=>nodes.every(node=>node.dataset.available==='false')),true,'Stale stream allowed an ordinary action');
    assert.equal(await page.$('.device-header__stop'),null,'Go2 header should remain compact when the stream is stale');
    await page.click('.device-header__settings');
    assert.equal(await page.$eval('.device-settings-safety',node=>node.disabled),false,'Stale stream disabled the settings stop action');
    await page.click('[aria-label="关闭设备设置"]');
    await page.evaluate(()=>__renderState({ism:'ON',ts:1},{unit_id:'lamp',name:'灯',_online:true,transport:{},state_machine:{states:['OFF','ON'],state_field:'ism',initial:'OFF',views:{OFF:{label:'关'},ON:{label:'开'}},transitions:[{src:'ON',dst:'OFF',trigger:'off',action:'SET_STATE',label:'关灯',params:{state:'OFF'}}]},widgets:[],modes:[],skills:[]}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="device:ON"]');
    assert.equal(await page.$$('.fsm-whiteboard__node').then(nodes=>nodes.length),2);
    await clickAction('关灯');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.state),'OFF');
    await page.evaluate(()=>__renderState({ism:'ON',ts:100,r:255,g:200,b:80,brightness:180,autonomy_mode:'reactive',control_state:'holding',control_hold_until:2000000000,control_ts:101},{...__lampCard,_online:true}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="device:ON"]');
    assert.deepEqual(await page.$$eval('.fsm-whiteboard__node', nodes => nodes.map(node => node.dataset.state).sort()),
      ['device:BLINKING','device:OFF','device:ON']);
    assert.equal(await page.$('[aria-label="选择灯光颜色"]')!==null,true,'Lamp color controls lost');
    await page.focus('[aria-label="选择灯光颜色"]');
    const beforeColor=await page.evaluate(()=>__calls.length);
    await page.keyboard.press('ArrowRight');await page.waitForFunction(before=>__calls.length>before,{},beforeColor);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'SET_COLOR');
    await page.click('.device-header__settings');
    assert.equal(await page.$$eval('.device-settings-axis button.is-active',nodes=>nodes.some(node=>node.textContent.includes('自动调光'))),true);
    await page.click('[aria-label="关闭设备设置"]');
    await page.screenshot({path:'/tmp/ssm-whiteboard-lamp-mobile.png',fullPage:true});
    for (const source of ['OFF','ON','BLINKING']) {
      for (const target of ['OFF','ON','BLINKING'].filter(state=>state!==source)) {
        await closeExplanation();
        await page.evaluate(source=>__renderState({...__state,ism:source,ts:__state.ts+1},{...__lampCard,_online:true}),source);
        await page.waitForSelector(`.fsm-whiteboard__node.is-current[data-state="device:${source}"]`);
        await clickNode(`device:${target}`);
        const call=await page.evaluate(()=>__calls.at(-1));
        assert.equal(call.action,target==='BLINKING'?'BLINK':'SET_STATE');
        if(target!=='BLINKING') assert.equal(call.params.state,target);
        assert.equal(await page.$(`.fsm-whiteboard__node.is-current[data-state="device:${source}"]`)!==null,true,'Lamp changed before feedback');
      }
    }
    await closeExplanation();
    await page.evaluate(()=>__renderState({epoch:'receipt-check',revision:1,connected:true,
      connection_state:'online',activity_state:'active',life_state:'idle',body_state:null,
      body_telemetry_fresh:false,available_manual_actions:__manualCommands,
      pending_action:null,last_action:null,available_actions:['StopMove']}));
    await page.waitForFunction(()=>document.querySelector('.device-whiteboard__feedback')?.textContent.includes('手动动作仍可发送'));
    await clickNode('body:sitting');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Sit','Missing posture blocked manual dispatch');
    await clickNode('body:greeting');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Hello','Next manual action waited for posture feedback');
    await closeExplanation();
    await page.evaluate(()=>__renderState({...__state,revision:2,body_state:'standing',pending_action:null,
      last_action:{command:'Hello',phase:'completed',result:'ok'}}));
    await page.waitForFunction(()=>document.querySelector('.device-whiteboard__feedback')?.textContent.includes('挥手 · 已完成'));
    assert.equal(await page.$$('.fsm-whiteboard__edge.is-running').then(nodes=>nodes.length),0,
      'A completed receipt must not animate as a running command');
    await page.evaluate(()=>{
      const machine={state_field:'position',connection_field:'attached',primary_region:'mechanism',regions:[{id:'mechanism',label:'位置',state_field:'position',states:['open','closed'],views:{open:{label:'打开'},closed:{label:'关闭'}},available_actions_field:'allowed',blocked_actions_field:'reasons',transitions:[{src:'open',dst:'closed',trigger:'close',action:'SET_POSITION',availability_key:'close',label:'关闭'}]}]};
      __renderState({position:'open',attached:true,allowed:['close'],ts:2},{unit_id:'generic',name:'设备',_online:true,state_machine:machine,transport:{},widgets:[],modes:[],skills:[]});
    });
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="mechanism:open"]');
    await clickAction('关闭');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'SET_POSITION');
    await page.evaluate(()=>__renderState({...__state,ts:3,attached:false},{...__card,_online:false,unit_id:'generic',transport:{}}));
    await page.waitForFunction(()=>document.querySelector('.device-header__status').textContent==='未连接');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge[data-action]',nodes=>nodes.every(node=>node.dataset.available==='false')),true);
    // Verify the real shared chat as well; a stub cannot catch flex overflow.
    await page.addScriptTag({content:compile('app/src/components/ChatPanel.jsx')});
    await page.evaluate(()=>{
      __renderState({epoch:'real-chat',revision:1,connected:true,connection_state:'online',activity_state:'active',life_state:'idle',body_state:'standing',fsm_state:'standing',available_actions:['StandDown','Sit','Hello','Stretch','Content','Dance1','Dance2','FingerHeart','FrontPounce','FrontJump','EnterMoveControl'],available_life_actions:['request_rest','start_explore']});
    });
    await page.waitForSelector('.device-page__chat input');
    for(const [width,height] of [[390,844],[360,740],[390,520]]) {
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      assert.equal(await page.$eval('.device-page__chat input',node=>{const r=node.getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0;}),true,`Real chat clipped at ${width}×${height}`);
      await assertAllStatesVisible();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if(height===844) await page.screenshot({path:'/tmp/go2-whiteboard-mobile.png',fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: centered shared video/map dialogs, matching close/backdrop/Escape behavior and focus restoration, portrait/landscape/desktop media sizing, all state nodes inside the viewport, contextual dual joystick, target state commands, edge explanations, pan/pinch/zoom/reset, duplicate guard, permissions, stale/offline gating and safety, lamp controls, SSE ordering, responsive layouts.');
    console.log('Screenshots: /tmp/go2-whiteboard-{mobile,rest-mobile,desktop}.png, /tmp/ssm-whiteboard-lamp-mobile.png');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
