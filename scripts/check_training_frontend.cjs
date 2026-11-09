/* Run the real React browser harness against simulated network and device state. */
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {transform} = require('sucrase');
const root = path.resolve(__dirname,'..');
const context = vm.createContext({window:{},React});
for (const name of ['app/src/utils/stateMachine.js','app/src/pages/TransitionInspector.jsx']) {
  const source = fs.readFileSync(path.join(root,name),'utf8');
  vm.runInContext(transform(source,{transforms:['jsx'],jsxRuntime:'classic',production:true}).code,context);
}
const inspect = (transition,records) => renderToStaticMarkup(React.createElement(context.window.TransitionInspector,{transition,projection:records}));
const reading = {teaching_id:'reading',desired_status:'enabled',name:'阅读灯光',load_status:'awaiting_load',
  definition:{situation:'准备看书时',defaults:{r:255,g:236,b:210,brightness:200},trigger:null}};
const colorTransition = {action:'SET_COLOR',label:'调色',trigger:'CMD_COLOR'};
const light = inspect(colorTransition,[reading]);
assert.match(light,/准备看书时/);
assert.match(light,/调色/);
assert.match(light,/255, 236, 210/);
assert.match(light,/亮度/);
assert.match(light,/>200</);
assert.match(light,/background-color:rgb\(255,236,210\)/);
assert.doesNotMatch(light,/已保存|已加载|已应用|等待加载|awaiting_load/);
assert.doesNotMatch(inspect(colorTransition,[{...reading,desired_status:'removed'}]),/准备看书时/);
assert.doesNotMatch(inspect(colorTransition,[]),/动作参数|training-inspector__rule/);
const hello = inspect({action:'Hello',label:'打招呼'},[{teaching_id:'hello',desired_status:'enabled',name:'看到人就打招呼',
  definition:{defaults:{},trigger:{event_id:'vision.person_presence',params:{}}}}]);
assert.match(hello,/看到人就打招呼/);
assert.doesNotMatch(hello,/动作参数/);
const blink = inspect({action:'BLINK',label:'闪烁'},[{teaching_id:'sound',desired_status:'enabled',name:'声音提醒',
  definition:{defaults:{count:0},trigger:{params:{count:2}},cooldown_s:20}}]);
assert.match(blink,/>2</);
assert.match(blink,/20 秒/);
assert.doesNotMatch(blink,/持续闪烁/);
console.log('PASS: teaching lines show situation, action and exact saved parameters without load/application labels.');
const args = [path.join(__dirname,'check_go2_frontend.cjs'), ...process.argv.slice(2), '--training-only'];
const result = spawnSync(process.execPath,args,{stdio:'inherit',env:process.env});
process.exit(result.status ?? 1);
