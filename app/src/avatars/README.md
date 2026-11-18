# 伙伴形象

设备大脑继续输出可选 `emotion`，前端选择完整的表情与动作预设。首页、设备详情页和活动空间统一调用 `CompanionAvatar`，共享同一个只读呈现投影。形象不会发送设备命令，也不会参与业务结果判定。沿用 SVG、CSS、React 和现有 SSE，没有额外模型调用或动画引擎。

## 情绪与日常表现

八种情绪：`neutral`（平常）、`happy`（开心）、`curious`（好奇）、`confused`（困惑）、`surprised`（惊讶）、`angry`（生气）、`sad`（难过）、`relaxed`（放松）。未知值、非字符串和缺失值回退为 `neutral`。

每种预设包含眼、眉、嘴和一次入场动作。持续行为和短暂反应分别计算：探索、休息、处理中等行为来自已有设备状态和当前请求；回复情绪与公开游戏动作短暂叠加，结束后恢复当前行为。休息关闭眼睛并减缓起伏；灯熄灭不表示伙伴休息。自然待机包括可辨认的呼吸和眨眼，小尺寸使用相应动作幅度。

这些表现只投影已有运行状态，不能使灯出现新状态、改变 Go2 的模式或姿态，也不能用动作接受结果伪造物理观测。

皮皮沿用黄绿色圆润轮廓，呆呆沿用青蓝色利落轮廓，未知设备使用中性外观。管家保留现有头像。

## 组件与加载

加载顺序保持：`utils/agentMeta.js`、`utils/stateSnapshot.js` → `avatars/registry.js` → `avatars/presentation.js` → `avatars/Avatar.jsx` → `avatars/CompanionAvatar.jsx` → 使用形象的组件。`styles/avatars.css` 在 `app.css` 后加载。具体脚本顺序见 `app/index.html`。

```jsx
// 实时入口：页面只选择设备身份、尺寸和摆放位置。
<CompanionAvatar unitId="go2" size={48} />

// 可提供当前 Card 作为名称、外观的补充，不由页面指定实时情绪或行为。
<CompanionAvatar unitId={card.unit_id} agent={card} size={36} />

// 历史入口：从同活动、同设备的原始事件选择记录时的情绪、公开动作和状态。
<CompanionAvatar unitId="go2" size={96} mode="history"
  snapshot={{events:activityEvents, activity_id:activity.activity_id}} />

// 仅供形象预览：本地预设不注入设备当前状态或实时事件。
<CompanionAvatar unitId="go2" size={112} mode="preview"
  snapshot={{emotion:'curious',behavior:'explore',animated:true}} />
```

`CompanionAvatar` 是产品界面的统一入口；`ShapeFace` 只负责 SVG 绘制、动画播放以及静态预设目录。业务页面不再自行拼装 `animated`、`behavior`、`emotion`、`shape` 和动画时序。预览主舞台也通过统一入口的 `preview` 模式展示本地预设，不连接设备或改写实时投影。

`presentation.js` 管理设备当前呈现、请求关联、情绪与动作的截止时间和事件去重。它消费既有设备快照和 API 事件，不建立第二条 SSE 或设备订阅，也不持久化另一套设备状态。`CompanionAvatar` 订阅这份共享投影，所以页面切换不会重置探索、休息或当前情绪。

隐藏页面和离屏头像暂停动画；系统减少动态效果时停用动画和过渡。所有状态与游戏结果仍有文字，图形不承担唯一的信息表达。

连接可用性、新鲜度和行为映射在共享投影内统一判断。明确离线时淡为灰色并停止正常动作；浏览器断流或状态待同步与设备离线分别处理。灯沿用按变化发布的状态语义，Go2 沿用新鲜遥测和 epoch/revision 顺序，云端控制投影不能刷新物理读数时间。只有真实离线到在线的变化才提供上线回应，初次挂载和浏览器重连不冒充设备上线。

## 实时与历史

首页“空间伙伴”、活动伙伴卡和设备页输入栏共享同一设备的当前行为、情绪和公开动作。首页保留 36px，设备页保留 48px，活动伙伴保留 96px；各处不再各自解释同一份事件。两个设备在同一条 `rps_dispatched` 事件到达时分别按 `game_throw.choices[unit_id]` 展示自己的公开出拳。

