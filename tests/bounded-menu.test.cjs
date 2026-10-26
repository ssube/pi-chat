const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
// The same contracts can run against the host SDK without installing it into source.
const tuiPath = process.env.PI_MENU_TUI_MODULE ?? require.resolve('@earendil-works/pi-tui', { paths: [path.resolve(__dirname, '../extensions')] });
const jiti = createJiti(__filename, { alias: { '@earendil-works/pi-tui': tuiPath } });
const { boundedMenuComponent, boundedMenu } = jiti('../extensions/lib/bounded-menu.ts');
const { SelectList, visibleWidth, getKeybindings, setKeybindings, KeybindingsManager } = jiti(tuiPath);

function setup(choices, body = '') {
  let rows = 24, result = 'not finished', redraws = 0;
  const view = boundedMenuComponent('A long title '.repeat(30), choices, body,
    () => rows, value => { result = value; }, () => redraws++);
  return { view, rows: value => { rows = value; }, result: () => result, redraws: () => redraws };
}

test('100 long Unicode choices stay within width/height; every choice is keyboard reachable', () => {
  const choices = Array.from({ length: 100 }, (_, i) => `${i}: 界界 long label `.repeat(10));
  const s = setup(choices);
  for (const rows of [8, 12, 24, 40]) {
    s.rows(rows);
    for (const width of [20, 40, 80]) {
      const lines = s.view.render(width);
      assert.ok(lines.length <= rows - 4, `${lines.length} lines at ${rows} rows`);
      assert.ok(lines.every(line => visibleWidth(line) <= width));
      assert.match(lines.at(-1), /escape back/);
    }
  }
  for (let i = 0; i < 99; i++) s.view.handleInput('\x1b[B');
  assert.match(s.view.render(80).join('\n'), /100\/100/);
  s.view.handleInput('\r');
  assert.equal(s.result(), choices[99]);
});

test('details scroll independently, retain actions and clamp after resize', () => {
  const body = Array.from({ length: 80 }, (_, i) => `Detail line ${i} ` + '界'.repeat(25)).join('\n');
  const s = setup(['Back', 'Send log tail', 'Cancel job'], body);
  for (const rows of [8, 12, 24, 40]) {
    s.rows(rows);
    for (const width of [20, 40, 80]) {
      const lines = s.view.render(width);
      assert.ok(lines.length <= rows - 4);
      assert.ok(lines.every(line => visibleWidth(line) <= width));
      assert.ok(lines.some(line => line.includes('→ Back')));
    }
  }
  for (let i = 0; i < 100; i++) { s.view.handleInput('\x1b[6~'); s.view.render(80); }
  const bottom = s.view.render(80).join('\n');
  assert.match(bottom, /Detail line 79/);
  assert.match(bottom, /→ Back/);
  for (let i = 0; i < 100; i++) s.view.handleInput('\x1b[5~');
  assert.match(s.view.render(80).join('\n'), /Detail line 0/);
  s.view.handleInput('\x1b');
  assert.equal(s.result(), undefined);
});

