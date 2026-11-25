/* Camera/QR/browser regression with real QR encoding, qr-scanner module/Worker and React 18.
 * No live APIs or devices. NODE_PATH must expose sucrase and puppeteer.
 * Set SSM_CHECK_REACT / SSM_CHECK_REACT_DOM to local React 18 UMD scripts;
 * optionally PUPPETEER_EXECUTABLE_PATH. Run from the repository root. */
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = file => sucrase.transform(fs.readFileSync(file, 'utf8'), { transforms: ['jsx'], production: true }).code;
const code = 'ssm-device:v1:esp32_desk:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG';

(async () => {
  const requests = [];
  let bindStatus = 200;
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
      res.setHeader('Content-Type', 'text/html');
      res.end('<meta name="viewport" content="width=device-width,initial-scale=1"><div id="root"></div>');
    } else if (pathname.startsWith('/src/')) {
      res.setHeader('Content-Type', 'text/javascript');
      res.end(fs.readFileSync(path.join('app', pathname)));
    } else if (pathname === '/api/admin/devices/bind') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        requests.push({ body: JSON.parse(body), csrf: req.headers['x-csrf-token'] });
        res.writeHead(bindStatus, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(bindStatus === 200 ? { device_id: 'esp32_desk', result: 'added' } : { detail: 'device card has not been published' }));
      });
    } else if (pathname === '/api/admin/device-codes/esp32_desk') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ device_id: 'esp32_desk', version: 1, code }));
    } else { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  let page;
  try {
    browser = await puppeteer.launch({ headless: true, protocolTimeout: 15000, executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
      args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    page = await browser.newPage();
    page.setDefaultTimeout(10000);
    await page.setViewport({ width: 390, height: 844 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('error', error => errors.push(error.message));
    if (process.env.SSM_CHECK_VERBOSE) page.on('console', message => console.log('Browser:', message.text()));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM]) {
      assert(file, 'Set SSM_CHECK_REACT and SSM_CHECK_REACT_DOM');
      await page.addScriptTag({ path: file });
    }
    await page.addStyleTag({ path: 'app/styles/app.css' });
    for (const file of ['ApiClient.js', 'utils/qrLibrary.js', 'utils/qrcode.vendor.js',
      'components/Toast.jsx', 'components/QrScanner.jsx', 'components/AdminSpaceDrawer.jsx', 'components/SessionGate.jsx', 'app.jsx']) {
      await page.addScriptTag({ content: compile(`app/src/${file}`) });
    }
    await page.evaluate(value => {
      window.testCode = value;
      window.Icon = () => null;
      window.root = ReactDOM.createRoot(document.getElementById('root'));
      apiClient.setCsrfToken('test-csrf');
      window.scanResults = []; window.bound = []; window.streams = []; window.tracks = [];
      window.RealWorker = window.Worker;
      window.workerStarts = 0; window.workerStops = 0;
      window.Worker = class extends RealWorker {
        constructor(...args) { super(...args); workerStarts++; }
        postMessage(message, ...args) {
          if (message.type === 'close') workerStops++;
          if (message.type === 'decode' && window.holdFrames) { window.pendingFrame = true; return; }
          super.postMessage(message, ...args);
        }
      };
      delete window.BarcodeDetector;
      window.camera = document.createElement('canvas');
      camera.width = 1920; camera.height = 1080;
      window.paintCamera = text => {
        const label = document.createElement('div');
        renderQrCode(label, QRCode, text);
        const ctx = camera.getContext('2d');
        ctx.fillStyle = '#777'; ctx.fillRect(0, 0, 1920, 1080);
        ctx.drawImage(label.querySelector('canvas'), 810, 390, 300, 300);
      };
      paintCamera(testCode);
      const tick = () => {
        camera.getContext('2d').fillRect(0, 0, 1, 1);
        requestAnimationFrame(tick);
      };
      tick();
      navigator.mediaDevices.getUserMedia = async constraints => {
        window.cameraConstraints = constraints;
        const stream = camera.captureStream(30);
        streams.push(stream); tracks.push(...stream.getTracks());
        return stream;
      };
      window.mountScanner = key => root.render(React.createElement(QrScanner, { key, onScan: value => scanResults.push(value) }));
      function ScanHarness() {
        const [open, setOpen] = React.useState(true);
        const { toast, showToast, dismissToast } = useToast();
        return React.createElement(React.Fragment, null,
          open && React.createElement(DeviceScanDialog, {
            onClose: () => setOpen(false),
            onScan: async code => {
              setOpen(false);
              if (!/^ssm-device:v1:(esp32_desk|go2):[A-Za-z0-9_-]+$/.test(code)) {
                showToast('请使用空间管理中的设备标签'); return;
              }
              try {
                const result = await apiClient.request('/api/admin/devices/bind', { method: 'POST', body: { code } });
                bound.push(result); showToast('添加成功');
              } catch (_) { showToast('未发现设备，请确认已启动'); }
            },
            onError: message => { setOpen(false); showToast(message); },
          }),
          toast && React.createElement(Toast, { key: toast.id, notice: toast, onDismiss: dismissToast }));
      }
      window.mountDialog = key => root.render(React.createElement(ScanHarness, { key }));
    }, code);
    const waitText = text => page.waitForFunction(text => document.body.innerText.includes(text), {}, text);

    // The saved PNG itself must include its white border, not just surrounding CSS.
    if (process.env.SSM_CHECK_VERBOSE) console.log("The saved PNG itself must include its white border, not just surrounding CSS.");
    await page.evaluate(() => root.render(React.createElement(DeviceLabelButton, { device: { device_id: 'esp32_desk', name: '灯' } })));
    await page.click('button');
    await page.waitForSelector('.device-label-qr canvas');
    const saved = await page.evaluate(async () => {
      const canvas = document.querySelector('.device-label-qr canvas');
      const ctx = canvas.getContext('2d');
      const Scanner = await loadQrLibrary('scan');
      const value = (await Scanner.scanImage(canvas, { returnDetailedScanResult: true })).data;
      const oldClick = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () { window.download = this.href; };
      [...document.querySelectorAll('button')].find(button => button.textContent === '保存二维码').click();
      HTMLAnchorElement.prototype.click = oldClick;
      return { value, width: canvas.width, border: Array.from(ctx.getImageData(0, 0, 1, 1).data),
        savedPng: download === canvas.toDataURL('image/png') };
    });
    assert.deepEqual(saved, { value: code, width: 640, border: [255, 255, 255, 255], savedPng: true });

    // Use the real camera component and real library worker all the way to bind.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Use the real camera component and real library worker all the way to bind.");
    await page.evaluate(() => mountDialog('worker'));
    await page.waitForFunction(() => bound.length === 1);
    assert.deepEqual(requests, [{ body: { code }, csrf: 'test-csrf' }]);
    assert.equal(await page.evaluate(() => tracks.every(track => track.readyState === 'ended')), true);
    await page.waitForFunction(() => workerStarts === workerStops);
    assert.equal(await page.evaluate(() => typeof window.jsQR), 'undefined');
    assert.equal(await page.evaluate(async () => (await loadQrLibrary('scan')) !== window.QrScanner), true);

    // Native success uses the library's central crop and does not start a worker.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Native success uses the library's central crop and does not start a worker.");
    const before = await page.evaluate(() => workerStarts);
    await page.evaluate(() => {
      window.BarcodeDetector = class {
        static async getSupportedFormats() { return ['qr_code']; }
        async detect(canvas) {
          window.nativeSize = [canvas.width, canvas.height];
          return [{ rawValue: testCode, cornerPoints: [{ x: 0, y: 0 }, { x: 400, y: 400 }] }];
        }
      };
      mountScanner('native-success');
    });
    await page.waitForFunction(() => scanResults.length === 1);
    assert.equal(await page.evaluate(() => workerStarts), before);
    assert.deepEqual(await page.evaluate(() => nativeSize), [400, 400]);

    // Native API service failure switches to the real library worker.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Native API service failure switches to the real library worker.");
    await page.evaluate(() => {
      window.BarcodeDetector = class {
        static async getSupportedFormats() { return ['qr_code']; }
        async detect() { window.nativeTried = true; throw new Error('service unavailable'); }
      };
      mountScanner('native-fallback');
    });
    await page.waitForFunction(() => scanResults.length === 2);
    assert.equal(await page.evaluate(() => nativeTried && scanResults[1] === testCode), true);

    // Discovery failure closes scanning and reports a passive bottom toast.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Discovery failure closes scanning and reports a passive bottom toast.");
    bindStatus = 404;
    await page.evaluate(() => { delete window.BarcodeDetector; mountDialog('not-discovered'); });
    await waitText('未发现设备，请确认已启动');
    assert.equal(await page.$('.device-scan-dialog'), null);
    assert.equal(await page.$('.toast button'), null);
    bindStatus = 200;
    await page.evaluate(() => mountDialog('retry-discovery'));
    await page.waitForFunction(() => bound.length === 2);
    assert.equal(requests.length, 3);
    assert.deepEqual(requests[2].body, { code });

    // A different QR is decoded but cannot be submitted as a device credential.
    if (process.env.SSM_CHECK_VERBOSE) console.log("A different QR is decoded but cannot be submitted as a device credential.");
    await page.evaluate(() => { paintCamera('https://example.test/#invite=visitor-token'); mountDialog('wrong-qr'); });
    await waitText('请使用空间管理中的设备标签');
    assert.equal(await page.$('.device-scan-dialog'), null);
    assert.equal(requests.length, 3);

    // The library handles denied permissions; retry reopens and scans successfully.
    if (process.env.SSM_CHECK_VERBOSE) console.log("The library handles denied permissions; retry reopens and scans successfully.");
    await page.evaluate(() => {
      window.getCamera = navigator.mediaDevices.getUserMedia;
      navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Denied', 'NotAllowedError'); };
      mountScanner('denied');
    });
    await waitText('请检查相机权限');
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = getCamera;
      paintCamera(testCode);
      document.querySelector('.qr-scanner__retry').click();
    });
    if (process.env.SSM_CHECK_VERBOSE) console.log('Retry state:', await page.evaluate(() => ({
      text: document.body.innerText, scans: scanResults, workers: [workerStarts, workerStops],
    })));
    await page.waitForFunction(() => scanResults.length === 3);

    // The library's overlay must match the crop even on a narrow preview.
    if (process.env.SSM_CHECK_VERBOSE) console.log("The library's overlay must match the crop even on a narrow preview.");
    await page.evaluate(() => {
      camera.getContext('2d').fillRect(0, 0, 1920, 1080);
      mountScanner('overlay');
    });
    await page.waitForFunction(() => {
      const overlay = document.querySelector('.qr-scanner__target');
      return overlay && getComputedStyle(overlay).display !== 'none' && overlay.offsetWidth > 0;
    });
    const overlay = await page.evaluate(() => {
      const guide = document.querySelector('.qr-scanner__target').getBoundingClientRect();
      const video = document.querySelector('video').getBoundingClientRect();
      return { x: guide.x - video.x, y: guide.y - video.y, width: guide.width,
        height: guide.height, previewWidth: video.width, previewHeight: video.height };
    });
    assert(Math.abs(overlay.width - overlay.height) < 1);
    assert(Math.abs(overlay.x + overlay.width / 2 - overlay.previewWidth / 2) < 1);
    assert(Math.abs(overlay.y + overlay.height / 2 - overlay.previewHeight / 2) < 1);

    // Closing during decoding releases the camera and asks the actual worker to close.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Closing during decoding releases the camera and asks the actual worker to close.");
    await page.evaluate(() => { window.holdFrames = true; mountScanner('close-pending'); });
    await page.waitForFunction(() => window.pendingFrame);
    await page.evaluate(() => root.render(null));
    await page.waitForFunction(() => tracks.every(track => track.readyState === 'ended') && workerStarts === workerStops);
    assert.equal(await page.evaluate(() => scanResults.length), 3);

    // A camera permission response arriving after closing must not reopen the view.
    if (process.env.SSM_CHECK_VERBOSE) console.log("A camera permission response arriving after closing must not reopen the view.");
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { window.resolveCamera = resolve; });
      mountScanner('close-opening');
    });
    await page.waitForFunction(() => window.resolveCamera);
    await page.evaluate(() => root.render(null));
    await page.evaluate(async () => resolveCamera(await getCamera({ video: true })));
    await page.waitForFunction(() => tracks.every(track => track.readyState === 'ended') && workerStarts === workerStops);

    // If playback fails after acquiring the camera, cleanup must still stop tracks.
    if (process.env.SSM_CHECK_VERBOSE) console.log("If playback fails after acquiring the camera, cleanup must still stop tracks.");
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = getCamera;
      window.nativePlay = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = async () => { throw new DOMException('Playback denied', 'NotAllowedError'); };
      mountScanner('playback-failed');
    });
    await waitText('请检查相机权限');
    await page.waitForFunction(() => tracks.every(track => track.readyState === 'ended') && workerStarts === workerStops);
    await page.evaluate(() => { HTMLMediaElement.prototype.play = nativePlay; window.holdFrames = false; });

    // Space joining shares the same scanner and still receives the invite URL.
    if (process.env.SSM_CHECK_VERBOSE) console.log("Space joining shares the same scanner and still receives the invite URL.");
    await page.evaluate(() => {
      window.joined = [];
      window.inviteUrl = location.origin + '/#invite=example-visitor-token';
      paintCamera(inviteUrl);
      root.render(React.createElement(SessionEntry, { onAdminLogin() {}, onVisitorJoin: value => joined.push(value) }));
    });
    await waitText('扫码加入');
    await page.evaluate(() => [...document.querySelectorAll('button')].find(button => button.textContent.includes('扫码加入')).click());
    await page.waitForFunction(() => joined.length === 1);
    assert.equal(await page.evaluate(() => joined[0] === inviteUrl), true);
    await page.waitForFunction(() => tracks.every(track => track.readyState === 'ended') && workerStarts === workerStops);
    assert.deepEqual(errors, []);
    console.log('PASS: saved QR PNG round-trip; real qr-scanner module/Worker camera → device bind with CSRF; native success/fallback; discovery failure closes scanner with toast; retry; wrong QR; permission retry; overlay alignment; closing and playback failure release camera/Worker; late camera acquisition; space invite scanning.');
  } catch (error) {
    if (page && !page.isClosed()) console.error('Browser state:', await page.evaluate(() => ({
      text: document.body.innerText, scans: window.scanResults,
      workers: [window.workerStarts, window.workerStops],
      tracks: window.tracks?.map(track => track.readyState),
      video: [...document.querySelectorAll('video')].map(video => ({
        paused: video.paused, ready: video.readyState, time: video.currentTime,
      })),
    })).catch(error => error.message));
    throw error;
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
