/* No build step or hardware. NODE_PATH must expose sucrase/react/react-dom;
 * --browser also needs puppeteer and a local Chrome (or PUPPETEER_EXECUTABLE_PATH).
 * Optional GO2_PREVIEW_REACT/GO2_PREVIEW_REACT_DOM select the product's cached UMD;
 * GO2_PREVIEW_FONT embeds a small local CJK font solely for readable screenshots.
 * --rule-layout-only checks direction tabs, lamp layout and immediate deletion.
 * --control-notices-only checks shared chat feedback for state-node commands.
 * --layout-only checks mobile/desktop layout, retained drafts, simulated keyboard
 * viewport and management drawer; SCREENSHOT_DIR optionally saves previews.
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
const grouped = context.window.stateGraphLinks([
  {id:'manual',src:'a',dst:'b',transition:{action:'GO'}},
  {id:'automatic',src:'a',dst:'b',transition:{trigger:'done'}},
  {id:'reverse',src:'b',dst:'a',transition:{}},
  {id:'loop',src:'a',dst:'a',transition:{}},
]);
assert.equal(grouped.length,1,'A node pair must have one line');
assert.equal(grouped[0].directions.length,2);
assert.equal(grouped[0].directions[0].edges.length,2,'Automatic rule was discarded behind a manual rule');
assert.equal(context.window.stateGraphLinks([{src:'on',dst:'on',hasTeaching:true,transition:{action:'SET_COLOR'}}]).length,1,
  'A taught parameter change disappeared because its physical state stays the same');
assert.equal(context.window.stateTransitionLabel({trigger:'action_completed',label:'动作完成并确认姿态'}),'动作执行完毕');
const sittingNode = {id:'body:sitting',label:'坐下',current:false};
const sitEdge = {dst:'body:sitting',active:true,reason:'',transition:{action:'Sit',params:{}}};
assert.equal(nodeControl(sittingNode,[sitEdge]).transition.action,'Sit','Target node must resolve its legal command');
assert.equal(nodeControl(sittingNode,[{...sitEdge,active:false}]).transition,null,'Node click must not invent a multi-step route');
assert.equal(nodeControl(sittingNode,[{...sitEdge,reason:'等待有效设备反馈'}]).reason,'等待有效设备反馈');
assert.equal(nodeControl({...sittingNode,current:true},[sitEdge]).transition,null,'Current state must not re-execute');
assert.equal(nodeControl({...sittingNode,current:true},[{...sitEdge,transition:{action:'Sit',dispatch_only:true}}]).transition,null,
  'Manual dispatch must not re-execute the current posture');
assert.equal(nodeControl(sittingNode,[sitEdge,{...sitEdge,transition:{action:'Other',params:{}}}]).transition,null,'Ambiguous targets need explicit action selection');
assert.equal(nodeControl(sittingNode,[sitEdge,{...sitEdge,src:'lying'}]).choices.length,1,'Identical commands were presented as separate choices');
const automaticEdge={dst:'body:sitting',active:true,reason:'',transition:{trigger:'observed'}};
assert.equal(nodeControl(sittingNode,[automaticEdge]).reachable,true,'Automatic destination was shown as unavailable');
assert.equal(nodeControl(sittingNode,[automaticEdge]).transition,null,'Automatic event became a command');
assert.equal(nodeControl(sittingNode,[{...automaticEdge,active:false}]).reachable,false);
assert.equal(nodeControl(sittingNode,[{...automaticEdge,reason:'offline'}]).reachable,false);
const feedbackMachine={primary_region:'mode',regions:[{id:'mode',state_field:'phase',states:['ready','working'],
  transitions:[{id:'enter',src:'ready',dst:'working',trigger:'start',action:'START',label:'开始工作'}],
  views:{working:{label:'工作中',description:'按当前安排继续工作。'}}}]};
const feedbackTransition=feedbackMachine.regions[0].transitions[0];
const feedback=context.window.stateCommandFeedback;
assert.equal(feedback(feedbackMachine,feedbackTransition,null),'正在请求开始工作，等待设备反馈。');
assert.equal(feedback(feedbackMachine,feedbackTransition,{result:'ok',phase:'working'},()=>''),
  '已进入工作中。按当前安排继续工作。','Control feedback must follow Card-owned states and copy');
for(const result of [{result:'accepted',phase:'working'},{result:'ok',dispatch_only:true,phase:'working'},
  {result:'ok',confirmation_scope:'request',phase:'working'},
  {result:'timeout',phase:'working'},{result:'unknown',phase:'working'},{result:'blocked',phase:'working'},
  {result:'ok'}]) {
  assert.equal(feedback(feedbackMachine,feedbackTransition,result,()=> '原始回执'), '开始工作：原始回执。',
    'A missing or unconfirmed target must not be reported as state entry');
}
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
let rebootedLamp = {ts:570,ism:'OFF',_received_at:1000,
  control_ts:2000,autonomy_mode:'reactive',control_state:'holding'};
rebootedLamp=merge(rebootedLamp,{ts:19,ism:'ON',_received_at:1001});
assert.equal(rebootedLamp.ism,'ON','ESP32 clock reset discarded a confirmed post-reboot state');
assert.equal(rebootedLamp.control_state,'holding','Post-reboot state erased the cloud overlay');
rebootedLamp=merge(rebootedLamp,{ts:570,ism:'OFF',_received_at:1000,
  control_ts:2001,autonomy_mode:'manual',control_state:'manual'});
assert.equal(rebootedLamp.ism,'ON','Delayed pre-reboot snapshot replaced the live state');
assert.equal(rebootedLamp.control_state,'manual','Older physical snapshot blocked a newer control overlay');
rebootedLamp=merge(rebootedLamp,{ts:null,ism:'OFF',_received_at:1002});
assert.equal(rebootedLamp.ism,'OFF','An unsynchronized device clock froze physical updates');
assert.equal(merge(rebootedLamp,{ts:9999,ism:'ON'}).ism,'OFF',
  'Retained state without live receipt evidence replaced the live state');
assert.equal(merge({ts:570,ism:'OFF'},{ts:19,ism:'ON',_received_at:1001}).ism,'ON',
  'First live post-reboot state was discarded behind a retained snapshot');
const files = ['app/src/utils/inputAttributes.js','app/src/components/Icon.jsx','app/src/avatars/registry.js','app/src/avatars/presentation.js','app/src/avatars/Avatar.jsx','app/src/avatars/CompanionAvatar.jsx','app/src/utils/color.js', 'app/src/utils/stateMachine.js', 'app/src/utils/agentMeta.js', 'app/src/utils/stateSnapshot.js',
  'app/src/hooks/useTraining.js', 'app/src/components/TrainingPreview.jsx', 'app/src/components/TrainingDrawer.jsx', 'app/src/pages/TransitionInspector.jsx',
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
  payload = {result:'unknown',error:'receipt_unavailable',execution_active:false,task_id:'old'};
  assert.equal((await waitForLampTask('esp32_desk_led','old',100)).result,'unknown',
    'A device-confirmed inactive task must stop waiting without claiming success');
  assert.match(sandbox.bus.describe(payload), /已不再执行/);
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
  assert.equal(sandbox.bus.describe({result:'blocked',error:'training_blocked',reasons:[{code:'cooldown',remaining_s:2.2}]}),'冷却中，还需等待 3 秒');
  let taskWaits = [];
  sandbox.bus.waitForGo2Task = async id => { taskWaits.push(id); return {result:'accepted',dispatch_only:true}; };
  for (const wrapped of [false,true]) {
    const result = {result:'executing',task_id:'body_preparation'};
    payload = wrapped ? {ok:true,result} : result;
    assert.equal((await sandbox.bus.command('go2','SPORT',{cmd:'Hello',prepare_standing:true})).result,'accepted');
  }
  assert.deepEqual(taskWaits,['body_preparation','body_preparation']);
  let clock=Date.now(), polls=0;
  const searchDeadline=clock/1000+270;
  const longSearch=vm.createContext({
    AbortController, Date:{now:()=>clock}, clearTimeout:()=>{},
    setTimeout:(callback,delay)=>{if(delay===1500){clock+=100000;callback();}return 1;},
    apiClient:{request:async()=>++polls<3
      ? {result:'executing',deadline:searchDeadline}
      : {result:'ok',response:'已在附近'}},
  });
  vm.runInContext(read('app/src/CommandBus.js')+'\nthis.bus=commandBus;',longSearch);
  assert.equal((await longSearch.bus.waitForGo2Task('long-search')).result,'ok',
    'Browser stopped waiting at 90 seconds while the same search was still running');
  assert.equal(polls,3);
  console.log('PASS: explicit command outcomes survive legacy accepted flags; new lamp dispatch still waits for its result.');
}

async function checkInteractions() {
  const hooks = [];
  let index = 0;
  let effects = [];
  const calls = [];
  const requests = [];
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
    useCallback: callback => callback,
  };
  const ChatPanel = () => null;
  const ui = vm.createContext({ React, window: {addEventListener(){},removeEventListener(){},dispatchEvent(){}}, console, setTimeout, clearTimeout, ChatPanel, Icon: () => null,
    sessionStorage: {getItem(){return null},setItem(){},removeItem(){}},
    apiClient: { addEventListener() {}, removeEventListener() {}, request: async url => {
      requests.push(url); return {records:[],edge_annotations:[]};
    } },
    commandBus: { describe: result => result.error || result.result,
      async command(unitId, action, params) { calls.push({unitId, action, params}); return { result: 'accepted' }; } },
    EventSource: function() { sources.push(this); this.close = () => {}; },
    Go2AssetsDrawer: () => null, apiErrorText: error => error.message,
  });
  for (const file of files) vm.runInContext(compile(file), ui, { filename: file });
  const originalRows = [
    {type:'user',text:'重复要求',runId:'run-old',requestId:'old'},
    {type:'ai',text:'原回复',requestId:'old'},
    {type:'user',text:'重复要求',runId:'run-new',requestId:'new'},
  ];
  const cardRows = ui.window.trainingTimeline(originalRows,[
    {proposal_id:'p-old',source_run_id:'run-old',source_text:'重复要求',created_at:1},
    {proposal_id:'p-next',source_run_id:'run-old',source_text:'重复要求',created_at:2},
  ]);
  assert.deepEqual(Array.from(cardRows,row=>row.type==='rule' ? row.proposal.proposal_id : row.runId || row.text),
    ['run-old','原回复','p-old','p-next','run-new'], 'Rule cards must anchor exact source runs and preserve chronological order');

  const sm = { primary_region:'body', connection_field:'connected',
    regions:[{id:'body',label:'身体',state_field:'body_state',states:['standing','sitting'],transitions:[],views:{}}] };
  const card = { unit_id:'go2',device_id:'go2',name:'Go2',_online:true,
    transport:{state_stream:'/api/go2/connection/stream'},state_machine:sm,widgets:[],modes:[],skills:[] };
  const initial = {epoch:'a',revision:2,connected:true,body_state:'standing'};
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
  assert.equal(board.props.streamHealth,'reconnecting','A cached snapshot must not confirm an unopened state stream');
  sources[0].onopen();
  assert.equal(find(render(), ui.window.HierarchicalFsmBoard).props.streamHealth,'reconnecting','Transport open is not a valid snapshot');
  sources[0].onmessage({data:JSON.stringify(initial)});
  board = find(render(), ui.window.HierarchicalFsmBoard);
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
  assert.equal(board.props.streamHealth,'reconnecting','A retired epoch must not restore stream health');
  sources[0].onmessage({data:JSON.stringify({...initial,epoch:'b',revision:2,body_state:null})});
  assert.equal(find(render(), ui.window.HierarchicalFsmBoard).props.streamHealth,'live');
  const lamp = {unit_id:'lamp',name:'灯',_online:true,transport:{},state_machine:{primary_region:'device',regions:[{id:'device',label:'照明',states:['OFF','ON'],state_field:'ism',transitions:[],views:{}}]},widgets:[],modes:[],skills:[]};
  const flat = find(render(lamp, {ism:'ON',ts:1}), ui.window.DeviceStateBoard);
  assert.equal(flat.props.current, 'ON', 'Old device stream contaminated the lamp');
  sources[0].onmessage({data:JSON.stringify({...initial,epoch:'late',revision:1})});
  assert.equal(find(render(lamp, {ism:'ON',ts:1}), ui.window.DeviceStateBoard).props.current, 'ON');
  render(lamp,{ism:'OFF',ts:570,_received_at:1000});
  const rebootedBoard = find(render(lamp,{ism:'ON',ts:19,_received_at:1001}),ui.window.DeviceStateBoard);
  assert.equal(rebootedBoard.props.current,'ON','Device page remained stuck in its pre-reboot state');
  let previewCount = 0, fireCount = 0;
  const graph = ui.window.FsmGraph({live:true,nodes:[{id:'standing',label:'standing',current:true},{id:'sitting',label:'sitting'}],
    edges:[{id:'sit',src:'standing',dst:'sitting',active:true,transition:{src:'standing',dst:'sitting',trigger:'Sit',action:'Sit',label:'坐下'}}],
    onSelectState() { previewCount++; }, onInspectTransition() { fireCount++; } });
  const descendants = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree)
    ? tree.flatMap(descendants) : [tree, ...descendants(tree.props?.children)];
  const previewNode = descendants(graph).find(node => node.props?.['aria-label'] === 'sitting，查看说明');
  previewNode.props.onClick({type:'click'});
  assert.equal(previewCount, 1);
  assert.equal(fireCount, 0, 'Node selection opened edge details');
  descendants(graph).find(node => node.props?.['data-action'] === 'Sit').props.onClick({type:'click'});
  assert.equal(fireCount, 1, 'Edge must open transition details');
  const modeCard = {...card, modes:[
    {id:'autonomy',get:'/api/go2/autonomy',options:[]},
    {id:'other',get:'/api/go2/other-mode',options:[]},
  ]};
  const settings = find(render(modeCard), ui.DeviceSettingsMenu);
  assert.deepEqual(Array.from(settings.props.modes, axis=>axis.id), ['other'],
    'Device management must hide autonomy while retaining other declared mode axes');
  assert.equal(requests.includes('/api/go2/autonomy'),false,'Hidden autonomy settings still fetched their state');
  assert.equal(requests.includes('/api/go2/other-mode'),true,'Other declared modes stopped loading');
  hooks.forEach(hook => hook?.cleanup?.());
  console.log('PASS: command duplicate guard and errors, SSE old epoch rejection, device switch isolation, region-based lamp state, separate node selection and edge inspection.');
}

(async () => {
  await checkCommandResults();
  await checkInteractions();
  if (!process.argv.includes('--browser') && !process.argv.includes('--preview')) return;
  const sm = JSON.parse(process.env.GO2_STATE_MACHINE_JSON ? fs.readFileSync(process.env.GO2_STATE_MACHINE_JSON, 'utf8') : (await promisify(execFile)('uv', ['run', 'python', '-c',
    'import json; from cloud.go2.card import _go2_state_machine; print(json.dumps(_go2_state_machine()))'],
  { cwd: root, env: { ...process.env, UV_CACHE_DIR: process.env.UV_CACHE_DIR || '/tmp/ssm-uv' }, encoding: 'utf8' })).stdout);
  const lampCard = JSON.parse((await promisify(execFile)('uv', ['run','python','-c',
    'import json; from edge.lamp_contract import FSM_VERSION; from cloud.cards.builder import build_card_from_manifest; print(json.dumps(build_card_from_manifest({"unit_id":"esp32_desk_led","name":"ws2812_ring","firmware_ver":"0.3.0","agent_type":"actuator","fsm_version":FSM_VERSION,"capabilities":[{"action":"SET_COLOR"},{"action":"SET_STATE"},{"action":"BLINK"}]})))'],
    {cwd:root,env:{...process.env,UV_CACHE_DIR:process.env.UV_CACHE_DIR || '/tmp/ssm-uv-cache'},encoding:'utf8'})).stdout);
  const velocitySchema = JSON.parse((await promisify(execFile)('uv', ['run','python','-c',
    'import json; from cloud.go2.actions import velocity_schema; print(json.dumps(velocity_schema()))'],
    {cwd:root,env:{...process.env,UV_CACHE_DIR:process.env.UV_CACHE_DIR || '/tmp/ssm-uv-cache'},encoding:'utf8'})).stdout);
  const React = require('react');
  const ReactDOMServer = require('react-dom/server');
  const previewContext = vm.createContext({ React, window: {}, console });
  for (const name of ['app/src/utils/inputAttributes.js', 'app/src/utils/stateMachine.js', 'app/src/hooks/useTraining.js', 'app/src/pages/StateVisual.jsx', 'app/src/pages/FsmGraph.jsx', 'app/src/pages/HierarchicalFsmBoard.jsx']) {
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
      liveState:state,widgets:[],connected:true,offline:false,streamHealth:'live',actor:'admin',color:'#C8FF3E',
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
  console.log('Local simulated browser launched.');
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1, isMobile: true, hasTouch: true });
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:', error.message); });
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (/^(data:|blob:)/.test(request.url())) return request.continue();
      // Exercise the snapshot fallback in this layout/control regression.
      if (/\/api\/go2\/video(?:\?|$)/.test(request.url())) {
        return request.respond({status:503,headers:{'access-control-allow-origin':'*'},body:''});
      }
      if (request.url().includes('/api/go2/map.png') || request.url().includes('/api/go2/video/snapshot')) {
        return request.respond({status:200,contentType:'image/svg+xml',headers:{'access-control-allow-origin':'*'},body:'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#14141f"/><path d="M30 30h260v120H30zM30 90h90V30" fill="none" stroke="#666" stroke-width="8"/><path d="M155 80l10 20-10-5-10 5z" fill="#c8ff3e"/><text x="100" y="165" fill="#999" font-size="10">SIMULATED</text></svg>'});
      }
      return request.abort();
    });
    await page.setContent('<html lang="zh-CN"><head><base href="https://ssm-test.invalid/"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>');
    console.log('Local simulated browser page ready.');
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
    await page.addScriptTag({ content: `if(!crypto.randomUUID)crypto.randomUUID=()=>('test-'+Math.random()).replace('.','');
      window.__calls=[]; window.__sources=[];window.__velocities=[];
      const listeners=new EventTarget();
      window.__emit=detail=>listeners.dispatchEvent(new CustomEvent('server-event',{detail}));
      window.__training={records:[],edge_annotations:[],catalog:{hash:'hash',transitions:[]}};window.__requests=[];window.__proposal=null;window.__proposals=[];
      window.apiClient={eventsConnected:true,devicesSynced:true,dispatchEvent:event=>listeners.dispatchEvent(event),addEventListener:(...a)=>listeners.addEventListener(...a),removeEventListener:(...a)=>listeners.removeEventListener(...a),async request(url,options={}){
        __requests.push({url,options});
        if((!options.method || options.method==='GET') && url.includes('/training')
            && (window.__heldTrainingReads || window.__holdNextTrainingRead)){
          window.__holdNextTrainingRead=false;
          const payload=url.startsWith('/api/training/proposals') ? {proposals:structuredClone(__proposals)} : structuredClone(__training);
          return new Promise(resolve=>(window.__heldTrainingReadQueue ||= []).push({url,payload,resolve}));
        }
        if(url.startsWith('/api/training/') && options.method==='DELETE'){
          const record=__training.records.find(r=>url.endsWith('/'+r.teaching_id));
          if(!record || record.revision!==options.body.expected_revision)throw Object.assign(new Error('conflict'),{status:409});
          const removed={...structuredClone(record),revision:record.revision+1,desired_status:'removed'};
          if(window.__deleteFailure)throw new Error('模拟删除请求失败');
          if(window.__invalidDeleteReceipt)return {...removed,...({wrong_id:{teaching_id:'another-rule'},same_revision:{revision:record.revision},enabled:{desired_status:'enabled'}}[window.__invalidDeleteReceipt])};
          __training.records=__training.records.filter(r=>r.teaching_id!==record.teaching_id);
          if(window.__holdReadsAfterDelete)window.__heldTrainingReads=true;
          __emit({type:'training.updated',unit_id:record.unit_id});return removed;}
        if(url.startsWith('/api/training/') && url.endsWith('/parameters') && options.method==='PATCH'){
          if(window.__parameterDelay)await new Promise(resolve=>setTimeout(resolve,window.__parameterDelay));
          if(window.__parameterConflict) throw Object.assign(new Error('conflict'),{status:409});
          const record=__training.records.find(r=>url.includes('/'+r.teaching_id+'/'));
          if(record.revision!==options.body.expected_revision)throw Object.assign(new Error('conflict'),{status:409});
          const old=structuredClone(record);old.history=undefined;
          record.history=[...(record.history || []),old];record.revision++;
          if(options.body.event_target){const labels={主人:['vision.owner_recognized','recognized'],陌生人:['vision.stranger_recognized','recognized'],人:['vision.person_presence','detected'],人脸:['vision.face_presence','detected']};const mapped=labels[options.body.event_target];record.definition.trigger=mapped ? {event_id:mapped[0],where:{[mapped[1]]:true}} : {event_id:'vision.target_presence',where:{target:options.body.event_target,detected:true}};}
          if(options.body.event_params && record.definition.trigger)Object.assign(record.definition.trigger.where,options.body.event_params);
          for(const step of options.body.steps || [])Object.assign(record.definition.steps[step.step_index].defaults,step.params);
          for(const key of ['emotion','cooldown_s'])if(key in options.body)record.definition[key]=options.body[key];
          __emit({type:'training.updated',unit_id:record.unit_id});
          return structuredClone(record);
        }
        if(url.startsWith('/api/training/proposals/') && url.endsWith('/confirm')){
          if(window.__conflict) throw Object.assign(new Error('conflict'),{status:409});
          const p=__proposals.find(p=>url.includes('/'+p.proposal_id+'/'));p.confirmed=true;
          if(p.operation==='remove')__training.records=__training.records.filter(r=>r.teaching_id!==p.teaching_id);
          else {const record={...structuredClone(p.after),revision:p.expected_revision+1,loaded_revision:null,load_status:'awaiting_load'};p.teaching_id=record.teaching_id;__training.records=[...__training.records.filter(r=>r.teaching_id!==record.teaching_id),record];}return structuredClone(__training.records.find(r=>r.teaching_id===p.teaching_id) || {saved:true});}
        if(url.startsWith('/api/training/proposals/') && url.endsWith('/cancel')){const p=__proposals.find(p=>url.includes('/'+p.proposal_id+'/'));p.cancelled=true;return p;}
        if(url.startsWith('/api/training/proposals/'))return structuredClone(__proposals.find(p=>url.endsWith('/'+p.proposal_id)) || __proposal);
        if(url.startsWith('/api/training/proposals') && options.method!=='POST'){
          const uid=new URL(url,'https://ssm-test.invalid').searchParams.get('unit_id');
          return {proposals:structuredClone(__proposals.filter(p=>!uid || p.unit_id===uid))};}
        if(url==='/api/training/proposals'){
          __proposal={...__proposal,proposal_id:'remove1',superseded_by:null,superseded:false,cancelled:false,operation:'remove',teaching_id:options.body.teaching_id,created_at:Date.now()/1000,confirmed:false};
          __proposals.push(__proposal);return __proposal;}
        if(url.startsWith('/api/training/') && options.method==='PUT')return {};
        if(url.includes('/training'))return structuredClone(__training);
        if(url==='/api/devices')return [__lampCard,__card];return {};}};
      window.apiErrorText=error=>error.message;
      window.commandBus={describe:r=>r.error || (r.result==='accepted'?'指令已发送，尚未确认完成':'执行完成'),
        command:async(unit,action,params)=>{__calls.push({unit,action,params}); await new Promise(r=>setTimeout(r,100));return {result:'accepted'};},
        velocity(unit,action,params){__velocities.push({unit,action,params});}};
      window.EventSource=function(){__sources.push(this);this.close=()=>{};queueMicrotask(()=>this.onmessage?.({data:JSON.stringify(__state)}));};
      window.Icon=()=>null;window.Go2AssetsDrawer=()=>null;window.LampAssetsDrawer=()=>null;window.AgentPersonalitySettings=()=>null;
      window.ChatPanel=props=>React.createElement('div',{style:{padding:'18px',fontSize:'13px',color:'#999'}},
        React.createElement('p',null,'浏览器验证 · 模拟状态，不连接真机'),
        React.createElement('textarea',{placeholder:props.placeholder,disabled:props.disabled,'aria-label':'设备对话',style:{marginTop:18,width:'100%',height:60,background:'#17171b',color:'#ccc',border:'1px solid #333',borderRadius:10,padding:12}}));` });
    for (const name of files) await page.addScriptTag({ content: compile(name) });
    if (process.argv.includes('--training-only')) {
      await page.addScriptTag({content:compile('app/src/utils/activityView.js')});
      await page.addScriptTag({content:compile('app/src/utils/runProcess.js')});
      await page.addScriptTag({content:compile('app/src/components/RunProcess.jsx')});
      await page.addScriptTag({content:compile('app/src/components/ChatMessages.jsx')});
      await page.addScriptTag({content:compile('app/src/components/ChatPanel.jsx')});
    }
    await page.addScriptTag({ content: `window.__velocitySchema=${JSON.stringify(velocitySchema)};window.__lampCard=${JSON.stringify(lampCard)};window.__sm=${JSON.stringify(sm)};
      window.__state={state_schema_version:2,epoch:'a',revision:1,connected:true,connection_state:'online',activity_state:'active',life_state:'idle',body_state:'standing',body_confirmation:'observed',control_mode:'normal',autonomy_mode:'manual',available_actions:['StandDown','Sit','Hello','Stretch','Content','Dance1','Dance2','FingerHeart','FrontPounce','FrontJump','EnterMoveControl','StopMove'],available_life_actions:['request_rest','start_explore']};
      window.__manualCommands=__sm.regions.find(region=>region.id==='body').transitions.filter(t=>t.dispatch_only).map(t=>t.action);
      __state.available_manual_actions=__manualCommands;
      window.__card={unit_id:'go2',device_id:'go2',name:'Go2 机器狗',agent_type:'robot',_online:true,state_machine:__sm,transport:{state_stream:'/api/go2/connection/stream'},widgets:[],modes:[],skills:[]};
      const appRoot=ReactDOM.createRoot(document.getElementById('root'));
      window.__renderState=(next,card=__card,actor='admin')=>{__state=next;appRoot.render(React.createElement(FsmDevicePage,{unitId:card.unit_id,device:card,liveState:{state:next},actor,sessionId:'simulated_session',onBack(){}}));};
      __renderState(__state);` });

    await page.waitForSelector('.device-state-board');
    await page.evaluate(() => document.fonts.ready);
    if(process.argv.includes('--control-notices-only')) {
      for(const name of ['app/src/utils/activityView.js','app/src/utils/runProcess.js',
        'app/src/components/RunProcess.jsx','app/src/components/ChatMessages.jsx','app/src/components/ChatPanel.jsx'])
        await page.addScriptTag({content:compile(name)});
      await page.addStyleTag({content:read('app/styles/process.css')});
      await page.evaluate(()=>{
        window.__controlRequests=[];
        commandBus.command=(unit,action,params)=>new Promise((resolve,reject)=>{
          __calls.push({unit,action,params});__controlRequests.push({resolve,reject});
        });
        __renderState(__state);
      });
      await page.waitForSelector('.chat-messages');
      const tap=async (id,count)=>{
        await page.tap(`[data-state="${id}"]`);
        await page.waitForFunction(count=>__controlRequests.length===count,{},count);
      };
      await tap('life:explore',1);
      await page.waitForFunction(()=>document.querySelector('.chat-system-status')?.textContent.includes('正在请求开启探索'));
      await page.evaluate(()=>{
        __emit({type:'agent_progress',agent_id:'go2',source:'autonomous',scope:'space',
          run_id:'exploration-notice-test',event_id:'exploration-observation',fact_kind:'observation',
          text:'看到了周围环境',ts:Date.now()/1000});
      });
      await page.waitForSelector('.run-process');
      assert.ok(await page.evaluate(()=>!!(document.querySelector('.chat-system-status').compareDocumentPosition(
        document.querySelector('.run-process')) & Node.DOCUMENT_POSITION_FOLLOWING)),
        'The click context must precede progress even when SSE beats the command receipt');
      await page.evaluate(()=>__controlRequests[0].resolve({...__state,result:'ok',life_state:'explore',exploration_enabled:true}));
      await page.waitForFunction(()=>document.querySelector('.chat-system-status')?.textContent.startsWith('已进入探索。'));
      assert.equal(await page.$$('.chat-system-status').then(nodes=>nodes.length),1,'The receipt must update the pending notice');
      assert.ok(await page.$eval('.chat-system-status',node=>node.textContent.includes('自主决定')));
      assert.ok(await page.$('[data-state="life:idle"].is-current'),'A chat notice must not invent a live state update');
      await page.evaluate(()=>__renderState({...__state,revision:2,life_state:'explore',available_life_actions:['stop_explore']}));
      await tap('life:idle',2);
      await page.evaluate(()=>__controlRequests[1].resolve({result:'blocked',error:'停止尚未确认'}));
      await page.waitForFunction(()=>[...document.querySelectorAll('.chat-system-status')].some(node=>node.textContent==='停止探索：停止尚未确认。'));
      assert.ok(!(await page.$eval('.chat-messages',node=>node.textContent)).includes('已进入待命。'));
      await page.evaluate(()=>__renderState({...__state,revision:3,life_state:'idle',available_life_actions:['request_rest','start_explore']}));
      await tap('body:sitting',3);
      await page.evaluate(()=>__controlRequests[2].resolve({result:'accepted',dispatch_only:true}));
      await page.waitForFunction(()=>[...document.querySelectorAll('.chat-system-status')].some(node=>node.textContent.includes('坐下：指令已发送')));
      assert.ok(!(await page.$eval('.chat-messages',node=>node.textContent)).includes('已进入坐下。'));
      await tap('activity:rest',4);
      await page.evaluate(()=>__controlRequests[3].reject(new Error('连接中断')));
      await page.waitForFunction(()=>[...document.querySelectorAll('.chat-system-status')].some(node=>node.textContent==='休息：连接中断。'));
      assert.equal(await page.$$('.chat-system-status').then(nodes=>nodes.length),4);
      assert.deepEqual(await page.evaluate(()=>__calls.map(call=>call.action)),['LIFE','LIFE','Sit','LIFE']);
      assert.deepEqual(errors,[]);
      console.log('PASS: Card-owned chat control notices, SSE ordering, pending receipt updates, failed/dispatch-only outcomes and unchanged state highlights.');
      return;
    }
    if(process.argv.includes('--rule-presentation-only') || process.argv.includes('--rule-layout-only')) {
      await page.evaluate(()=>{
        __lampCard._online=true;
        const region=__lampCard.state_machine.regions[0];
        __training.catalog={hash:'a'.repeat(64),events:__lampCard.observations,
          presentation:{actions:Object.fromEntries(region.transitions.filter(t=>t.action).map(t=>[t.action,t.label])),
            parameters:Object.fromEntries(__lampCard.skills.map(skill=>[skill.invoke.action,skill.parameter_labels || {}]))},
          transitions:region.transitions.map(edge=>({...edge,fixed_params:edge.training_fixed_params || [],
            params_schema:__lampCard.skills.find(skill=>skill.invoke.action===edge.action)?.params_schema || {properties:{}}}))};
        __training.records=[{teaching_id:'expression',unit_id:__lampCard.unit_id,revision:1,history:[],capability_hash:__training.catalog.hash,
          desired_status:'enabled',definition:{emotion:'happy',origins:['user_delegated','agent_autonomous'],
            steps:[{transition_ids:region.transitions.filter(t=>t.action==='BLINK').map(t=>t.id),defaults:{count:3}}]}}];
        __renderState({ism:'OFF',ts:Date.now()/1000},__lampCard);
        __emit({type:'training.updated',unit_id:__lampCard.unit_id});
      });
      await page.waitForSelector('.fsm-whiteboard__edge.is-taught');
      const edge='.fsm-whiteboard__edge[data-source="device:OFF"][data-target="device:BLINKING"]';
      if(process.argv.includes('--rule-layout-only')) {
        await page.$eval(edge,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
        await page.waitForSelector('.device-whiteboard__directions');
        const directionSizes=await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.map(button=>{
          const range=document.createRange();range.selectNodeContents(button);
          return {width:button.getBoundingClientRect().width,textWidth:range.getBoundingClientRect().width};
        }));
        assert.ok(directionSizes.every(size=>size.width-size.textWidth<=24),'Direction backgrounds stay close to their labels instead of stretching across the card');
        await page.screenshot({path:'/tmp/ssm-direction-tabs-compact.png',fullPage:true});
        await page.keyboard.press('Escape');
        await page.evaluate(()=>{window.__layoutTeaching=structuredClone(__training.records[0]);__training.records=[];__emit({type:'training.updated',unit_id:__lampCard.unit_id});});
        await page.waitForFunction(()=>!document.querySelector('.fsm-whiteboard__event-marker'),{polling:'mutation'});
        await page.$eval(edge,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
        await page.waitForSelector('.device-whiteboard__popover--transition .training-transition-events');
        assert.ok(await page.$('.device-whiteboard__directions'),'Native-only bidirectional transitions retain direction tabs');
        assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-custom-events'),null,'The entire empty custom-event section is absent');
        assert.ok(await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events',node=>node.textContent.trim().length>0));
        await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.find(button=>button.textContent.includes('闪烁 → 熄灭')).click());
        await page.waitForFunction(()=>document.querySelectorAll('[data-transition-direction]:not([hidden]) .training-transition-events li').length===2);
        assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-custom-events'),null);
        assert.match(await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events',node=>node.textContent),/已完成指定闪烁次数.*闪烁前为熄灭/);
        await page.screenshot({path:'/tmp/ssm-native-transition-without-custom.png',fullPage:true});
        await page.keyboard.press('Escape');
        await page.evaluate(()=>{__training.records=[{...__layoutTeaching,desired_status:'removed'}];__emit({type:'training.updated',unit_id:__lampCard.unit_id});});
        await page.$eval(edge,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
        await page.waitForSelector('.device-whiteboard__popover--transition');
        assert.ok(await page.$('.device-whiteboard__directions'));
        assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-custom-events'),null,'Removed events cannot leave behind a custom-event heading, divider or empty prompt');
        await page.keyboard.press('Escape');
        await page.evaluate(()=>{__training.records=[{...__layoutTeaching,desired_status:'disabled'}];__emit({type:'training.updated',unit_id:__lampCard.unit_id});});
        await page.waitForSelector('.fsm-whiteboard__event-marker');
        await page.$eval(edge,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
        await page.waitForSelector('.device-whiteboard__directions');
        assert.match(await page.$eval('[data-transition-direction]:not([hidden]) .training-custom-events',node=>node.textContent),/未启用/);
        await page.keyboard.press('Escape');
        // ON and BLINKING share the same colour/brightness controls. Measure
        // the actual visible group rather than the width of its grid tracks.
        for(const width of [320,390,1440]) {
          await page.setViewport({width,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
          for(const state of ['ON','BLINKING']) {
            await page.evaluate(state=>__renderState({ism:state,ts:Date.now()/1000,r:255,g:200,b:80,brightness:180},__lampCard),state);
            await page.waitForSelector(`.fsm-whiteboard__node.is-current[data-state="device:${state}"]`);
            await page.waitForSelector('.device-led-controls__wheel');
            const alignment=await page.$eval('.device-led-controls',node=>{
              const parent=node.getBoundingClientRect();
              const wheel=node.querySelector('.device-led-controls__wheel').getBoundingClientRect();
              const brightness=node.querySelector('.device-led-controls__brightness').getBoundingClientRect();
              const picker=node.querySelector('.device-led-controls__picker').getBoundingClientRect();
              const side=node.querySelector('.device-led-controls__side').getBoundingClientRect();
              const left=Math.min(picker.left,side.left),right=Math.max(picker.right,side.right);
              return {offsetX:Math.abs((left+right)/2-parent.left-parent.width/2),
                offsetY:Math.abs(wheel.top+wheel.height/2-brightness.top-brightness.height/2),
                width:right-left,parentWidth:parent.width,text:node.querySelector('output').textContent};
            });
            assert.ok(alignment.offsetX<=1.5,`Lamp ${state} colour/brightness group must be horizontally centred at ${width}px`);
            assert.ok(alignment.offsetY<=1.5,`Lamp ${state} colour and brightness must share a vertical centre at ${width}px`);
            assert.ok(alignment.width<=alignment.parentWidth,'Lamp controls stay inside their parent');
            assert.match(alignment.text,/71%|180/);
            if(width===390)await page.screenshot({path:`/tmp/ssm-lamp-centred-${state.toLowerCase()}-mobile.png`,fullPage:true});
          }
        }
        await page.evaluate(()=>{
          const color=__training.catalog.transitions.find(edge=>edge.action==='SET_COLOR' && edge.src==='ON' && edge.dst==='ON');
          const other={...structuredClone(__layoutTeaching),teaching_id:'keep-color',
            definition:{situation:'阅读时',origins:['user_delegated'],steps:[{transition_ids:[color.id],conditions:[],defaults:{r:12,g:34,b:56,brightness:160}}]}};
          __training.records=[structuredClone(__layoutTeaching),other];
          __state={ism:'ON',ts:Date.now()/1000,epoch:'delete-layout-epoch'};
          function DeletionProbe(){
            const training=useTraining(__lampCard.unit_id,{sessionId:'delete-layout',canEdit:true});
            window.__deletionTraining=training;
            React.useEffect(()=>{void training.loadAll([__lampCard.unit_id]);},[]);
            return React.createElement('div',{id:'training-delete-probe',style:{height:'100%',display:'flex'}},
              React.createElement(DeviceStateBoard,{unitId:__lampCard.unit_id,sm:__lampCard.state_machine,
                current:'ON',widgets:[],liveState:__state,connected:true,offline:false,actor:'admin',streamHealth:'live',
                card:__lampCard,training,color:'#b8a4ff'}));
          }
          appRoot.render(React.createElement(DeletionProbe));
        });
        await page.waitForFunction(()=>__deletionTraining?.records.length===2 && __deletionTraining?.allRecords.length===2);
        await page.waitForSelector('#training-delete-probe .fsm-whiteboard__edge[data-teaching-ids="expression"] .fsm-whiteboard__event-marker');
        await page.$eval('#training-delete-probe .fsm-whiteboard__edge[data-teaching-ids="expression"]',node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
        await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-custom-events');
        const otherBefore=await page.evaluate(()=>JSON.stringify(__deletionTraining.records.find(record=>record.teaching_id==='keep-color')));
        const startRemoval=mode=>page.evaluate(mode=>{
          window.__invalidDeleteReceipt=mode;window.__deleteOutcome='pending';window.__deleteError='';
          const record=__deletionTraining.records.find(item=>item.teaching_id==='expression');
          window.__deletePromise=__deletionTraining.remove(record).then(result=>{
            window.__deleteOutcome='resolved';window.__deleteResult=result;
          },error=>{window.__deleteOutcome='rejected';window.__deleteError=error.message;});
        },mode);
        for(const invalid of ['wrong_id','same_revision','enabled']) {
          await startRemoval(invalid);
          await page.waitForFunction(()=>__deleteOutcome==='rejected');
          assert.match(await page.evaluate(()=>__deleteError),/删除回执/);
          assert.ok(await page.$('#training-delete-probe .fsm-whiteboard__edge[data-teaching-ids="expression"] .fsm-whiteboard__event-marker'),'An invalid deletion receipt cannot remove the event marker');
          assert.equal(await page.evaluate(()=>__deletionTraining.records.find(record=>record.teaching_id==='expression').desired_status),'enabled');
        }
        await page.evaluate(()=>{window.__deleteFailure=true;});
        await startRemoval(false);
        await page.waitForFunction(()=>__deleteOutcome==='rejected');
        assert.match(await page.evaluate(()=>__deleteError),/模拟删除请求失败/);
        assert.equal(await page.evaluate(()=>__deletionTraining.allRecords.find(record=>record.teaching_id==='expression').desired_status),'enabled');
        await page.evaluate(()=>{
          window.__deleteFailure=false;
          const now=Date.now()/1000;
          __emit({type:'agent_progress',result_kind:'training',agent_id:__lampCard.unit_id,teaching_id:'expression',
            revision:1,version:1,run_id:'delete-layout-run',step_id:'delete-layout-run:0',step_index:0,
            transition_id:__layoutTeaching.definition.steps[0].transition_ids[0],device_epoch:'delete-layout-epoch',
            ts:now,sequence:0,fact_kind:'call',stage:'executing'});
          window.__holdNextTrainingRead=true;
          window.__staleLoadDone=false;
          window.__staleLoadPromise=__deletionTraining.load().then(()=>{window.__staleLoadDone=true;});
        });
        await page.waitForFunction(()=>__heldTrainingReadQueue?.length===1 && __deletionTraining.activities.expression);
        await page.evaluate(()=>{window.__holdReadsAfterDelete=true;});
        await startRemoval(false);
        await page.waitForFunction(()=>__deleteOutcome==='resolved',{timeout:3000});
        await page.waitForFunction(()=>!document.querySelector('#training-delete-probe .fsm-whiteboard__edge[data-teaching-ids="expression"] .fsm-whiteboard__event-marker'));
        assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-custom-events'),null,'Deleting the last custom event immediately removes its complete section');
        assert.ok(await page.$('.device-whiteboard__directions'),'Deleting custom events does not hide native direction navigation');
        assert.ok(await page.$('[data-transition-direction]:not([hidden]) .training-transition-events'));
        assert.equal(await page.evaluate(()=>__deleteResult),true);
        assert.equal(await page.evaluate(()=>__deletionTraining.records.find(record=>record.teaching_id==='expression').desired_status),'removed');
        assert.equal(await page.evaluate(()=>__deletionTraining.allRecords.find(record=>record.teaching_id==='expression').desired_status),'removed');
        assert.equal(await page.evaluate(()=>!!__deletionTraining.activities.expression),false,'A confirmed deletion immediately retires its execution highlight');
        assert.ok(await page.evaluate(()=>__heldTrainingReadQueue.length>=3),'The snapshot/history refresh is still waiting after remove has resolved');
        assert.ok(await page.$('#training-delete-probe .fsm-whiteboard__edge[data-teaching-ids="keep-color"] .fsm-whiteboard__event-marker'),'Other saved events retain their marker');
        assert.equal(await page.evaluate(()=>JSON.stringify(__deletionTraining.records.find(record=>record.teaching_id==='keep-color'))),otherBefore);
        await page.evaluate(()=>{const stale=__heldTrainingReadQueue.shift();stale.resolve(stale.payload);});
        await page.waitForFunction(()=>__staleLoadDone);
        assert.equal(await page.evaluate(()=>__deletionTraining.records.find(record=>record.teaching_id==='expression').desired_status),'removed','An older in-flight GET cannot resurrect the deleted event');
        assert.ok(await page.evaluate(()=>__heldTrainingReadQueue.length>0));
        await page.screenshot({path:'/tmp/ssm-training-delete-immediate.png',fullPage:true});
        await page.evaluate(()=>{
          window.__heldTrainingReads=false;window.__holdReadsAfterDelete=false;
          for(const read of __heldTrainingReadQueue.splice(0))read.resolve(read.payload);
        });
        assert.equal(await page.evaluate(()=>__calls.length),0,'Presentation checks never execute a device command');
        assert.deepEqual(errors,[]);
        console.log('PASS: compact bidirectional navigation, no empty custom-event section, centred lamp controls and immediate receipt-backed deletion while refreshes are pending.');
        return;
      }
      await page.$eval(edge,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card');
      assert.equal(await page.$('.device-whiteboard__rule-or'),null);
      assert.match(await page.$eval('[data-transition-direction]:not([hidden]) .training-rule-title',node=>node.textContent),/开心.*闪烁.*3.*次/);
      assert.equal(await page.$('.training-rule-group__heading'),null,'Compact cards use one sentence instead of stacked action/parameter sections');
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]');
      await page.screenshot({path:'/tmp/ssm-rule-hierarchy-mobile.png',fullPage:true});
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',nodes=>nodes.find(node=>node.textContent==='取消').click());
      await page.$$eval('.device-whiteboard__directions button',nodes=>nodes.find(node=>node.textContent.includes('闪烁 → 熄灭')).click());
      await page.waitForFunction(()=>document.querySelectorAll('[data-transition-direction]:not([hidden]) .training-transition-events li').length===2);
      const list=await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events',node=>node.textContent);
      assert.match(list,/收到关灯指令|请求关灯/);assert.match(list,/指定次数完成|有限闪烁完成/);assert.match(list,/闪烁前为熄灭/);
      assert.equal(await page.$$('[data-transition-direction]:not([hidden]) .training-inspector').then(nodes=>nodes.length),1);
      assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-inspector__card'),null,'A completion event must not inherit the rule that starts blinking');
      for(const width of [320,390]){
        await page.setViewport({width,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      }
      await page.screenshot({path:'/tmp/ssm-transition-events-mobile.png',fullPage:true});
      assert.equal(await page.evaluate(()=>__calls.length),0,'Inspecting transitions or editing a form must not dispatch actions');
      assert.deepEqual(errors,[]);
      console.log('PASS: 18/16/14 typography, inline parameter edit, one event list per transition, correct completion guards and 320/390px layout.');
      return;
    }
    if (process.argv.includes('--offline-only')) {
      const active='.fsm-whiteboard__node.is-current,.fsm-whiteboard__node.is-actionable,.fsm-whiteboard__node.is-reachable,.fsm-whiteboard__edge.is-emphasized,.fsm-whiteboard__edge.is-running,.fsm-whiteboard__edge.is-taught,.fsm-whiteboard__edge.is-teaching-observed,.fsm-whiteboard__edge.is-teaching-executing,.fsm-whiteboard__edge.is-teaching-result,.fsm-whiteboard__edge.is-new-teaching';
      const assertInactive=async reason=>{
        await page.waitForFunction(()=>document.querySelector('.device-state-board')?.dataset.live==='false');
        assert.equal(await page.$$(active).then(nodes=>nodes.length),0,reason);
        assert.ok(await page.$('.device-header__status i.is-offline'),'The header must use the same live gate');
      };
      await page.waitForSelector('[data-state="body:standing"].is-current');
      await page.evaluate(()=>{
        const transition=__sm.regions.find(region=>region.id==='body').transitions.find(t=>t.action==='Sit' && t.src==='standing');
        transition.id='offline-sit'; window.__sitId=transition.id;
        __training.records=[{teaching_id:'offline-rule',revision:1,desired_status:'enabled',name:'看到人就坐下',
          definition:{steps:[{transition_ids:[__sitId],conditions:[],defaults:{}}]}}];
        __emit({type:'training.updated',unit_id:'go2'});
        __renderState({...__state,revision:2,pending_action:{command:'Sit'}});
      });
      await page.waitForSelector('.fsm-whiteboard__edge.is-taught');
      await page.waitForSelector('.fsm-whiteboard__edge.is-running');
      await page.evaluate(()=>{
        const now=Date.now()/1000;
        window.__offlineProgress={type:'agent_progress',result_kind:'training',agent_id:'go2',teaching_id:'offline-rule',
          revision:1,version:1,run_id:'pre-disconnect',step_id:'pre-disconnect:0',step_index:0,transition_id:__sitId,
          device_epoch:__state.epoch,ts:now,sequence:0,fact_kind:'observation',observation_id:'vision.owner_recognized',
          observation_event_id:'appearance',as_of:now};
        __emit(__offlineProgress);
      });
      await page.waitForSelector('.fsm-whiteboard__edge.is-teaching-observed');
      await page.evaluate(()=>__renderState(__state,{...__card,_online:false}));
      await assertInactive('MQTT offline must override cached connected=true, posture, pending actions and teaching activity');
      await page.$eval('.fsm-whiteboard__edge[data-action="Sit"]',node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card');
      assert.equal(await page.evaluate(()=>__calls.length),0,'Offline inspection sent a physical action');
      await page.screenshot({path:'/tmp/ssm-offline-rules.png',fullPage:true});
      await page.keyboard.press('Escape');
      await page.evaluate(()=>{
        __sources.at(-1).onmessage({data:JSON.stringify({...__state,revision:3})});
      });
      await assertInactive('A live device stream cannot override offline MQTT status');
      await page.evaluate(()=>__renderState({...__state,revision:4,pending_action:null}));
      await page.waitForSelector('[data-state="body:standing"].is-current');
      assert.equal(await page.$('.fsm-whiteboard__edge.is-teaching-observed'),null,'Reconnection must not replay pre-disconnect execution highlights');
      await page.evaluate(()=>__renderState({...__state,revision:5,connected:false}));
      await assertInactive('An online Go2 service must not substitute for the physical control connection');
      await page.evaluate(()=>__renderState({...__state,revision:6,connected:true,body_telemetry_fresh:false}));
      await page.waitForFunction(()=>document.querySelector('.device-state-board')?.dataset.live==='true');
      assert.equal(await page.$('.fsm-whiteboard__node.is-current[data-state^="body:"]'),null,'Invalid body feedback must not illuminate a cached posture');
      assert.ok(await page.$('[data-state="life:idle"].is-current'),'Body validity must not suppress independent behavior facts');
      await page.evaluate(()=>__renderState({...__state,revision:7,body_telemetry_fresh:true}));
      await page.waitForSelector('[data-state="body:standing"].is-current');
      await page.evaluate(()=>{apiClient.eventsConnected=false;apiClient.dispatchEvent(new Event('events-error'));});
      await assertInactive('Global event loss invalidates MQTT presence even with a separate healthy device stream');
      await page.evaluate(()=>{
        __sources.at(-1).onmessage({data:JSON.stringify({...__state,revision:8})});
        apiClient.eventsConnected=true;apiClient.dispatchEvent(new Event('events-open'));
      });
      await assertInactive('Reopening global SSE must wait for device snapshot resynchronization');
      await page.evaluate(()=>{apiClient.devicesSynced=true;apiClient.dispatchEvent(new Event('devices-synced'));});
      await page.waitForSelector('[data-state="body:standing"].is-current');
      await page.evaluate(()=>__sources.at(-1).onerror());
      await assertInactive('Independent device stream failure must remove all live highlights');
      await page.evaluate(()=>{
        const source=__sources.at(-1);source.onopen();
        for(const state of [{},[],null])source.onmessage({data:JSON.stringify(state)});
      });
      await assertInactive('Transport open and empty frames do not restore live state');
      await page.evaluate(()=>__sources.at(-1).onmessage({data:JSON.stringify({...__state,epoch:'b',revision:1})}));
      await page.waitForSelector('[data-state="body:standing"].is-current');
      await page.evaluate(()=>{
        const source=__sources.at(-1);source.onerror();source.onopen();
        source.onmessage({data:JSON.stringify({...__state,epoch:'a',revision:999})});
      });
      await assertInactive('A retired epoch must not restore stream health or highlights');
      await page.evaluate(()=>__sources.at(-1).onmessage({data:JSON.stringify({...__state,epoch:'b',revision:0})}));
      await assertInactive('An older revision must not restore stream health or highlights');
      await page.evaluate(()=>__sources.at(-1).onmessage({data:JSON.stringify({...__state,epoch:'b',revision:2})}));
      await page.waitForSelector('[data-state="body:standing"].is-current');
      await page.evaluate(()=>__renderState({ism:'ON',ts:1},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:ON"].is-current');
      await page.evaluate(()=>__renderState(__state,{...__lampCard,_online:false}));
      await assertInactive('A cached lamp ON state must not stay illuminated while offline');
      assert.ok(await page.$('[data-state="device:ON"].is-retained'),'Last observation can remain as a neutral, retained marker');
      await page.evaluate(()=>__renderState({...__state,ts:2},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:ON"].is-current');
      assert.equal(await page.evaluate(()=>__calls.length),0,'Availability changes must never dispatch actions');
      assert.deepEqual(errors,[]);
      console.log('PASS: shared MQTT/control/SSE highlight gates, offline rule inspection, neutral cached observations, independent body validity, no stale execution replay, and valid-snapshot-only reconnection.');
      return;
    }
    if (process.argv.includes('--lamp-reboot-only')) {
      await page.evaluate(() => __renderState({ism:'OFF',ts:570,_received_at:1000},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:OFF"].is-current');
      await page.evaluate(() => __renderState({ism:'ON',ts:19,_received_at:1001},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:ON"].is-current');
      await page.waitForFunction(() => document.querySelector('[data-state="device:OFF"]').dataset.executable==='true');
      await page.tap('[data-state="device:OFF"]');
      await page.waitForFunction(() => __calls.length===1);
      assert.deepEqual(await page.evaluate(() => __calls[0]),{
        unit:lampCard.unit_id,action:'SET_STATE',params:{state:'OFF'},
      },'Post-reboot state did not enable the correct command');
      // Acceptance alone must not move the state node before physical feedback.
      await page.waitForSelector('[data-state="device:ON"].is-current');
      await page.evaluate(() => __renderState({ism:'OFF',ts:20,_received_at:1002},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:OFF"].is-current');
      await page.evaluate(() => __renderState({ism:'ON',ts:570,_received_at:1000},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:OFF"].is-current');
      assert.equal(await page.evaluate(() => __calls.length),1,'State updates replayed a command');
      assert.deepEqual(errors,[]);
      console.log('PASS: real-browser lamp reboot clock rollback, correct target click, acceptance waits for physical feedback, delayed snapshot rejection.');
      return;
    }
    if (process.argv.includes('--posture-only')) {
      for (const source of [null, undefined]) {
        await page.evaluate(source => {
          const next = {...__state,revision:__state.revision+1,body_state:source,
            body_confirmation:'unknown',body_telemetry_fresh:true};
          if (source === null) next.body_state = null;
          else delete next.body_state;
          __renderState(next);
        }, source);
        await page.waitForFunction(() => document.querySelector('[data-state="body:standing"]').dataset.executable==='false');
        assert.equal(await page.$$('[data-state="body:standing"]').then(nodes => nodes.length),1);
        const count = await page.evaluate(() => __calls.length);
        await page.tap('[data-state="body:standing"]');
        assert.equal(await page.evaluate(() => __calls.length),count,'Unknown posture guessed a rise command');
        assert.equal(await page.$('[aria-label="状态说明"] .btn'),null,'Unknown posture offered two rise commands');
        assert.match(await page.$eval('[data-state="body:standing"]',node => node.getAttribute('aria-label')),
          /姿态尚未确认，等待设备反馈/);
      }
      const tap = async (value, action, params = {}) => {
        const selector = `[data-state="body:${value}"]`;
        await page.waitForFunction(selector => document.querySelector(selector)?.dataset.executable === 'true', {}, selector);
        const count = await page.evaluate(() => __calls.length);
        await page.evaluate(selector => {
          const node = document.querySelector(selector);
          node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
          node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        }, selector);
        await page.waitForFunction(() => document.querySelector('.device-state-board').getAttribute('aria-busy') === 'false');
        assert.equal(await page.evaluate(() => __calls.length), count + 1);
        assert.deepEqual(await page.evaluate(() => ({action:__calls.at(-1).action,params:__calls.at(-1).params})), {action,params});
        assert.equal(await page.$('[aria-label="状态说明"] .btn'),null,'Known posture offered two rise commands');
      };
      for (const [source,stand] of [['sitting','RiseSit'],['lying','StandUp']]) {
        await page.evaluate(source => __renderState({...__state,revision:__state.revision+1,
          body_state:source,body_confirmation:'observed',body_telemetry_fresh:true,
          available_actions:[source==='sitting'?'RiseSit':'StandUp']}), source);
        await tap('standing',stand);
        const targets = await page.evaluate(source => __sm.regions.find(r => r.id==='body').transitions
          .filter(t => t.src==='standing' && t.dispatch_only && t.dst!==source).map(t => [t.dst,t.action]), source);
        for (const [target,action] of targets) await tap(target,'SPORT',{cmd:action,prepare_standing:true});
        const count = await page.evaluate(() => __calls.length);
        await page.tap(`[data-state="body:${source}"]`);
        assert.equal(await page.evaluate(() => __calls.length),count,'Current pose was re-executed');
      }
      await page.evaluate(() => __renderState({...__state,revision:__state.revision+1,body_state:'sitting',
        body_telemetry_fresh:false}));
      await page.waitForFunction(() => document.querySelector('[data-state="body:lying"]').dataset.executable==='false');
      const count = await page.evaluate(() => __calls.length);
      await page.$eval('[data-state="body:lying"]',node => node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
      assert.equal(await page.evaluate(() => __calls.length),count,'Stale posture started an automatic sequence');
      assert.deepEqual(errors,[]);
      console.log('PASS: unknown posture waits without rise choices; sitting/lying select the correct rise, every standing-based action requests server preparation, duplicate taps/current poses do not replay, stale preparation is blocked.');
      return;
    }
    if (process.argv.includes('--training-only')) {
      await page.click('.device-header__settings');
      await page.$$eval('.device-settings-go2', nodes => nodes.find(node => node.textContent.includes('设备能力与技能')).click());
      await page.waitForSelector('[role="dialog"][aria-label="设备能力与技能"]');
      await page.waitForFunction(() => document.querySelector('.device-settings-panel').getAnimations().every(animation => animation.playState === 'finished'));
      assert.equal(await page.$$('.device-settings-panel').then(nodes => nodes.length), 1);
      assert.equal(await page.$('details.device-settings-capabilities'), null, 'Capabilities should open as a drawer');
      assert.equal(await page.$eval('.device-settings-panel', node => Math.round(node.getBoundingClientRect().bottom)), 844);
      assert.equal(await page.$eval('.device-settings-capabilities', node => node.textContent.includes('公开技能')), true);
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('.device-settings-panel'));
      await page.evaluate(() => {
        const edge=__sm.regions.find(r=>r.id==='body').transitions.find(t=>t.action==='Sit' && t.src==='standing');
        edge.id='tr_sit'; edge.training_fixed_params=[];
        __training.catalog.transitions=[{id:'tr_sit',trainable:true,src:'standing',dst:'sitting',state_field:'body_state',source_label:'站立',destination_label:'坐下'}];
        __training.catalog.events=[{id:'vision.owner_recognized',name:'识别到主人',fields:{recognized:'boolean'}},{id:'vision.target_presence',name:'看到目标物体',fields:{target:'string',detected:'boolean'}}];
        __training.catalog.limits={cooldown_s:{minimum:1,maximum:3600}};
        __training.catalog.presentation={actions:{Sit:'坐下'}};
        __training.catalog.transitions[0].action='Sit';
        __training.catalog.state_fields={body_state:{label:'身体',labels:{standing:'站立',sitting:'坐下'}}};
        __training.records=[{unit_id:'go2',teaching_id:'teach1',revision:1,history:[],updated_at:1,capability_hash:'hash',
          definition:{steps:[{transition_ids:['tr_sit'],conditions:[],defaults:{},goal_state:'sitting'}],
            trigger:{event_id:'vision.owner_recognized',where:{recognized:true}},origins:['agent_autonomous'],cooldown_s:3},
          name:'坐下后至少间隔三秒再坐',summaryText:'坐下后至少间隔三秒再坐',source_text:'坐下前等三秒',desired_status:'enabled'}];
        __training.edge_annotations=[{transition_id:'tr_sit',teachings:[{desired_status:'enabled'}]}];
        __renderState(__state);
        __emit({type:'training.updated',unit_id:'go2',cursor:1});
      });
      await page.waitForFunction(()=>__requests.some(r=>r.url.includes('/api/devices/go2/training')));
      await page.waitForSelector('.fsm-whiteboard__edge[data-teaching-ids="teach1"].is-taught');
      assert.equal(await page.$('.state-teachings'),null,'Teaching belongs in the graph, without a duplicate top section');
      assert.ok(await page.$('.fsm-whiteboard__edge .fsm-whiteboard__rule-badge'),'Saved rules need a persistent marker');
      assert.equal(await page.$$('.fsm-whiteboard__edge:not(.is-taught) .fsm-whiteboard__event-marker').then(nodes=>nodes.length),0,'Preset-only connections do not show event dots');
      assert.equal(await page.$('.fsm-whiteboard__edge text'),null,'Rule markers must not add text to the lines');
      assert.equal(await page.$('.fsm-whiteboard__legend'),null,'The graph must not include an explanatory legend');
      assert.ok(await page.$eval('.fsm-whiteboard__edge.is-taught',node=>node.getAttribute('aria-label').includes('看到（主人）')));
      assert.ok(await page.$('.fsm-whiteboard__node[data-state="body:standing"].is-current'));
      const count=await page.evaluate(()=>__calls.length);
      await page.screenshot({path:'/tmp/ssm-training-graph.png',fullPage:true});
      await page.evaluate(()=>{
        const now=Date.now()/1000;
        window.__trainingProgress={type:'agent_progress',result_kind:'training',agent_id:'go2',teaching_id:'teach1',
          revision:1,version:1,run_id:'learned-owner',step_id:'learned-owner:0',step_index:0,transition_id:'tr_sit',
          device_epoch:__state.epoch,ts:now,sequence:0,fact_kind:'observation',stage:'observed',status:'observed',
          observation_id:'vision.owner_recognized',observation_event_id:'owner-appearance',as_of:now};
        __emit(__trainingProgress);
      });
      await page.waitForSelector('.fsm-whiteboard__edge.is-teaching-observed');
      await page.evaluate(()=>__emit({...__trainingProgress,sequence:1,fact_kind:'call',stage:'executing',status:'executing'}));
      await page.waitForSelector('.fsm-whiteboard__edge.is-teaching-executing');
      await page.evaluate(()=>__emit({...__trainingProgress,sequence:2,fact_kind:'result',status:'ok',public_result:{result:'ok'}}));
      await page.waitForSelector('.fsm-whiteboard__edge.is-teaching-result');
      assert.ok(await page.$('.fsm-whiteboard__node[data-state="body:standing"].is-current'),'Action receipt must not fabricate a measured sitting posture');
      await page.evaluate(()=>{
        window.__originalTeaching=structuredClone(__training.records[0]);
        __training.records=Array.from({length:12},(_,index)=>({...structuredClone(__originalTeaching),
          teaching_id:index ? `teach-${index}` : 'teach1',name:`教导 ${index+1}`,summaryText:`教导 ${index+1}`}));
        __emit({type:'training.updated',unit_id:'go2'});
      });
      await page.waitForFunction(()=>document.querySelector('.fsm-whiteboard__edge[data-action="Sit"]')?.dataset.teachingIds.split(',').length===12);
      await page.tap('.fsm-whiteboard__edge[data-action="Sit"]');
      await page.waitForFunction(()=>document.querySelectorAll('[data-transition-direction]:not([hidden]) .training-inspector__card').length===12);
      assert.equal(await page.$('.device-whiteboard__popover--expanded'),null,'Transition events must not use an expanded panel');
      assert.ok(await page.$eval('.device-whiteboard__popover--transition',node=>{
        const rect=node.getBoundingClientRect();return rect.width<=280 && rect.height<=248 && rect.height<innerHeight*.5;
      }),'A transition card must leave the state machine visible');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events',node=>!!node.querySelector('button,input,select')),false,'Preset events are read-only');
      assert.ok(await page.$('[data-transition-direction]:not([hidden]) .training-custom-events'));
      assert.equal(await page.$$('.fsm-whiteboard__rule-badge').then(nodes=>nodes.length),1,'Multiple rules on one direction share a compact marker');
      assert.ok(await page.$eval('.device-whiteboard__popover-body',node=>node.scrollHeight>node.clientHeight),'Many rules must scroll inside the popover');
      assert.equal(await page.$('.device-whiteboard__popover--transition button[aria-label^="关闭"]'),null,'Compact transition cards close through outside interaction');
      const fixedTop=await page.$eval('.device-whiteboard__popover-heading',node=>node.getBoundingClientRect().top);
      const tabsTop=await page.$eval('.device-whiteboard__directions',node=>node.getBoundingClientRect().top);
      await page.$eval('.device-whiteboard__popover-body',node=>{node.scrollTop=node.scrollHeight;});
      assert.equal(await page.$eval('.device-whiteboard__popover-heading',node=>node.getBoundingClientRect().top),fixedTop,'The transition title stays fixed while events scroll');
      assert.equal(await page.$eval('.device-whiteboard__directions',node=>node.getBoundingClientRect().top),tabsTop,'Direction tabs stay fixed while events scroll');
      await page.screenshot({path:'/tmp/ssm-training-many-rules.png',fullPage:true});
      const lastRevise=(await page.$$('[data-transition-direction]:not([hidden]) .training-inspector__revise')).at(-1);
      await lastRevise.scrollIntoView();
      await lastRevise.click();
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="teach-11"] input[aria-label="事件参数：看到的对象"]');
      assert.equal(await page.$('.training-selection'),null,'Parameter editing must stay in the original rule card');
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="teach-11"] button',buttons=>buttons.find(b=>b.textContent==='取消').click());
      await page.mouse.click(195,28);
      await page.waitForFunction(()=>!document.querySelector('.device-whiteboard__popover--transition'));
      await page.evaluate(()=>{
        __training.records=[__originalTeaching];
        __emit({type:'training.updated',unit_id:'go2'});
      });
      await page.waitForFunction(()=>document.querySelector('.fsm-whiteboard__edge[data-action="Sit"]')?.dataset.teachingIds==='teach1');
      await page.tap('.fsm-whiteboard__edge[data-action="Sit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',input=>input.tagName),'INPUT');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',input=>input.type),'text');
      assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-inspector select'),null,'Inline parameters use free text inputs');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]',button=>button.disabled),true,'Unchanged event parameters cannot be saved');
      const setTarget=async value=>page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',(input,value)=>{
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
        input.dispatchEvent(new Event('input',{bubbles:true}));
      },value);
      await setTarget('花');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]',button=>button.disabled),false);
      assert.ok(await page.$('[data-transition-direction]:not([hidden]) .training-parameter-actions .btn--accent'));
      const chatRequestsBefore=await page.evaluate(()=>__calls.length);
      await page.evaluate(()=>{window.__parameterConflict=true;});
      await page.click('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card .training-error');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',input=>input.value),'花');
      await page.evaluate(()=>{window.__parameterConflict=false;window.__parameterDelay=400;});
      await page.click('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]');
      await page.waitForFunction(()=>document.querySelector('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]')?.textContent.includes('保存中'));
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]',button=>button.disabled),true);
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-save-success');
      assert.match(await page.$eval('[data-transition-direction]:not([hidden]) .training-save-success',node=>node.textContent),/已更新/);
      assert.equal(await page.evaluate(()=>__training.records[0].definition.trigger.where.target),'花');
      assert.equal(await page.evaluate(()=>__requests.filter(r=>r.options.method==='PATCH').at(-1).options.body.event_target),'花');
      assert.equal(await page.evaluate(()=>__calls.length),chatRequestsBefore,'Direct event edits must not call chat or execute a device action');
      await page.screenshot({path:'/tmp/ssm-event-parameter-saved.png',fullPage:true});
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await setTarget('主人');
      await page.click('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-save-success');
      await page.evaluate(()=>{window.__parameterDelay=0;window.__originalTeaching=structuredClone(__training.records[0]);});
      await page.keyboard.press('Escape');
      await page.evaluate(()=>{
        const edge=__sm.regions.find(r=>r.id==='body').transitions.find(t=>t.src==='sitting' && t.dst==='standing');
        edge.id='tr_reverse';
        __training.catalog.transitions.push({id:'tr_reverse',action:edge.action,src:'sitting',dst:'standing',
          state_field:'body_state',source_label:'坐下',destination_label:'站立',params_schema:{properties:{}},fixed_params:[]});
        __training.catalog.presentation.actions[edge.action]='站立';
        __training.records.push({...structuredClone(__originalTeaching),teaching_id:'reverse-event',revision:1,history:[],
          definition:{trigger:{event_id:'vision.target_presence',where:{target:'椅子',detected:true}},origins:['agent_autonomous'],
            steps:[{transition_ids:['tr_reverse'],defaults:{},conditions:[],goal_state:'standing'}]}});
        __renderState(__state);__emit({type:'training.updated',unit_id:'go2'});
      });
      await page.tap('.fsm-whiteboard__edge[data-action="Sit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="teach1"]');
      const originalForward=await page.evaluate(()=>JSON.stringify(__training.records.find(r=>r.teaching_id==='teach1')));
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await setTarget('花');
      await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.find(button=>button.textContent.includes('坐下 → 站立')).click());
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="reverse-event"]');
      assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="teach1"]'),null,'The reverse tab only shows its own events');
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await setTarget('盒子');
      await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.find(button=>button.textContent.includes('站立 → 坐下')).click());
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',input=>input.value),'花','Switching direction preserves the forward draft');
      await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.find(button=>button.textContent.includes('坐下 → 站立')).click());
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：看到的对象"]',input=>input.value),'盒子','Switching back preserves the reverse draft independently');
      await page.evaluate(()=>{window.__parameterDelay=400;});
      await page.click('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]');
      await page.waitForFunction(()=>[...document.querySelectorAll('.device-whiteboard__directions button')].every(button=>button.disabled));
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-save-success');
      assert.equal(await page.evaluate(()=>JSON.stringify(__training.records.find(r=>r.teaching_id==='teach1'))),originalForward,'Saving a reverse event cannot modify the forward event');
      const savedReverse=await page.evaluate(()=>JSON.stringify(__training.records.find(r=>r.teaching_id==='reverse-event')));
      await page.$$eval('.device-whiteboard__directions button',buttons=>buttons.find(button=>button.textContent.includes('站立 → 坐下')).click());
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card[data-teaching-id="teach1"]');
      await setTarget('树');
      await page.click('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-save-success');
      assert.equal(await page.evaluate(()=>JSON.stringify(__training.records.find(r=>r.teaching_id==='reverse-event'))),savedReverse,'Saving a forward event cannot modify the reverse event');
      await page.screenshot({path:'/tmp/ssm-compact-direction-save.png',fullPage:true});
      await page.keyboard.press('Escape');
      await page.evaluate(()=>{
        __training.records=[structuredClone(__originalTeaching)];window.__parameterDelay=0;
        __emit({type:'training.updated',unit_id:'go2'});
      });
      await page.evaluate(()=>{
        __training.catalog.hash='new-capabilities';
        __training.records[0].capability_hash='old-capabilities';
        __training.records[0].execution_reasons=[{code:'capability_changed'}];
        __emit({type:'training.updated',unit_id:'go2'});
      });
      await page.waitForSelector('.fsm-whiteboard__edge.is-teaching-unconfirmed');
      assert.equal(await page.$('.fsm-whiteboard__edge.is-teaching-result'),null,'An incompatible association must not retain an active highlight');
      await page.tap('.fsm-whiteboard__edge[data-action="Sit"]');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector');
      await page.waitForFunction(()=>{
        const text=document.querySelector('[data-transition-direction]:not([hidden]) .training-inspector')?.textContent || '';
        return text.includes('看到') && text.includes('主人');
      });
      assert.equal(await page.evaluate(()=>__calls.length),count,'Line inspection sent a command');
      assert.ok(await page.$eval('[data-transition-direction]:not([hidden]) .training-inspector__notice',node=>node.textContent.includes('能力已变化')));
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events h4',node=>node.textContent),'预置事件');
      assert.ok(await page.$('[data-transition-direction]:not([hidden]) .training-inspector__card h3.training-rule-title'));
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button[type="submit"]',button=>button.disabled),true,'Incompatible capabilities cannot be saved against a stale schema');
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(button=>button.textContent==='重新载入' || button.textContent==='取消').click());
      await page.screenshot({path:'/tmp/ssm-training-rule-card.png',fullPage:true});
      await page.evaluate(()=>{
        __training.catalog.hash='hash';
        __training.records[0].capability_hash='hash';
        __training.records[0].execution_reasons=[];
        __emit({type:'training.updated',unit_id:'go2'});
      });
      await page.waitForFunction(()=>!document.querySelector('.fsm-whiteboard__edge.is-teaching-unconfirmed'));
      await page.keyboard.press('Escape');
      await page.type('.device-page__chat [aria-label="聊天消息"]','以后坐下后等三秒才能再次坐下');
      await page.keyboard.press('Enter');
      await page.waitForFunction(()=>__calls.some(r=>r.action==='chat'));
      await page.evaluate(()=>{
        const call=__calls.find(r=>r.action==='chat');
        __proposal={proposal_id:'p1',unit_id:'go2',operation:'create',source_run_id:call.params.task_id,source_text:call.params.message,created_at:1,expected_revision:0,capability_hash:'hash',summaryText:'坐下后至少间隔三秒再坐',changeText:'坐下完成后，三秒内不重复坐下。',confirmation_allowed:true,after:{...__training.records[0],source_text:call.params.message}};
        __proposals.push(__proposal);
        __emit({type:'training.proposed',unit_id:'go2',proposal_id:'p1'});
      });
      await page.waitForSelector('.chat-messages .training-change');
      assert.ok(await page.$eval('.training-event-chain',node=>node.textContent.includes('站立') && node.textContent.includes('坐下')));
      assert.equal(await page.$('.training-event-chain__event'),null,'The compact chat card does not repeat the event inside its state pair');
      assert.ok(await page.$eval('.training-change',node=>node.textContent.includes('看到主人时') && node.textContent.includes('切换到坐下')));
      assert.ok(await page.$eval('.training-change__status',node=>node.textContent==='待确认'));
      assert.equal(await page.$('.training-event-details'),null,'The sentence replaces redundant parameter rows');
      assert.equal(await page.$eval('.chat-messages',node=>node.textContent.split('以后坐下后等三秒才能再次坐下').length-1),1,'Proposal duplicated the original user message');
      assert.equal(await page.$eval('.training-change',node=>/用户委托|已加载|JSON/.test(node.textContent)),false);
      assert.equal(await page.$('.training-change button[aria-label="修改规则"]'),null,'Drafts must not send parameter edits back into chat');
      await page.type('.device-page__chat [aria-label="聊天消息"]','改成五秒');
      await page.keyboard.press('Enter');
      await page.waitForFunction(()=>__calls.filter(r=>r.action==='chat').length===2);
      assert.equal(await page.evaluate(()=>!!__calls.filter(r=>r.action==='chat')[1].params.selection),false,'Natural-language messages must not acquire an edit selection from the rule card');
      await page.evaluate(()=>{
        const call=__calls.filter(r=>r.action==='chat')[1];
        __proposals[0].superseded_by='p2';
        __proposals.push({...__proposal,proposal_id:'p2',superseded_by:null,source_run_id:call.params.task_id,source_text:call.params.message,created_at:2,summaryText:'坐下后至少间隔五秒再坐',changeText:'坐下完成后，五秒内不重复坐下。',before:{name:__proposal.summaryText},beforeSummaryText:__proposal.summaryText});
        __emit({type:'training.proposed',unit_id:'go2',proposal_id:'p2'});
      });
      await page.waitForFunction(()=>document.querySelectorAll('.chat-messages .training-change').length===2);
      assert.equal(await page.$('.training-change__version--before'),null,'A new rule must not invent a previous saved version');
      await page.evaluate(()=>window.__conflict=true);
      await page.$$eval('.training-change button',buttons=>buttons.find(b=>b.textContent==='确认并生效').click());
      await page.waitForFunction(()=>document.querySelector('.chat-messages').textContent.includes('规则已经变化'));
      await page.evaluate(()=>window.__conflict=false);
      await page.$$eval('.training-change button',buttons=>buttons.find(b=>b.textContent==='确认并生效').click());
      await page.waitForFunction(()=>__proposals.find(p=>p.proposal_id==='p2').confirmed);
      await page.waitForFunction(()=>document.querySelectorAll('.training-change .training-actions').length===0);
      assert.equal(await page.$('.training-change--active'),null,'A saved event must wait for the exact executor load acknowledgement');
      assert.ok(await page.$eval('.chat-messages',node=>node.textContent.includes('等待设备加载')));
      await page.evaluate(()=>{
        __training.records[0].loaded_revision=__training.records[0].revision;
        __training.records[0].load_status='loaded';
        __emit({type:'training.loaded',unit_id:'go2'});
      });
      await page.waitForSelector('.training-change--active');
      assert.equal(await page.$$('.training-change--active button').then(nodes=>nodes.length),1);
      assert.match(await page.$eval('.training-change--active',node=>node.textContent),/看到主人时.*切换到坐下/);
      assert.ok(await page.$('.training-change--active button[aria-label="查看状态转移"] svg'));
      assert.equal(await page.$eval('.training-change--active button[aria-label="查看状态转移"]',node=>node.textContent.trim()),'');
      assert.deepEqual(await page.$eval('.training-change--active',node=>{
        const pair=getComputedStyle(node.querySelector('.training-event-chain__path'));
        const icon=getComputedStyle(node.querySelector('[aria-label="查看状态转移"]'));
        return {pairFont:pair.fontSize,iconPosition:icon.position,iconMarginTop:icon.marginTop,
          iconWidth:icon.width,iconHeight:icon.height};
      }),
      {pairFont:'12px',iconPosition:'absolute',iconMarginTop:'0px',iconWidth:'44px',iconHeight:'44px'},
      'The mobile state pair remains small and the accessible locate icon stays compact');
      assert.ok(await page.$eval('.training-change--active',node=>{
        const card=node.getBoundingClientRect(),button=node.querySelector('[aria-label="查看状态转移"]').getBoundingClientRect();
        return button.top-card.top<20 && card.right-button.right<20;
      }),'The accessible locate icon remains in the top right corner');
      await page.screenshot({path:'/tmp/ssm-event-active-chat.png',fullPage:true});
      const pointerSession=await page.createCDPSession();
      await page.setViewport({width:1440,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await pointerSession.send('Emulation.setTouchEmulationEnabled',{enabled:false});
      assert.deepEqual(await page.$eval('.training-change--active button[aria-label="查看状态转移"]',node=>{
        const style=getComputedStyle(node);return {width:style.width,height:style.height,marginTop:style.marginTop};
      }),{width:'32px',height:'32px',marginTop:'0px'},'The desktop locate icon uses the compact fine-pointer size');
      await pointerSession.send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:1});
      await pointerSession.detach();
      await page.setViewport({width:390,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await page.click('.training-change--active .training-event-locate');
      await page.waitForSelector('.device-whiteboard__popover--transition [data-transition-direction]:not([hidden]) .training-inspector__card.is-new');
      assert.ok(await page.$('.fsm-whiteboard__edge.is-selected'),'View transition must select and open the matching graph connection');
      await page.keyboard.press('Escape');
      assert.equal(await page.$$('.chat-messages .training-change').then(nodes=>nodes.length),2,'Confirmation removed the history card');
      assert.equal(await page.evaluate(()=>__calls.filter(r=>r.action!=='chat').length),0,'Confirmation sent a device action');
      await page.evaluate(()=>{
        __proposals.push({...__proposal,proposal_id:'p3',superseded_by:null,source_text:'另一条规则',created_at:3});
        __emit({type:'training.proposed',unit_id:'go2',proposal_id:'p3'});
      });
      await page.waitForFunction(()=>document.querySelectorAll('.training-change').length===3);
      await page.$$eval('.training-change button',buttons=>buttons.find(b=>b.textContent==='先不添加').click());
      await page.waitForFunction(()=>__proposals.find(p=>p.proposal_id==='p3').cancelled);
      await page.click('.device-header__settings');
      await page.$$eval('.device-settings-go2', nodes => nodes.find(node => node.textContent.includes('设备规则')).click());
      await page.waitForSelector('.training-drawer');
      assert.equal(await page.$eval('.training-drawer',node=>/已加载|暂停|启用|伙伴训练/.test(node.textContent)),false);
      assert.equal(await page.$$eval('.training-record__actions button',nodes=>nodes.every(node=>!node.textContent.trim() && node.querySelector('svg') && node.getAttribute('aria-label'))),true);
      await page.click('.training-record button[aria-label="修改参数"]');
      await page.waitForSelector('.training-record input[type="text"]');
      assert.equal(await page.$('.training-selection'),null);
      assert.ok(await page.$('.training-drawer'),'Editing must not navigate away from the rule list');
      await page.$$eval('.training-record button',buttons=>buttons.find(b=>b.textContent==='取消').click());
      await page.click('.training-record button[aria-label="删除规则"]');
      await page.waitForFunction(()=>document.querySelectorAll('.training-record').length===0);
      assert.equal(await page.evaluate(()=>__requests.some(r=>r.url==='/api/training/teach1' && r.options.method==='DELETE')),true);
      assert.equal(await page.evaluate(()=>__requests.some(r=>r.url==='/api/training/proposals' && r.options.body.operation==='remove')),false,'Direct deletion must not create a conversation proposal');
      await page.keyboard.press('Escape');
      await page.evaluate(()=>{
        __renderState({...__state},__lampCard);
      });
      await page.waitForFunction(()=>!document.querySelector('.device-header')?.textContent.includes('Go2'));
      await page.evaluate(()=>__renderState({...__state},__card));
      await page.waitForFunction(()=>document.querySelectorAll('.chat-messages .training-change').length>=3);
      assert.equal(await page.$eval('.chat-messages',node=>node.textContent.includes('改成五秒')),true,'Returning to device lost the rule conversation');
      for (const width of [768,1440,390]) {
        await page.setViewport({width,height:844,deviceScaleFactor:1,hasTouch:true,isMobile:true});
        assert.equal(await page.$eval('.fsm-whiteboard__viewport',e=>e.getBoundingClientRect().width>0),true);
      }
      await page.screenshot({path:'/tmp/ssm-rule-chat-preview.png',fullPage:true});
      await page.evaluate(() => {
        __lampCard._online=true;
        const region=__lampCard.state_machine.regions.find(region=>region.id==='device');
        __training.catalog={hash:'a'.repeat(64),events:__lampCard.observations,limits:{cooldown_s:{minimum:1,maximum:3600}},
          presentation:{actions:Object.fromEntries(region.transitions.filter(t=>t.action).map(t=>[t.action,t.label]))},
          transitions:region.transitions.map(edge=>({...edge,fixed_params:edge.training_fixed_params || [],
            params_schema:__lampCard.skills.find(skill=>skill.invoke.action===edge.action)?.params_schema || {type:'object',properties:{}}}))};
        const edge=__lampCard.state_machine.regions.find(region=>region.id==='device').transitions
          .find(edge=>edge.src==='ON' && edge.dst==='ON' && edge.action==='SET_COLOR');
        __training.records=[{unit_id:__lampCard.unit_id,teaching_id:'reading',revision:1,history:[],updated_at:1,desired_status:'enabled',capability_hash:__training.catalog.hash,
          name:'阅读灯光',definition:{situation:'准备看书时',origins:['user_delegated'],
            steps:[{transition_ids:[edge.id],conditions:[],defaults:{r:255,g:236,b:210,brightness:200}}]}}];
        __renderState({ism:'ON',ts:Date.now()/1000},__lampCard);
        __emit({type:'training.updated',unit_id:__lampCard.unit_id});
      });
      const loopSelector='.fsm-whiteboard__edge[data-source="device:ON"][data-target="device:ON"]';
      await page.waitForSelector(loopSelector);
      const callsBeforeInspect=await page.evaluate(()=>__calls.length);
      await page.$eval(loopSelector,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
      await page.waitForFunction(()=>document.querySelector('[data-transition-direction]:not([hidden]) .training-inspector')?.textContent.includes('255'));
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) .training-inspector',node=>/已保存|已加载|已应用/.test(node.textContent)),false);
      assert.equal(await page.evaluate(()=>__calls.length),callsBeforeInspect,'Parameter inspection executed an action');
      await page.screenshot({path:'/tmp/ssm-lamp-teaching-preview.png',fullPage:true});
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) input[data-path="step:0:brightness"]');
      assert.equal(await page.$('.training-selection'),null);
      await page.$eval('[data-transition-direction]:not([hidden]) input[data-path="step:0:brightness"]',input=>{
        const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
        setter.call(input,'190');input.dispatchEvent(new Event('input',{bubbles:true}));
      });
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      await page.waitForFunction(()=>__training.records[0].revision===2 && !document.querySelector('[data-transition-direction]:not([hidden]) .training-parameter-actions'));
      assert.equal(await page.evaluate(()=>__training.records[0].definition.steps[0].defaults.brightness),190);
      assert.deepEqual(await page.evaluate(()=>__training.records[0].definition.steps[0].defaults),{r:255,g:236,b:210,brightness:190},'Editing brightness preserves every other saved color parameter');
      assert.equal(await page.evaluate(()=>__calls.length),callsBeforeInspect);
      await page.keyboard.press('Escape');
      await page.evaluate(()=>{
        const transitions=__lampCard.state_machine.regions.find(region=>region.id==='device').transitions.filter(edge=>edge.action==='BLINK');
        __training.records=[{unit_id:__lampCard.unit_id,teaching_id:'happy-blink',revision:1,history:[],desired_status:'enabled',capability_hash:__training.catalog.hash,
          name:'开心时闪烁5下',definition:{emotion:'happy',trigger:null,origins:['user_delegated','agent_autonomous'],
            steps:[{transition_ids:transitions.map(edge=>edge.id),conditions:[],defaults:{count:5,r:120,g:40,b:210}}],cooldown_s:12}}];
        __renderState({ism:'BLINKING',count:5,ts:Date.now()/1000+1},__lampCard);
        __emit({type:'training.updated',unit_id:__lampCard.unit_id});
      });
      await page.waitForFunction(()=>document.querySelectorAll('.fsm-whiteboard__rule-badge').length===3);
      const blinkLoop='.fsm-whiteboard__edge[data-source="device:BLINKING"][data-target="device:BLINKING"]';
      assert.ok(await page.$eval(blinkLoop,node=>node.getAttribute('aria-label').includes('开心时')));
      assert.equal(await page.$('.fsm-whiteboard__edge text'),null);
      // Saving associates conditions with the declared loop; it never dispatches an action.
      assert.equal(await page.evaluate(()=>__calls.length),callsBeforeInspect);
      await page.screenshot({path:'/tmp/ssm-lamp-happy-rule-mobile.png',fullPage:true});
      await page.$eval(blinkLoop,node=>node.dispatchEvent(new MouseEvent('click',{bubbles:true})));
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-transition-events__self');
      for(const value of ['120','40','210','12'])assert.ok(await page.$eval('[data-transition-direction]:not([hidden]) .training-rule-title',(node,value)=>node.textContent.includes(value),value),'The sentence retains non-default colors and cooldown');
      assert.match(await page.$eval('[data-transition-direction]:not([hidden]) .training-transition-events__self',node=>node.textContent),/源状态与目标状态相同/);
      assert.ok(await page.$eval('.fsm-whiteboard__rule-ring',node=>Number(node.getAttribute('r'))<=3.5),'The line marker must stay small');
      await page.click('[data-transition-direction]:not([hidden]) .training-inspector__revise');
      await page.waitForSelector('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]');
      assert.equal(await page.$('.training-selection'),null);
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]',input=>input.type),'text');
      assert.equal(await page.$('[data-transition-direction]:not([hidden]) .training-inspector select'),null);
      const setCount=async value=>page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]',(input,value)=>{
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
        input.dispatchEvent(new Event('input',{bubbles:true}));
      },value);
      const patchesBeforeInvalid=await page.evaluate(()=>__requests.filter(r=>r.options.method==='PATCH').length);
      await setCount('21');
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card .training-error');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]',input=>input.value),'21','Invalid numeric text remains available for correction');
      assert.equal(await page.evaluate(()=>__requests.filter(r=>r.options.method==='PATCH').length),patchesBeforeInvalid,'Out-of-range values are rejected before the API request');
      await setCount('1.5');
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      assert.equal(await page.evaluate(()=>__requests.filter(r=>r.options.method==='PATCH').length),patchesBeforeInvalid,'Integer schema still applies to text inputs');
      const emotionInput='[data-transition-direction]:not([hidden]) input[aria-label="事件参数：情绪"]';
      assert.equal(await page.$eval(emotionInput,input=>input.type),'text');
      assert.equal(await page.$eval(emotionInput,input=>input.value),'开心');
      await page.$eval(emotionInput,input=>{
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'兴奋');
        input.dispatchEvent(new Event('input',{bubbles:true}));
      });
      await setCount('6');
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      assert.equal(await page.evaluate(()=>__requests.filter(r=>r.options.method==='PATCH').length),patchesBeforeInvalid,'Unsupported emotion labels cannot bypass the enum schema');
      assert.equal(await page.$eval(emotionInput,input=>input.value),'兴奋');
      await page.$eval(emotionInput,input=>{
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'好奇');
        input.dispatchEvent(new Event('input',{bubbles:true}));
      });
      await setCount('6');
      await page.evaluate(()=>window.__parameterConflict=true);
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      await page.waitForSelector('[data-transition-direction]:not([hidden]) .training-inspector__card .training-error');
      assert.equal(await page.$eval('[data-transition-direction]:not([hidden]) input[aria-label="事件参数：次数"]',input=>input.value),'6','A conflict must preserve unsaved input');
      assert.equal(await page.evaluate(()=>__training.records[0].revision),1);
      await page.evaluate(()=>window.__parameterConflict=false);
      await setCount('7');
      await page.screenshot({path:'/tmp/ssm-rule-parameter-editor-mobile.png',fullPage:true});
      const cardsBefore=await page.$$('.chat-rule-message').then(nodes=>nodes.length);
      await page.$$eval('[data-transition-direction]:not([hidden]) .training-parameter-actions button',buttons=>buttons.find(b=>b.textContent==='保存').click());
      await page.waitForFunction(()=>__training.records[0].revision===2 && !document.querySelector('[data-transition-direction]:not([hidden]) .training-parameter-actions'));
      assert.equal(await page.evaluate(()=>__training.records[0].definition.steps[0].defaults.count),7);
      assert.equal(await page.evaluate(()=>__training.records[0].definition.emotion),'curious','Chinese emotion text maps back to its declared enum value');
      assert.deepEqual(await page.evaluate(()=>__training.records[0].definition.steps[0].defaults),{count:7,r:120,g:40,b:210},'Saving inline count preserves additional action parameters');
      assert.equal(await page.evaluate(()=>__training.records[0].definition.cooldown_s),12);
      assert.equal(await page.$$('.fsm-whiteboard__rule-badge').then(nodes=>nodes.length),3,'One saved rule must continue to annotate all its transitions');
      assert.equal(await page.$$('.chat-rule-message').then(nodes=>nodes.length),cardsBefore,'A parameter save must not add conversation cards');
      assert.equal(await page.evaluate(()=>__calls.length),callsBeforeInspect,'Editing parameters must not dispatch chat or physical actions');
      await page.keyboard.press('Escape');
      for (const width of [320,390,1440]) {
        await page.setViewport({width,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Rule markers must not cause horizontal page overflow');
      }
      await page.screenshot({path:'/tmp/ssm-lamp-happy-rule-desktop.png',fullPage:true});
      assert.deepEqual(errors,[]);
      console.log('PASS: compact event popovers, inline target saving and conflict recovery, complete proposal chains, exact load acknowledgements, transition navigation, direct deletion and responsive layout.');
      return;
    }
    if (process.argv.includes('--reachability-only')) {
      const play='[data-state="life:play"]';
      assert.equal(await page.$eval(play,node=>node.dataset.reachable),'true','Idle cannot reach interaction');
      assert.equal(await page.$eval(play,node=>node.dataset.executable),'false','Interaction invented a manual action');
      assert.equal(await page.evaluate(()=>{
        const fill=id=>getComputedStyle(document.querySelector(`[data-state="${id}"] .fsm-whiteboard__circle`)).fill;
        return fill('life:play')===fill('activity:rest') && fill('life:play')!==fill('life:idle');
      }),true,'Automatic destination lacks the reachable tint');
      await page.tap(play);
      await page.waitForSelector('[aria-label="转换说明"]');
      assert.equal(await page.evaluate(()=>__calls.length),0,'Inspecting automatic reachability dispatched a command');
      assert.equal(await page.$eval('[aria-label="转换说明"]',node=>node.innerText.includes('接到互动任务')),true);
      assert.equal(await page.$eval('[data-state="body:sitting"]',node=>getComputedStyle(node).filter),'blur(1.5px)');
      assert.equal(await page.$eval(play,node=>getComputedStyle(node).filter),'none');
      assert.equal(await page.$eval('[data-state="life:idle"]',node=>getComputedStyle(node).filter),'none');
      assert.equal(await page.$eval('.fsm-whiteboard__edge.is-selected',node=>getComputedStyle(node).filter),'none');
      await page.screenshot({path:'/tmp/ssm-state-rule-focus.png',fullPage:true});
      await page.keyboard.press('Escape');
      assert.equal(await page.$eval('[data-state="body:sitting"]',node=>getComputedStyle(node).filter),'none');
      await page.evaluate(()=>__renderState({...__state,revision:2,activity_state:'rest',life_state:null,available_life_actions:['wake'],available_manual_actions:[]}));
      await page.waitForFunction(()=>document.querySelector('[data-state="life:play"]').dataset.reachable==='false');
      assert.equal(await page.$eval('[data-state="life:idle"]',node=>node.dataset.executable),'true','Wake lost its manual action');
      await page.evaluate(()=>__renderState({...__state,revision:3,activity_state:'active',life_state:'idle',connected:false},{...__card,_online:false}));
      await page.waitForFunction(()=>document.querySelector('[data-state="life:play"]').dataset.reachable==='false');
      await page.evaluate(()=>__renderState({ism:'BLINKING',ts:1},{...__lampCard,_online:true}));
      await page.waitForSelector('[data-state="device:BLINKING"].is-current');
      assert.deepEqual(await page.$$eval('.fsm-whiteboard__node',nodes=>nodes.map(node=>node.dataset.state).sort()),
        ['device:BLINKING','device:OFF','device:ON']);
      assert.equal(await page.$eval('[data-state="device:ON"]',node=>node.dataset.reachable),'true');
      assert.equal(await page.$eval('[data-state="device:ON"]',node=>node.dataset.executable),'true','Blinking lamp cannot return to steady light');
      assert.equal(await page.evaluate(()=>__calls.length),0,'Rendering lamp states dispatched a command');
      assert.deepEqual(errors,[]);
      console.log('PASS: manual versus event reachability, idle-to-interaction, rest/offline gates, shared lamp projection, focused rules and blur dismissal.');
      return;
    }
    if (process.argv.includes('--remote-only') || process.argv.includes('--direct-only')) {
      await page.evaluate(()=>{
        __card.widgets=[{type:'joystick',action:'velocity',params_schema:__velocitySchema,states:['standing','moving'],state_field:'body_state',
          node_state:'moving',enter_action:'EnterMoveControl',exit_action:'StopMove',enabled_when:{control_mode:'remote'},
          available_actions_field:'available_actions',availability_key:'Move'}];
        __renderState(__state);
      });
      await page.waitForFunction(()=>document.querySelector('[data-state="body:moving"]').dataset.executable==='true');
      await page.evaluate(()=>{
        const node=document.querySelector('[data-state="body:moving"]');
        node.dispatchEvent(new MouseEvent('click',{bubbles:true}));node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
      });
      await page.waitForSelector('.device-remote-controls');
      assert.equal(await page.$('.device-remote-controls header'),null,'Movement instructions remain above the sticks');
      await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
      assert.equal(await page.evaluate(()=>__calls.length),1,'Repeated movement taps dispatched duplicate entry');
      assert.equal(await page.evaluate(()=>__calls[0].action),'EnterMoveControl');
      assert.equal(await page.$$('.device-remote-controls button').then(nodes=>nodes.length),0,'A separate movement-mode button remains');
      assert.equal(await page.evaluate(()=>__velocities.length),0,'Selecting movement sent velocity');
      const pad='[aria-label="移动摇杆，方向键控制，松开停止"]';
      assert.equal(await page.$eval(pad,node=>node.getAttribute('aria-disabled')),'true');
      await page.evaluate(()=>__renderState({...__state,revision:2,control_mode:'remote',available_actions:['Move','StopMove']}));
      await page.waitForFunction(selector=>document.querySelector(selector).getAttribute('aria-disabled')==='false',{},pad);
      await page.tap('[data-state="body:moving"]');
      assert.equal(await page.evaluate(()=>__calls.length),1,'Selecting active remote mode entered again');
      await page.focus(pad);await page.keyboard.down('ArrowUp');
      await page.waitForFunction(()=>__velocities.some(call=>call.params.vx>0));
      await page.keyboard.up('ArrowUp');
      await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
      const turnPad='[aria-label="转向摇杆，方向键控制，松开停止"]';
      await page.focus(turnPad);await page.keyboard.down('ArrowRight');
      await page.waitForFunction(()=>__velocities.some(call=>call.params.vyaw<0));
      await page.keyboard.up('ArrowRight');
      await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
      await page.$eval('.device-joysticks',node=>node.scrollIntoView({block:'center'}));
      const centers=await page.$$eval('.device-joysticks [role="group"]',nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
      }));
      const moveTouch=await page.touchscreen.touchStart(centers[0].x,centers[0].y-20);
      const turnTouch=await page.touchscreen.touchStart(centers[1].x+20,centers[1].y);
      await page.waitForFunction(()=>__velocities.at(-1)?.params.vx>0&&__velocities.at(-1)?.params.vyaw<0);
      await moveTouch.end();
      await page.waitForFunction(()=>__velocities.at(-1)?.params.vx===0&&__velocities.at(-1)?.params.vyaw<0);
      await turnTouch.end();
      await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
      const beforeStop=await page.evaluate(()=>__calls.length);
      await page.tap('[data-state="body:standing"]');
      await page.waitForFunction(count=>__calls.length===count+1,{},beforeStop);
      assert.equal(await page.evaluate(()=>__calls.at(-1).action),'StopMove','Selecting standing did not leave movement');
      await page.evaluate(()=>__renderState({...__state,revision:3,control_mode:'normal',available_actions:[]}));
      await page.waitForFunction(()=>!document.querySelector('.device-remote-controls'));
      await page.tap('[data-state="body:moving"]');
      assert.equal(await page.evaluate(()=>__calls.length),beforeStop+1,'Unavailable movement bypassed state checks');
      assert.deepEqual(errors,[]);
      console.log('PASS: one-tap movement, no mode buttons, translation/turning and release-to-stop, selecting standing exits, duplicate and state gates.');
      if (process.argv.includes('--direct-only')) {
        const tapOnce = async (id,action,params={}) => {
          const selector=`[data-state="${id}"]`;
          await page.waitForFunction(selector=>document.querySelector(selector)?.dataset.executable==='true',{},selector);
          const count=await page.evaluate(()=>__calls.length);
          await page.evaluate(selector=>{
            const node=document.querySelector(selector);
            node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
            node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
          },selector);
          await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
          assert.equal(await page.evaluate(()=>__calls.length),count+1,'State click must send exactly one command');
          assert.deepEqual(await page.evaluate(()=>({action:__calls.at(-1).action,params:__calls.at(-1).params})),{action,params});
          assert.equal(await page.$('[aria-label="状态说明"] .btn'),null,'Redundant action button remains');
        };
        await tapOnce('body:sitting','Sit');
        await tapOnce('body:greeting','Hello');
        await tapOnce('body:lying','StandDown');
        await tapOnce('life:explore','LIFE',{action:'start_explore'});
        await tapOnce('activity:rest','LIFE',{action:'request_rest'});
        const count=await page.evaluate(()=>__calls.length);
        await page.evaluate(()=>__renderState({...__state,revision:4,available_manual_actions:[]}));
        await page.waitForFunction(()=>document.querySelector('[data-state="body:sitting"]').dataset.executable==='false');
        await page.tap('[data-state="body:sitting"]');
        assert.equal(await page.evaluate(()=>__calls.length),count,'Blocked action was sent');
        await page.tap('.fsm-whiteboard__edge[data-action="Sit"]');
        await page.waitForSelector('[aria-label="转换说明"]');
        assert.equal(await page.evaluate(()=>__calls.length),count,'Inspecting rules sent a command');
        // Distinct declared commands still require the user to choose one.
        await page.evaluate(()=>{
          const card=structuredClone(__card);
          card.state_machine.regions.find(r=>r.id==='body').transitions.push({src:'standing',dst:'sitting',action:'AlternateSit',label:'另一种坐下',dispatch_only:true});
          __renderState({...__state,revision:5,available_manual_actions:['Sit','AlternateSit']},card);
        });
        await page.waitForFunction(()=>document.querySelector('[data-state="body:sitting"]').classList.contains('is-actionable'));
        await page.tap('[data-state="body:sitting"]');
        await page.waitForSelector('[aria-label="状态说明"] .btn');
        assert.equal(await page.$$eval('[aria-label="状态说明"] .btn',nodes=>nodes.length),2);
        assert.equal(await page.evaluate(()=>__calls.length),count,'An ambiguous action was chosen automatically');
        await page.click('[aria-label="状态说明"] .btn');
        await page.waitForFunction(()=>document.querySelector('.device-state-board').getAttribute('aria-busy')==='false');
        assert.equal(await page.evaluate(()=>__calls.length),count+1);
        await page.evaluate(()=>__renderState({ism:'OFF',ts:1},{...__lampCard,_online:true}));
        await tapOnce('device:ON','SET_STATE',{state:'ON'});
        await page.evaluate(()=>__renderState({ism:'ON',ts:2},{...__lampCard,_online:true}));
        await tapOnce('device:OFF','SET_STATE',{state:'OFF'});
        const blink=lampCard.state_machine.regions[0].transitions.find(t=>t.src==='ON' && t.action==='BLINK');
        await tapOnce('device:BLINKING','BLINK',blink.id ? {} : blink.params);
        assert.deepEqual(errors,[]);
        console.log('PASS: direct dog and lamp state commands, duplicate declarations collapsed, blocked actions and rule inspection never dispatch, distinct actions remain selectable.');
      }
      return;
    }
    if (process.argv.includes('--layout-only')) {
      for (const style of ['process','avatars']) await page.addStyleTag({path:`app/styles/${style}.css`});
      await page.addScriptTag({content:compile('app/src/app.jsx')});
      await page.addScriptTag({content:compile('app/src/utils/activityView.js')});
      await page.addScriptTag({content:compile('app/src/utils/runProcess.js')});
      await page.addScriptTag({content:compile('app/src/components/RunProcess.jsx')});
      await page.addScriptTag({content:compile('app/src/components/ChatMessages.jsx')});
      await page.addScriptTag({content:compile('app/src/components/ChatPanel.jsx')});
      await page.evaluate(() => {
        const ResponsiveDevice = props => {
          useAppViewport();
          return React.createElement('div',{className:'device-page-route'},React.createElement(FsmDevicePage,props));
        };
        window.__renderState=(next,card=__card,actor='admin')=>{
          __state=next; window.responsiveUnit=card.unit_id;
          appRoot.render(React.createElement(ResponsiveDevice,{unitId:card.unit_id,device:card,
            liveState:{state:next},actor,sessionId:'simulated_session',onBack(){}}));
        };
      });
      const checkResponsiveLayout = async (width,height) => {
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const layout=await page.evaluate(() => {
          const bounds=selector=>document.querySelector(selector).getBoundingClientRect().toJSON();
          const input=document.querySelector('.device-page__chat [aria-label="聊天消息"]');
          return {board:bounds('.device-state-workspace'),chat:bounds('.device-page__chat'),
            body:bounds('.device-page__body'),input:input.getBoundingClientRect().toJSON(),
            same:input===window.responsiveInput,draft:input.value,calls:__calls.length};
        });
        assert(layout.input.bottom<=height && layout.input.top>=0, `Composer clipped at ${width}×${height}`);
        assert(layout.body.left>=0 && layout.body.right<=width && layout.body.width<=1600);
        if(width>=1024) {
          assert(layout.board.right<=layout.chat.left+1 && Math.abs(layout.board.top-layout.chat.top)<1,
            `Desktop board and conversation must be side by side at ${width}×${height}`);
          assert(layout.chat.width>=360, 'Desktop conversation is too narrow');
        } else assert(layout.board.bottom<=layout.chat.top+1, 'Mobile board must stay above the conversation');
        assert(layout.same && layout.draft==='保留设备草稿', 'Changing layout must preserve the composer and draft');
        assert.equal(layout.calls,await page.evaluate(()=>window.responsiveCalls), 'Resizing must not send a device command');
        if(process.env.SCREENSHOT_DIR && [390,1440,1920].includes(width)) {
          fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
          await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`responsive-${await page.evaluate(()=>responsiveUnit)}-${width}-${height}.png`)});
        }
      };
      const rememberComposer = async () => {
        await page.focus('.device-page__chat [aria-label="聊天消息"]');
        await page.keyboard.down('Control'); await page.keyboard.press('KeyA'); await page.keyboard.up('Control');
        await page.keyboard.press('Backspace');
        await page.type('.device-page__chat [aria-label="聊天消息"]','保留设备草稿');
        await page.$eval('.device-page__chat [aria-label="聊天消息"]',node=>node.blur());
        await page.evaluate(()=>{
          window.responsiveInput=document.querySelector('.device-page__chat [aria-label="聊天消息"]');
          window.responsiveCalls=__calls.length;
        });
      };
      await page.evaluate(()=>{
        __card.widgets=[{type:'video',endpoint:'/api/go2/video',states:[],requires_connection:true},
          {type:'map',endpoint:'/api/go2/map.png',states:[],requires_connection:true}];
        __renderState(__state);
      });
      await page.waitForSelector('.device-whiteboard__media');
      await rememberComposer();
      const go2Height = await page.$eval('.device-state-workspace', element => element.getBoundingClientRect().height);
      const graph = await page.$eval('.fsm-whiteboard__viewport',svg=>({
        loops:[...svg.querySelectorAll('.fsm-whiteboard__edge')].filter(edge=>edge.dataset.source===edge.dataset.target).length,
        nodes:Object.fromEntries([...svg.querySelectorAll('.fsm-whiteboard__node')].map(node=>
          [node.dataset.state,{label:node.querySelector('text').textContent,position:node.getAttribute('transform')}])),
      }));
      assert.equal(graph.loops,0);
      assert.equal(graph.nodes['body:moving'].label,'移动');
      assert.equal(graph.nodes['body:content'].label,'开心');
      assert.equal(graph.nodes['body:sitting'].label,'坐下');
      await page.screenshot({path:'/tmp/ssm-go2-radial-layout.png',fullPage:true});
      for (const [width,height] of [[320,740],[360,740],[390,844],[430,932],[768,1024],[1023,768],[1024,768],[1366,768],[1440,1000],[1920,1080],[844,390],[390,520]]) {
        await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
        await checkResponsiveLayout(width,height);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await page.evaluate(() => {
          const tiles = [...document.querySelectorAll('.device-whiteboard__media-tile')].map(node => node.getBoundingClientRect());
          const [video,map] = tiles;
          const behavior = [...document.querySelectorAll('.fsm-whiteboard__node')].filter(node => node.dataset.group === '行为');
          const poses = [...document.querySelectorAll('.fsm-whiteboard__node')].filter(node => node.dataset.group === '姿态与动作');
          const canvas = document.querySelector('.device-whiteboard__canvas').getBoundingClientRect();
          return tiles.length === 2 && Math.abs(video.width-map.width) < 1 && Math.abs(video.height-map.height) < 1
            && Math.abs(video.width/video.height-16/9) < .03 && Math.abs(video.left-map.left) < 1 && video.bottom < map.top
            && behavior.length === 4 && behavior.every(node => node.getBoundingClientRect().left > video.right)
            && poses.every(node => node.getBoundingClientRect().top > map.bottom)
            && [...document.querySelectorAll('.fsm-whiteboard__node')].every(node => {
              const rect = node.getBoundingClientRect();
              return rect.left >= canvas.left-1 && rect.right <= canvas.right+1 && rect.top >= canvas.top-1 && rect.bottom <= canvas.bottom+1;
            }) && document.documentElement.scrollWidth <= innerWidth;
        }), true, `Go2 media or states overlap at ${width}×${height}`);
        assert.equal(await page.$eval('.device-page__chat [aria-label="聊天消息"]', node => node.getBoundingClientRect().bottom <= innerHeight), true);
      }
      await page.setViewport({width:390,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await page.evaluate(()=>__renderState({ism:'ON',ts:1},{...__lampCard,_online:true}));
      await page.waitForSelector('.fsm-whiteboard__node[data-state="device:ON"]');
      await rememberComposer();
      assert.equal(await page.$$eval('.fsm-whiteboard__edge',edges=>edges.some(edge=>edge.dataset.source===edge.dataset.target)),false);
      const lampHeight = await page.$eval('.device-state-workspace', element => element.getBoundingClientRect().height);
      assert(lampHeight < go2Height - 80, `Lamp board did not shrink: lamp=${lampHeight}, Go2=${go2Height}`);
      console.log(`Adaptive board height at 390px: lamp=${Math.round(lampHeight)}px, Go2=${Math.round(go2Height)}px`);
      for (const [width,height] of [[320,740],[360,740],[390,844],[430,932],[1024,768],[1366,768],[1440,1000],[1920,1080],[844,390],[390,520]]) {
        await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
        await checkResponsiveLayout(width,height);
        assert.equal(await page.$eval('.fsm-whiteboard__viewport', svg => {
          const bounds = svg.getBoundingClientRect();
          return [...svg.querySelectorAll('.fsm-whiteboard__node')].every(node => {
            const r = node.getBoundingClientRect();
            return r.left >= bounds.left-1 && r.right <= bounds.right+1 && r.top >= bounds.top-1 && r.bottom <= bounds.bottom+1;
          });
        }), true, `Lamp state clipped at ${width}×${height}`);
        assert.equal(await page.$eval('.device-page__chat [aria-label="聊天消息"]', element => element.getBoundingClientRect().bottom <= innerHeight), true);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      }
      await page.setViewport({width:390,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await page.screenshot({path:'/tmp/ssm-lamp-no-loops.png',fullPage:true});
      // The route wrapper and page must follow the same visual viewport once.
      await page.evaluate(() => {
        Object.defineProperty(visualViewport,'height',{configurable:true,value:300});
        Object.defineProperty(visualViewport,'offsetTop',{configurable:true,value:40});
        visualViewport.dispatchEvent(new Event('resize'));
      });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const keyboard=await page.$eval('.device-page',node=>{
        const rect=node.getBoundingClientRect();
        const workspace=node.querySelector('.device-state-workspace');
        workspace.scrollTop=workspace.scrollHeight;
        return {top:rect.top,height:rect.height,inputBottom:node.querySelector('[aria-label="聊天消息"]').getBoundingClientRect().bottom,
          controlsBottom:node.querySelector('.device-whiteboard__controls').getBoundingClientRect().bottom,
          workspaceBottom:workspace.getBoundingClientRect().bottom};
      });
      assert.equal(keyboard.top,40);
      assert.equal(keyboard.height,300);
      assert(keyboard.inputBottom<=340, 'Device composer left the simulated keyboard viewport');
      assert(keyboard.controlsBottom<=keyboard.workspaceBottom+1, 'Short viewport must allow scrolling to the device controls');
      await page.click('.device-header__settings');
      await page.waitForFunction(() => {
        const panel=document.querySelector('.device-settings-panel')?.getBoundingClientRect();
        return panel && Math.abs(panel.bottom-340)<1 && panel.top>=40;
      });
      await page.keyboard.press('Escape');
      await page.waitForSelector('.device-settings-panel',{hidden:true});
      await page.evaluate(() => {
        delete visualViewport.height; delete visualViewport.offsetTop;
        visualViewport.dispatchEvent(new Event('resize'));
      });
      assert.deepEqual(errors,[]);
      console.log('PASS: Go2/lamp mobile and desktop layout, media/state containment, retained drafts, no resize commands, simulated keyboard viewport and management drawer.');
      return;
    }
    assert.equal(await page.$$('.device-state-lane,.device-state-action-row,.device-state-topology,.device-region-diagram').then(nodes=>nodes.length),0);
    assert.equal(await page.$eval('.device-header__status',node=>node.textContent),'在线');
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="activity:active"],.fsm-whiteboard__node[data-state^="connection:"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current').then(nodes=>nodes.length),2,'Parallel current facts were lost');
    assert.equal(await page.$$('.fsm-whiteboard__node[data-group="行为"]').then(nodes=>nodes.length),4);
    assert.equal(await page.$$('.fsm-whiteboard__node[data-group="姿态与动作"]').then(nodes=>nodes.length),12);
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="body:unknown"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$eval('.fsm-whiteboard__node[data-group="行为"]',nodes=>nodes.every(node=>Number(node.dataset.radius)>40)),true);
    assert.equal(await page.$$eval('.fsm-whiteboard__node[data-group="姿态与动作"]',nodes=>nodes.every(node=>Number(node.dataset.radius)<40)),true);
    assert.equal(await page.$('.fsm-whiteboard__section'),null,'Removed section headings returned');
    assert.equal(await page.$eval('.fsm-whiteboard__viewport',svg=>{
      const fill=selector=>getComputedStyle(svg.querySelector(selector+' .fsm-whiteboard__circle')).fill;
      return fill('.is-current')!==fill('.is-reachable:not(.is-current)');
    }),true,'Current and reachable states must have distinct fills');
    const closeExplanation = async () => {
      if (await page.$('.device-whiteboard__popover')) {
        await page.keyboard.press('Escape');
        await page.waitForFunction(()=>!document.querySelector('.device-whiteboard__popover'));
      }
    };
    const clickNode = async state => {
      await closeExplanation();
      const selector = `.fsm-whiteboard__node[data-state="${state}"]`;
      await page.waitForFunction(selector => document.querySelector(selector)?.dataset.executable === 'true', {}, selector);
      const before = await page.evaluate(()=>__calls.length);
      const bounds = await page.$eval('.fsm-whiteboard__viewport',node=>JSON.stringify(node.getBoundingClientRect().toJSON()));
      await page.tap(selector);
      const afterBounds = await page.$eval('.fsm-whiteboard__viewport',node=>JSON.stringify(node.getBoundingClientRect().toJSON()));
      if (state === 'body:moving') {
        // Entering remote control reveals its panel below the viewport.
        for (const key of ['left','top','width']) {
          assert.equal(JSON.parse(afterBounds)[key],JSON.parse(bounds)[key],`Remote control moved canvas ${key}`);
        }
      } else assert.equal(afterBounds,bounds,'State action resized the canvas');
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
    assert.equal(await page.$$eval('.fsm-whiteboard__edge',nodes=>nodes.some(node=>Number(node.dataset.ruleCount)>1)),true,'Grouped lines must retain multiple rules');
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
    assert.equal(await page.$eval('.fsm-whiteboard__node.is-selected',node=>getComputedStyle(node).outlineStyle),'none','Node selection painted a rectangular focus ring');
    assert.equal(await page.$('[aria-label="状态说明"]'),null,'Direct command still opens a redundant card');
    const assertPopover = async () => assert.equal(await page.$eval('.device-whiteboard__popover',node=>{
      const r=node.getBoundingClientRect(), canvas=document.querySelector('.device-whiteboard__canvas').getBoundingClientRect();
      return r.left>=canvas.left+7 && r.right<=canvas.right-7 && r.top>=canvas.top+7 && r.bottom<=canvas.bottom-7
        && getComputedStyle(node).backgroundColor==='rgba(28, 28, 42, 0.9)';
    }),true,'Translucent card escaped the canvas');
    await assertAllStatesVisible();
    await page.screenshot({path:'/tmp/ssm-whiteboard-node-selected.png',fullPage:true});
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current[data-state="body:standing"]').then(nodes=>nodes.length),1,'Command acceptance changed observed state');
    await closeExplanation();
    await clickNode('body:greeting');
    assert.equal(await page.$$('.fsm-whiteboard__edge text').then(nodes=>nodes.length),0,'Edges still have overlapping captions');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Hello');
    await closeExplanation();
    await clickNode('body:lying');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'StandDown');
    await closeExplanation();
    const beforeInspect = await page.evaluate(()=>__calls.length);
    await page.$eval('.fsm-whiteboard__edge[data-trigger="Sit"]',node=>node.focus());
    await page.keyboard.press('Enter');
    await page.waitForSelector('[aria-label="转换说明"]');
    assert.equal(await page.evaluate(()=>__calls.length),beforeInspect,'Edge inspection sent a command');
    assert.equal(await page.$eval('.fsm-whiteboard__edge.is-selected',node=>getComputedStyle(node).outlineStyle),'none','Edge focus painted its bounding box');
    assert.equal(await page.$('.fsm-whiteboard__edge.is-selected rect'),null);
    await assertPopover();
    await page.screenshot({path:'/tmp/ssm-whiteboard-edge-selected.png',fullPage:true});
    for (const [width,height] of [[360,520],[1440,1000],[390,844]]) {
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      await new Promise(resolve=>setTimeout(resolve,100));
      await assertPopover();
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.$('.device-whiteboard__popover'),null,'Escape did not close the card');
    assert.equal(await page.$eval('.fsm-whiteboard__edge[data-trigger="Sit"]',node=>node===document.activeElement),true,'Escape lost the selected edge keyboard focus');
    await page.keyboard.press('Enter');
    const blank = await page.$eval('.device-whiteboard__canvas',node=>{const r=node.getBoundingClientRect();return {x:r.left+3,y:r.top+3};});
    await page.touchscreen.tap(blank.x,blank.y);
    assert.equal(await page.$('.device-whiteboard__popover'),null,'Blank canvas did not dismiss the card');
    await page.$eval('.fsm-whiteboard__edge[data-trigger="Sit"]',node=>node.focus());
    await page.keyboard.press('Enter');
    await page.waitForSelector('[aria-label="转换说明"]');
    assert.equal(await page.$('[aria-label="转换说明"] .btn'),null,'Transition inspection must remain read-only');
    assert.equal(await page.evaluate(()=>__calls.length),beforeInspect,'Inspecting an edge sent a command');
    await closeExplanation();
    await clickNode('body:sitting');
    await page.waitForFunction(before => __calls.length === before + 1, {}, beforeInspect);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Sit','Node action lost its declared command');
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
    assert.equal(await page.$('.fsm-whiteboard__toolbar,.device-whiteboard__expand'),null,'Removed whiteboard tools returned');
    // Reset the test fixture by changing its layout signature; no reset UI is needed.
    const resetCamera = async () => {
      await page.evaluate(()=>{
        __card.state_machine.diagram.width += 1; __renderState(__state);
      });
      await page.waitForFunction(()=>document.querySelector('.fsm-whiteboard__viewport').viewBox.baseVal.width===__card.state_machine.diagram.width);
      await page.evaluate(()=>{__card.state_machine.diagram.width -= 1; __renderState(__state);});
      await page.waitForFunction(expected=>document.querySelector('.fsm-whiteboard__viewport > g').getAttribute('transform')===expected,{},originalTransform);
    };
    const box=await page.$eval('.fsm-whiteboard__viewport',node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
    const center={x:box.x+box.width/2,y:box.y+box.height/2};
    await page.mouse.move(center.x,center.y);
    await page.mouse.down();await page.mouse.move(center.x+45,center.y+25,{steps:6});await page.mouse.up();
    assert.notEqual(await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform')),originalTransform,'Canvas did not pan');
    assert.equal(await page.evaluate(()=>__calls.length),0,'Dragging sent a command');
    await resetCamera();
    const touchClient=await page.createCDPSession();
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:center.x-35,y:center.y,id:1},{x:center.x+35,y:center.y,id:2}]});
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:center.x-65,y:center.y,id:1},{x:center.x+65,y:center.y,id:2}]});
    await touchClient.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    assert.notEqual(await page.$eval('.fsm-whiteboard__viewport > g',node=>node.getAttribute('transform')),originalTransform,'Pinch did not zoom');
    await resetCamera();
    await page.screenshot({path:'/tmp/go2-whiteboard-mobile.png',fullPage:true});
    await page.evaluate(()=>{
      const node=document.querySelector('.fsm-whiteboard__node[data-state="body:sitting"]');
      node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
      node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
    });
    assert.equal(await page.evaluate(()=>__calls.length),1,'Duplicate request escaped guard');
    await page.waitForFunction(()=>document.querySelector('.device-state-board')?.getAttribute('aria-busy')==='false');
    await clickAction('休息');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'request_rest');
    await page.evaluate(()=>__renderState({...__state,revision:2,activity_state:'rest',life_state:null,body_state:'lying',available_actions:[],available_life_actions:['wake']}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="activity:rest"]');
    assert.equal(await page.$$('.fsm-whiteboard__node.is-current[data-state^="body:"]').then(nodes=>nodes.length),0);
    assert.equal(await page.$$('.fsm-whiteboard__node.is-retained[data-state="body:lying"]').then(nodes=>nodes.length),1);
    await page.screenshot({path:'/tmp/go2-whiteboard-rest-mobile.png',fullPage:true});
    await clickAction('唤醒');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'wake');
    await page.evaluate(()=>__sources[0].onmessage({data:JSON.stringify({...__state,epoch:'b',revision:1,activity_state:'active',life_state:'idle',body_state:null,body_confirmation:'unknown'})}));
    await page.waitForFunction(()=>document.querySelectorAll('.fsm-whiteboard__node.is-current[data-state^="body:"]').length===0);
    assert.equal(await page.$('.device-header__stop'),null,'Go2 header should not show a stop button');
    await page.click('.device-header__settings');
    assert.equal(await page.$eval('.device-settings-safety',node=>node.disabled),false,'No feedback removed the settings stop action');
    await page.click('[aria-label="关闭管理"]');
    await page.evaluate(()=>__sources[0].onmessage({data:JSON.stringify({...__state,epoch:'a',revision:99,body_state:'standing'})}));
    assert.equal(await page.$$('.fsm-whiteboard__node[data-state="body:unknown"],.fsm-whiteboard__node.is-current[data-state^="body:"]').then(nodes=>nodes.length),0);
    await page.evaluate(()=>__renderState({...__state,epoch:'c',revision:1,activity_state:'active',life_state:'idle',body_state:'standing',available_actions:['EnterMoveControl','StopMove'],available_life_actions:['request_rest','start_explore','stop_explore']},__card,'visitor'));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="body:standing"]');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge',nodes=>nodes.find(node=>node.dataset.label==='开启探索').dataset.available),'false');
    await clickAction('停止探索');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.action),'stop_explore','Idle exploration permission has no stop action');
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
    await page.tap('.fsm-whiteboard__node[data-state="body:standing"]');
    await page.waitForFunction(before=>__calls.length>before,{},beforeExit);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'StopMove');
    await page.evaluate(()=>__renderState({...__state,revision:3,control_mode:'normal',available_actions:['EnterMoveControl','StopMove']}));
    await page.waitForFunction(()=>!document.querySelector('.device-remote-controls'));
    await page.tap('.fsm-whiteboard__node[data-state="body:standing"]');
    await page.waitForFunction(()=>!document.querySelector('.device-remote-controls'));
    await closeExplanation();
    await page.evaluate(()=>{
      __renderState({...__state,epoch:'d',revision:1,life_state:'explore',body_state:'moving',control_mode:'remote',available_actions:['Move','StopMove'],available_life_actions:['stop_explore']});
    });
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="body:moving"]');
    await clickAction('停止探索');
    await page.focus('[aria-label="移动摇杆，方向键控制，松开停止"]');
    await page.keyboard.down('ArrowUp');await page.waitForFunction(()=>__velocities.length>0);await page.keyboard.up('ArrowUp');
    assert.equal(await page.evaluate(()=>__velocities.at(-1).params.vx),0);
    await page.tap('[aria-label="放大视频"]');
    await page.waitForSelector('.device-media-viewer');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>!document.querySelector('.device-media-viewer'));
    assert.equal(await page.$('.device-whiteboard__expand'),null);
    assert.equal(await page.$('.device-whiteboard__feedback'),null,'Removed status strip returned');
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
    await page.waitForFunction(()=>Object.values(__velocities.at(-1).params).every(value=>value===0));
    await page.keyboard.up('ArrowUp');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge[data-action]',nodes=>nodes.every(node=>node.dataset.available==='false')),true,'Stale stream allowed an ordinary action');
    assert.equal(await page.$('.device-header__stop'),null,'Go2 header should remain compact when the stream is stale');
    await page.click('.device-header__settings');
    assert.equal(await page.$eval('.device-settings-safety',node=>node.disabled),false,'Stale stream disabled the settings stop action');
    await page.click('[aria-label="关闭管理"]');
    await page.evaluate(()=>__renderState({ism:'ON',ts:1},{unit_id:'lamp',name:'灯',_online:true,transport:{},state_machine:{primary_region:'device',regions:[{id:'device',label:'照明',states:['OFF','ON'],state_field:'ism',views:{OFF:{label:'关'},ON:{label:'开'}},transitions:[{src:'ON',dst:'OFF',trigger:'off',action:'SET_STATE',label:'关灯',params:{state:'OFF'}}]}]},widgets:[],modes:[],skills:[]}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="device:ON"]');
    assert.equal(await page.$$('.fsm-whiteboard__node').then(nodes=>nodes.length),2);
    await clickAction('关灯');
    assert.equal(await page.evaluate(()=>__calls.at(-1).params.state),'OFF');
    await page.evaluate(()=>{
      __renderState({ism:'ON',ts:100,r:255,g:200,b:80,brightness:180,autonomy_mode:'reactive',control_state:'holding',control_hold_until:2000000000,control_ts:101},{...__lampCard,_online:true});
    });
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="device:ON"]');
    assert.deepEqual(await page.$$eval('.fsm-whiteboard__node', nodes => nodes.map(node => node.dataset.state).sort()),
      ['device:BLINKING','device:OFF','device:ON']);
    assert.equal(await page.$('[aria-label="选择灯光颜色"]')!==null,true,'Lamp color controls lost');
    await page.focus('[aria-label="选择灯光颜色"]');
    const beforeColor=await page.evaluate(()=>__calls.length);
    await page.keyboard.press('ArrowRight');await page.waitForFunction(before=>__calls.length>before,{},beforeColor);
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'SET_COLOR');
    await page.click('.device-header__settings');
    assert.equal(await page.$$('.device-settings-axis').then(nodes=>nodes.length),0,'Autonomy settings must stay out of device management');
    await page.click('[aria-label="关闭管理"]');
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
    await page.waitForFunction(()=>document.querySelectorAll('.fsm-whiteboard__node.is-current[data-state^="body:"]').length===0);
    await clickNode('body:sitting');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Sit','Missing posture blocked manual dispatch');
    await clickNode('body:greeting');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'Hello','Next manual action waited for posture feedback');
    await closeExplanation();
    await page.evaluate(()=>__renderState({...__state,revision:2,body_state:'standing',body_confirmation:'observed',body_telemetry_fresh:true,pending_action:null,
      last_action:{command:'Hello',phase:'completed',result:'ok'}}));
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="body:standing"]');
    assert.equal(await page.$$('.fsm-whiteboard__edge.is-running').then(nodes=>nodes.length),0,
      'A completed receipt must not animate as a running command');
    await page.evaluate(()=>{
      const machine={connection_field:'attached',primary_region:'mechanism',regions:[{id:'mechanism',label:'位置',state_field:'position',states:['open','closed'],views:{open:{label:'打开'},closed:{label:'关闭'}},available_actions_field:'allowed',blocked_actions_field:'reasons',transitions:[{src:'open',dst:'closed',trigger:'close',action:'SET_POSITION',availability_key:'close',label:'关闭'}]}]};
      __renderState({position:'open',attached:true,allowed:['close'],ts:2},{unit_id:'generic',name:'设备',_online:true,state_machine:machine,transport:{},widgets:[],modes:[],skills:[]});
    });
    await page.waitForSelector('.fsm-whiteboard__node.is-current[data-state="mechanism:open"]');
    await clickAction('关闭');
    assert.equal(await page.evaluate(()=>__calls.at(-1).action),'SET_POSITION');
    await page.evaluate(()=>__renderState({...__state,ts:3,attached:false},{...__card,_online:false,unit_id:'generic',transport:{}}));
    await page.waitForFunction(()=>document.querySelector('.device-header__status').textContent==='未连接');
    assert.equal(await page.$$eval('.fsm-whiteboard__edge[data-action]',nodes=>nodes.every(node=>node.dataset.available==='false')),true);
    // Verify the real shared chat as well; a stub cannot catch flex overflow.
    await page.addScriptTag({content:compile('app/src/utils/activityView.js')});
      await page.addScriptTag({content:compile('app/src/utils/runProcess.js')});
      await page.addScriptTag({content:compile('app/src/components/RunProcess.jsx')});
    await page.addScriptTag({content:compile('app/src/components/ChatMessages.jsx')});
    await page.addScriptTag({content:compile('app/src/components/ChatPanel.jsx')});
    await page.evaluate(()=>{
      __renderState({epoch:'real-chat',revision:1,connected:true,connection_state:'online',activity_state:'active',life_state:'idle',body_state:'standing',available_actions:['StandDown','Sit','Hello','Stretch','Content','Dance1','Dance2','FingerHeart','FrontPounce','FrontJump','EnterMoveControl'],available_life_actions:['request_rest','start_explore']});
    });
    await page.waitForSelector('.device-page__chat [aria-label="聊天消息"]');
    for(const [width,height] of [[390,844],[360,740],[390,520]]) {
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:true,hasTouch:true});
      assert.equal(await page.$eval('.device-page__chat [aria-label="聊天消息"]',node=>{const r=node.getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0;}),true,`Real chat clipped at ${width}×${height}`);
      await assertAllStatesVisible();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if(height===844) await page.screenshot({path:'/tmp/go2-whiteboard-mobile.png',fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: shared centered media; translucent anchored popovers without layout shifts; endpoint avoidance and mobile/desktop containment; Escape/background dismissal and focus restoration; state colors, selection, pan/pinch, joystick, command guards, lamp controls and SSE ordering.');
    console.log('Screenshots: /tmp/go2-whiteboard-{mobile,rest-mobile,desktop}.png, /tmp/ssm-whiteboard-lamp-mobile.png');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