test('control sequences cannot affect the terminal; empty widths and lists are safe', () => {
  const s = setup(['Bad\x1b[2J\x07label'], 'Body\x1b[31m\x07text');
  assert.deepEqual(s.view.render(0), []);
  assert.ok(s.view.render(80).every(line => !/[\x00-\x1f\x7f]/.test(line.replace(/\x1b\[[0-9;:]*m/g, ''))));
  assert.ok(s.view.render(80).every(line => !line.includes('[2J')), 'unsafe CSI is removed completely');
  const empty = setup([]);
  empty.view.handleInput('\r');
  assert.equal(empty.result(), 'not finished', 'native empty list does not activate an item');
});

test('custom adapter follows live terminal height on resize', async () => {
  let tui = { terminal: { rows: 24 }, requestRender() {} };
  const ctx = { ui: { custom: factory => factory(tui, {}, {}, () => {}) } };
  const view = await boundedMenu(ctx, 'Jobs', Array.from({ length: 100 }, (_, i) => String(i)));
  const large = view.render(80);
  tui.terminal.rows = 8;
  const small = view.render(80);
  assert.ok(small.length < large.length);
  assert.ok(small.length <= 4);
});

test('host navigation/page bindings win over mnemonic keys and are shown in help', () => {
  const keys = new KeybindingsManager({
    'tui.select.up': { defaultKeys: 'u' }, 'tui.select.down': { defaultKeys: 'a' },
    'tui.select.confirm': { defaultKeys: 'v' }, 'tui.select.cancel': { defaultKeys: 'b' },
    'tui.select.pageUp': { defaultKeys: 'h' }, 'tui.select.pageDown': { defaultKeys: 'l' },
  });
  const previous = getKeybindings(); setKeybindings(keys);
  try {
    const choices = Array.from({ length: 100 }, (_, i) => `item ${i}`);
    let result;
    const view = boundedMenuComponent('Menu', choices, '', () => 24, value => { result = value; }, () => {},
      { shortcuts: { a: 'Answer' } });
    view.render(80); view.handleInput('a');
    assert.equal(result, undefined, 'configured Down beats action mnemonic a');
    assert.equal(view.list.getSelectedItem().value, 'item 1');
    view.handleInput('l'); assert.equal(view.list.getSelectedItem().value, 'item 11');
    view.handleInput('h'); assert.equal(view.list.getSelectedItem().value, 'item 1');
    view.handleInput('?'); assert.match(view.render(100).join('\n'), /u: previous item/);
    view.handleInput('v'); assert.equal(result, 'item 1');
    view.handleInput('b'); assert.equal(result, undefined);
  } finally { setKeybindings(previous); }
});

test('q closes the whole menu, Esc returns one level, and action mnemonics remain menu-local', () => {
  const { MENU_CLOSE } = jiti('../extensions/lib/bounded-menu.ts');
  let result;
  const view = boundedMenuComponent('Actions', ['Answer', 'Clear'], '', () => 24,
    value => { result = value; }, () => {}, { shortcuts: { a: 'Answer', x: 'Clear' } });
  view.handleInput('a'); assert.equal(result, 'Answer');
  view.handleInput('x'); assert.equal(result, 'Clear');
  view.handleInput('\x1b'); assert.equal(result, undefined);
  view.handleInput('q'); assert.equal(result, MENU_CLOSE);
});

test('an aborted menu settles without activation and disposes its abort listener', async () => {
  const controller = new AbortController(); let value = 'waiting', disposed;
  const ctx = { ui: { custom: factory => {
    const view = factory({ terminal: { rows: 24 }, requestRender() {} }, {}, {}, next => { value = next; });
    disposed = () => view.dispose(); return view;
  } } };
  await boundedMenu(ctx, 'Approval', ['Approve', 'Deny'], '', { signal: controller.signal });
  controller.abort(); assert.equal(value, undefined); disposed();
});

test('status colors survive menu sanitization without literal CSI fragments', () => {
  const red = '\x1b[38;2;220;60;60m', reset = '\x1b[0m';
  const view = boundedMenuComponent(`Tasks ${red}[blocked]${reset}`, [`#1 ${red}[blocked]${reset} Example`], '', () => 24, () => {}, () => {});
  const rows = view.render(80);
  assert.ok(rows[0].includes(red), 'heading retains its safe SGR color');
  assert.ok(rows[1].includes(red), 'status retains its safe SGR color');
  const { stripVTControlCharacters } = require('node:util');
  assert.equal(stripVTControlCharacters(rows[1]), '→ #1 [blocked] Example');
  assert.equal(stripVTControlCharacters(rows[1]).includes('[38;2;'), false);
});

test('menu applies supplied theme to selection marker, title and navigation hint', () => {
  const calls = [];
  const theme = { fg: (role, text) => { calls.push(role); return `\x1b[${role === 'dim' ? 90 : 36}m${text}\x1b[0m`; } };
  const view = boundedMenuComponent('Tasks', ['One'], '', () => 24, () => {}, () => {}, { theme });
  const rows = view.render(80);
  assert.ok(calls.includes('accent')); assert.ok(calls.includes('dim'));
  assert.match(rows[1], /\x1b\[36m→ One\x1b\[0m/);
});

test('mnemonics accept encoded keyboard events recognized by the host', () => {
  let result;
  const view = boundedMenuComponent('Tasks', ['One'], '', () => 24, value => { result = value; }, () => {}, { shortcuts: { e: 'Edit' } });
  view.handleInput('\x1b[101u'); assert.equal(result, 'Edit');
  result = undefined; view.handleInput('\x1b[101;1:2u'); assert.equal(result, 'Edit');
});

test('mouse wheel and click retain the old task-list selection behavior within the cap', () => {
  let result;
  const view = boundedMenuComponent('Tasks', ['One', 'Two', 'Three'], '', () => 24, value => { result = value; }, () => {});
  view.render(80);
  assert.equal(typeof view.handleMouse, 'function');
  view.handleMouse({ type: 'wheel', x: 0, y: 1, wheelDelta: 1 });
  assert.equal(view.list.getSelectedItem().value, 'Two');
  view.handleMouse({ type: 'press', button: 'left', x: 0, y: 1 });
  view.handleMouse({ type: 'click', button: 'left', x: 0, y: 1 });
  assert.equal(result, 'One');
});


test('menu composes a native SelectList and delegates rendering/input/mouse without a selection facade', () => {
  const s = setup(['One', 'Two', 'Three']); s.view.render(80);
  assert.ok(s.view.list instanceof SelectList);
  const list = s.view.list;
  let renders = 0, inputs = 0, mouseY;
  const render = list.render.bind(list), input = list.handleInput.bind(list), mouse = list.handleMouse.bind(list);
  list.render = width => { renders++; return render(width); };
  list.handleInput = data => { inputs++; return input(data); };
  list.handleMouse = event => { mouseY = event.y; return mouse(event); };
  s.view.render(80); s.view.handleInput('\x1b[B');
  s.view.handleMouse({ type: 'wheel', x: 0, y: 1, wheelDelta: 1 });
  assert.equal(renders, 1); assert.equal(inputs, 1); assert.equal(mouseY, 0);
  assert.equal(s.view.list.getSelectedItem().value, 'Three');
});

test('native selection wraps in both directions for empty, single and long menus', () => {
  for (const count of [0, 1, 3, 100]) {
    const choices = Array.from({ length: count }, (_, i) => `Item ${i}`);
    const s = setup(choices); s.view.render(80);
    s.view.handleInput('\x1b[A');
    assert.equal(s.view.list.getSelectedItem()?.value, choices.at(-1));
    s.view.handleInput('\x1b[B');
    assert.equal(s.view.list.getSelectedItem()?.value, choices[0]);
    for (let i = 0; i < count; i++) s.view.handleInput('\x1b[B');
    assert.equal(s.view.list.getSelectedItem()?.value, choices[0]);
  }
});

test('resize preserves native item identity, duplicate-value index, and bounded scroll indicator', () => {
  const s = setup(Array(100).fill('Same label'), 'Details');
  s.view.list.setSelectedIndex(67);
  const selected = s.view.list.getSelectedItem();
  for (const rows of [24, 8, 40, 12, 24]) {
    s.rows(rows);
    for (const width of [1, 2, 3, 20, 80]) {
      const lines = s.view.render(width);
      assert.ok(s.view.list instanceof SelectList);
      assert.equal(s.view.list.getSelectedItem(), selected);
      assert.ok(lines.length <= rows - 4);
      assert.ok(lines.every(line => visibleWidth(line) <= width));
    }
    assert.match(s.view.render(80).join('\n'), /68\/100/);
  }
});


test('details-wheel routing and native list hit testing do not activate title/footer/scroll-info rows', () => {
  const choices = Array.from({ length: 100 }, (_, i) => `Action ${i}`);
  const s = setup(choices, Array.from({ length: 60 }, (_, i) => `Detail ${i}`).join('\n'));
  s.view.list.setSelectedIndex(50);
  let lines = s.view.render(80);
  const selected = s.view.list.getSelectedItem();
  s.view.handleMouse({ type: 'wheel', x: 0, y: 1, wheelDelta: 1 });
  lines = s.view.render(80);
  assert.equal(s.view.list.getSelectedItem(), selected);
  assert.equal(lines[1], 'Detail 3');
  for (const y of [0, lines.length - 1, lines.length - 2]) {
    s.view.handleMouse({ type: 'press', button: 'left', x: 0, y });
    s.view.handleMouse({ type: 'click', button: 'left', x: 0, y });
    assert.equal(s.result(), 'not finished');
    assert.equal(s.view.list.getSelectedItem(), selected);
  }
  const listTop = lines.findIndex(line => /^  Action/.test(line));
  assert.ok(listTop > 0);
  const value = lines[listTop].trim();
  s.view.handleMouse({ type: 'press', button: 'left', x: 0, y: listTop });
  s.view.handleMouse({ type: 'click', button: 'left', x: 0, y: listTop });
  assert.equal(s.result(), value);
});

test('help/resize rebuilds preserve selected item and activation returns original, not display-cleaned value', () => {
  const value = '#8 \x1b[33m[blocked]\x1b[0m  Unmodified value';
  const choices = Array.from({ length: 100 }, (_, i) => `Item ${i}`); choices[8] = value;
  const s = setup(choices); s.view.list.setSelectedIndex(8);
  const item = s.view.list.getSelectedItem();
  for (const key of ['?', '\x1b[6~', '?']) {
    s.view.handleInput(key); s.view.render(80);
    assert.equal(s.view.list.getSelectedItem(), item);
  }
  s.view.handleInput('\r'); assert.equal(s.result(), value);
  const controls = setup(['\x1b[2J', '\x07']);
  assert.ok(controls.view.render(80).every(line => !/[\x00-\x1f\x7f]/.test(line.replace(/\x1b\[[0-9;:]*m/g, ''))));
});

const themePath = process.env.PI_MENU_THEME_MODULE ?? path.resolve(__dirname, 'pi-sdk/node_modules/@earendil-works/pi-coding-agent/dist/modes/interactive/theme/theme.js');
test('real system/dark/light themes preserve semantic status SGR through native selected-row rendering', {
  skip: !require('node:fs').existsSync(themePath),
}, async () => {
  const { getThemeByName } = await import(require('node:url').pathToFileURL(themePath).href);
  const { stripVTControlCharacters } = require('node:util');
  for (const name of ['system', 'dark', 'light']) {
    const theme = getThemeByName(name); assert.ok(theme, name);
    const colors = [];
    for (const [role, state] of [['muted', 'pending'], ['accent', 'progress'], ['warning', 'blocked'], ['success', 'complete']]) {
      const styled = theme.fg(role, `[${state}]`); colors.push(styled.split('[' + state + ']')[0]);
      const view = boundedMenuComponent('Tasks', [`#1 ${styled} Example`], '', () => 24, () => {}, () => {}, { theme });
      const rows = view.render(80);
      assert.ok(rows[1].includes(styled), `${name}/${state}: original semantic SGR retained`);
      assert.equal(rows[1], view.list.render(80)[0], 'shell preserves actual native row');
      assert.equal(stripVTControlCharacters(rows[1]), `→ #1 [${state}] Example`);
    }
    assert.equal(new Set(colors).size, 4, `${name}: distinct foreground sequences`);
  }
});
