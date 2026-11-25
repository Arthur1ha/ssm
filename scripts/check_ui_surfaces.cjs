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
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error('Management page error:', error.message); });
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
    for (const file of ['utils/inputAttributes.js', 'utils/agentMeta.js', 'utils/qrLibrary.js', 'utils/qrcode.vendor.js',
      'ApiClient.js', 'hooks/useTraining.js', 'pages/TransitionInspector.jsx', 'components/Icon.jsx', 'components/SessionGate.jsx',
      'components/SpaceMenu.jsx', 'components/TrainingDrawer.jsx', 'components/AdminSpaceDrawer.jsx',
      'components/AgentPersonalitySettings.jsx', 'components/OrchestratorSettingsDrawer.jsx', 'components/Go2AssetsDrawer.jsx',
      'components/SpacePeopleDrawer.jsx', 'pages/FsmDevicePage.jsx']) {
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
      window.getAgentDisplayName = id => ({go2:'呆呆狗',lamp:'皮皮灯'}[typeof id === 'string' ? id : id.unit_id] || '空间管家');
      window.setAgentDisplayNames = () => {};
      const devices = [
        {device_id:'esp32_desk',name:'桌面智能灯',description:'灯光与环境感知',online:true,added:true},
        {device_id:'go2',name:'Go2',description:'移动、观察与互动',online:true,added:true},
        {device_id:'esp32_spare',name:'备用智能灯',description:'灯光与环境感知',online:false,added:false},
      ];
      const fixtures = {
        '/api/admin/space': {name:'测试空间',device_ids:['esp32_desk','go2']},
        '/api/admin/device-candidates': devices,
        '/api/people': [
          {person_id:'person-a',name:'陈小明',role:'owner',face_count:2},
          {person_id:'person-b',name:'一个用于检查窄屏排版的较长人员名字',role:'friend',face_count:0},
        ],
        '/api/admin/invite': {enabled:true,version:1,share_id:'simulated-stable-space-share-code'},
        '/api/go2/navigation/locations': [{name:'窗边',x:1,y:2}],
        '/api/go2/memory': {today:'已完成一次空间观察。',summaries:[]},
        '/api/esp32/esp32_desk_led/memory': {daily_summary:'今天提供了阅读照明。',episodes:[]},
        '/api/agents/esp32_desk_led/personality': {name:'皮皮灯',prompt:'以温和的灯光回应人的需要。',revision:2,
          traits:{initiative:60},trait_specs:{initiative:{label:'社交主动性',description:'主动参与互动的意愿',min:0,max:100}}},
        '/api/agents/cloud_orchestrator/personality': {name:'空间管家',prompt:'理解用户的请求，用简洁自然的语言回应。',revision:3,
          traits:{warmth:65,clarification:45},trait_specs:{
            warmth:{label:'温暖度',description:'影响闲聊、确认与结果回复的语气',effect:'回复表达',min:0,max:100},
            clarification:{label:'澄清倾向',description:'遇到不确定需求时，倾向先询问清楚',effect:'问题澄清',min:0,max:100}}},
        '/api/agents/go2/personality': {name:'Go2',prompt:'好奇地观察周围，友好地与人互动。',revision:2,
          traits:{curiosity:70},trait_specs:{curiosity:{label:'好奇心',description:'对新事物的关注程度',min:0,max:100}}},
      };
      window.__managementRequests = [];
      apiClient.request = async (endpoint, options = {}) => {
        __managementRequests.push({endpoint, ...options});
        if (endpoint.startsWith('/api/admin/device-codes/')) return {code:'ssm-device:v1:fixture:simulated-token'};
        if (!(endpoint in fixtures)) throw new Error('Unexpected API: ' + endpoint);
        if (options.method === 'PUT' && endpoint.includes('/personality')) {
          const old = fixtures[endpoint];
          if (options.body.revision !== old.revision) throw new Error('Missing original revision');
          fixtures[endpoint] = {...old,...options.body,revision:old.revision+1};
        }
        if (options.method === 'PATCH') fixtures[endpoint] = {...fixtures[endpoint],...options.body};
        return fixtures[endpoint];
      };
      function SurfaceHarness() {
        const [active, setActive] = React.useState('');
        const [rules, setRules] = React.useState([
          {unit_id:'go2',teaching_id:'wave',desired_status:'enabled',revision:1,definition:{origins:['user_delegated'],cooldown_s:10,steps:[{transition_ids:['wave'],defaults:{}}]}},
          {unit_id:'lamp',teaching_id:'blink',desired_status:'enabled',revision:1,definition:{origins:['agent_autonomous'],trigger:{event_id:'sound',where:{detected:true}},steps:[{transition_ids:['blink'],defaults:{count:2}}]}},
          {unit_id:'go2',teaching_id:'old',desired_status:'removed',summaryText:'已经删除的规则'},
        ]);
        const loadAll = React.useCallback(async () => {}, []);
        const close = () => setActive('');
        return React.createElement(React.Fragment, null,
          React.createElement(SpaceMenu, {
            onTraining:()=>setActive('training'),onShareSpace:()=>setActive('invite'),
            onSpaceSettings:()=>setActive('space'),onOrchestratorSettings:()=>setActive('agent'),
            onPeople:()=>setActive('people'),onExit:close,
          }),
          React.createElement('button', {onClick:()=>setActive('go2'),id:'open-go2-assets'}, 'Go2 管理预览'),
          React.createElement('button', {onClick:()=>setActive('lamp'),id:'open-lamp-assets'}, '灯管理预览'),
          active === 'training' && React.createElement(TrainingDrawer,{
            unitId:'',agents:[{unit_id:'go2'},{unit_id:'lamp'}],
            training:{records:[],allRecords:rules,loadAll,
              catalogs:{go2:{limits:{cooldown_s:{minimum:1,maximum:3600}},transitions:[{id:'wave',action:'挥手'}]},
                lamp:{events:[{id:'sound',name:'检测到声音',fields:{detected:'boolean'}}],transitions:[{id:'blink',action:'闪烁',params_schema:{properties:{count:{type:'integer',minimum:0,maximum:20}}}}]}},
              remove:async record=>setRules(previous=>previous.filter(item=>item.teaching_id!==record.teaching_id)),
              saveParameters:async(record,patch)=>{
                const saved=structuredClone(record);saved.revision++;
                for(const step of patch.steps)Object.assign(saved.definition.steps[step.step_index].defaults,step.params);
                window.__saved=saved;setRules(previous=>previous.map(item=>item.teaching_id===saved.teaching_id ? saved : item));return saved;
              }},onClose:close,
          }),
          ...[[AdminInviteDrawer,'invite'],[AdminSpaceDrawer,'space'],
            [OrchestratorSettingsDrawer,'agent'],[SpacePeopleDrawer,'people'],[Go2AssetsDrawer,'go2']].map(([Component,key]) =>
              React.createElement(Component,{key,open:active===key,onClose:close})),
          React.createElement(LampAssetsDrawer,{unitId:'esp32_desk_led',open:active==='lamp',onClose:close}),
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
      if (process.env.SSM_SCREENSHOT_WIDTH && page.viewport().width !== Number(process.env.SSM_SCREENSHOT_WIDTH)) return;
      fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, `${name}.png`) });
    };
    const drawers = [
      ['分享空间', '.invite-drawer', '分享空间'],
      ['设备规则', '.training-drawer', '设备规则'],
      ['空间管理', '[aria-labelledby="space-drawer-title"]', '空间管理'],
      ['空间管家', '.orchestrator-settings-drawer', '空间管家管理'],
      ['人员库', '[aria-labelledby="space-people-title"]', '空间人员库'],
    ];
    for (const width of [320, 390, 1280]) {
      await page.setViewport({ width, height: 844 });
      await assertClosed();
      for (const [menuText, selector, title] of drawers) {
        if (process.env.SSM_CHECK_VERBOSE) console.log('Checking management surface:', width, menuText);
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
        const close = `${selector} header button[aria-label^="关闭"]`;
        await page.waitForSelector(close, { visible: true });
        await assertClosed();
        await assertLayout(selector);
        if (menuText === '设备规则') {
          assert.deepEqual(await page.$$eval('.training-device-group__heading h3', nodes => nodes.map(node=>node.textContent)), ['呆呆狗','皮皮灯']);
          assert.equal(await page.$$eval('.training-record', nodes => nodes.length), 2);
          assert.equal(await page.$('.training-device-select'), null, 'Rules should be grouped without a device selector');
          assert.equal(await page.$eval(selector, node => /待确认|未应用|已保存|已加载|暂停|追改|教它/.test(node.textContent)), false);
        }
        if (menuText === '分享空间') {
          await page.waitForSelector('.invite-pass__qr canvas');
          assert.equal(await page.$('.invite-state,.invite-drawer__actions'), null);
          assert.equal(await page.$eval('.invite-pass', node => /扫码可加入|保持不变|停用分享|邀请已启用/.test(node.textContent)), false);
          assert.ok(await page.$('.invite-pass__link button[aria-label="复制邀请链接"]'));
          await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {configurable:true,
            value:{writeText:async text=>{window.__copiedInvite=text;}}}));
          await page.click('.invite-pass__link button');
          await page.waitForFunction(()=>window.__copiedInvite?.includes('#invite=simulated-stable-space-share-code'));
        }
        if (menuText === '空间管理') {
          await page.waitForSelector('.space-device-option');
          assert.equal(await page.$('.space-drawer__footer,.space-members'), null);
          assert.equal(await page.$eval('[aria-label="刷新空间"]', node=>node.textContent.trim()), '');
          assert.equal(await page.$$eval('.space-device-option', nodes=>nodes.length), 2);
          const rows=await page.$$eval('.space-device-option',nodes=>nodes.map(node=>{
            const name=node.querySelector('strong').getBoundingClientRect();
            const state=node.querySelector('.space-device-option__state').getBoundingClientRect();
            const menu=node.querySelector('[aria-haspopup="menu"]').getBoundingClientRect();
            return {nameRight:name.right,stateLeft:state.left,nameCenter:(name.top+name.bottom)/2,
              stateCenter:(state.top+state.bottom)/2,menuRight:menu.right,rowRight:node.getBoundingClientRect().right};
          }));
          for(const row of rows){assert(row.stateLeft>=row.nameRight);assert(Math.abs(row.nameCenter-row.stateCenter)<2);assert(Math.abs(row.menuRight-row.rowRight)<1);}
          assert.equal(await page.$('.space-device-option__actions'),null,'Device actions belong in the trailing menu');
          assert.equal(await page.$eval('.space-device-history',node=>node.open), false);
          assert.equal(await page.$eval('.space-device-history summary',node=>node.textContent),'历史设备');
          const menuButton = '.space-device-option button[aria-haspopup="menu"]';
          await page.click(menuButton);
          await page.waitForSelector('.space-device-menu:not([hidden])');
          assert.deepEqual(await page.$$eval('.space-device-menu:not([hidden]) [role="menuitem"]',nodes=>nodes.map(node=>node.textContent.trim())),['设备标签','移除设备']);
          const bounds=await page.$eval('.space-device-menu:not([hidden])',node=>{
            const r=node.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,width:innerWidth};
          });
          assert(bounds.left>=0 && bounds.right<=bounds.width && bounds.top>=0,'Device menu leaves the viewport');
          await page.keyboard.press('Escape');
          await page.waitForFunction(()=>!document.querySelector('.space-device-menu:not([hidden])'));
          assert.equal(await page.$eval(menuButton,node=>node===document.activeElement),true,'Escape must restore the menu trigger focus');
          await page.click(menuButton);
          await page.keyboard.press('ArrowDown');
          assert.equal(await page.evaluate(()=>document.activeElement.textContent.trim()),'移除设备');
          await page.keyboard.press('ArrowUp');
          await page.click('.space-device-menu:not([hidden]) [role="menuitem"]:not(.btn--danger)');
          await page.waitForSelector('.device-label-qr canvas');
          assert.equal(await page.$eval(selector,node=>node.inert),true,'The parent drawer must be inert behind the label dialog');
          assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('.device-label-dialog'))),true);
          for(let tab=0;tab<4;tab++) {
            await page.keyboard.press('Tab');
            assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('.device-label-dialog'))),true,'Label focus escaped to the background');
          }
          assert.deepEqual(await page.$$eval('.device-label-actions button',nodes=>nodes.map(node=>node.textContent.trim())),['保存']);
          const labelLayout=await page.$eval('.device-label-dialog',node=>{
            const rect=node.getBoundingClientRect();
            return {left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,width:rect.width,
              centerX:(rect.left+rect.right)/2,centerY:(rect.top+rect.bottom)/2,
              viewportWidth:innerWidth,viewportHeight:innerHeight,scroll:node.scrollWidth,client:node.clientWidth};
          });
          assert(labelLayout.width<=320 && labelLayout.left>=16 && labelLayout.right<=labelLayout.viewportWidth-16,'Label dialog must stay compact');
          assert(Math.abs(labelLayout.centerX-labelLayout.viewportWidth/2)<1 && Math.abs(labelLayout.centerY-labelLayout.viewportHeight/2)<1,'Label dialog must be centred');
          assert(labelLayout.scroll<=labelLayout.client,'Label dialog must not overflow horizontally');
          await screenshot(`device-label-${width}`);
          await page.keyboard.press('Escape');
          await page.waitForFunction(()=>!document.querySelector('.device-label-dialog'));
          assert.equal(await page.$eval(selector,node=>node.inert),false,'Closing a label must retain its space drawer');
          assert.equal(await page.$eval(menuButton,node=>node===document.activeElement),true,'Closing a label must restore its device menu trigger');
        }
        if (menuText === '空间管家') {
          await page.waitForSelector('.orchestrator-settings-drawer .personality-editor textarea');
          assert.equal(await page.$('.orchestrator-settings-drawer .agent-personality__trait-effect'),null);
          const fields=await page.$$eval('.orchestrator-settings-drawer .personality-editor input[type="text"],.orchestrator-settings-drawer .personality-editor textarea',nodes=>nodes.map(node=>({autocomplete:node.autocomplete,inputMode:node.inputMode,fontSize:parseFloat(getComputedStyle(node).fontSize)})));
          for(const field of fields){assert.equal(field.autocomplete,'off');assert.equal(field.inputMode,'text');assert(field.fontSize>=16);}
          const traits=await page.$$eval('.orchestrator-settings-drawer .agent-personality__trait',nodes=>nodes.map(node=>({
            rangeBottom:node.querySelector('input').getBoundingClientRect().bottom,
            valueTop:node.querySelector('output').getBoundingClientRect().top,
            border:getComputedStyle(node).borderRadius,
          })));
          assert.equal(traits.length,2);
          for(const trait of traits){assert(trait.valueTop>=trait.rangeBottom,'Trait value must be below its slider');assert.equal(trait.border,'0px');}
          assert.equal(await page.$eval('.orchestrator-settings-drawer .personality-editor',node=>/版本|下次对话生效|影响：|无需在人格/.test(node.textContent)),false);
        }
        if (menuText === '人员库') {
          await page.waitForSelector('.person-row');
          await page.waitForFunction(()=>!document.querySelector('.people-directory').getAttribute('aria-busy') || document.querySelector('.people-directory').getAttribute('aria-busy')==='false');
          assert.equal(await page.$eval('[aria-label="刷新人员库"]',node=>node.textContent.trim()),'');
          assert.equal(await page.$$eval('.person-row',nodes=>nodes.length),2);
          assert.equal(await page.$eval('.person-enrol button[type="submit"]',node=>node.disabled),true);
          const hierarchy=await page.$eval(selector,panel=>{
            const style=selector=>getComputedStyle(panel.querySelector(selector));
            const heading=style('header h2'),section=style('.asset-section__heading h3'),name=style('.person-row strong'),meta=style('.person-row__photos'),label=style('label[for="person-name"]'),input=style('#person-name'),select=style('#person-relationship');
            return {sizes:[heading.fontSize,section.fontSize,name.fontSize,label.fontSize,meta.fontSize,input.fontSize,select.fontSize],
              families:[heading.fontFamily,name.fontFamily,label.fontFamily,input.fontFamily,select.fontFamily],
              colors:[heading.color,name.color,label.color,meta.color]};
          });
          assert.deepEqual(hierarchy.sizes,['18px','16px','15px','14px','12px','16px','16px']);
          assert.equal(new Set(hierarchy.families).size,1,'Management text and controls must share the system font stack');
          assert.equal(new Set(hierarchy.colors).size,4,'Heading, primary text, labels and metadata must have distinct emphasis');
          const rows=await page.$$eval('.person-row',nodes=>nodes.map(node=>({scroll:node.scrollWidth,width:node.clientWidth})));
          for(const row of rows) assert(row.scroll<=row.width,'A long person name overflowed its row');
          page.once('dialog',dialog=>dialog.dismiss());
          await page.click('.person-row__remove');
          assert.equal(await page.$$eval('.person-row',nodes=>nodes.length),2);
          assert.equal(await page.evaluate(()=>__managementRequests.some(request=>request.method==='DELETE')),false);
        }
        await screenshot(`${menuText === '设备规则' ? 'training' : selector.includes('people') ? 'people' : menuText === '分享空间' ? 'invite' : menuText === '空间管理' ? 'space' : 'agent'}-${width}`);
        await page.click(close);
        await page.waitForFunction(selector => !document.querySelector(selector) || getComputedStyle(document.querySelector(selector)).visibility === 'hidden', {}, selector);
      }
      await page.click('#open-go2-assets');
      await page.waitForSelector('.go2-assets-drawer .personality-editor textarea');
      await assertLayout('.go2-assets-drawer');
      const profile=await page.$eval('.go2-assets-drawer .agent-management-profile',node=>({width:node.getBoundingClientRect().width,body:node.parentElement.clientWidth}));
      assert(profile.width>profile.body*.85,'The personality editor must use the drawer width');
      await screenshot(`go2-assets-${width}`);
      await page.click('.go2-assets-drawer [aria-label^="关闭"]');
      await page.waitForFunction(()=>document.querySelector('.go2-assets-drawer').inert);
      await page.click('#open-lamp-assets');
      await page.waitForSelector('[aria-labelledby="lamp-assets-title"] .personality-editor textarea');
      await assertLayout('[aria-labelledby="lamp-assets-title"]');
      const lampProfile=await page.$eval('[aria-labelledby="lamp-assets-title"] .agent-management-profile',node=>({width:node.getBoundingClientRect().width,body:node.parentElement.clientWidth}));
      assert(lampProfile.width>lampProfile.body*.85,'The lamp editor must use the drawer width');
      await screenshot(`lamp-assets-${width}`);
      await page.click('[aria-labelledby="lamp-assets-title"] [aria-label="关闭灯管理"]');
      await page.waitForFunction(()=>document.querySelector('[aria-labelledby="lamp-assets-title"]').inert);
      // Keyboard navigation must never land in any closed drawer.
      await page.focus('.space-menu__trigger');
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.space-drawer,.invite-drawer'))), false);
      }
    }
    await page.click('.space-menu__trigger');
    await page.evaluate(()=>[...document.querySelectorAll('.space-menu__item')].find(button=>button.querySelector('strong')?.textContent==='空间管家').click());
    await page.waitForSelector('.orchestrator-settings-drawer .personality-editor textarea');
    await page.$eval('#personality-name-cloud_orchestrator',input=>{
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'小管家');input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    await page.$eval('#personality-prompt-cloud_orchestrator',input=>{
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,'用自然、简洁的语言回应。');input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    await page.$eval('#personality-trait-cloud_orchestrator-warmth',input=>{
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'72');input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    await page.click('.orchestrator-settings-drawer .agent-personality__actions button');
    await page.waitForFunction(()=>document.querySelector('.agent-personality__feedback')?.textContent==='已保存');
    const save=await page.evaluate(()=>__managementRequests.find(request=>request.method==='PUT'));
    assert.deepEqual(save.body,{name:'小管家',prompt:'用自然、简洁的语言回应。',traits:{warmth:72,clarification:45},revision:3});
    await page.click('.orchestrator-settings-drawer [aria-label="关闭管家管理"]');
    await page.click('.space-menu__trigger');
    await page.evaluate(()=>[...document.querySelectorAll('.space-menu__item')].find(button=>button.querySelector('strong')?.textContent==='空间管理').click());
    await page.waitForSelector('.space-device-option');
    await page.click('.space-device-option button[aria-haspopup="menu"]');
    await page.evaluate(()=>{
      window.__originalConfirm=window.confirm;window.__removalConfirmations=0;
      window.confirm=()=>{window.__removalConfirmations++;return true;};
    });
    await page.click('.space-device-menu:not([hidden]) .btn--danger');
    await page.waitForFunction(()=>document.querySelectorAll('.space-device-option').length===1);
    assert.deepEqual(await page.evaluate(()=>__managementRequests.find(request=>request.method==='PATCH').body),{device_ids:['go2']});
    await page.click('.space-device-option button[aria-haspopup="menu"]');
    await page.click('.space-device-menu:not([hidden]) .btn--danger');
    await page.waitForFunction(()=>document.querySelectorAll('.space-device-option').length===0);
    assert.equal(await page.evaluate(()=>__removalConfirmations),0,'Device removal must not open a native confirmation');
    await page.evaluate(()=>{window.confirm=__originalConfirm;});
    assert.equal(await page.$eval('.asset-section--devices .asset-empty',node=>node.textContent),'暂无设备');
    assert.equal(await page.$eval('.asset-section--devices',node=>node.textContent.includes('设备已从当前空间移除')),false);
    await page.click('.space-device-history summary');
    assert.deepEqual(await page.$$eval('.space-device-history__row button',nodes=>nodes.map(node=>node.textContent.trim())),['设备标签','设备标签','设备标签']);
    const inputHints=await page.$$eval('input:not([type="range"]):not([type="file"]),textarea',nodes=>nodes.map(node=>({autocomplete:node.autocomplete,inputMode:node.inputMode})));
    for(const hint of inputHints){assert.equal(hint.autocomplete,'off');assert(['text','numeric','decimal','url'].includes(hint.inputMode));}
    await page.click('[aria-labelledby="space-drawer-title"] [aria-label="关闭空间管理"]');
    await page.click('.space-menu__trigger');
    await page.evaluate(() => [...document.querySelectorAll('.space-menu__item')].find(button=>button.textContent.includes('设备规则')).click());
    await page.waitForSelector('.training-record');
    await page.click('.training-record button[aria-label="删除规则"]');
    await page.waitForFunction(()=>document.querySelectorAll('.training-record').length===1);
    assert.equal(await page.$('.training-record[data-teaching-id="wave"]'),null,'Deleted rule remained visible');
    await page.click('.training-record button[aria-label="修改参数"]');
    await page.waitForSelector('input[aria-label="次数参数"]');
    await page.$eval('input[aria-label="次数参数"]',input=>{
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'3');input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    await page.$$eval('.training-parameter-actions button',buttons=>buttons.find(button=>button.textContent==='保存').click());
    await page.waitForFunction(()=>window.__saved?.revision===2);
    assert.equal(await page.evaluate(()=>window.__saved.unit_id), 'lamp', 'Parameter edit lost the selected device');
    assert.ok(await page.$('.training-drawer'),'Parameter editing must keep the original drawer open');
    assert.deepEqual(errors, []);
    console.log('PASS: seven management surfaces at 320/390/1280px; compact label dialog, direct removal/empty state/history, personality revision/save and rule editing; no overflow, unexpected network or browser errors.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
