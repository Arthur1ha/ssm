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
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
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
    const stage = '.preview-stage .shape-face';
    await page.waitForSelector(stage);
    assert.equal(await page.$$eval(stage, elements => elements.length), 3, 'Every character starts in its daily form');
    assert.equal(await page.$$eval('[data-emotion-choice]', elements => elements.length), 8);
    assert.equal(await page.$eval('body', element => element.scrollWidth), 390);
    assert.equal(await page.$eval(`${stage} .shape-face__ambient`, el => getComputedStyle(el).animationName), 'avatar-breathe');
    assert.equal(await page.$eval('.preview-emotion .shape-face', el => el.dataset.animated), 'false', 'Palette is static');
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive:true});
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR, 'avatar-preview-390.png'), fullPage:true});
    }
    await page.click('[data-emotion-choice="happy"]');
    await page.waitForSelector(`${stage}[data-emotion="happy"] [data-motion="pop"]`);
    await delay(250);
    assert.equal(await page.$eval(`${stage} .shape-face__expression[data-current="true"]`, el => getComputedStyle(el).opacity), '1');
    await page.click('[data-gesture-choice="scissors"]');
    await page.waitForSelector(`${stage}[data-shape="scissors"]`);
    await page.waitForSelector(`${stage}[data-shape="idle"][data-emotion="happy"] [data-motion="none"]`);
    await page.click('[data-behavior-choice="rest"]');
    assert.equal(await page.$eval(`${stage} .shape-face__expression[data-current="true"] .shape-face__eyes`, el => el.dataset.closed), 'true');
    await page.click('[data-behavior-choice="idle"]');
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion', value:'reduce'}]);
    assert.equal(await page.$eval(`${stage} .shape-face__ambient`, el => getComputedStyle(el).animationName), 'none');
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion', value:'no-preference'}]);
    if (process.env.SCREENSHOT_DIR) {
      await page.click('[data-emotion-choice="curious"]');
      await delay(300);
      await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR, 'avatar-curious-390.png'), fullPage:true});
      // Actual browser frames; stitch as a GIF externally when motion evidence is needed.
      await page.click('[data-sequence]');
      for (let frame = 0; frame < 24; frame++) {
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR, `motion-${String(frame).padStart(2,'0')}.png`), clip:{x:0,y:0,width:390,height:430}});
        await delay(90);
      }
    }
    for (const file of ['components/Icon.jsx', 'components/ActivityMembers.jsx', 'hooks/useActivityTimeline.js'])
      await page.addScriptTag({content:compile(fs.readFileSync('app/src/' + file, 'utf8'))});
    await page.evaluate(() => {
      window.apiClient = new EventTarget(); apiClient.devicesSynced = true;
      apiClient.request = async path => path === '/api/activity/current'
        ? {activity:{activity_id:'a',status:'active',devices:{go2:{status:'joined'}}}}
        : {events:[{event_id:'history',activity_id:'a',event_type:'message',unit_id:'go2',emotion:'sad',ts:Date.now()/1000}]};
      window.apiErrorText = error => error.message;
      window.navigate = () => {};
      window.activityMemberView = (card, state, membership, connected) => ({label:'站立', detail:'自主活动中',
        stale:!connected, disconnected:state.connected === false});
      window.emitAvatar = (id, extra = {}) => apiClient.dispatchEvent(new CustomEvent('server-event', {detail:{
        type:'activity', event_id:id, activity_id:'a', event_type:'message',unit_id:'go2',ts:Date.now()/1000,...extra}}));
      const container = document.createElement('div'); document.body.appendChild(container);
      document.getElementById('root').style.display='none';
      window.memberRoot = ReactDOM.createRoot(container);
      window.connected = true; window.mountKey = 0; window.ended = false; window.resting = false;
      window.ActivityHarness = function() {
        const timeline = useActivityTimeline();
        return React.createElement(ActivityMembers, {key:mountKey,activity:{...timeline.displayActivity,...(ended ? {status:'ended'} : {})},
          events:timeline.events, agents:[{unit_id:'go2',agent_type:'robot'}],
          unitData:{go2:{state:{connected:true,life_state:'play',activity_state:resting ? 'rest' : 'active'}}},connected});
      };
      window.renderAvatars = () => ReactDOM.flushSync(() => memberRoot.render(React.createElement(ActivityHarness)));
      renderAvatars();
    });
    const member = '.activity-member__portrait .shape-face';
    await page.waitForSelector(member);
    const mood = () => page.$eval(member, el => el.dataset.emotion);
    const shape = () => page.$eval(member, el => el.dataset.shape);
    assert.equal(await mood(), 'neutral', 'REST emotion history must not drive a live avatar');
    await page.evaluate(() => emitAvatar('happy-1', {emotion:'happy'}));
    await page.waitForSelector(`${member}[data-emotion="happy"]`);
    await page.evaluate(() => emitAvatar('rps-1', {stage:'rps_result',game_round:{round:1,choices:{go2:'rock'}}}));
    await page.waitForSelector(`${member}[data-shape="rock"]`);
    await page.waitForSelector(`${member}[data-shape="idle"][data-emotion="happy"] [data-motion="none"]`);
    await page.evaluate(() => emitAvatar('rps-1', {stage:'rps_result',game_round:{round:1,choices:{go2:'rock'}}}));
    assert.equal(await shape(), 'idle', 'Duplicate must not replay');
    await page.evaluate(() => emitAvatar('private', {activity_id:'other',emotion:'angry',scope:'session'}));
    assert.equal(await mood(), 'happy', 'Other contexts cannot change this activity');
    await page.evaluate(() => emitAvatar('old', {ts:1,emotion:'sad'}));
    assert.equal(await mood(), 'happy', 'Old events cannot replace current expression');
    await page.evaluate(() => emitAvatar('rps-new', {stage:'rps_result',game_round:{round:2,choices:{go2:'paper'}}}));
    await page.waitForSelector(`${member}[data-shape="paper"]`);
    await page.evaluate(() => emitAvatar('queued-sad', {emotion:'sad'}));
    await delay(100);
    assert.equal(await mood(), 'happy', 'An incoming emotion waits until the game gesture finishes');
    await page.waitForSelector(`${member}[data-shape="idle"][data-emotion="sad"]`);
    await page.evaluate(() => emitAvatar('bad-emotion', {emotion:{name:'angry'}}));
    await page.waitForSelector(`${member}[data-emotion="neutral"]`);
    await page.evaluate(() => emitAvatar('sad-2', {emotion:'sad'}));
    await page.waitForSelector(`${member}[data-emotion="sad"]`);
    await page.evaluate(() => {mountKey++; renderAvatars();});
    await page.waitForSelector(`${member}[data-emotion="neutral"]`);
    await page.evaluate(() => emitAvatar('happy-3', {emotion:'happy'}));
    await page.waitForSelector(`${member}[data-emotion="happy"]`);
    await page.waitForSelector(`${member}[data-emotion="neutral"]`, {timeout:8000});
    await page.evaluate(() => {connected=false; renderAvatars(); connected=true; apiClient.eventsOpenedAt=Date.now(); renderAvatars(); apiClient.dispatchEvent(new Event('events-open'));});
    assert.equal(await mood(), 'neutral', 'Reconnect does not restore past emotion');
    await page.evaluate(() => emitAvatar('unseen-before-reconnect', {ts:(apiClient.eventsOpenedAt-2000)/1000, emotion:'angry',
      stage:'rps_result',game_round:{round:3,choices:{go2:'paper'}}}));
    await delay(50);
    assert.equal(await mood(), 'neutral', 'An unseen event created just before reconnect cannot animate');
    assert.equal(await shape(), 'idle');
    await page.evaluate(() => {ended=true; renderAvatars(); emitAvatar('late-end', {emotion:'angry',stage:'rps_result',game_round:{round:2,choices:{go2:'paper'}}});});
    await delay(50);
    assert.equal(await shape(), 'idle'); assert.equal(await mood(), 'neutral');
    await page.evaluate(() => {ended=false; resting=true; renderAvatars();});
    assert.equal(await page.$eval(member, el => el.dataset.behavior), 'rest');
    assert.equal(await page.$eval(`${member} .shape-face__expression[data-current="true"] .shape-face__eyes`, el => el.dataset.closed), 'true');
    assert.deepEqual(errors, []);
    console.log('Eight emotions, ambient motion, game restoration, real SSE/history pipeline, expiry, scopes, duplicates, remount, reconnect, rest and reduced motion passed');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
