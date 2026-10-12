'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const motionPath = path.join(__dirname, '..', 'motion.js');

function actor(id, room, state, sourceRoom = room, overrides = {}) {
  return {
    id,
    kind: id.startsWith('call:') ? 'call' : 'job',
    room,
    sourceRoom,
    state,
    label: id,
    time: '2026-10-12T00:00:00.000Z',
    jobId: null,
    tool: null,
    ...overrides,
  };
}

function persistentActor(id, state = 'idle', room = 'waiting', overrides = {}) {
  return actor(id, room, state, overrides.sourceRoom || room, {
    kind: 'agent',
    taskId: null,
    ...overrides,
  });
}

function completedTask(id, actorId, overrides = {}) {
  return {
    id,
    actorId,
    state: 'succeeded',
    sourceRoom: 'coding',
    room: 'done',
    action: 'code',
    label: id,
    time: '2026-10-12T00:00:01.000Z',
    ...overrides,
  };
}

function byId(engine, id) {
  return engine.snapshot().find(item => item.id === id);
}

function advanceUntil(engine, predicate, limit = 1000) {
  const observed = [];
  for (let index = 0; index < limit; index += 1) {
    engine.advance(0.01);
    const current = engine.snapshot()[0];
    observed.push(current);
    if (predicate(current)) return observed;
  }
  assert.fail('motion did not reach the expected state');
}

test('exports the same motion API through CommonJS and a browser global', () => {
  const commonJs = require(motionPath);
  const source = fs.readFileSync(motionPath, 'utf8');
  const context = {window: {}};
  vm.runInNewContext(source, context, {filename: motionPath});

  assert.deepEqual(Object.keys(commonJs).sort(), ['ROOMS', 'WORLD', 'create', 'slot']);
  assert.equal(typeof commonJs.create, 'function');
  assert.equal(typeof context.window.WorkDashboardMotion.create, 'function');
});

test('initial sync and terminal history place actors at stable bounded room slots', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const initial = Array.from({length: 6}, (_, index) =>
    actor(`job:${index}`, 'testing', 'running'));

  engine.sync(initial, {initial: true});
  assert.deepEqual(
    engine.snapshot().map(item => ({id: item.id, x: item.x, y: item.y, walking: item.walking})),
    initial.map((item, index) => ({id: item.id, ...slot('testing', index), walking: false})),
  );
  assert.notDeepEqual(slot('testing', 0), slot('testing', 5));

  const historical = actor('call:old', 'done', 'returned', 'analysis');
  engine.sync([historical]);
  assert.deepEqual(
    {x: byId(engine, historical.id).x, y: byId(engine, historical.id).y, walking: byId(engine, historical.id).walking},
    {...slot('done', 0), walking: false},
  );
});

test('a replayed quick call visits its processing room and keeps its actual terminal state', () => {
  const {create, ROOMS, slot} = require(motionPath);
  const engine = create();
  const record = actor('call:quick', 'done', 'returned', 'analysis');
  const analysisDoor = ROOMS.find(room => room.id === 'analysis').door;

  engine.sync([record], {replayIds: [record.id]});
  const observed = advanceUntil(engine, current => !current.replaying);

  assert.ok(observed.some(item => item.x === analysisDoor.x
    && item.y >= analysisDoor.y && item.y <= 320));
  assert.ok(observed.some(item => item.displayRoom === 'analysis' && !item.walking));
  assert.ok(observed.every(item => item.actor.state === 'returned'));
  assert.deepEqual(
    {x: observed.at(-1).x, y: observed.at(-1).y, displayRoom: observed.at(-1).displayRoom},
    {...slot('done', 0), displayRoom: 'done'},
  );
});

test('a running actor that fails travels through both doors and the corridor', () => {
  const {create, ROOMS} = require(motionPath);
  const engine = create();
  const running = actor('job:run', 'execution', 'running');
  const failed = {...running, room: 'attention', state: 'failed', time: '2026-10-12T00:00:02.000Z'};
  const executionDoor = ROOMS.find(room => room.id === 'execution').door;
  const attentionDoor = ROOMS.find(room => room.id === 'attention').door;

  engine.sync([running], {initial: true});
  engine.sync([failed]);
  const observed = advanceUntil(engine, current => !current.walking);

  assert.ok(observed.some(item => item.x === executionDoor.x
    && item.y >= 320 && item.y <= executionDoor.y));
  assert.ok(observed.some(item => item.y === 320
    && item.x >= executionDoor.x && item.x <= attentionDoor.x));
  assert.ok(observed.some(item => item.x === attentionDoor.x
    && item.y >= 320 && item.y <= attentionDoor.y));
  assert.equal(observed.at(-1).displayRoom, 'attention');
  assert.equal(observed.at(-1).actor.state, 'failed');
});

