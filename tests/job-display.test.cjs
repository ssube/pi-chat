const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const jiti = createJiti(__filename);
const { jobStatusText, humanBytes, humanCpuTime } = jiti('../extensions/lib/job-display.ts');
const { boundedMenuComponent } = jiti('../extensions/lib/bounded-menu.ts');
const { stripVTControlCharacters } = require('node:util');

test('job states use semantic theme roles and retain visible status text', () => {
  const expected = { running: 'mdLink', stopping: 'warning', deadline_exceeded: 'warning', succeeded: 'success', failed: 'error', timed_out: 'error', cancelled: 'muted', detached: 'warning', unknown: 'warning' };
  for (const [status, role] of Object.entries(expected)) {
    let called;
    const theme = { fg: (color, value) => { called = color; return `\x1b[33m${value}\x1b[0m`; } };
    const colored = jobStatusText(status, theme);
    assert.equal(called, role); assert.equal(stripVTControlCharacters(colored), status);
    const view = boundedMenuComponent('Jobs', [`job-1 · ${colored}`], '', () => 24, () => {}, () => {});
    assert.ok(view.render(80)[1].includes('\x1b[33m'), 'menu preserves job color');
    assert.equal(jobStatusText(status), status, 'unavailable themes do not alter data');
  }
  assert.equal(jobStatusText('future-state', { fg: (color, value) => `${color}:${value}` }), 'warning:future-state');
  assert.equal(jobStatusText('running', undefined, 'activating'), 'activating');
});


test('memory bytes are readable without changing unknown/unavailable into zero', () => {
  for (const [value, expected] of [[0, '0 B'], ['1024', '1 KiB'], [1536, '1.5 KiB'], [1048576, '1 MiB'], ['1073741824', '1 GiB'], [1099511627776, '1 TiB']]) assert.equal(humanBytes(value), expected);
  for (const value of [undefined, null, '', 'unavailable', '[not set]', '-1', -1, NaN, Infinity, {}, '18446744073709551615']) assert.equal(humanBytes(value), 'unavailable');
});

test('CPU nanoseconds are labeled as elapsed CPU time, not percent or bytes', () => {
  for (const [value, expected] of [[0, '0 ms'], ['500000', '0.5 ms'], [1000000, '1 ms'], ['1500000000', '1.5 s'], [60000000000, '1 min'], [5400000000000, '1.5 h']]) assert.equal(humanCpuTime(value), expected);
  for (const value of [undefined, null, '', 'unavailable', '-1', -1, NaN, Infinity, {}, '18446744073709551615']) assert.equal(humanCpuTime(value), 'unavailable');
});
