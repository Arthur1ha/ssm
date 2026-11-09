/* Simulated input and delayed HTTP only. NODE_PATH must expose sucrase. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const sucrase = require('sucrase');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const compile = name => sucrase.transform(read(name), {transforms:['jsx'], production:true}).code;
const element = (type, props, ...children) => ({type, props:{...props, children}});

function checkInput() {
  const samples = [];
  const ui = vm.createContext({window:{VirtualJoystick:'stick'},
    React:{createElement:element, useRef:current=>({current}), useCallback:fn=>fn},
    stateWidgetEnabled:()=>true, commandBus:{velocity:(_unit,_action,params)=>samples.push(params)}});
  vm.runInContext(compile('app/src/pages/fsmWidgets.jsx') +
    '\nthis.axis=joystickAxis; this.translation=joystickTranslation; this.controls=JoystickControls;', ui);
  for (const value of [-.12, -.01, 0, .01, .12]) assert.equal(Math.abs(ui.axis(value)), 0);
  assert.equal(ui.axis(1), 1);
  assert.equal(ui.axis(-1), -1);
  assert.equal(ui.axis(-.5), -ui.axis(.5));
  assert.ok(ui.axis(.5) > 0 && ui.axis(.5) < .5, 'Low-speed control needs finer resolution');
  assert.equal(ui.translation(.15, -1).x, 0, 'Forward finger drift became lateral movement');
  assert.ok(ui.translation(.7, -.7).x > 0 && ui.translation(.7, -.7).y < 0,
    'A deliberate diagonal must remain available');
  const controls = ui.controls({unitId:'go2',widget:{action:'velocity'}, connected:true});
  const [move, turn] = controls.props.children;
  assert.equal(move.props.radius,36);
  assert.equal(turn.props.radius,36);
  assert.equal(move.props.label,'移动');
  assert.equal(turn.props.label,'转向');
  move.props.onMove(.15,-1);
  assert.equal(samples.at(-1).vx,.5);
  assert.equal(Math.abs(samples.at(-1).vy),0);
  turn.props.onMove(-1);
  assert.equal(samples.at(-1).vyaw,.8);
  move.props.onStop();
  assert.equal(samples.at(-1).vx,0);
  assert.equal(samples.at(-1).vyaw,.8,'Releasing translation must not release the turn stick');
  turn.props.onStop();
  assert.ok(Object.values(samples.at(-1)).every(value=>value === 0));
}

function checkPointers() {
  const intervals = new Map();
  const documentEvents = new Map();
  const effects = [];
  const refs = [];
  let sent = 0, stopped = 0;
  const ui = vm.createContext({window:{addEventListener(){},removeEventListener(){}},
    document:{hidden:false,addEventListener:(name,fn)=>documentEvents.set(name,fn),removeEventListener(){}},
    setInterval:fn=>{const id=intervals.size+1; intervals.set(id,fn); return id;},
    clearInterval:id=>intervals.delete(id),
    React:{createElement:element, useRef:current=>{const ref={current}; refs.push(ref); return ref;},
      useState:value=>[value,()=>{}], useCallback:fn=>fn, useEffect:fn=>effects.push(fn)}});
  vm.runInContext(compile('app/src/components/DeviceWidgets.jsx'),ui);
  const tree = ui.window.VirtualJoystick({onMove:()=>sent++,onStop:()=>stopped++,radius:36});
  effects.forEach(fn=>fn());
  const pad = tree.props.children[0];
  pad.props.ref.current = {getBoundingClientRect:()=>({left:0,top:0,width:72,height:72})};
  const pointer = id => ({pointerId:id,clientX:36,clientY:10,currentTarget:{setPointerCapture(){}}});
  pad.props.onPointerDown(pointer(10));
  pad.props.onPointerMove(pointer(10));
  assert.equal(sent,2,'Pointer movement should update input without waiting for a heartbeat');
  pad.props.onPointerUp(pointer(20));
  assert.equal(stopped,0,'An unrelated second touch stopped the primary pointer');
  pad.props.onPointerMove(pointer(20));
  assert.equal(sent,2,'An unrelated second touch changed movement');
  pad.props.onPointerCancel(pointer(10));
  assert.equal(stopped,1);
  assert.equal(intervals.size,0);
  pad.props.onPointerDown(pointer(11));
  ui.document.hidden=true;
  documentEvents.get('visibilitychange')();
  assert.equal(stopped,2);
  assert.equal(intervals.size,0,'A background page must not retain its heartbeat');
}

async function checkTransport() {
  const pending = [];
  const ui = vm.createContext({crypto:{randomUUID}});
  vm.runInContext(read('app/src/CommandBus.js')+'\nthis.bus=commandBus;',ui);
  ui.bus.command = (_unit,_action,params,options) => new Promise(resolve=>pending.push({params,options,resolve}));
  const move = vx=>ui.bus.velocity('go2','velocity',{vx,vy:0,vyaw:0},()=>{});
  move(.1); move(.2); move(.3); move(.4);
  assert.equal(pending.length,2,'Slow HTTP caused an unbounded queue of movement writes');
  move(0);
  assert.equal(pending.length,3,'Release was blocked behind in-flight movement');
  assert.equal(pending[2].params.sequence,5);
  assert.equal(pending[2].options.keepalive,true,'Release should survive a page navigation');
  assert.equal(pending[0].options.timeoutMs,1500);
  assert.equal(pending[0].params.controller_id,pending[2].params.controller_id);
  pending[0].resolve({result:'accepted'});
  pending[1].resolve({result:'accepted'});
  pending[2].resolve({result:'accepted'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(pending.length,3,'Queued movement was replayed after release');
  move(.2);
  assert.equal(pending[3].params.sequence,6,'A new gesture must continue the ordering sequence');
  pending[3].resolve({result:'accepted'});
  await new Promise(resolve=>setImmediate(resolve));
  // Pending samples are coalesced to the newest complete pair of stick values.
  move(.1); move(.2); move(.3); move(.4);
  const start=pending.length;
  pending[start-2].resolve({result:'accepted'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(pending.length,start+1);
  assert.equal(pending.at(-1).params.vx,.4);
  pending[start-1].resolve({result:'accepted'});
  pending.at(-1).resolve({result:'accepted'});
  await new Promise(resolve=>setImmediate(resolve));
}

async function checkBrowser() {
  const puppeteer = require('puppeteer');
  const browser = await puppeteer.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror',error=>errors.push(error.message));
    await page.setViewport({width:390,height:500,isMobile:true,hasTouch:true});
    await page.setContent('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">' +
      '<style>'+read('app/styles/app.css')+'</style><div id="root"></div>');
    await page.addScriptTag({path:process.env.GO2_PREVIEW_REACT});
    await page.addScriptTag({path:process.env.GO2_PREVIEW_REACT_DOM});
    await page.addScriptTag({content:`
      window.__samples=[];
      window.stateWidgetEnabled=()=>true;
      window.commandBus={velocity:(_unit,_action,params)=>__samples.push(params)};
    `});
    await page.addScriptTag({content:compile('app/src/components/DeviceWidgets.jsx')});
    await page.addScriptTag({content:compile('app/src/pages/fsmWidgets.jsx')});
    await page.evaluate(()=>ReactDOM.createRoot(document.querySelector('#root')).render(
      React.createElement(JoystickControls,{unitId:'go2',widget:{action:'velocity'},connected:true})));
    await page.waitForSelector('.device-joysticks [role="group"]');
    const geometry = ()=>page.$$eval('.device-joysticks [role="group"]',nodes=>nodes.map(node=>{
      const {x,y,width,height}=node.getBoundingClientRect(); return {x,y,width,height};
    }));
    const before = await geometry();
    assert.equal(before.length,2);
    assert.ok(before.every(rect=>rect.width===72 && rect.height===72),'Joystick size changed');
    const [left,right]=before.map(rect=>({x:rect.x+rect.width/2,y:rect.y+rect.height/2}));
    const move = await page.touchscreen.touchStart(left.x+2,left.y-24);
    await page.waitForFunction(()=>__samples.at(-1)?.vx>0 && __samples.at(-1)?.vy===0);
    const turn = await page.touchscreen.touchStart(right.x+24,right.y);
    await page.waitForFunction(()=>__samples.at(-1)?.vx>0 && __samples.at(-1)?.vyaw<0);
    await move.end();
    await page.waitForFunction(()=>__samples.at(-1)?.vx===0 && __samples.at(-1)?.vyaw<0);
    await turn.end();
    await page.waitForFunction(()=>Object.values(__samples.at(-1)).every(value=>value===0));
    await page.focus('[aria-label="移动摇杆，方向键控制，松开停止"]');
    await page.keyboard.down('ArrowUp');
    await page.waitForFunction(()=>__samples.at(-1)?.vx===.5);
    await page.keyboard.up('ArrowUp');
    await page.waitForFunction(()=>__samples.at(-1)?.vx===0);
    assert.deepEqual(await geometry(),before,'Input changed the joystick layout');
    assert.deepEqual(errors,[]);
    await page.screenshot({path:'/tmp/ssm-manual-controls-mobile.png'});
    console.log('PASS: React 18 browser touch/keyboard input, independent release, original 72px controls and stable layout.');
  } finally { await browser.close(); }
}

(async()=>{
  checkInput();
  checkPointers();
  await checkTransport();
  console.log('PASS: input shaping, dual-stick release, pointer ownership, background stop, bounded HTTP, immediate release and sequence ordering.');
  if (process.argv.includes('--browser')) await checkBrowser();
})().catch(error=>{console.error(error); process.exitCode=1;});