test('room entry crosses each doorway before moving horizontally inside the foyer', () => {
  const {create, ROOMS} = require(motionPath);
  const engine = create();
  const north = actor('job:north', 'analysis', 'running');
  const south = actor('job:south', 'testing', 'running');
  const northRoom = ROOMS.find(room => room.id === 'analysis');
  const southRoom = ROOMS.find(room => room.id === 'testing');
  const observed = {north: [], south: []};

  engine.sync([north, south]);
  for (let index = 0; index < 1000; index += 1) {
    engine.advance(0.01);
    observed.north.push(byId(engine, north.id));
    observed.south.push(byId(engine, south.id));
    if (!byId(engine, north.id).walking && !byId(engine, south.id).walking) break;
  }

  assert.ok(observed.north.some(item => (item.direction === 'e' || item.direction === 'w')
    && item.y === northRoom.door.y - 24));
  assert.ok(observed.south.some(item => (item.direction === 'e' || item.direction === 'w')
    && item.y === southRoom.door.y + 48));
  assert.equal(observed.north.some(item => (item.direction === 'e' || item.direction === 'w')
    && item.y === northRoom.door.y && item.x !== northRoom.door.x), false);
  assert.equal(observed.south.some(item => (item.direction === 'e' || item.direction === 'w')
    && item.y === southRoom.door.y && item.x !== southRoom.door.x), false);
});

test('unchanged polling updates actor data without resetting in-flight position', () => {
  const {create} = require(motionPath);
  const engine = create();
  const first = actor('job:poll', 'coding', 'running');

  engine.sync([first]);
  engine.advance(0.2);
  const before = byId(engine, first.id);
  engine.sync([{...first, time: '2026-10-12T00:00:10.000Z'}]);
  const after = byId(engine, first.id);

  assert.deepEqual({x: after.x, y: after.y, walking: after.walking},
    {x: before.x, y: before.y, walking: before.walking});
  assert.equal(after.actor.time, '2026-10-12T00:00:10.000Z');
});

test('poll ordering changes do not swap stable slots between unchanged IDs', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const first = actor('job:first', 'coding', 'running');
  const second = actor('job:second', 'coding', 'running');

  engine.sync([first, second], {initial: true});
  engine.sync([
    {...second, time: '2026-10-12T00:00:10.000Z'},
    {...first, time: '2026-10-12T00:00:09.000Z'},
  ]);

  assert.deepEqual(
    {x: byId(engine, first.id).x, y: byId(engine, first.id).y, walking: byId(engine, first.id).walking},
    {...slot('coding', 0), walking: false},
  );
  assert.deepEqual(
    {x: byId(engine, second.id).x, y: byId(engine, second.id).y, walking: byId(engine, second.id).walking},
    {...slot('coding', 1), walking: false},
  );
});

test('motion advances only when advance is called', () => {
  const {create} = require(motionPath);
  const engine = create();
  const moving = actor('job:paused', 'review', 'running');

  engine.sync([moving]);
  engine.advance(0.1);
  const paused = byId(engine, moving.id);
  const later = byId(engine, moving.id);

  assert.deepEqual(later, paused);
});

test('clear removes actors and prevents stale replay after an initial reload', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const record = actor('call:reload', 'done', 'returned', 'coding');

  engine.sync([record], {replayIds: [record.id]});
  engine.advance(0.1);
  engine.clear();
  assert.deepEqual(engine.snapshot(), []);

  engine.sync([record], {initial: true, replayIds: [record.id]});
  assert.deepEqual(
    {x: byId(engine, record.id).x, y: byId(engine, record.id).y, replaying: byId(engine, record.id).replaying},
    {...slot('done', 0), replaying: false},
  );
});

test('settle snaps every route to its latest destination', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const actors = [
    actor('job:a', 'analysis', 'running'),
    actor('job:b', 'coding', 'running'),
  ];

  engine.sync(actors);
  engine.advance(0.1);
  engine.settle();

  assert.deepEqual(engine.snapshot().map(item => ({id: item.id, x: item.x, y: item.y, walking: item.walking})), [
    {id: 'job:a', ...slot('analysis', 0), walking: false},
    {id: 'job:b', ...slot('coding', 0), walking: false},
  ]);
});

