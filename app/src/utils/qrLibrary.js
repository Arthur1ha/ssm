/* Load QR helpers only when the matching UI is opened. Share concurrent loads. */
const qrLibraryLoads = {};

function loadQrLibrary(name) {
  const libraries = {
    scan: ['jsQR', 'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js'],
    generate: ['QRCode', 'https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js'],
  };
  const library = libraries[name];
  if (!library) return Promise.reject(new Error('Unknown QR library'));
  if (window[library[0]]) return Promise.resolve(window[library[0]]);
  if (!qrLibraryLoads[name]) {
    qrLibraryLoads[name] = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = library[1];
      script.crossOrigin = 'anonymous';
      script.onload = () => window[library[0]]
        ? resolve(window[library[0]])
        : reject(new Error('QR library unavailable'));
      script.onerror = () => reject(new Error('QR library failed to load'));
      document.head.appendChild(script);
    }).catch(error => {
      delete qrLibraryLoads[name];
      throw error;
    });
  }
  return qrLibraryLoads[name];
}
