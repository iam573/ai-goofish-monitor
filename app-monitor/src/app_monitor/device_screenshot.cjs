// Read the existing WDA on the requested phone. Never create/delete a session.
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const [root, udid, port] = process.argv.slice(2);
const sockets = new Set();
let server;
const watchdog = setTimeout(() => process.exit(2), 15000);
(async () => {
  const {utilities} = require(require.resolve('appium-ios-device', {
    paths: [path.join(root, '.appium/node_modules/appium-xcuitest-driver')],
  }));
  // Use the same device transport as Appium, without reusing its mutable driver.
  server = net.createServer(async local => {
    sockets.add(local);
    local.on('error', () => {});
    try {
      const remote = await utilities.connectPort(udid, Number(port));
      sockets.add(remote);
      remote.on('error', () => local.destroy());
      remote.on('close', () => local.destroy());
      local.on('close', () => remote.destroy());
      local.pipe(remote);
      remote.pipe(local);
    } catch (_) { local.destroy(); }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const data = await new Promise((resolve, reject) => {
    const request = http.get({host: '127.0.0.1', port: server.address().port,
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
}).finally(() => {
  clearTimeout(watchdog);
  for (const socket of sockets) socket.destroy();
  server?.close();
});
