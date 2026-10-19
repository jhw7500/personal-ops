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

test('opt-in active arrivals launch in supplied order while historical idle actors are placed', () => {
  const {create, WORLD, slot} = require(motionPath);
  const engine = create();
  const first = actor('job:entry-first', 'coding', 'running');
  const second = actor('job:entry-second', 'review', 'running');
  const historical = persistentActor('agent:historical');

  engine.sync([first, second, historical], {initial: true, staggerEntries: true});

  assert.deepEqual(
    {x: byId(engine, first.id).x, y: byId(engine, first.id).y,
      walking: byId(engine, first.id).walking, entryPending: byId(engine, first.id).entryPending},
    {...WORLD.entry, walking: true, entryPending: undefined},
  );
  assert.deepEqual(
    {x: byId(engine, second.id).x, y: byId(engine, second.id).y,
      walking: byId(engine, second.id).walking, entryPending: byId(engine, second.id).entryPending},
    {...WORLD.entry, walking: false, entryPending: true},
  );
  assert.deepEqual(
    {x: byId(engine, historical.id).x, y: byId(engine, historical.id).y,
      walking: byId(engine, historical.id).walking, entryPending: byId(engine, historical.id).entryPending},
    {...slot('waiting', 0), walking: false, entryPending: undefined},
  );

  engine.advance(0.39);
  assert.equal(byId(engine, second.id).entryPending, true);
  engine.advance(0.02);
  assert.equal(byId(engine, second.id).entryPending, undefined);
  assert.equal(byId(engine, second.id).walking, true);
});

test('identical polling does not reorder or restart pending staggered arrivals', () => {
  const {create} = require(motionPath);
  const engine = create();
  const first = actor('job:stable-first', 'analysis', 'running');
  const second = actor('job:stable-second', 'analysis', 'running');
  const third = actor('job:stable-third', 'analysis', 'running');

  engine.sync([first, second, third], {staggerEntries: true});
  engine.advance(0.25);
  engine.sync([
    {...third, label: 'third refreshed'},
    {...second, label: 'second refreshed'},
    {...first, label: 'first refreshed'},
  ], {staggerEntries: true});
  engine.advance(0.16);

  assert.equal(byId(engine, second.id).entryPending, undefined);
  assert.equal(byId(engine, second.id).walking, true);
  assert.equal(byId(engine, third.id).entryPending, true);
  assert.equal(byId(engine, third.id).actor.label, 'third refreshed');

  engine.advance(0.4);
  assert.equal(byId(engine, third.id).entryPending, undefined);
  assert.equal(byId(engine, third.id).walking, true);
});

test('a full room admits a replacement only after the departing occupant reaches the corridor', () => {
  const {create} = require(motionPath);
  const engine = create();
  const residents = Array.from({length: 6}, (_, index) =>
    actor(`job:resident-${index}`, 'coding', 'running'));
  const replacement = actor('job:replacement', 'coding', 'running');

  engine.sync(residents, {initial: true});
  engine.depart([residents[0].id]);
  engine.sync([...residents.slice(1), replacement], {staggerEntries: true});
  assert.equal(byId(engine, replacement.id).entryPending, true);

  let admittedWhileDepartingInside = false;
  let maximumOccupancy = 0;
  for (let index = 0; index < 1000 && byId(engine, replacement.id).entryPending; index += 1) {
    engine.advance(0.01);
    const snapshot = engine.snapshot();
    const leaving = byId(engine, residents[0].id);
    const incoming = byId(engine, replacement.id);
    if (leaving && leaving.displayRoom === 'coding' && !incoming.entryPending) {
      admittedWhileDepartingInside = true;
    }
    maximumOccupancy = Math.max(maximumOccupancy,
      snapshot.filter(item => item.displayRoom === 'coding').length);
  }

  assert.equal(admittedWhileDepartingInside, false);
  assert.equal(maximumOccupancy, 6);
  assert.equal(byId(engine, replacement.id).entryPending, undefined);
  assert.equal(byId(engine, replacement.id).walking, true);

  for (let index = 0; index < 1000 && byId(engine, replacement.id).walking; index += 1) {
    engine.advance(0.01);
    maximumOccupancy = Math.max(maximumOccupancy,
      engine.snapshot().filter(item => item.displayRoom === 'coding').length);
  }
  assert.equal(maximumOccupancy, 6);
});

