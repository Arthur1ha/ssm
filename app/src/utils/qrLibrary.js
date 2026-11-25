/* Load QR helpers only when the matching UI is opened. Share concurrent loads. */
const qrLibraryLoads = {};

function loadQrLibrary(name) {
  if (name === 'scan') {
    if (!qrLibraryLoads.scan) {
      qrLibraryLoads.scan = import('/src/utils/qr-scanner/qr-scanner.min.js')
        .then(module => module.default)
        .catch(() => {
          delete qrLibraryLoads.scan;
          throw new Error('QR library failed to load');
        });
    }
    return qrLibraryLoads.scan;
  }
  const libraries = {
    generate: ['QRCode', '/src/utils/qrcode.vendor.js'],
  };
  const library = libraries[name];
  if (!library) return Promise.reject(new Error('Unknown QR library'));
  if (window[library[0]]) return Promise.resolve(window[library[0]]);
  if (!qrLibraryLoads[name]) {
    qrLibraryLoads[name] = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timeout = setTimeout(() => {
        script.remove();
        reject(new Error('QR library load timed out'));
      }, 8000);
      script.src = library[1];
      script.crossOrigin = 'anonymous';
      script.onload = () => {
        clearTimeout(timeout);
        return window[library[0]]
          ? resolve(window[library[0]])
          : reject(new Error('QR library unavailable'));
      };
      script.onerror = () => {
        clearTimeout(timeout);
        reject(new Error('QR library failed to load'));
      };
      document.head.appendChild(script);
    }).catch(error => {
      delete qrLibraryLoads[name];
      throw error;
    });
  }
  return qrLibraryLoads[name];
}

/* Include the white quiet zone in the actual PNG, including saved/printed labels.
 * Render at high resolution so printing doesn't magnify a 176px bitmap. */
function renderQrCode(container, QRCode, text) {
  const source = document.createElement('div');
  new QRCode(source, { text, width: 512, height: 512,
    colorDark: '#000000', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 640;
  canvas.style.width = canvas.style.height = '176px';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', '二维码');
  const context = canvas.getContext('2d');
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, 640, 640);
  context.drawImage(source.querySelector('canvas'), 64, 64);
  container.replaceChildren(canvas);
}
