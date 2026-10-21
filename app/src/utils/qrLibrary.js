/* Load QR helpers only when the matching UI is opened. Share concurrent loads. */
const qrLibraryLoads = {};

function loadQrLibrary(name) {
  const libraries = {
    scan: ['jsQR', '/src/utils/jsQR.vendor.js'],
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
