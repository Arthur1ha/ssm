/* Reusable camera QR reader. The caller decides what the decoded text means. */
function QrScanner({ onScan, hint = '将二维码放入框内，识别后自动读取' }) {
  const { useEffect, useRef, useState } = React;
  const videoRef = useRef(null);
  const onScanRef = useRef(onScan);
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState('starting');
  const [error, setError] = useState('');

  onScanRef.current = onScan;

  useEffect(() => {
    let stopped = false;
    let stream = null;
    let timer = null;
    const video = videoRef.current;

    const stop = () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      stream?.getTracks().forEach(track => track.stop());
      if (video) video.srcObject = null;
    };

    const start = async () => {
      setStatus('starting');
      setError('');
      if (!navigator.mediaDevices?.getUserMedia) {
        setError('当前环境无法调用摄像头，请使用 HTTPS 打开页面');
        setStatus('error');
        return;
      }
      try {
        const decodeQr = await loadQrLibrary('scan');
        if (stopped) return;
        const acquired = await navigator.mediaDevices.getUserMedia({
          audio: false, video: { facingMode: 'environment' },
        });
        if (stopped) {
          acquired.getTracks().forEach(track => track.stop());
          return;
        }
        stream = acquired;
        video.srcObject = stream;
        await video.play();
        if (stopped) return;
        setStatus('scanning');

        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) throw new Error('canvas unavailable');

        const scanFrame = () => {
          if (stopped) return;
          if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight));
            canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
            canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
            context.drawImage(video, 0, 0, canvas.width, canvas.height);
            const frame = context.getImageData(0, 0, canvas.width, canvas.height);
            const code = decodeQr(frame.data, frame.width, frame.height, {
              inversionAttempts: 'dontInvert',
            });
            if (code?.data) {
              stop();
              setStatus('done');
              onScanRef.current(code.data);
              return;
            }
          }
          timer = setTimeout(scanFrame, 220);
        };
        scanFrame();
      } catch (cameraError) {
        if (stopped) return;
        stop();
        if (cameraError.name === 'NotAllowedError' || cameraError.name === 'PermissionDeniedError') {
          setError('摄像头权限未开启，请允许访问');
        } else if (cameraError.name === 'NotFoundError' || cameraError.name === 'OverconstrainedError') {
          setError('没有找到可用摄像头');
        } else if (cameraError.message?.startsWith('QR library')) {
          setError('扫码组件加载失败，请重试');
        } else {
          setError('摄像头启动失败，请重试');
        }
        setStatus('error');
      }
    };

    start();
    return stop;
  }, [attempt]);

  return (
    <div className="qr-scanner">
      <div className="qr-scanner__view">
        <video ref={videoRef} autoPlay muted playsInline aria-label="扫描二维码的摄像头画面"/>
        <span className="qr-scanner__target" aria-hidden="true"/>
        {status !== 'scanning' && <span className="qr-scanner__overlay">
          {status === 'starting' ? '正在打开摄像头…' : status === 'done' ? '已识别二维码' : '摄像头不可用'}
        </span>}
      </div>
      {error && <p className="qr-scanner__error" role="status">{error}</p>}
      {status === 'done' || status === 'error' ? (
        <button type="button" className="btn qr-scanner__retry" onClick={() => setAttempt(value => value + 1)}>重新扫码</button>
      ) : <p className="qr-scanner__hint">{hint}</p>}
    </div>
  );
}

window.QrScanner = QrScanner;
