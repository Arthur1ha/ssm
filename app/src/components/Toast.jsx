/* Reusable bottom feedback: showToast(message, { action: {
 * label, onClick, successMessage, errorMessage } }). New feedback replaces old
 * feedback; asynchronous actions cannot overwrite a newer toast. */
function useToast() {
  const [toast, setToast] = React.useState(null);
  const sequence = React.useRef(0);
  const showToast = React.useCallback(function publish(message, options = {}) {
    const id = ++sequence.current;
    const notice = { ...options, message, id };
    const action = options.action;
    if (action) {
      let pending = false;
      notice.action = { ...action, onClick: async () => {
        if (pending || sequence.current !== id) return;
        pending = true;
        setToast(current => current?.id === id ? { ...current, busy: true } : current);
        try {
          await action.onClick();
          if (sequence.current === id) publish(action.successMessage || '操作成功');
        } catch (_) {
          if (sequence.current === id) publish(action.errorMessage || '操作失败，请重试', {
            action: { ...action, label: '重试' },
          });
        } finally { pending = false; }
      } };
    }
    setToast(notice);
  }, []);
  React.useEffect(() => {
    if (!toast) return;
    const id = toast.id;
    const timer = setTimeout(() => setToast(current => current?.id === id ? null : current), 3000);
    return () => clearTimeout(timer);
  }, [toast?.id]);
  React.useEffect(() => () => { sequence.current++; }, []);
  return { toast, showToast };
}

function Toast({ notice, anchorRef }) {
  const [bottom, setBottom] = React.useState(null);
  React.useLayoutEffect(() => {
    const anchor = anchorRef?.current;
    if (!anchor) { setBottom(null); return; }
    const position = () => setBottom(Math.max(12, window.innerHeight - anchor.getBoundingClientRect().top + 12));
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(position) : null;
    observer?.observe(anchor);
    window.addEventListener('resize', position);
    window.visualViewport?.addEventListener('resize', position);
    window.visualViewport?.addEventListener('scroll', position);
    position();
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', position);
      window.visualViewport?.removeEventListener('resize', position);
      window.visualViewport?.removeEventListener('scroll', position);
    };
  }, [anchorRef]);
  return ReactDOM.createPortal(
    <div className="toast-region" style={bottom === null ? undefined : { bottom }}>
      <div className="toast" role="status" aria-live="polite" aria-atomic="true">
        <span className="toast__message">{notice.message}</span>
        {notice.action && <button type="button" className="toast__action" disabled={notice.busy} onClick={notice.action.onClick}>
          {notice.busy ? '处理中…' : notice.action.label}
        </button>}
      </div>
    </div>, document.body
  );
}

window.useToast = useToast;
window.Toast = Toast;
