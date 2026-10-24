(function (root, factory) {
  'use strict';

  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WorkDashboardMotion = api;
}(typeof window === 'object' ? window : null, function () {
  'use strict';

  const WORLD = Object.freeze({
    width: 1024,
    height: 640,
    hallY: 320,
    entry: Object.freeze({x: 12, y: 320}),
  });
  const ROOM_IDS = [
    'waiting', 'analysis', 'coding', 'review',
    'execution', 'testing', 'done', 'attention',
  ];
  const ROOMS = Object.freeze(ROOM_IDS.map((id, index) => {
    const column = index % 4;
    const top = index < 4;
    const x = 24 + 244 * column;
    const y = top ? 48 : 368;
    return Object.freeze({
      id,
      x,
      y,
      width: 232,
      height: 224,
      door: Object.freeze({x: x + 116, y: top ? 272 : 368}),
    });
  }));
  const ROOM_BY_ID = new Map(ROOMS.map(room => [room.id, room]));
  const ACTIVE_STATES = new Set(['started', 'running', 'queued', 'canceling']);
  const TERMINAL_STATES = new Set([
    'returned', 'succeeded', 'failed', 'timed_out', 'canceled', 'interrupted',
  ]);
  const PERSISTENT_KINDS = new Set(['mcp', 'executor', 'agent']);
  const TASK_ACTIONS = new Set([
    'read', 'search', 'code', 'build', 'test', 'review', 'execute', 'commit', 'push',
    'reason',
  ]);
  const TASK_ROOMS = new Set(['analysis', 'coding', 'review', 'execution', 'testing']);
  const TASK_TERMINAL_ROOMS = new Set(['done', 'attention']);
  const SPEED = 210;
  const WORK_BEAT_SECONDS = 0.3;
  const RECORDED_WORK_BEAT_SECONDS = 1;
  const REPLAY_ENTRY_DELAY_SECONDS = 0.16;
  const STAGGER_ENTRY_SECONDS = 0.4;
  const ROOM_CAPACITY = 6;
  const DEPARTURE_HOLD_SECONDS = 1;

  function slot(roomId, index) {
    const room = ROOM_BY_ID.get(roomId);
    if (!room) return null;
    const bounded = Math.max(0, Math.min(5, Number.isInteger(index) ? index : 0));
    return {
      x: room.x + 48 + 68 * (bounded % 3),
      y: room.y + 128 + 62 * Math.floor(bounded / 3),
    };
  }

  function copy(value) {
    if (Array.isArray(value)) return value.map(copy);
    if (!value || typeof value !== 'object') return value;
    const result = {};
    for (const key of Object.keys(value)) result[key] = copy(value[key]);
    return result;
  }

  function addPoint(points, x, y, displayRoom, wait = 0) {
    const previous = points.at(-1);
    if (previous && previous.x === x && previous.y === y) {
      previous.displayRoom = displayRoom;
      previous.wait = Math.max(previous.wait || 0, wait);
      return;
    }
    points.push({x, y, displayRoom, wait});
  }

  function enterRoom(points, roomId, destination, wait = 0) {
    const room = ROOM_BY_ID.get(roomId);
    if (!room || !destination) return;
    const foyerY = room.door.y < WORLD.hallY ? room.door.y - 24 : room.door.y + 48;
    addPoint(points, room.door.x, WORLD.hallY, null);
    addPoint(points, room.door.x, room.door.y, roomId);
    addPoint(points, room.door.x, foyerY, roomId);
    addPoint(points, destination.x, foyerY, roomId);
    addPoint(points, destination.x, destination.y, roomId, wait);
  }

  function leaveRoom(points, roomId, x, y) {
    const room = ROOM_BY_ID.get(roomId);
    if (!room) {
      addPoint(points, x, WORLD.hallY, null);
      return;
    }
    addPoint(points, room.door.x, y, roomId);
    addPoint(points, room.door.x, room.door.y, roomId);
    addPoint(points, room.door.x, WORLD.hallY, null);
  }

  function routeBetween(state, roomId, destination) {
    const points = [];
    if (state.displayRoom) leaveRoom(points, state.displayRoom, state.x, state.y);
    else addPoint(points, state.x, WORLD.hallY, null);
    enterRoom(points, roomId, destination);
    return points;
  }

  function entryRoute(roomId, destination) {
    const points = [];
    enterRoom(points, roomId, destination);
    return points;
  }

  function departureRoute(state) {
    const points = [];
    if (state.displayRoom) leaveRoom(points, state.displayRoom, state.x, state.y);
    else addPoint(points, state.x, WORLD.hallY, null);
    addPoint(points, WORLD.entry.x, WORLD.entry.y, null);
    return points;
  }

  function replayRoute(sourceRoom, sourceDestination, terminalRoom, terminalDestination) {
    const points = [];
    enterRoom(points, sourceRoom, sourceDestination, WORK_BEAT_SECONDS);
    const sourceState = {
      x: sourceDestination.x,
      y: sourceDestination.y,
      displayRoom: sourceRoom,
    };
    points.push(...routeBetween(sourceState, terminalRoom, terminalDestination));
    return points;
  }

  function recordedTaskRoute(state, task, index) {
    const sourceDestination = slot(task.sourceRoom, index);
    const terminalDestination = slot(task.room, index);
    const points = routeBetween(state, task.sourceRoom, sourceDestination);
    if (points.length) points.at(-1).wait = RECORDED_WORK_BEAT_SECONDS;
    points.push(...routeBetween({
      x: sourceDestination.x,
      y: sourceDestination.y,
      displayRoom: task.sourceRoom,
    }, task.room, terminalDestination));
    points.push(...routeBetween({
      x: terminalDestination.x,
      y: terminalDestination.y,
      displayRoom: task.room,
    }, state.destination.room, state.destination));
    return points;
  }

  function taskTimestamp(task) {
    if (!task || typeof task.time !== 'string') return null;
    const milliseconds = Date.parse(task.time);
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }

  function validReplayTask(task) {
    return task && typeof task.id === 'string' && task.id.length > 0
      && typeof task.actorId === 'string' && task.actorId.length > 0
      && TERMINAL_STATES.has(task.state)
      && TASK_ROOMS.has(task.sourceRoom)
      && TASK_TERMINAL_ROOMS.has(task.room)
      && TASK_ACTIONS.has(task.action)
      && task.replayable !== false
      && taskTimestamp(task) !== null;
  }

  function latestTasks(tasks, requested = null) {
    const selected = new Map();
    const seenIds = new Set();
    for (const task of Array.isArray(tasks) ? tasks : []) {
      if (!validReplayTask(task) || seenIds.has(task.id)) continue;
      seenIds.add(task.id);
      if (requested && requested.size > 0
          && !requested.has(task.id) && !requested.has(task.actorId)) continue;
      const milliseconds = taskTimestamp(task);
      const previous = selected.get(task.actorId);
      if (!previous || milliseconds > previous.milliseconds) {
        selected.set(task.actorId, {task, milliseconds, ambiguous: false});
      } else if (milliseconds === previous.milliseconds && task.id !== previous.task.id) {
        previous.ambiguous = true;
      }
    }
    const result = new Map();
    for (const [actorId, candidate] of selected) {
      if (!candidate.ambiguous) result.set(actorId, candidate.task);
    }
    return result;
  }

  function create() {
    const states = new Map();
    const entryQueue = [];
    let schedulerTime = 0;
    let nextEntryAt = 0;

    function removeQueuedEntry(id) {
      const index = entryQueue.indexOf(id);
      if (index >= 0) entryQueue.splice(index, 1);
    }

    function roomOccupancy(roomId) {
      let count = 0;
      for (const state of states.values()) {
        if (state.displayRoom === roomId) count += 1;
      }
      return count;
    }

    function reservedEntries(roomId) {
      let count = 0;
      for (const state of states.values()) {
        if (state.entryAdmissionRoom === roomId) count += 1;
      }
      return count;
    }

    function updateFlags(state) {
      state.walking = state.wait <= 0 && state.route.length > 0;
      if (state.route.length === 0 && state.wait <= 0) {
        state.replaying = false;
        state.replayTask = null;
      }
    }

    function place(state, roomId, destination) {
      state.x = destination.x;
      state.y = destination.y;
      state.displayRoom = roomId;
      state.destination = {room: roomId, x: destination.x, y: destination.y};
      state.route = [];
      state.wait = 0;
      state.walking = false;
      state.replaying = false;
      state.replayTask = null;
      state.departing = false;
      state.entryPending = false;
      state.entryAdmissionRoom = null;
      state.deferredReplayTask = null;
    }

    function queueEntry(state) {
      if (!state.entryPending) entryQueue.push(state.actor.id);
      state.entryPending = true;
      state.entryAdmissionRoom = null;
      state.x = WORLD.entry.x;
      state.y = WORLD.entry.y;
      state.direction = 'e';
      state.displayRoom = null;
      state.route = [];
      state.wait = 0;
      state.walking = false;
      state.replaying = false;
      state.replayTask = null;
      state.deferredReplayTask = null;
      state.departing = false;
    }

    function releaseQueuedEntry() {
      while (entryQueue.length > 0) {
        const state = states.get(entryQueue[0]);
        if (state && state.entryPending) break;
        entryQueue.shift();
      }
      if (entryQueue.length === 0 || schedulerTime + 1e-9 < nextEntryAt) return false;
      const state = states.get(entryQueue[0]);
      const deferredReplay = state.deferredReplayTask;
      const roomId = deferredReplay ? deferredReplay.sourceRoom : state.destination.room;
      if (roomOccupancy(roomId) + reservedEntries(roomId) >= ROOM_CAPACITY) return false;
      entryQueue.shift();
      state.entryPending = false;
      state.entryAdmissionRoom = roomId;
      state.deferredReplayTask = null;
      if (!deferredReplay || !startTaskReplay(state, deferredReplay)) {
        state.route = entryRoute(roomId, state.destination);
        state.wait = 0;
        updateFlags(state);
      }
      nextEntryAt = schedulerTime + STAGGER_ENTRY_SECONDS;
      return true;
    }

    function startLegacyReplay(state, index) {
      if (state.departing || state.entryPending || !TERMINAL_STATES.has(state.actor.state)
          || !ROOM_BY_ID.has(state.actor.sourceRoom)
          || !ROOM_BY_ID.has(state.actor.room)) return false;
      const terminalDestination = slot(state.actor.room, index);
      const sourceDestination = slot(state.actor.sourceRoom, index);
      state.x = WORLD.entry.x;
      state.y = WORLD.entry.y;
      state.direction = 'e';
      state.displayRoom = null;
      state.destination = {
        room: state.actor.room,
        x: terminalDestination.x,
        y: terminalDestination.y,
      };
      state.route = replayRoute(
        state.actor.sourceRoom,
        sourceDestination,
        state.actor.room,
        terminalDestination,
      );
      state.wait = index * REPLAY_ENTRY_DELAY_SECONDS;
      state.replaying = true;
      state.replayTask = null;
      updateFlags(state);
      return true;
    }

    function startTaskReplay(state, task, force = false) {
      if (state.departing || state.entryPending || state.actor.state !== 'idle'
          || !PERSISTENT_KINDS.has(state.actor.kind)
          || task.actorId !== state.actor.id
          || (!force && state.lastReplayTaskId === task.id)) return false;
      state.route = recordedTaskRoute(state, task, state.slotIndex);
      state.wait = 0;
      state.replaying = true;
      state.replayTask = copy(task);
      state.deferredReplayTask = null;
      state.lastReplayTaskId = task.id;
      updateFlags(state);
      return true;
    }

    function deferTaskReplay(state, task, force = false) {
      if (!state.entryPending || state.departing || state.actor.state !== 'idle'
          || !PERSISTENT_KINDS.has(state.actor.kind) || task.actorId !== state.actor.id
          || (!force && state.lastReplayTaskId === task.id)) return false;
      state.deferredReplayTask = copy(task);
      return true;
    }

    function preemptReplay(state, roomId, destination) {
      state.destination = {room: roomId, x: destination.x, y: destination.y};
      state.route = routeBetween(state, roomId, destination);
      state.wait = 0;
      state.replaying = false;
      state.replayTask = null;
      updateFlags(state);
    }

    function sync(actors, options = {}) {
      const {
        initial = false,
        animate = true,
        replayIds = [],
        replayTasks = [],
        staggerEntries = false,
      } = options || {};
      const replaySet = new Set(Array.isArray(replayIds) ? replayIds : []);
      const incoming = [];
      const present = new Set();
      for (const candidate of Array.isArray(actors) ? actors : []) {
        if (!candidate || typeof candidate.id !== 'string' || !ROOM_BY_ID.has(candidate.room)) continue;
        if (present.has(candidate.id)) continue;
        present.add(candidate.id);
        incoming.push(candidate);
      }

      const assignments = new Map();
      const reserved = new Map(ROOM_IDS.map(id => [id, new Set()]));
      for (const candidate of incoming) {
        const state = states.get(candidate.id);
        if (!state || state.destination.room !== candidate.room
            || state.slotIndex < 0 || state.slotIndex > 5
            || reserved.get(candidate.room).has(state.slotIndex)) continue;
        assignments.set(candidate.id, state.slotIndex);
        reserved.get(candidate.room).add(state.slotIndex);
      }
      for (const candidate of incoming) {
        if (assignments.has(candidate.id)) continue;
        const occupied = reserved.get(candidate.room);
        let index = 0;
        while (index < 5 && occupied.has(index)) index += 1;
        assignments.set(candidate.id, index);
        occupied.add(index);
      }

      for (const candidate of incoming) {
        const index = assignments.get(candidate.id);
        const destination = slot(candidate.room, index);
        const actorCopy = copy(candidate);
        let state = states.get(candidate.id);

        if (!state) {
          state = {
            actor: actorCopy,
            x: WORLD.entry.x,
            y: WORLD.entry.y,
            direction: 's',
            walking: false,
            replaying: false,
            replayTask: null,
            departing: false,
            entryPending: false,
            entryAdmissionRoom: null,
            deferredReplayTask: null,
            capacityGated: staggerEntries === true,
            lastReplayTaskId: null,
            displayRoom: null,
            destination: {room: candidate.room, x: destination.x, y: destination.y},
            route: [],
            wait: 0,
            slotIndex: index,
          };
          states.set(candidate.id, state);
          if (animate === false) {
            place(state, candidate.room, destination);
          } else if (staggerEntries === true
              && (!initial || ACTIVE_STATES.has(candidate.state))) {
            queueEntry(state);
          } else if (initial) {
            place(state, candidate.room, destination);
          } else if (candidate.kind === 'call' && TERMINAL_STATES.has(candidate.state)
              && replaySet.has(candidate.id)) {
            startLegacyReplay(state, index);
          } else if (ACTIVE_STATES.has(candidate.state)) {
            state.route = entryRoute(candidate.room, destination);
            updateFlags(state);
          } else {
            place(state, candidate.room, destination);
          }
          continue;
        }

        const destinationChanged = state.destination.room !== candidate.room
          || state.destination.x !== destination.x || state.destination.y !== destination.y;
        const wasDeparting = state.departing;
        state.actor = actorCopy;
        state.slotIndex = index;
        state.capacityGated = state.capacityGated || staggerEntries === true;
        if (animate === false) {
          removeQueuedEntry(candidate.id);
          place(state, candidate.room, destination);
        } else if (state.entryPending) {
          state.destination = {room: candidate.room, x: destination.x, y: destination.y};
          if (candidate.state !== 'idle') state.deferredReplayTask = null;
        } else if (initial && staggerEntries === true && ACTIVE_STATES.has(candidate.state)) {
          queueEntry(state);
        } else if (initial) {
          place(state, candidate.room, destination);
        } else if (wasDeparting) {
          state.destination = {room: candidate.room, x: destination.x, y: destination.y};
          state.route = state.displayRoom === candidate.room
              && state.x === destination.x && state.y === destination.y
            ? [] : routeBetween(state, candidate.room, destination);
          state.wait = 0;
          state.replaying = false;
          state.replayTask = null;
          state.departing = false;
          updateFlags(state);
        } else if (state.replayTask && candidate.state !== 'idle') {
          preemptReplay(state, candidate.room, destination);
        } else if (destinationChanged) {
          state.destination = {room: candidate.room, x: destination.x, y: destination.y};
          state.route = routeBetween(state, candidate.room, destination);
          state.wait = 0;
          state.replaying = false;
          state.replayTask = null;
          updateFlags(state);
        }
      }

      if (!initial && animate !== false) {
        for (const [actorId, task] of latestTasks(replayTasks)) {
          const state = states.get(actorId);
          if (state && !deferTaskReplay(state, task)) startTaskReplay(state, task);
        }
      }

      for (const id of Array.from(states.keys())) {
        const state = states.get(id);
        if (!present.has(id) && !state.departing) {
          removeQueuedEntry(id);
          states.delete(id);
        }
      }
      if (animate === false && entryQueue.length === 0) nextEntryAt = schedulerTime;
      releaseQueuedEntry();
      return snapshot();
    }

    function advanceStates(seconds) {
      for (const [id, state] of states) {
        let remaining = seconds;
        while (remaining > 0) {
          if (state.wait > 0) {
            const elapsed = Math.min(remaining, state.wait);
            state.wait -= elapsed;
            remaining -= elapsed;
            if (state.wait > 1e-9) break;
            state.wait = 0;
          }
          const target = state.route[0];
          if (!target) break;
          if (state.capacityGated && target.displayRoom && !state.displayRoom
              && roomOccupancy(target.displayRoom) >= ROOM_CAPACITY) break;
          const dx = target.x - state.x;
          const dy = target.y - state.y;
          const distance = Math.abs(dx) + Math.abs(dy);
          if (distance <= 1e-9) {
            state.x = target.x;
            state.y = target.y;
            state.displayRoom = target.displayRoom;
            if (target.displayRoom) state.entryAdmissionRoom = null;
            state.route.shift();
            state.wait = target.wait || 0;
            continue;
          }
          const travel = Math.min(distance, remaining * SPEED);
          if (dx !== 0) {
            const sign = Math.sign(dx);
            state.x += sign * travel;
            state.direction = sign > 0 ? 'e' : 'w';
          } else {
            const sign = Math.sign(dy);
            state.y += sign * travel;
            state.direction = sign > 0 ? 's' : 'n';
          }
          remaining -= travel / SPEED;
          if (travel < distance) break;
          state.x = target.x;
          state.y = target.y;
          state.displayRoom = target.displayRoom;
          if (target.displayRoom) state.entryAdmissionRoom = null;
          state.route.shift();
          state.wait = target.wait || 0;
        }
        updateFlags(state);
        if (state.departing && state.wait <= 0 && state.route.length === 0) {
          states.delete(id);
        }
      }
    }

    function advance(seconds) {
      if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) {
        return snapshot();
      }
      let remaining = seconds;
      while (remaining > 1e-9) {
        releaseQueuedEntry();
        let step = remaining;
        if (entryQueue.length > 0) {
          const untilScheduled = nextEntryAt - schedulerTime;
          step = untilScheduled > 1e-9
            ? Math.min(step, untilScheduled)
            : Math.min(step, 0.01);
        }
        advanceStates(step);
        schedulerTime += step;
        remaining -= step;
      }
      releaseQueuedEntry();
      return snapshot();
    }

    function snapshot() {
      return Array.from(states, ([id, state]) => ({
        id,
        actor: copy(state.actor),
        x: state.x,
        y: state.y,
        direction: state.direction,
        walking: state.walking,
        replaying: state.replaying,
        replayTask: state.replayTask ? copy(state.replayTask) : null,
        displayRoom: state.displayRoom,
        ...(state.entryPending ? {entryPending: true} : {}),
        ...(state.departing ? {departing: true} : {}),
      }));
    }

    function clear() {
      states.clear();
      entryQueue.length = 0;
      schedulerTime = 0;
      nextEntryAt = 0;
      return snapshot();
    }

    function replay(ids, tasks = []) {
      const requested = new Set(Array.isArray(ids) ? ids : []);
      for (const [id, state] of states) {
        if (requested.has(id)) startLegacyReplay(state, state.slotIndex);
      }
      for (const [actorId, task] of latestTasks(tasks, requested)) {
        const state = states.get(actorId);
        if (state && !deferTaskReplay(state, task, true)) startTaskReplay(state, task, true);
      }
      return snapshot();
    }

    function depart(ids, options = {}) {
      const {animate = true} = options || {};
      const requested = new Set(Array.isArray(ids)
        ? ids.filter(id => typeof id === 'string' && id.length > 0)
        : []);
      for (const id of requested) {
        const state = states.get(id);
        if (!state) continue;
        removeQueuedEntry(id);
        state.entryPending = false;
        state.entryAdmissionRoom = null;
        state.deferredReplayTask = null;
        if (animate === false) {
          states.delete(id);
          continue;
        }
        if (state.departing) continue;
        state.route = departureRoute(state);
        state.wait = DEPARTURE_HOLD_SECONDS;
        state.replaying = false;
        state.replayTask = null;
        state.departing = true;
        updateFlags(state);
      }
      return snapshot();
    }

    function settle() {
      for (const [id, state] of states) {
        if (state.departing) {
          states.delete(id);
          continue;
        }
        place(state, state.destination.room, state.destination);
      }
      entryQueue.length = 0;
      nextEntryAt = schedulerTime;
      return snapshot();
    }

    return {sync, advance, snapshot, clear, replay, settle, depart};
  }

  return {WORLD, ROOMS, slot, create};
}));
