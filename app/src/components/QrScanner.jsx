/* qr-scanner owns the camera, frame scheduling, native/Worker decoding and scan
 * region. This component only adapts its lifecycle and results to React. */
function QrScanner({ onScan, onError, frameOnly = false, hint = '将二维码放入框内，识别后自动读取' }) {
  const { useEffect, useRef, useState } = React;
  const videoRef = useRef(null);
  const overlayRef = useRef(null);
  const onScanRef = useRef(onScan);
  const onErrorRef = useRef(onError);
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState('');

  onScanRef.current = onScan;
  onErrorRef.current = onError;

  useEffect(() => {
    let stopped = false;
    let scanner = null;
    const video = videoRef.current;

    const stop = () => {
      stopped = true;
      if (!scanner) return;
      const stream = video.srcObject;
      scanner.pause(true);
      scanner.destroy();
      scanner = null;
      // start() can fail after assigning a stream but before playback. In that
      // case the library is inactive, so pause() alone does not release it.
      stream?.getTracks().forEach(track => track.stop());
      video.srcObject = null;
    };

    const fail = message => {
      stop();
      setError(message);
      setStatus('error');
      onErrorRef.current?.(message);
    };

    const start = async () => {
      setStatus('loading');
      setError('');
      if (!navigator.mediaDevices?.getUserMedia) {
        fail('当前环境无法调用摄像头，请使用 HTTPS 打开页面');
        return;
      }
      try {
        const Scanner = await loadQrLibrary('scan');
        if (stopped) return;
        scanner = new Scanner(video, result => {
          if (stopped) return;
          stop();
          setStatus('done');
          onScanRef.current(result.data);
        }, {
          preferredCamera: 'environment',
          maxScansPerSecond: 12,
          highlightScanRegion: true,
          overlay: overlayRef.current,
          returnDetailedScanResult: true,
          onDecodeError: scanError => {
            // qr-scanner 1.4.2 wraps native no-match results in "Scanner error:"
            // while its Worker returns the bare sentinel. Both are normal frames.
            const message = scanError?.message || String(scanError);
            if (stopped || message === Scanner.NO_QR_CODE_FOUND
                || message === `Scanner error: ${Scanner.NO_QR_CODE_FOUND}`) return;
            fail('扫码器暂时不可用，请重新打开扫码');
          },
        });
        setStatus('starting');
        await scanner.start();
        if (!stopped) setStatus('scanning');
      } catch (cameraError) {
        if (stopped) return;
        fail(cameraError.message?.startsWith('QR library')
          ? '扫码组件加载失败，请重试'
          : '摄像头无法打开，请检查相机权限及是否被其他应用占用后重试');
      }
    };

    start();
    return stop;
  }, [attempt]);

  return (
    <div className="qr-scanner">
      <div className="qr-scanner__view">
        <video ref={videoRef} autoPlay muted playsInline aria-label="扫描二维码的摄像头画面"/>
        <div ref={overlayRef} className="qr-scanner__target" style={{ display: 'none' }} aria-hidden="true"/>
        {status !== 'scanning' && <span className="qr-scanner__overlay">
          {status === 'starting' ? '正在打开摄像头…' : status === 'loading' ? '正在加载扫码器…' : status === 'done' ? '已识别二维码' : '摄像头不可用'}
        </span>}
      </div>
      {!frameOnly && (status === 'done' || status === 'error' ? (
        <><button type="button" className="btn qr-scanner__retry" onClick={() => setAttempt(value => value + 1)}>重新扫码</button>
          {error && <p className="action-feedback action-feedback--error" role="status" title={error}>{error}</p>}</>
      ) : <p className="qr-scanner__hint">{hint}</p>)}
    </div>
  );
}

window.QrScanner = QrScanner;
