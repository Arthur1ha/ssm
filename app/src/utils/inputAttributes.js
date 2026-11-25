/* Shared browser input hints. Autofill is separate from keyboard layout;
 * the browser or installed keyboard may still offer its own suggestions. */
(function () {
  const text = { autoComplete: 'off', inputMode: 'text', enterKeyHint: 'done' };
  window.ssmInputAttributes = Object.freeze({
    form: Object.freeze({ autoComplete: 'off' }),
    text: Object.freeze(text),
    chat: Object.freeze({ ...text, enterKeyHint: 'send' }),
    multiline: Object.freeze({ ...text, enterKeyHint: 'enter' }),
    url: Object.freeze({ ...text, inputMode: 'url', enterKeyHint: 'go' }),
    numeric: Object.freeze({ ...text, inputMode: 'numeric' }),
    decimal: Object.freeze({ ...text, inputMode: 'decimal' }),
  });
})();