test('a newly observed idle call waits outside a full waiting room until its replacement exits', () => {
  const {create, WORLD} = require(motionPath);
  const engine = create();
  const residents = Array.from({length: 6}, (_, index) =>
    persistentActor(`agent:waiting-${index}`));
  const completedCall = persistentActor('agent:completed-before-observation');

  engine.sync(residents, {initial: true, staggerEntries: true});
  engine.depart([residents[0].id]);
  engine.sync([...residents.slice(1), completedCall], {staggerEntries: true});

  assert.deepEqual(
    {x: byId(engine, completedCall.id).x, y: byId(engine, completedCall.id).y,
      walking: byId(engine, completedCall.id).walking,
      entryPending: byId(engine, completedCall.id).entryPending},
    {...WORLD.entry, walking: false, entryPending: true},
  );

  let maximumOccupancy = 0;
  let admittedWhileDepartingInside = false;
  for (let index = 0; index < 1000 && byId(engine, completedCall.id).entryPending; index += 1) {
    engine.advance(0.01);
    const leaving = byId(engine, residents[0].id);
    const incoming = byId(engine, completedCall.id);
    if (leaving && leaving.displayRoom === 'waiting' && !incoming.entryPending) {
      admittedWhileDepartingInside = true;
    }
    maximumOccupancy = Math.max(maximumOccupancy,
      engine.snapshot().filter(item => item.displayRoom === 'waiting').length);
  }

  assert.equal(admittedWhileDepartingInside, false);
  assert.equal(maximumOccupancy, 6);
  assert.equal(byId(engine, completedCall.id).entryPending, undefined);
  assert.equal(byId(engine, completedCall.id).walking, true);
});

test('an active arrival that completes while queued defers its recorded work until admission', () => {
  const {create, WORLD, slot} = require(motionPath);
  const engine = create();
  const leading = actor('job:leading-arrival', 'coding', 'running');
  const queued = persistentActor('agent:fast-call', 'running', 'analysis');
  const completed = {...queued, state: 'idle', room: 'waiting', sourceRoom: 'waiting'};
  const replayTask = completedTask('task:fast-call', queued.id, {sourceRoom: 'analysis'});

  engine.sync([leading, queued], {staggerEntries: true});
  assert.equal(byId(engine, queued.id).entryPending, true);
  engine.advance(0.2);
  engine.sync([leading, completed], {staggerEntries: true, replayTasks: [replayTask]});

  assert.deepEqual(
    {x: byId(engine, queued.id).x, y: byId(engine, queued.id).y,
      walking: byId(engine, queued.id).walking,
      replaying: byId(engine, queued.id).replaying,
      entryPending: byId(engine, queued.id).entryPending},
    {...WORLD.entry, walking: false, replaying: false, entryPending: true},
  );
  assert.equal(byId(engine, queued.id).actor.state, 'idle');

  engine.advance(0.21);
  const admitted = byId(engine, queued.id);
  assert.equal(admitted.entryPending, undefined);
  assert.equal(admitted.walking, true);
  assert.equal(admitted.replaying, true);
  assert.equal(admitted.replayTask.id, replayTask.id);

  const observed = [];
  for (let index = 0; index < 3000 && byId(engine, queued.id).replaying; index += 1) {
    engine.advance(0.01);
    observed.push(byId(engine, queued.id));
  }
  assert.ok(observed.some(item => item.displayRoom === 'analysis'));
  assert.ok(observed.some(item => item.displayRoom === 'done'));
  assert.deepEqual(
    {x: byId(engine, queued.id).x, y: byId(engine, queued.id).y,
      displayRoom: byId(engine, queued.id).displayRoom,
      replaying: byId(engine, queued.id).replaying,
      replayTask: byId(engine, queued.id).replayTask},
    {...slot('waiting', 0), displayRoom: 'waiting', replaying: false, replayTask: null},
  );
});

test('non-animated sync and settle reveal queued arrivals immediately and discard departures', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const first = actor('job:reduced-first', 'testing', 'running');
  const second = actor('job:reduced-second', 'testing', 'running');

  engine.sync([first, second], {staggerEntries: true});
  assert.equal(byId(engine, second.id).entryPending, true);
  engine.sync([first, second], {animate: false, staggerEntries: true});
  assert.deepEqual(engine.snapshot().map(item => ({id: item.id, x: item.x, y: item.y,
    walking: item.walking, entryPending: item.entryPending})), [
    {id: first.id, ...slot('testing', 0), walking: false, entryPending: undefined},
    {id: second.id, ...slot('testing', 1), walking: false, entryPending: undefined},
  ]);

  engine.depart([first.id]);
  const third = actor('job:settle-third', 'review', 'running');
  const fourth = actor('job:settle-fourth', 'review', 'running');
  engine.sync([second, third, fourth], {staggerEntries: true});
  assert.equal(byId(engine, fourth.id).entryPending, true);
  engine.settle();
  assert.equal(byId(engine, first.id), undefined);
  assert.deepEqual(
    {x: byId(engine, fourth.id).x, y: byId(engine, fourth.id).y,
      walking: byId(engine, fourth.id).walking, entryPending: byId(engine, fourth.id).entryPending},
    {...slot('review', 1), walking: false, entryPending: undefined},
  );
});

