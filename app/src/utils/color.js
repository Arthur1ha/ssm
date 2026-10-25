/* Small HSV helpers shared by the phone color picker and its checks. */
(function () {
  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function rgbToHsv(r, g, b) {
    r = clamp(Number(r) / 255, 0, 1);
    g = clamp(Number(g) / 255, 0, 1);
    b = clamp(Number(b) / 255, 0, 1);
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const delta = max - min;
    let h = 0;

    if (delta) {
      if (max === r) h = 60 * (((g - b) / delta) % 6);
      else if (max === g) h = 60 * ((b - r) / delta + 2);
      else h = 60 * ((r - g) / delta + 4);
    }
    if (h < 0) h += 360;
    return { h, s: max === 0 ? 0 : delta / max, v: max };
  }

  function hsvToRgb(h, s) {
    h = ((Number(h) % 360) + 360) % 360;
    s = clamp(Number(s), 0, 1);
    const chroma = s;
    const x = chroma * (1 - Math.abs((h / 60) % 2 - 1));
    let r = 0, g = 0, b = 0;
    if (h < 60) [r, g, b] = [chroma, x, 0];
    else if (h < 120) [r, g, b] = [x, chroma, 0];
    else if (h < 180) [r, g, b] = [0, chroma, x];
    else if (h < 240) [r, g, b] = [0, x, chroma];
    else if (h < 300) [r, g, b] = [x, 0, chroma];
    else [r, g, b] = [chroma, 0, x];
    const m = 1 - chroma;
    return {
      r: Math.round((r + m) * 255),
      g: Math.round((g + m) * 255),
      b: Math.round((b + m) * 255),
    };
  }

  window.ssmColor = { rgbToHsv, hsvToRgb };
})();
