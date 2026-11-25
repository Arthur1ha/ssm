# qr-scanner 1.4.2

Unmodified browser module and Worker wrapper from the official npm package:
https://registry.npmjs.org/qr-scanner/-/qr-scanner-1.4.2.tgz

Upstream: https://github.com/nimiq/qr-scanner
License: MIT; see [LICENSE](LICENSE).

The archive was checked against npm's SHA-512 integrity value:

```
sha512-kV1yQUe2FENvn59tMZW6mOVfpq9mGxGf8l6+EGaXUOd4RBOLg7tRC83OrirM5AtDvZRpdjdlXURsHreAOSPOUw==
```

Keep `qr-scanner.min.js` and `qr-scanner-worker.min.js` together: the module
dynamically imports the adjacent Worker wrapper when native detection is
unavailable. The wrapper creates a Blob Worker; a deployment that sets CSP
must permit it with `worker-src blob:`. No CDN, package install or build step
is required at runtime. Source maps are omitted.