test('clear resets stagger timing for the next observation', () => {
  const {create, WORLD} = require(motionPath);
  const engine = create();
  const first = actor('job:before-clear-first', 'analysis', 'running');
  const second = actor('job:before-clear-second', 'analysis', 'running');

  engine.sync([first, second], {staggerEntries: true});
  engine.advance(0.2);
  engine.clear();

  const after = actor('job:after-clear', 'coding', 'running');
  engine.sync([after], {staggerEntries: true});
  assert.deepEqual(
    {x: byId(engine, after.id).x, y: byId(engine, after.id).y,
      walking: byId(engine, after.id).walking, entryPending: byId(engine, after.id).entryPending},
    {...WORLD.entry, walking: true, entryPending: undefined},
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

test('departure holds its result, exits through the corridor, and disappears at the entry', () => {
  const {create, WORLD} = require(motionPath);
  const engine = create();
  const leaving = actor('job:depart', 'coding', 'succeeded');

  engine.sync([leaving], {initial: true});
  const started = engine.depart([leaving.id]);
  assert.equal(started[0].departing, true);
  assert.equal(started[0].walking, false);
  assert.equal(started[0].replaying, false);
  assert.equal(started[0].replayTask, null);

  engine.advance(0.99);
  assert.equal(byId(engine, leaving.id).walking, false);
  engine.advance(0.02);
  assert.equal(byId(engine, leaving.id).walking, true);

  const observed = [];
  for (let index = 0; index < 1000 && byId(engine, leaving.id); index += 1) {
    observed.push(byId(engine, leaving.id));
    engine.advance(0.01);
  }
  assert.ok(observed.some(item => item.displayRoom === null && item.y === WORLD.hallY));
  assert.ok(observed.at(-1).x > WORLD.entry.x);
  assert.equal(byId(engine, leaving.id), undefined);
});

test('sync retains an absent departing actor until its exit completes', () => {
  const {create} = require(motionPath);
  const engine = create();
  const leaving = actor('job:retained', 'review', 'succeeded');

  engine.sync([leaving], {initial: true});
  engine.depart([leaving.id]);
  engine.sync([]);

  assert.equal(byId(engine, leaving.id).departing, true);
  engine.advance(1000);
  assert.deepEqual(engine.snapshot(), []);
});

test('the same incoming ID preempts departure once and preserves its current position', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const leaving = actor('job:relaunched', 'analysis', 'succeeded');
  const relaunched = {...leaving, state: 'running', time: '2026-10-12T00:00:03.000Z'};

  engine.sync([leaving], {initial: true});
  engine.depart([leaving.id]);
  engine.advance(0.4);
  const held = byId(engine, leaving.id);
  engine.sync([relaunched, {...relaunched, label: 'duplicate'}]);

  const current = engine.snapshot();
  assert.equal(current.length, 1);
  assert.equal(Object.hasOwn(current[0], 'departing'), false);
  assert.deepEqual({x: current[0].x, y: current[0].y}, {x: held.x, y: held.y});
  assert.equal(current[0].actor.label, relaunched.label);
  engine.advance(1000);
  assert.deepEqual(
    {x: byId(engine, leaving.id).x, y: byId(engine, leaving.id).y},
    slot('analysis', 0),
  );
});

test('repeated and invalid departures do not restart the hold or duplicate actors', () => {
  const {create} = require(motionPath);
  const engine = create();
  const leaving = actor('job:idempotent', 'testing', 'succeeded');

  engine.sync([leaving], {initial: true});
  engine.depart([leaving.id]);
  engine.advance(0.6);
  const before = byId(engine, leaving.id);
  engine.depart([null, '', 'missing', leaving.id, leaving.id]);
  const after = byId(engine, leaving.id);
  assert.deepEqual(after, before);

  engine.advance(0.41);
  assert.equal(byId(engine, leaving.id).walking, true);
  assert.equal(engine.snapshot().length, 1);
});

test('non-animated departure, settle, and clear remove departing actors cleanly', () => {
  const {create, slot} = require(motionPath);
  const engine = create();
  const immediate = actor('job:immediate', 'coding', 'succeeded');
  const settling = actor('job:settling', 'review', 'succeeded');
  const staying = actor('job:staying', 'analysis', 'running');

  engine.sync([immediate, settling, staying], {initial: true});
  engine.depart([immediate.id]);
  engine.depart([immediate.id], {animate: false});
  assert.equal(byId(engine, immediate.id), undefined);

  engine.depart([settling.id]);
  engine.settle();
  assert.equal(byId(engine, settling.id), undefined);
  assert.deepEqual(
    {x: byId(engine, staying.id).x, y: byId(engine, staying.id).y},
    slot('analysis', 0),
  );

  engine.depart([staying.id]);
  engine.clear();
  assert.deepEqual(engine.snapshot(), []);
});

test('departure cancels replay metadata without changing the legacy replay contract', () => {
  const {create} = require(motionPath);
  const engine = create();
  const record = actor('call:depart-replay', 'done', 'returned', 'analysis');

  engine.sync([record], {initial: true});
  engine.replay([record.id]);
  assert.equal(byId(engine, record.id).replaying, true);

  engine.depart([record.id]);
  const leaving = byId(engine, record.id);
  assert.equal(leaving.departing, true);
  assert.equal(leaving.replaying, false);
  assert.equal(leaving.replayTask, null);

  engine.replay([record.id]);
  assert.equal(byId(engine, record.id).replaying, false);
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
