'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const pixelsPath = path.join(__dirname, '..', 'pixels.js');
const {drawPerson} = require(pixelsPath);

function canvasRecorder() {
  const raster = Array.from({length: 28}, () => Array(20).fill(null));
  const context = {
    fillStyle: null,
    imageSmoothingEnabled: true,
    clearRect(x, y, width, height) {
      for (let yy = y; yy < y + height; yy += 1) {
        for (let xx = x; xx < x + width; xx += 1) {
          if (raster[yy] && xx >= 0 && xx < 20) raster[yy][xx] = null;
        }
      }
    },
    fillRect(x, y, width, height) {
      for (let yy = y; yy < y + height; yy += 1) {
        for (let xx = x; xx < x + width; xx += 1) {
          if (raster[yy] && xx >= 0 && xx < 20) raster[yy][xx] = this.fillStyle;
        }
      }
    },
  };
  return {
    width: 20,
    height: 28,
    getContext: () => context,
    raster,
  };
}

function render(overrides = {}) {
  const canvas = canvasRecorder();
  drawPerson(canvas, {
    id: 'agent:pixel-test',
    kind: 'agent',
    direction: 's',
    walking: false,
    working: true,
    clock: 0,
    ...overrides,
  });
  return canvas.raster;
}

function serialize(raster) {
  return raster.map(row => row.map(color => color || '.').join(',')).join('\n');
}

function coordinates(raster, color) {
  const result = [];
  raster.forEach((row, y) => row.forEach((pixel, x) => {
    if (pixel === color) result.push(`${x},${y}`);
  }));
  return result;
}

test('each supported work action draws a distinct recognizable prop', () => {
  const accents = {
    read: '#f3df9b',
    search: '#7dd6d0',
    code: '#79d98b',
    build: '#f0a84b',
    test: '#85c7ef',
    review: '#d8a2e8',
    execute: '#65d6a6',
    commit: '#e87878',
    push: '#e6b85c',
    reason: '#ffd966',
  };
  const images = [];

  for (const [action, accent] of Object.entries(accents)) {
    const raster = render({action});
    assert.ok(coordinates(raster, accent).length >= 2, `${action} should expose its prop accent`);
    images.push(serialize(raster));
  }

  assert.equal(new Set(images).size, Object.keys(accents).length);
});

test('idle and walking people do not carry work props', () => {
  assert.equal(
    serialize(render({action: 'read', working: false})),
    serialize(render({action: undefined, working: false})),
  );
  assert.equal(
    serialize(render({action: 'build', walking: true, working: true, clock: 0.2})),
    serialize(render({action: undefined, walking: true, working: true, clock: 0.2})),
  );
  assert.notEqual(
    serialize(render({action: 'build', walking: true, working: true, clock: 0})),
    serialize(render({action: 'build', walking: true, working: true, clock: 0.2})),
  );
});

test('work props remain visible on all four directional sprites', () => {
  const images = ['n', 'e', 's', 'w'].map(direction => {
    const raster = render({action: 'read', direction});
    assert.ok(coordinates(raster, '#f3df9b').length >= 2);
    return serialize(raster);
  });

  assert.equal(new Set(images).size, 4);
});

test('every work prop moves between animation frames', () => {
  const accents = {
    read: '#f3df9b',
    search: '#7dd6d0',
    code: '#79d98b',
    build: '#f0a84b',
    test: '#85c7ef',
    review: '#d8a2e8',
    execute: '#65d6a6',
    commit: '#e87878',
    push: '#e6b85c',
    reason: '#ffd966',
  };

  for (const [action, accent] of Object.entries(accents)) {
    assert.notDeepEqual(
      coordinates(render({action, clock: 0}), accent),
      coordinates(render({action, clock: 0.21}), accent),
      `${action} should animate its prop`,
    );
  }
});

test('reasoning indicator appears only for explicit active reasoning work', () => {
  const active = render({action: 'reason', working: true, walking: false});
  const idle = render({action: 'reason', working: false, walking: false});
  const walking = render({action: 'reason', working: true, walking: true});

  assert.ok(coordinates(active, '#ffd966').length >= 2);
  assert.equal(coordinates(idle, '#ffd966').length, 0);
  assert.equal(coordinates(walking, '#ffd966').length, 0);
});

test('entity kinds keep distinct bags while legacy kinds retain their established colors', () => {
  const colors = Object.fromEntries(['mcp', 'executor', 'agent', 'call', 'job'].map(kind => [
    kind,
    render({kind, working: false})[18][3],
  ]));

  assert.equal(colors.mcp, '#2c9293');
  assert.equal(colors.call, colors.mcp);
  assert.equal(colors.executor, '#c17a2e');
  assert.equal(colors.job, colors.executor);
  assert.notEqual(colors.agent, colors.mcp);
  assert.notEqual(colors.agent, colors.executor);
});

test('drawing a person preserves the caller-owned pose', () => {
  const pose = {
    id: 'mcp:immutable',
    kind: 'mcp',
    direction: 'w',
    walking: false,
    working: true,
    action: 'review',
    clock: 0.21,
  };
  const before = JSON.stringify(pose);

  const canvas = canvasRecorder();
  drawPerson(canvas, pose);

  assert.equal(JSON.stringify(pose), before);
  assert.equal(canvas.width, 20);
  assert.equal(canvas.height, 28);
});
