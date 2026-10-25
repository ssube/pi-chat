// Capture only BEL; preserve the test runner's ordinary stdout.
module.exports = async function captureBells(run) {
  let count = 0;
  const oldWrite = process.stdout.write;
  const tty = Object.getOwnPropertyDescriptor(process.stdout, 'isTTY');
  Object.defineProperty(process.stdout, 'isTTY', { configurable: true, value: true });
  process.stdout.write = function(value, ...args) {
    if (value === '\x07') { count++; return true; }
    return oldWrite.call(this, value, ...args);
  };
  try { return await run(() => count); }
  finally {
    process.stdout.write = oldWrite;
    if (tty) Object.defineProperty(process.stdout, 'isTTY', tty);
    else delete process.stdout.isTTY;
  }
};