test('explicit replay affects only requested existing terminal records', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const first = actor('call:first', 'done', 'returned', 'analysis');
  const second = actor('call:second', 'attention', 'failed', 'coding');

  engine.sync([first, second], {initial: true});
  engine.replay([second.id]);

  assert.deepEqual(
    {x: byId(engine, first.id).x, y: byId(engine, first.id).y, replaying: byId(engine, first.id).replaying},
    {...slot('done', 0), replaying: false},
  );
  assert.equal(byId(engine, second.id).replaying, true);
  assert.equal(byId(engine, second.id).actor.state, 'failed');
});

test('same-source replays form a staggered line along the entry corridor', () => {
  const {create, WORLD} = require(motionPath);
  const engine = create();
  const records = Array.from({length: 3}, (_, index) =>
    actor(`call:line-${index}`, 'done', 'returned', 'analysis'));

  engine.sync(records, {initial: true});
  engine.replay(records.map(record => record.id));
  engine.advance(0.5);

  const moving = engine.snapshot();
  assert.equal(new Set(moving.map(item => `${item.x},${item.y}`)).size, 3);
  assert.ok(moving.every(item => item.y === WORLD.hallY && item.x > WORLD.entry.x));
  assert.deepEqual(moving.map(item => ({room: item.actor.room, sourceRoom: item.actor.sourceRoom,
    state: item.actor.state, replaying: item.replaying})), [
    {room: 'done', sourceRoom: 'analysis', state: 'returned', replaying: true},
    {room: 'done', sourceRoom: 'analysis', state: 'returned', replaying: true},
    {room: 'done', sourceRoom: 'analysis', state: 'returned', replaying: true},
  ]);
});

test('snapshot is defensive and a large delta settles deterministically', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const input = actor('job:defensive', 'waiting', 'queued');

  engine.sync([input]);
  const exposed = byId(engine, input.id);
  exposed.actor.state = 'fabricated';
  exposed.x = 9999;
  input.state = 'also-fabricated';
  engine.advance(1000);

  const final = byId(engine, input.id);
  assert.equal(final.actor.state, 'queued');
  assert.deepEqual({x: final.x, y: final.y, walking: final.walking},
    {...slot('waiting', 0), walking: false});
});

test('an idle persistent actor replays only its latest completed task without changing actor truth', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const worker = persistentActor('agent:stable');
  const working = {...worker, state: 'running', room: 'coding', sourceRoom: 'coding', taskId: 'task:latest'};
  const older = completedTask('task:older', worker.id);
  const latest = completedTask('task:latest', worker.id, {
    sourceRoom: 'analysis',
    action: 'read',
    time: '2026-10-12T00:00:02.000Z',
  });

  engine.sync([working], {initial: true});
  engine.sync([worker], {replayTasks: [older, latest]});
  latest.label = 'mutated after sync';

  const started = byId(engine, worker.id);
  assert.equal(engine.snapshot().length, 1);
  assert.equal(started.actor.id, worker.id);
  assert.equal(started.actor.state, 'idle');
  assert.equal(started.actor.room, 'waiting');
  assert.equal(started.replayTask.id, 'task:latest');
  assert.equal(started.replayTask.label, 'task:latest');
  started.replayTask.label = 'mutated snapshot';
  assert.equal(byId(engine, worker.id).replayTask.label, 'task:latest');

  const observed = advanceUntil(engine, current => !current.replaying, 3000);
  assert.ok(observed.some(item => item.displayRoom === 'analysis'));
  assert.ok(observed.some(item => item.displayRoom === 'done'));
  assert.ok(observed.every(item => item.actor.state === 'idle'));
  assert.deepEqual(
    {x: observed.at(-1).x, y: observed.at(-1).y, displayRoom: observed.at(-1).displayRoom,
      replayTask: observed.at(-1).replayTask},
    {...slot('waiting', 0), displayRoom: 'waiting', replayTask: null},
  );
});

test('recorded work holds its source-room pose long enough to read', () => {
  const {create} = require(motionPath);
  const engine = create();
  const worker = persistentActor('executor:hold', 'idle', 'waiting', {kind: 'executor'});
  const task = completedTask('task:hold', worker.id, {sourceRoom: 'review', action: 'code'});

  engine.sync([worker], {initial: true});
  engine.sync([worker], {replayTasks: [task]});
  advanceUntil(engine, current => current.displayRoom === 'review' && !current.walking);
  engine.advance(0.8);

  const held = byId(engine, worker.id);
  assert.equal(held.displayRoom, 'review');
  assert.equal(held.walking, false);
  assert.equal(held.replayTask.id, task.id);
});

