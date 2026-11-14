/* Offline media regression. Node 18+; no live API or hardware. Optional --browser
 * needs NODE_PATH with puppeteer/react/react-dom/sucrase and local Chrome. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/src/components/DeviceWidgets.jsx'), 'utf8');
const mapStart = source.indexOf('function MapWidget(');
const mapCode = source.slice(mapStart, source.indexOf('  return <MediaWidget', mapStart)) + '}';
const plain = source.slice(source.indexOf('async function readMjpegFrames('), source.indexOf('function MapWidget('));
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const bytes = text => new TextEncoder().encode(text);
const part = payload => Buffer.concat([Buffer.from(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${payload.length}\r\nX-Frame-Id: 1\r\n\r\n`), payload, Buffer.from('\r\n')]);
const jpeg = Buffer.from([255, 216, 1, 2, 3, 255, 217]);
const response = chunks => ({
  headers: { get: () => 'multipart/x-mixed-replace; boundary="frame"' },
  body: new ReadableStream({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close(); } }),
});

function harness() {
  const timers = new Map(), listeners = new Set(), fetches = [], images = [], draws = [], urls = new Set(), states = [];
  let timerId = 0, urlId = 0, refIndex = 0, effect, autoLoad = true;
  const canvas = {width:640,height:360,getContext:()=>({drawImage: image => draws.push(image),clearRect() {}})};
  const refs = [];
  const document = {hidden:false,addEventListener:(_name,callback)=>listeners.add(callback),removeEventListener:(_name,callback)=>listeners.delete(callback)};
  const context = vm.createContext({
    React: {useRef(value) {const i=refIndex++;return refs[i] ||= {current:value};},useEffect(callback) {effect=callback;},useState(initial) {return [initial,value=>states.push(value)];},createElement() {}},
    Uint8Array, Blob, TextDecoder, DOMException, AbortController, Date,
    URL: {createObjectURL() {const url=`blob:${++urlId}`;urls.add(url);return url;},revokeObjectURL:url=>urls.delete(url)},
    Image: class {set src(url) {this.url=url;images.push(this);if(autoLoad)queueMicrotask(()=>this.onload?.());}},
    document,
    setTimeout(callback,delay) {const id=++timerId;timers.set(id,{callback,delay});return id;},
    clearTimeout:id=>timers.delete(id),
    setInterval(callback,delay) {const id=++timerId;timers.set(id,{callback,delay,repeat:true});return id;},
    clearInterval:id=>timers.delete(id),
    fetch(url,options) {return new Promise((resolve,reject)=>fetches.push({url,options,resolve,reject}));},
  });
  vm.runInContext(plain+mapCode,context);
  return {
    context,timers,fetches,images,draws,urls,document,listeners,states,
    mount(endpoint='/api/go2/video') {refIndex=0;context.VideoCanvas({connected:true,endpoint});refs[0].current=canvas;return effect();},
    mountMap() {context.MapWidget({connected:true,endpoint:'/api/go2/map.png'});return effect();},
    visibility(hidden) {document.hidden=hidden;for(const callback of listeners) callback();},
    fire(delay) {for(const [id,timer] of [...timers])if(timer.delay===delay){if(!timer.repeat)timers.delete(id);timer.callback();}},
    setAutoLoad(value) {autoLoad=value;},
    open(request) {
      let stream;
      const body = new ReadableStream({start(controller){stream=controller;}});
      request.options.signal.addEventListener('abort',()=>{try{stream.error(new DOMException('cancelled','AbortError'));}catch(_){}},{once:true});
      request.resolve({ok:true,headers:{get:()=> 'multipart/x-mixed-replace; boundary=frame'},body});
      return stream;
    },
  };
}

async function checkBrowserStream() {
  if (!process.argv.includes('--browser')) return;
  const http = require('node:http');
  const puppeteer = require('puppeteer');
  const sucrase = require('sucrase');
  const moduleFile = (module, file) => fs.readFileSync(path.join(path.dirname(require.resolve(module + '/package.json')), 'cjs', file), 'utf8');
  const modules = {
    react: moduleFile('react', 'react.production.js'),
    'react-dom': moduleFile('react-dom', 'react-dom.production.js'),
    'react-dom/client': moduleFile('react-dom', 'react-dom-client.production.js'),
    scheduler: moduleFile('scheduler', 'scheduler.production.js'),
  };
  const runtime = `const modules=${JSON.stringify(modules)},loaded={};function require(id){if(loaded[id])return loaded[id].exports;const module={exports:{}};loaded[id]=module;new Function('module','exports','require',modules[id])(module,module.exports,require);return module.exports;}window.React=require('react');window.ReactDOM={...require('react-dom'),...require('react-dom/client')};`;
  const compiled = sucrase.transform(source,{transforms:['jsx'],jsxRuntime:'classic',production:true}).code;
  const realJpeg = Buffer.from('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCGiiivNPhT/9k=', 'base64');
  let activeStreams=0, streams=0, snapshots=0;
  const sockets=new Set();
  const server=http.createServer((request,result)=>{
    if(request.url==='/api/go2/video'){
      activeStreams++;streams++;
      result.writeHead(200,{'Content-Type':'multipart/x-mixed-replace; boundary=frame','Cache-Control':'no-store'});
      const send=()=>{const frame=part(realJpeg);result.write(frame.subarray(0,13));result.write(frame.subarray(13,69));result.write(frame.subarray(69));};
      send();
      const timer=setInterval(send,120);
      request.on('close',()=>{activeStreams--;clearInterval(timer);});
      return;
    }
    if(request.url.startsWith('/api/go2/video/snapshot')){snapshots++;result.writeHead(200,{'Content-Type':'image/jpeg'});result.end(realJpeg);return;}
    result.writeHead(200,{'Content-Type':'text/html'});
    result.end(`<html><body><div id="root"></div><script>${runtime}\n${compiled}\nconst root=ReactDOM.createRoot(document.getElementById('root'));window.renderVideo=connected=>root.render(React.createElement(VideoCanvas,{connected}));window.__hidden=false;Object.defineProperty(document,'hidden',{get:()=>window.__hidden});window.setHidden=hidden=>{window.__hidden=hidden;document.dispatchEvent(new Event('visibilitychange'));};renderVideo(true);</script></body></html>`);
  });
  server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  let browser;
  try {
    browser=await puppeteer.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage'],...(process.env.PUPPETEER_EXECUTABLE_PATH?{executablePath:process.env.PUPPETEER_EXECUTABLE_PATH}:{})});
    const page=await browser.newPage();
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const green=()=>{const pixel=document.querySelector('canvas')?.getContext('2d').getImageData(10,10,1,1).data;return pixel&&pixel[0]<60&&pixel[1]>130&&pixel[2]<110;};
    await page.waitForFunction(green);
    assert.equal(activeStreams,1);assert.equal(streams,1);assert.equal(snapshots,0);
    await page.evaluate(()=>setHidden(true));
    await page.waitForFunction(()=>document.querySelector('canvas').getContext('2d').getImageData(10,10,1,1).data[3]===0);
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(activeStreams,0,'Hidden page retained its MJPEG connection');
    await page.evaluate(()=>setHidden(false));
    await page.waitForFunction(green);
    assert.equal(activeStreams,1);assert.equal(streams,2);
    await page.evaluate(()=>renderVideo(false));
    await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(activeStreams,0);
    await page.evaluate(()=>renderVideo(true));
    await page.waitForFunction(green);
    assert.equal(activeStreams,1);assert.equal(streams,3);assert.equal(snapshots,0);
    assert.deepEqual(errors,[]);
    console.log('PASS: real browser decodes chunked JPEG stream with one connection, clears/aborts on hide and reconnects without snapshot polling.');
  } finally {
    await browser?.close();
    for(const socket of sockets)socket.destroy();
    await new Promise(resolve=>server.close(resolve));
  }
}

(async () => {
  const parser = harness().context.readMjpegFrames;
  const wire = part(jpeg);
  const frames = [];
  await parser(response([...wire].map(byte=>new Uint8Array([byte]))), blob=>frames.push(blob));
  assert.equal(frames.length,1,'Byte-split multipart did not produce a frame');
  assert.deepEqual(Buffer.from(await frames[0].arrayBuffer()),jpeg);
  const combined=[];
  await parser(response([Buffer.concat([part(jpeg),part(Buffer.from([255,216,9,255,217]))])]),blob=>combined.push(blob));
  assert.equal(combined.length,1,'Coalesced older frames must be dropped');
  assert.equal(new Uint8Array(await combined[0].arrayBuffer())[2],9);
  for (const invalid of [bytes('--frame\r\nContent-Length: 2097153\r\n\r\n'),bytes('x'.repeat(4096)),new Uint8Array(2*2097152+4097)]) {
    await assert.rejects(()=>parser(response([invalid]),()=>{}),/limit|header/);
  }

  const reconnect=harness();
  const oldCleanup=reconnect.mount();
  const old=reconnect.fetches[0];
  oldCleanup();
  const cleanup=reconnect.mount();
  const current=reconnect.fetches[1];
  assert(old.options.signal.aborted,'Old stream was not aborted');
  // Simulate a response arriving despite cancellation; it cannot restart a loop.
  old.resolve({ok:false});
  const stream=reconnect.open(current);
  await flush();
  stream.enqueue(part(jpeg));
  await flush();
  assert.equal(reconnect.draws.length,1);
  assert.equal(reconnect.fetches.length,2);
  assert.equal([...reconnect.timers.values()].filter(timer=>timer.delay===8000).length,1,'Old finally cleared the new watchdog');
  reconnect.visibility(true);
  await flush();
  assert(current.options.signal.aborted);
  assert.equal(reconnect.timers.size,0,'Hidden stream left a retry or watchdog');
  reconnect.visibility(false);
  assert.equal(reconnect.fetches.length,3);
  cleanup();
  reconnect.fetches[2].reject(new DOMException('cancelled','AbortError'));
  await flush();
  assert.equal(reconnect.listeners.size,0);
  assert.equal(reconnect.timers.size,0);
  assert.equal(reconnect.urls.size,0);

  const pending=harness();
  pending.setAutoLoad(false);
  const end=pending.mount();
  const delayed=pending.open(pending.fetches[0]);
  await flush();
  delayed.enqueue(part(jpeg));
  await flush();
  assert.equal(pending.images.length,1);
  pending.visibility(true);
  pending.images[0].onload?.();
  await flush();
  assert.equal(pending.draws.length,0,'Hidden old decode painted a stale frame');
  assert.equal(pending.urls.size,0);
  end();

  const fallback=harness();
  const finish=fallback.mount('/api/go2/video?quality=preview');
  const initial=fallback.fetches[0];
  initial.reject(new Error('stream unsupported'));
  await flush();
  assert(initial.options.signal.aborted,'Fallback left the original stream alive');
  fallback.fire(200);
  assert.match(fallback.fetches[1].url,/\/video\/snapshot\?quality=preview&preview=true&_/);
  const snap=fallback.fetches[1];
  snap.resolve({ok:true,blob:async()=>new Blob([jpeg])});
  await flush();
  assert.equal(fallback.draws.length,1);
  finish();
  assert.equal(fallback.timers.size,0);
  const eof=harness();
  eof.setAutoLoad(false);
  const disposeEof=eof.mount();
  const eofStream=eof.open(eof.fetches[0]);
  await flush();
  eofStream.enqueue(part(jpeg));
  await flush();
  eofStream.close();
  await flush();
  eof.images[0].onload?.();
  assert.equal(eof.draws.length,0,'EOF accepted a stale pending decode');
  assert.equal(eof.urls.size,0);
  disposeEof();

  const stalled=harness();
  const disposeStall=stalled.mount();
  stalled.fetches[0].options.signal.addEventListener('abort',()=>stalled.fetches[0].reject(new DOMException('cancelled','AbortError')));
  stalled.fire(8000);
  await flush();
  assert(stalled.fetches[0].options.signal.aborted,'Missing first frame did not time out');
  stalled.fire(200);
  assert.equal(stalled.fetches.length,2);
  assert(stalled.fetches[1].url.includes('/snapshot'));
  disposeStall();
  stalled.fetches[1].reject(new Error('cancelled'));
  await flush();

  const map=harness();
  const disposeMap=map.mountMap();
  map.fetches[0].resolve({ok:true,blob:async()=>new Blob([jpeg])});
  await flush();
  const mapUrl=map.states.at(-1);
  assert(mapUrl.startsWith('blob:'));
  map.fire(1000);
  map.fetches[1].resolve({ok:false});
  await flush();
  assert.equal(map.states.at(-1),mapUrl,'Brief map error erased the recent map');
  map.fire(1000);
  const stuck=map.fetches[2];
  stuck.options.signal.addEventListener('abort',()=>stuck.reject(new DOMException('cancelled','AbortError')));
  map.fire(1000);
  assert.equal(map.fetches.length,3,'Map overlapped requests');
  map.fire(8000);
  await flush();
  assert(stuck.options.signal.aborted);
  assert.equal(map.states.at(-1),'','Expired map still appeared current');
  map.fire(1000);
  assert.equal(map.fetches.length,4,'Timed-out map request permanently blocked refresh');
  disposeMap();
  map.fetches[3].reject(new Error('cancelled'));
  await flush();
  assert.equal(map.urls.size,0);
  assert.equal(map.timers.size,0);
  const mapRace=harness();
  mapRace.setAutoLoad(false);
  const disposeRace=mapRace.mountMap();
  mapRace.fetches[0].resolve({ok:true,blob:async()=>new Blob([jpeg])});
  await flush();
  mapRace.images[0].onload();
  mapRace.visibility(true);
  await flush();
  assert.equal(mapRace.states.length,0,'Hidden map accepted an already-resolved cancelled decode');
  assert.equal(mapRace.urls.size,0);
  disposeRace();
  console.log('PASS: byte-split/coalesced multipart, bounded parser, latest-frame selection, reconnect/hidden/EOF/decode cleanup, preview fallback, first-frame timeout and bounded map recovery.');
  await checkBrowserStream();
})().catch(error=>{console.error(error);process.exitCode=1;});
