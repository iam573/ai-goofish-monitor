// Read the existing WDA on the requested phone. Never create/delete a session.
const http = require('node:http');
const {forwardDevicePort} = require('./preinstalled_wda.cjs');
const [root, udid, port] = process.argv.slice(2);
let forward;
const watchdog = setTimeout(() => process.exit(2), 15000);
(async () => {
  forward = await forwardDevicePort(root, udid, port);
  const data = await new Promise((resolve, reject) => {
    const request = http.get({host: '127.0.0.1', port: forward.port,
      path: '/screenshot', agent: false}, response => {
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 24 * 1024 * 1024) response.destroy(new Error('response too large'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('aborted', () => reject(new Error('response interrupted')));
      response.on('end', () => {
        try {
          const payload = JSON.parse(Buffer.concat(chunks).toString());
          if (response.statusCode !== 200 || typeof payload.value !== 'string') throw new Error('capture failed');
          resolve(Buffer.from(payload.value, 'base64'));
        } catch (error) { reject(error); }
      });
    });
    request.on('error', reject);
    request.setTimeout(12000, () => request.destroy(new Error('capture timeout')));
  });
  process.stdout.write(data);
})().catch(() => {
  process.stderr.write('WDA_SCREENSHOT_UNAVAILABLE\n');
  process.exitCode = 1;
}).finally(async () => {
  clearTimeout(watchdog);
  await forward?.close();
});