test('recorded replay accepts an explicit reasoning action', () => {
  const {create} = require(motionPath);
  const engine = create();
  const worker = persistentActor('agent:reasoning');
  const task = completedTask('task:reasoning', worker.id, {
    sourceRoom: 'analysis',
    action: 'reason',
  });

  engine.sync([worker], {initial: true});
  engine.sync([worker], {replayTasks: [task]});

  assert.equal(byId(engine, worker.id).replayTask.action, 'reason');
});

test('live work preempts recorded playback even when the actor destination room is unchanged', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const idle = persistentActor('agent:preempt');
  const task = completedTask('task:preempt', idle.id, {sourceRoom: 'analysis'});

  engine.sync([idle], {initial: true});
  engine.sync([idle], {replayTasks: [task]});
  engine.advance(0.4);
  engine.sync([{...idle, state: 'queued'}]);

  const preempted = byId(engine, idle.id);
  assert.equal(preempted.replaying, false);
  assert.equal(preempted.replayTask, null);
  assert.equal(preempted.actor.state, 'queued');
  engine.advance(1000);
  assert.deepEqual(
    {x: byId(engine, idle.id).x, y: byId(engine, idle.id).y,
      displayRoom: byId(engine, idle.id).displayRoom},
    {...slot('waiting', 0), displayRoom: 'waiting'},
  );
});

test('identical polling does not restart or repeat recorded playback', () => {
  const {create} = require(motionPath);
  const engine = create();
  const worker = persistentActor('mcp:poll', 'idle', 'waiting', {kind: 'mcp'});
  const task = completedTask('task:poll', worker.id, {sourceRoom: 'analysis', action: 'read'});

  engine.sync([worker], {initial: true});
  engine.sync([worker], {replayTasks: [task]});
  engine.advance(0.2);
  const before = byId(engine, worker.id);
  engine.sync([{...worker, label: 'fresh poll'}], {replayTasks: [{...task}]});
  const after = byId(engine, worker.id);
  assert.deepEqual({x: after.x, y: after.y, replayTask: after.replayTask.id},
    {x: before.x, y: before.y, replayTask: task.id});

  engine.advance(1000);
  engine.sync([worker], {replayTasks: [task]});
  assert.equal(byId(engine, worker.id).replaying, false);
  assert.equal(byId(engine, worker.id).replayTask, null);
});

test('recorded tasks ignore active, unknown, missing, and ambiguous actor associations', () => {
  const {create} = require(motionPath);
  const engine = create();
  const actors = [
    persistentActor('agent:active', 'running', 'coding'),
    persistentActor('agent:unknown', 'unknown'),
    persistentActor('agent:idle'),
  ];
  const tasks = [
    completedTask('task:active', actors[0].id),
    completedTask('task:unknown', actors[1].id),
    completedTask('task:missing', 'agent:missing'),
    completedTask('task:ambiguous', '', {actorId: undefined}),
    completedTask('task:tie-a', actors[2].id),
    completedTask('task:tie-b', actors[2].id),
    completedTask('task:ineligible', actors[2].id, {
      replayable: false,
      time: '2026-10-12T00:00:03.000Z',
    }),
  ];

  engine.sync(actors, {initial: true});
  engine.sync(actors, {replayTasks: tasks});

  assert.ok(engine.snapshot().every(item => item.replaying === false));
  assert.ok(engine.snapshot().every(item => item.replayTask === null));
});

test('explicit task replay targets an existing idle actor and settle clears replay metadata', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const worker = persistentActor('agent:manual');
  const task = completedTask('task:manual', worker.id, {room: 'attention', state: 'failed'});

  engine.sync([worker], {initial: true});
  engine.replay([worker.id], [task]);
  assert.equal(byId(engine, worker.id).replayTask.id, task.id);

  engine.settle();
  assert.deepEqual(
    {x: byId(engine, worker.id).x, y: byId(engine, worker.id).y,
      replaying: byId(engine, worker.id).replaying, replayTask: byId(engine, worker.id).replayTask},
    {...slot('waiting', 0), replaying: false, replayTask: null},
  );

  engine.replay([task.id], [task]);
  engine.sync([worker], {initial: true, replayTasks: [task]});
  assert.equal(byId(engine, worker.id).replaying, false);
  assert.equal(byId(engine, worker.id).replayTask, null);
});
