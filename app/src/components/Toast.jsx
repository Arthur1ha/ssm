/* Reusable bottom feedback: showToast(message, { action: {
 * label, onClick, successMessage, errorMessage } }). New feedback replaces old
 * feedback; asynchronous actions cannot overwrite a newer toast. */
function useToast() {
  const [toast, setToast] = React.useState(null);
  const sequence = React.useRef(0);
  const showToast = React.useCallback((message, options = {}) => {
    setToast({ ...options, message, id: ++sequence.current });
  }, []);
  const dismissToast = React.useCallback(id => {
    setToast(current => current?.id === id ? null : current);
  }, []);
  return { toast, showToast, dismissToast };
}

function Toast({ notice, onDismiss }) {
  const [hovered, setHovered] = React.useState(false);
  const [focused, setFocused] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [feedback, setFeedback] = React.useState(null);
  const mounted = React.useRef(true);
  const inFlight = React.useRef(false);
  const action = (!feedback || feedback.error) ? notice.action : null;
  const message = feedback?.message || notice.message;

  React.useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  React.useEffect(() => {
    if (hovered || focused || busy) return;
    const timer = setTimeout(() => onDismiss(notice.id), 3000);
    return () => clearTimeout(timer);
  }, [notice, message, action, feedback, hovered, focused, busy, onDismiss]);

  const runAction = async () => {
    if (inFlight.current || !action) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await action.onClick();
      if (mounted.current) {
        setFeedback({ message: action.successMessage || '操作成功' });
        setFocused(false);
      }
    } catch (_) {
      if (mounted.current) setFeedback({ message: action.errorMessage || '操作失败，请重试', error: true });
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return ReactDOM.createPortal(
    <div className="toast-region">
      <div className="toast" role="status" aria-live="polite" aria-atomic="true"
        onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
        onFocusCapture={() => setFocused(true)}
        onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }}>
        <span className="toast__message">{message}</span>
        {action && <button type="button" className="toast__action" disabled={busy} onClick={runAction}>
          {busy ? '处理中…' : feedback?.error ? '重试' : action.label}
        </button>}
      </div>
    </div>, document.body
  );
}

window.useToast = useToast;
window.Toast = Toast;
