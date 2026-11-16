# 伙伴形象

轻量 SVG 轮廓 + 两眼一嘴。形状、表情、CSS 动效独立组合；不依赖 GIF、画布、构建工具或第二套设备状态。

从 PWA 同源地址打开 `/avatar-preview.html`，可以查看全部形状、选择表情和播放一次动效。该页面不调用设备 API。

## 文件与加载顺序

1. `utils/agentMeta.js`
2. `avatars/registry.js`（普通脚本）
3. `avatars/Avatar.jsx`（Babel JSX，依赖 React）
4. `components/ActivityMembers.jsx`（同时依赖 Icon、activityView、ApiClient）

`styles/avatars.css` 在 `styles/app.css` 后加载。统一组件接口：

```jsx
<ShapeFace unitId="go2" shape="scissors" expression="happy"
  motion="pop" animationKey={newEventId} size={72} label="呆呆 · 剪刀" />
```

省略 `animationKey` 时始终静止。动画只执行一次；系统减少动态效果设置优先。省略 `label` 表示装饰图形，外部必须保留可读的动作文字。

## 扩展方法

- 设备风格：在 `AVATAR_PROFILES` 增加颜色、面部颜色和轮廓类型，通过 `resolveAvatarProfile` 匹配真实 Card 类型或明确设备 ID。未知设备使用中性风格。
- 形状：在 `AVATAR_SHAPES` 增加一个键、中文 `label`、面部纵坐标 `faceY`、100 × 100 坐标系下的 `round/angular` SVG 路径。整个角色就是这个轮廓，无需添加手脚。尖角和圆角可使用不同路径。
- 表情：在 `AVATAR_EXPRESSIONS` 增加眼睛样式与嘴巴路径；必要时扩展 `Avatar.jsx` 的眼部渲染。表情只用于表达设计，不能把它当成模型真实心情或硬件事实。
- 动效：在 `AVATAR_MOTIONS` 增加名称，并在 `avatars.css` 增加一次性的 CSS animation。禁止无限循环或依赖动画才能读懂结果。
- 事件：在 `AVATAR_EVENT_MAPPINGS` 增加对明确结构化字段的映射。不要从自由文本、相邻消息或旧快照猜测动作、情绪、成功或因果。

## 当前事实与回放边界

唯一动作映射是公开的 `stage=rps_result` / `game_round.choices[unit_id]`，对应 `rock/scissors/paper`。`game_round.mapping[unit_id].label` 保留实际设备动作解释，供过程展示使用。后端在揭晓且真实执行回执确认后才提供这些字段，未揭晓或失败不预演出拳。

顶部卡显示“最近出拳 · 第 N 轮 · 石头”，与独立的当前设备状态、灯色和连接状态分开。新结果短暂变形后回到当前状态脸；历史手势在活动记录中显示静帧，不表示现在正在执行该动作。没有结构化记录则保持日常形状，不补猜历史。`avatarStatePresentation` 仅用新鲜的明确 `activity_state=rest` 表示闭眼、`life_state=explore/play` 表示专注，配套文字保留实际状态名；离线和待确认保持中性。

只有当前连接期间到达的新 SSE 事件可以播放一次。组件挂载、初次读取历史、重复事件和断线重连不触发回放；事件 ID 去重与连接起点时间共同过滤旧消息。关闭或结束活动、断开连接时不播放。所有信息均有静态文本，形象不更改 Card、动作门禁或任何真实设备状态。

## 回归

`scripts/check_avatar_frontend.cjs` 只加载本地静态资产并拦截外部请求。按脚本头部配置 React、Chrome 和 Node 依赖路径后运行 `node scripts/check_avatar_frontend.cjs`。覆盖无游戏记录的初始卡、历史静止、新 SSE 一次变形、恢复当前状态、重复与重连不回放、陈旧事件、手机宽度和减少动态效果。`SSM_CHECK_FONT` 和 `SCREENSHOT_DIR` 仅用于本地截图，不是产品依赖。
