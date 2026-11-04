// Shared USB forwarding for WDA sessions and read-only screenshot requests.
const net = require('node:net');
const path = require('node:path');
async function forwardDevicePort(root, udid, remotePort) {
  const {utilities} = require(require.resolve('appium-ios-device', {
    paths: [path.join(root, '.appium/node_modules/appium-xcuitest-driver')],
  }));
  const sockets = new Set();
  const server = net.createServer(async local => {
    sockets.add(local);
    local.on('error', () => {});
    local.on('close', () => sockets.delete(local));
    try {
      const remote = await utilities.connectPort(udid, Number(remotePort));
      if (local.destroyed) { remote.destroy(); return; }
      sockets.add(remote);
      remote.on('error', () => local.destroy());
      remote.on('close', () => { sockets.delete(remote); local.destroy(); });
      local.on('close', () => remote.destroy());
      local.pipe(remote);
      remote.pipe(local);
    } catch (_) { local.destroy(); }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return {
    port: server.address().port,
    close() {
      for (const socket of sockets) socket.destroy();
      return new Promise(resolve => server.close(resolve));
    },
  };
}
module.exports = {forwardDevicePort};
if (require.main === module) {
  forwardDevicePort(...process.argv.slice(2)).then(forward => {
    process.stdout.write(JSON.stringify({port: forward.port}) + '\n');
    const stop = () => forward.close().then(() => process.exit(0));
    process.on('SIGTERM', stop);
    process.on('SIGINT', stop);
    process.stdin.resume();
    process.stdin.on('end', stop);
  }).catch(() => process.exit(1));
}
