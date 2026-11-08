/* Real-component browser checks for closed drawer paint/focus and responsive surfaces.
 * No live API or devices. NODE_PATH must expose sucrase and puppeteer.
 * Set SSM_CHECK_REACT / SSM_CHECK_REACT_DOM to local React 18 UMD scripts;
 * optionally PUPPETEER_EXECUTABLE_PATH, SSM_CHECK_FONT and SCREENSHOT_DIR.
 * Run from the repository root.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = file => sucrase.transform(fs.readFileSync(file, 'utf8'), { transforms: ['jsx'], production: true }).code;

(async () => {
  const browser = await puppeteer.launch({ headless: true,
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (request.isNavigationRequest() && request.url() === 'http://ssm.test/') {
        request.respond({ status: 200, contentType: 'text/html', body: '<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>' });
      } else if (request.url() === 'http://ssm.test/favicon.ico') {
        request.respond({ status: 204, body: '' });
      } else if (request.url().startsWith('data:')) request.continue();
      else { errors.push(`Unexpected network request: ${request.url()}`); request.abort(); }
    });
    await page.goto('http://ssm.test/');
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
    await page.addStyleTag({ path: 'app/styles/app.css' });
    for (const file of [process.env.SSM_CHECK_REACT, process.env.SSM_CHECK_REACT_DOM]) {
      assert(file, 'Set SSM_CHECK_REACT and SSM_CHECK_REACT_DOM to local React scripts');
      await page.addScriptTag({ path: file });
    }
    for (const file of ['ApiClient.js', 'components/Icon.jsx', 'components/SessionGate.jsx',
      'components/SpaceMenu.jsx', 'components/TrainingDrawer.jsx', 'components/AdminSpaceDrawer.jsx',
      'components/OrchestratorSettingsDrawer.jsx', 'components/SpacePeopleDrawer.jsx']) {
      await page.addScriptTag({ content: compile(`app/src/${file}`) });
    }
    if (process.env.SSM_CHECK_FONT) {
      const data = fs.readFileSync(process.env.SSM_CHECK_FONT).toString('base64');
      await page.evaluate(async data => {
        const font = new FontFace('SurfaceCheckCJK', `url(data:font/otf;base64,${data})`);
        await font.load(); document.fonts.add(font);
      }, data);
      await page.addStyleTag({ content: ':root{--font-sans:SurfaceCheckCJK,sans-serif;--font-mono:SurfaceCheckCJK,monospace}button,input,select{font-family:var(--font-sans)}' });
    }
    await page.addScriptTag({ content: `
      window.getAgentDisplayName = () => '空间管家';
      window.AgentPersonalitySettings = () => React.createElement('p', null, '人格设置预览');
      const fixtures = {
        '/api/admin/space': {name:'测试空间',device_ids:[]},
        '/api/admin/device-candidates': [], '/api/people': [],
        '/api/admin/invite': {enabled:false,version:1,share_id:''},
      };
      apiClient.request = async endpoint => {
        if (!(endpoint in fixtures)) throw new Error('Unexpected API: ' + endpoint);
        return fixtures[endpoint];
      };
      function SurfaceHarness() {
        const [active, setActive] = React.useState('');
        const close = () => setActive('');
        return React.createElement(React.Fragment, null,
          React.createElement(SpaceMenu, {
            onTraining:()=>setActive('training'),onShareSpace:()=>setActive('invite'),
            onSpaceSettings:()=>setActive('space'),onOrchestratorSettings:()=>setActive('agent'),
            onPeople:()=>setActive('people'),onExit:close,
          }),
          active === 'training' && React.createElement(TrainingDrawer,{unitId:'go2',training:{records:[]},onClose:close}),
          ...[[AdminInviteDrawer,'invite'],[AdminSpaceDrawer,'space'],
            [OrchestratorSettingsDrawer,'agent'],[SpacePeopleDrawer,'people']].map(([Component,key]) =>
              React.createElement(Component,{key,open:active===key,onClose:close})),
        );
      }
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(SurfaceHarness));
    ` });
    await page.waitForSelector('.space-menu__trigger');
    const assertClosed = async () => {
      const result = await page.evaluate(() => {
        const drawers = [...document.querySelectorAll('.space-drawer,.invite-drawer')];
        const hidden = drawers.filter(element => !element.classList.contains('is-open'));
        return hidden.map(element => {
          const style = getComputedStyle(element);
          const button = element.querySelector('button');
          button?.focus();
          return { visibility: style.visibility, inert: element.inert,
            receivesFocus: element.contains(document.activeElement) };
        });
      });
      assert(result.length >= 3, 'Expected real closed drawers to remain mounted');
      for (const item of result) assert.deepEqual(item, { visibility: 'hidden', inert: true, receivesFocus: false });
    };
    const widths = new Map();
    const assertLayout = async selector => {
      const layout = await page.$eval(selector, element => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right, width: innerWidth,
          scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
          documentWidth: document.documentElement.scrollWidth,
          shadow: getComputedStyle(element).boxShadow, panelWidth: rect.width,
          height: rect.height, top: rect.top, bottom: rect.bottom, viewportHeight: innerHeight };
      });
      assert(layout.left >= -1 && layout.right <= layout.width + 1, `${selector} leaves viewport: ${JSON.stringify(layout)}`);
      assert(layout.documentWidth <= layout.width + 1, 'Document has horizontal overflow');
      assert(layout.scrollWidth <= layout.clientWidth + 1, `${selector} has horizontal overflow`);
      if (!widths.has(layout.width)) widths.set(layout.width, Math.round(layout.panelWidth));
      assert.equal(Math.round(layout.panelWidth), widths.get(layout.width), `${selector} differs from the management drawer width`);
      assert.equal(Math.round(layout.bottom), layout.viewportHeight, `${selector} is not anchored to the bottom`);
      assert(layout.top > 0 && layout.height > 0, `${selector} should fit within the viewport`);
      assert.equal(layout.shadow, 'none', `${selector} has a decorative shadow`);
    };
    const screenshot = async name => {
      if (!process.env.SCREENSHOT_DIR) return;
      fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, `${name}.png`) });
    };
    const drawers = [
      ['分享空间', '.invite-drawer', '分享空间'],
      ['伙伴训练', '.training-drawer', '伙伴训练'],
      ['空间管理', '[aria-labelledby="space-drawer-title"]', '空间管理'],
      ['空间管家', '.orchestrator-settings-drawer', '空间管家管理'],
      ['人员库', '[aria-labelledby="space-people-title"]', '空间人员库'],
    ];
    for (const width of [320, 390, 1280]) {
      await page.setViewport({ width, height: 844 });
      await assertClosed();
      for (const [menuText, selector, title] of drawers) {
        await page.click('.space-menu__trigger');
        await page.waitForSelector('.space-menu__panel', { visible: true });
        await assertClosed();
        await assertLayout('.space-menu__panel');
        const bottomGap = await page.$eval('.space-menu__panel', panel =>
          panel.getBoundingClientRect().bottom - panel.querySelector('.space-menu__item--exit').getBoundingClientRect().bottom);
        assert(bottomGap <= 24, `Empty space below exit: ${bottomGap}px`);
        if (menuText === '分享空间') await screenshot(`management-${width}`);
        await page.evaluate(text => [...document.querySelectorAll('.space-menu__item')]
          .find(button => button.querySelector('strong').textContent === text).click(), menuText);
        await page.waitForSelector(selector, { visible: true });
        assert.equal(await page.$eval(`${selector} h2`, element => element.textContent), title);
        assert.equal(await page.$eval(selector, element => element.inert), false);
        const close = `${selector} header button[aria-label]`;
        await page.waitForSelector(close, { visible: true });
        await assertClosed();
        await assertLayout(selector);
        await screenshot(`${menuText === '伙伴训练' ? 'training' : selector.includes('people') ? 'people' : menuText === '分享空间' ? 'invite' : menuText === '空间管理' ? 'space' : 'agent'}-${width}`);
        await page.click(close);
        await page.waitForFunction(selector => !document.querySelector(selector) || getComputedStyle(document.querySelector(selector)).visibility === 'hidden', {}, selector);
      }
      // Keyboard navigation must never land in any closed drawer.
      await page.focus('.space-menu__trigger');
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.space-drawer,.invite-drawer'))), false);
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: real closed drawers do not paint or receive focus; management menu and five drawers open/close; 320/390/1280px layout; no network or browser errors.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