`useSsmCore` 将现有 Card 和合并后的设备快照交给呈现模块；设备页已有的状态流也只转交其合并后的观测。`CommandBus` 关联本次请求的开始与完成，将 HTTP 回复中的 `emotion` 与既有 SSE 回复汇入同一呈现入口。页面自己的聊天记录保留原始文字和情绪元数据，但不再把聊天历史重新转成实时形象事件。

实时情绪保持 6 秒；出拳从设备派发保持到双方真实回执产生同局同轮 `rps_result`，不设显示倒计时。普通发言和内部请求不清除出拳；结算、游戏关闭或活动结束清除相应手势。当前活动归属、设备身份、请求身份、事件时间和去重统一校验；旧轮回执不能清除新轮，旧局和已结束活动不覆盖当前表现，重复事件也不重播。切页保留正在执行的手势和尚未过期的情绪，已经发生的入场动作不会从头播放；刷新建立新的呈现会话，REST 历史不能启动动作。

历史模式只使用传入快照：记录时的情绪、明确公开的游戏手势和可信的 `state_at_event.values`。缺失信息使用中性外观，不读取当前设备状态补历史事实，不播放事件入场动作。已结束的活动伙伴采用历史模式，其状态文字与色点也只读取当时的观测；缺失时明确显示“当时状态未记录”。

设备详情页通过 `ChatPanel.inputAvatar` 将 48px 的形象放在输入框左侧，复用输入栏原有高度，标题不再重复显示形象。手机宽度（不超过 600px）输入聚焦时缩为 36px，失焦后恢复；减少动态效果时禁用尺寸过渡。首页管家聊天不传该属性，输入栏沿用原布局；首页伙伴入口仍由独立的 `HomeDeviceRow` 展示形象和状态。灯与 Go2 共用设备详情页的这套呈现。

实时游戏动作只由公开的 `stage=rps_dispatched` / `game_throw.choices[unit_id]` 触发，对应 `rock/scissors/paper`；该事件表示开始执行，不表示动作已完成或已判胜。`rps_result` / `game_round` 保留真实回执后的历史静态手势和实际设备动作说明。`throw_announcement`、自由文本和未公开选择都不能触发出拳。

## 扩展

- 新设备外观：增加 `AVATAR_PROFILES` 配置与匹配，复用现有情绪和播放器；这里不声明物理能力或在线状态。
- 新情绪：增加 `AVATAR_EXPRESSIONS` 的面部、入场动作，和必要 CSS；同步后端 `cloud/agents/common/emotions.py` 中的允许值与提示目录。
- 新游戏动作：在游戏动作目录增加轮廓，仅映射明确的公开结构化事件；不从模型文本猜动作或完成结果。
- 动画时长：在 `AVATAR_TIMING` 与 CSS 中统一调整，不让模型输出时间轴。

## 预览与验证

从 PWA 同源地址打开 `/avatar-preview.html`。默认展示日常角色，可以选择八种心情、休息/处理中、静态模式和游戏动作，并演示“开心 → 出拳 → 恢复”。此页不调用设备 API。

`scripts/check_avatar_frontend.cjs` 加载本地静态资产并拦截外部请求，验证独立预览中的八种表情、游戏恢复、休息与减少动态效果。`scripts/check_companion_frontend.cjs` 验证统一呈现入口，包括首页／设备页／活动空间一致性、模型情绪与公开手势、到期与去重、同运行续答、旧运行和旧 epoch 拒绝、活动结束、重连、物理任务别名、历史快照、小尺寸实际位移和切页动画续播。`scripts/check_chat_frontend.cjs` 验证设备聊天入口与首页输入布局；`scripts/check_activity_frontend.cjs --browser` 验证活动 UI。以上均使用模拟 API／设备数据，不代表真实模型质量或真机动作验收。

按各脚本头部配置 React、Chrome 和 Node 依赖路径后运行。`SSM_CHECK_FONT` 与 `SCREENSHOT_DIR` 仅用于本地截图；截图字体、动画录屏工具不进入产品依赖。
