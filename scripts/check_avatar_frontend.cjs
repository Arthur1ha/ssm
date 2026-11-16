/* Local UI only; no device API or external scripts are contacted.
 * NODE_PATH exposes sucrase and puppeteer. Set PUPPETEER_EXECUTABLE_PATH,
 * SSM_CHECK_REACT and SSM_CHECK_REACT_DOM. Optional: SSM_CHECK_FONT, SCREENSHOT_DIR.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const sucrase = require('sucrase');
const puppeteer = require('puppeteer');
const compile = source => sucrase.transform(source, {transforms:['jsx'], production:true}).code;
async function main() {
  const browser = await puppeteer.launch({executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,
    headless:true, args:['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', request => request.url().startsWith('data:') || request.url() === 'about:blank'
      ? request.continue() : request.abort());
    await page.setViewport({width:390, height:1000, deviceScaleFactor:1});
    await page.setContent('<div id="root"></div>');
    for (const file of ['app/styles/app.css', 'app/styles/avatars.css']) await page.addStyleTag({path:file});
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT});
    await page.addScriptTag({path:process.env.SSM_CHECK_REACT_DOM});
    for (const file of ['utils/agentMeta.js', 'avatars/registry.js', 'avatars/Avatar.jsx']) {
      await page.addScriptTag({content:compile(fs.readFileSync('app/src/' + file, 'utf8'))});
    }
    const preview = fs.readFileSync('app/avatar-preview.html', 'utf8');
    await page.addStyleTag({content:preview.match(/<style>([\s\S]*?)<\/style>/)[1]});
    await page.addScriptTag({content:compile(preview.match(/<script type="text\/babel">([\s\S]*?)<\/script>/)[1])});
    if (process.env.SSM_CHECK_FONT) {
      await page.evaluate(async data => {
        const font = new FontFace('AvatarCJK', `url(data:font/otf;base64,${data})`);
        await font.load(); document.fonts.add(font);
      }, fs.readFileSync(process.env.SSM_CHECK_FONT).toString('base64'));
      await page.addStyleTag({content:'body { font-family: AvatarCJK, sans-serif; }'});
    }
    await page.waitForSelector('svg');
    assert.equal(await page.$$eval('svg', elements => elements.length), 12);
    assert.equal(await page.$eval('body', element => element.scrollWidth), 390);
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive:true});
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR, 'avatar-preview-390.png'), fullPage:true});
    }
    await page.click('button');
    assert.equal(await page.$$eval('[data-motion="pop"]', elements => elements.length), 12);
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion', value:'reduce'}]);
    assert.equal(await page.$eval('[data-motion="pop"]', element => getComputedStyle(element).animationName), 'none');
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion', value:'no-preference'}]);
    for (const file of ['components/Icon.jsx', 'components/ActivityMembers.jsx']) {
      await page.addScriptTag({content:compile(fs.readFileSync('app/src/' + file, 'utf8'))});
    }
    await page.evaluate(() => {
      window.apiClient = new EventTarget(); apiClient.devicesSynced = true;
      window.navigate = () => {};
      // Physical state projection is separately covered by check_activity_frontend.
      window.activityMemberView = (card, state, membership, connected) => ({label:'站立', detail:'自主活动中',
        stale:!connected, disconnected:state.connected === false});
      window.testAvatarEvent = id => ({event_id:id, activity_id:'a', event_type:'message', stage:'rps_result',
        ts:Date.now()/1000, game_round:{round:1, choices:{go2:'rock'}}});
      window.avatarProps = {activity:{activity_id:'a', status:'active', devices:{go2:{status:'joined'}}},
        events:[], agents:[{unit_id:'go2', agent_type:'robot'}],
        unitData:{go2:{state:{connected:true, life_state:'play'}}}, connected:true};
      document.getElementById('root').remove();
      const container = document.createElement('div'); document.body.appendChild(container);
      const root = ReactDOM.createRoot(container);
      window.renderAvatars = () => ReactDOM.flushSync(() => root.render(React.createElement(ActivityMembers, avatarProps)));
      renderAvatars();
    });
    const shape = () => page.$eval('.shape-face', element => element.dataset.shape);
    assert.equal(await shape(), 'idle', 'An initial card without game data must render');
    await page.evaluate(() => {
      avatarProps = {...avatarProps, events:[testAvatarEvent('history')]}; renderAvatars();
    });
    assert.equal(await shape(), 'idle', 'REST history must not replay');
    await page.evaluate(() => {
      const event = testAvatarEvent('new');
      apiClient.dispatchEvent(new CustomEvent('server-event', {detail:event}));
      avatarProps = {...avatarProps, events:[...avatarProps.events, event]}; renderAvatars();
    });
    await page.waitForSelector('.shape-face[data-shape="rock"]');
    assert.equal(await page.$eval('.shape-face__form', element => element.dataset.motion), 'pop');
    await page.waitForFunction(() => document.querySelector('.shape-face').dataset.shape === 'idle');
    await page.evaluate(() => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:avatarProps.events[1]})));
    assert.equal(await shape(), 'idle', 'Duplicate SSE must not replay');
    await page.evaluate(() => {
      avatarProps = {...avatarProps, connected:false}; renderAvatars();
      avatarProps = {...avatarProps, connected:true}; renderAvatars();
      apiClient.dispatchEvent(new Event('events-open'));
      apiClient.dispatchEvent(new CustomEvent('server-event', {detail:avatarProps.events[1]}));
    });
    assert.equal(await shape(), 'idle', 'Reconnect must not replay');
    await page.evaluate(() => {
      const event = {...testAvatarEvent('unseen-old'), ts:1};
      apiClient.dispatchEvent(new CustomEvent('server-event', {detail:event}));
      avatarProps = {...avatarProps, events:[...avatarProps.events, event]}; renderAvatars();
    });
    assert.equal(await shape(), 'idle', 'Previously unseen stale SSE must not replay');
    const faces = await page.evaluate(() => [
      avatarStatePresentation({activity_state:'rest'}, {}).expression,
      avatarStatePresentation({life_state:'explore'}, {}).expression,
      avatarStatePresentation({activity_state:'rest'}, {disconnected:true}).expression,
    ]);
    assert.deepEqual(faces, ['blink', 'curious', 'neutral']);
    assert.deepEqual(errors, []);
    console.log('Avatar preview, mobile layout, reduced motion, history, new SSE, duplicates, reconnect and state expressions passed');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
