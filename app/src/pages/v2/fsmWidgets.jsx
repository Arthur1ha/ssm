/* fsmWidgets — 按 card.widgets 声明渲染态内富控件（type 枚举：joystick/video/map）。 */
function fsmWidget(unitId, state, widgets) {
  if (!widgets || !widgets.length) return null;

  const VirtualJoystick = window.VirtualJoystick;
  const VideoCanvas = window.VideoCanvas;

  // 只渲染当前状态匹配的 widget（states 为空=全程显示）
  const active = widgets.filter(w => !w.states || !w.states.length || w.states.includes(state));
  if (!active.length) return null;

  const sendVelocity = (endpoint, vx, vy, vyaw) => fetch(endpoint, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vx, vy, vyaw }),
  }).catch(e => console.error('[fsmWidget] velocity failed:', e));

  const nodes = active.map((w, idx) => {
    if (w.type === 'joystick' && VirtualJoystick) {
      const ep = w.endpoint || '/api/go2/velocity';
      return (
        <div key={'joy' + idx} style={{ marginTop: 14, display: 'flex', justifyContent: 'space-between' }}>
          <VirtualJoystick label="MOVE" disabled={false}
            onMove={(dx, dy) => sendVelocity(ep, +(-dy * 0.5).toFixed(2), +(-dx * 0.5).toFixed(2), 0)}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
          <VirtualJoystick label="ROTATE" disabled={false}
            onMove={(dx) => sendVelocity(ep, 0, 0, +(-dx * 0.8).toFixed(2))}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
        </div>
      );
    }
    if (w.type === 'video' && VideoCanvas) {
      // w.endpoint 字段（如 /api/go2/video）暂未消费：
      // VideoCanvas 内部硬编码抓帧路径（/api/go2/video/snapshot），不接受外部 url prop。
      return (
        <div key={'cam' + idx} style={{ marginTop: 14, aspectRatio: '16/9', background: '#0a0c14',
          border: '1px solid var(--color-accent-border)', borderRadius: 'var(--radius-sm)',
          overflow: 'hidden' }}>
          <VideoCanvas connected={true}/>
        </div>
      );
    }
    return null;
  }).filter(Boolean);

  if (!nodes.length) return null;
  return nodes;
}
window.fsmWidget = fsmWidget;
