# 伙伴形象

设备大脑只输出可选 `emotion`，前端选择完整的表情与动作预设。形象不会发送设备命令，也不会参与业务结果判定。沿用 SVG、CSS、React 和现有 SSE，没有额外模型调用或动画引擎。

## 情绪与日常表现

八种情绪：`neutral`（平常）、`happy`（开心）、`curious`（好奇）、`confused`（困惑）、`surprised`（惊讶）、`angry`（生气）、`sad`（难过）、`relaxed`（放松）。未知值、非字符串和缺失值回退为 `neutral`。

每种预设包含眼、眉、嘴和一次入场动作。面部层交叉淡化，身体姿态平滑切换；自然待机包括轻微起伏和眨眼。`behavior=rest` 关闭眼睛并减缓起伏，`thinking` 提供关注表现。这些表现只投影已有运行状态，不能使灯出现新状态或改变 Go2 的模式、姿态。

皮皮沿用黄绿色圆润轮廓，呆呆沿用青蓝色利落轮廓，未知设备使用中性外观。管家保留现有头像。

## 组件与加载

加载顺序保持：`utils/agentMeta.js` → `avatars/registry.js` → `avatars/Avatar.jsx` → 使用头像的组件。`styles/avatars.css` 在 `app.css` 后加载。

```jsx
// 实时头像：默认保持日常外观，有新表达时播放一次入场。
<ShapeFace unitId="go2" emotion="happy" animated
  animationKey={newEventId} behavior="idle" size={72} />

// 历史记录和预设目录：静帧，无持续动画。
<ShapeFace unitId="go2" emotion="sad" size={28} />

// 公开游戏动作，结束后由调用方恢复日常形象。
<ShapeFace unitId="go2" shape="scissors" emotion="happy" animated
  motion="pop" animationKey={gameEventId} size={72} />
```

`animated` 缺省为 false。`animationKey` 只控制一次入场，日常微动画由 `animated` 控制。同一组件记住已播事件，游戏结束恢复旧情绪不会重复其入场。休息闭眼覆盖所有情绪的眼部动作。

隐藏页面和离屏头像暂停动画；系统减少动态效果时停用动画和过渡。所有状态与游戏结果仍有文字，图形不承担唯一的信息表达。

实时头像可传入 `online`：true 保留正常配色，false 用 300 毫秒淡为灰色并停止动画。仅同一设备从明确离线变为在线时轻弹一次；初次挂载不播放。浏览器连接恢复或状态待同步传 null，保持静态且不判为设备离线，也不播放上线轻弹。历史头像不传该属性。首页伙伴、活动伙伴和设备页标题沿用各自现有连接状态传入，不改变设备状态、休息逻辑、情绪或游戏规则。

## 实时与历史

首页“空间伙伴”与活动伙伴卡消费同一份活动事件，复用 `useAvatarPresentation`。两个设备分别按 `game_round.choices[unit_id]` 展示自己的公开出拳，随后恢复未过期情绪或日常形象；首页不新增事件连接。

`useActivityTimeline` 在现有 SSE 接收入口为实时事件附加浏览器本地 `avatarReceivedAt`，REST 历史没有该标记。`useAvatarPresentation` 消费已有事件数组，不建立 SSE 连接，不读取硬件 API。

活动情绪保持 6 秒，出拳展示 1.6 秒。出拳期间冻结当时表情，新情绪在动作结束后应用；未过期的旧情绪可以恢复，但其入场不重播。刷新、组件重新挂载、重复事件和重连不会回放过去的表达。过时事件和其他活动的事件不覆盖当前形象；结束活动清除临时表现。

设备页标题同样使用共享 hook：当前请求处理中提供思考表现，新回复带动情绪，之后恢复待机。真实 SSE 时间及活动归属用于过滤迟到事件，旧聊天运行的回复仍可保留为历史文字。聊天发言的头像只在新回复时短暂播放，随后保持该条消息的静态表情；历史头像不会一起跳动。

唯一游戏动作映射仍是公开的 `stage=rps_result` / `game_round.choices[unit_id]`，对应 `rock/scissors/paper`。`throw_announcement`、自由文本和未公开选择都不能触发出拳。游戏历史保留静态手势和实际设备动作说明。

## 扩展

- 新设备外观：增加 `AVATAR_PROFILES` 配置与匹配，复用现有情绪和播放器；这里不声明物理能力或在线状态。
- 新情绪：增加 `AVATAR_EXPRESSIONS` 的面部、入场动作，和必要 CSS；同步后端 `cloud/agents/common/emotions.py` 中的允许值与提示目录。
- 新游戏动作：在游戏动作目录增加轮廓，仅映射明确的公开结构化事件；不从模型文本猜动作或完成结果。
- 动画时长：在 `AVATAR_TIMING` 与 CSS 中统一调整，不让模型输出时间轴。

## 预览与验证

从 PWA 同源地址打开 `/avatar-preview.html`。默认展示日常角色，可以选择八种心情、休息/处理中、静态模式和游戏动作，并演示“开心 → 出拳 → 恢复”。此页不调用设备 API。

`scripts/check_avatar_frontend.cjs` 加载本地静态资产并拦截外部请求，覆盖预览、手机宽度、减少动态效果、真实 SSE hook 与历史合并、情绪到期、游戏恢复、重复事件、重连、重新挂载和活动范围。`scripts/check_chat_frontend.cjs` 验证设备聊天入口及旧运行/过期活动的静态处理；`scripts/check_activity_frontend.cjs --browser` 验证真实活动 UI。

按各脚本头部配置 React、Chrome 和 Node 依赖路径后运行。`SSM_CHECK_FONT` 与 `SCREENSHOT_DIR` 仅用于本地截图；截图字体、动画录屏工具不进入产品依赖。
