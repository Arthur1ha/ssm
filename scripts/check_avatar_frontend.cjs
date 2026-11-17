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
    for (const file of ['utils/stateSnapshot.js', 'utils/agentMeta.js', 'avatars/registry.js', 'avatars/presentation.js', 'avatars/Avatar.jsx', 'avatars/CompanionAvatar.jsx']) {
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
    // Shared state/event and cross-surface lifecycle coverage lives in
    // check_companion_frontend.cjs; this script covers the local preset gallery.
    assert.deepEqual(errors, []);
    console.log('Eight emotions, ambient motion, preset game restoration, rest, mobile preview and reduced motion passed; shared lifecycle checks: check_companion_frontend.cjs.');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
