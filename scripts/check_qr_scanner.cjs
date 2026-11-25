/* Focused camera regression using the real qr-scanner module and simulated
 * BarcodeDetector/getUserMedia. No live API or hardware. Use the same
 * NODE_PATH, SSM_CHECK_REACT, SSM_CHECK_REACT_DOM and Chrome environment as
 * check_qr_onboarding.cjs. SSM_CHECK_QR_SOURCE optionally selects a fixture. */
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require('puppeteer');
const sucrase = require('sucrase');

(async () => {
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname.startsWith('/src/utils/qr-scanner/')) {
      response.setHeader('Content-Type', 'text/javascript');
      response.end(fs.readFileSync(path.join('app', pathname)));
    } else response.end('<div id="root"></div>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await puppeteer.launch({ headless: true, protocolTimeout: 10000,
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
      args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM])
      await page.addScriptTag({ path: file });
    await page.addScriptTag({ path: 'app/src/utils/qrLibrary.js' });
    const source = fs.readFileSync(process.env.SSM_CHECK_QR_SOURCE || 'app/src/components/QrScanner.jsx', 'utf8');
    await page.addScriptTag({ content: sucrase.transform(source, { transforms: ['jsx'], production: true }).code });
    await page.evaluate(() => {
      window.camera = document.createElement('canvas'); camera.width = 320; camera.height = 240;
      setInterval(() => { camera.getContext('2d').fillRect(0, 0, 320, 240); }, 80);
      window.tracks = []; window.results = []; window.scanErrors = [];
      navigator.mediaDevices.getUserMedia = async () => {
        const stream = camera.captureStream(12); tracks.push(...stream.getTracks()); return stream;
      };
      window.BarcodeDetector = class {
        static async getSupportedFormats() { return ['qr_code']; }
        async detect() {
          window.calls++;
          if (window.fatal) throw new Error('unsupported format');
          if (!window.allowCode) {
            if (window.emptyAsError) throw new Error('No QR code found');
            return [];
          }
          return [{ rawValue: 'ssm-device:v1:go2:test', cornerPoints: [{ x: 0, y: 0 }] }];
        }
      };
      window.root = ReactDOM.createRoot(document.getElementById('root'));
      window.mount = key => {
        window.calls = 0; window.allowCode = false;
        root.render(React.createElement(QrScanner, { key, frameOnly: !!window.frameOnly,
          onScan: result => results.push(result), onError: error => scanErrors.push(error) }));
      };
    });
    for (const emptyAsError of [false, true]) {
      await page.evaluate(flag => { window.emptyAsError = flag; mount(String(flag)); }, emptyAsError);
      await page.waitForFunction(() => calls >= 3 || document.body.innerText.includes('扫码器暂时不可用'));
      assert.equal(await page.evaluate(() => document.body.innerText.includes('扫码器暂时不可用')), false,
        'A normal frame without a QR must not disable the scanner and camera');
      assert.equal(await page.evaluate(() => tracks.at(-1).readyState), 'live');
      await page.evaluate(() => { window.allowCode = true; });
      await page.waitForFunction(count => results.length === count, {}, emptyAsError ? 2 : 1);
      assert.equal(await page.evaluate(() => results.at(-1)), 'ssm-device:v1:go2:test');
      assert.equal(await page.evaluate(() => tracks.at(-1).readyState), 'ended');
    }
    await page.evaluate(() => { window.frameOnly = true; window.fatal = true; mount('fatal'); });
    await page.waitForFunction(() => scanErrors.length === 1);
    assert.equal(await page.$$eval('.qr-scanner button,.qr-scanner > p',elements=>elements.length),0);
    assert.equal(await page.evaluate(() => tracks.every(track => track.readyState === 'ended')), true);
    assert.deepEqual(errors, []);
    console.log('PASS: real qr-scanner native empty frames/normal no-match errors keep camera live; later code is read; genuine failure still releases camera.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
