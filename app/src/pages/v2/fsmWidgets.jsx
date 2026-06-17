/* fsmWidgets — (unit_id, state) → 态内富控件。复用 Go2Page 的摇杆/视频组件。 */
function fsmWidget(unitId, state) {
  if (unitId !== 'go2') return null;

  const sendVelocity = (vx, vy, vyaw) => fetch('/api/go2/velocity', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vx, vy, vyaw }),
  });

  const VirtualJoystick = window.VirtualJoystick;
  const VideoCanvas = window.VideoCanvas;

  /* moving 态：实时双摇杆 */
  if (state === 'moving' && VirtualJoystick) {
    return (
      <div key="joy" style={{ marginTop: 14, display: 'flex', justifyContent: 'space-between' }}>
        <VirtualJoystick label="MOVE" disabled={false}
          onMove={(dx, dy) => sendVelocity(+(-dy * 0.5).toFixed(2), +(-dx * 0.5).toFixed(2), 0)}
          onStop={() => sendVelocity(0, 0, 0)}/>
        <VirtualJoystick label="ROTATE" disabled={false}
          onMove={(dx) => sendVelocity(0, 0, +(-dx * 0.8).toFixed(2))}
          onStop={() => sendVelocity(0, 0, 0)}/>
      </div>
    );
  }

  /* standing/executing：摄像头画面 */
  if (['standing', 'executing'].includes(state) && VideoCanvas) {
    return (
      <div key="cam" style={{ marginTop: 14, aspectRatio: '16/9', background: '#0a0c14',
        border: '1px solid var(--color-accent-border)', borderRadius: 'var(--radius-sm)',
        overflow: 'hidden' }}>
        <VideoCanvas connected={true}/>
      </div>
    );
  }
  return null;
}
window.fsmWidget = fsmWidget;
