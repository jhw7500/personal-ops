(function (root, factory) {
  'use strict';

  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WorkDashboardPixels = api;
}(typeof window === 'object' ? window : null, function (root) {
  'use strict';

  const ROOM_COLORS = {
    waiting: ['#d9b879', '#b98b50', '#70482f'],
    analysis: ['#667f7a', '#3f615f', '#263f46'],
    coding: ['#bd8551', '#84503d', '#4f3340'],
    review: ['#967a78', '#654f62', '#3e354a'],
    execution: ['#637985', '#3e5663', '#293a45'],
    testing: ['#799497', '#4f7075', '#314b55'],
    done: ['#9a7e59', '#6e573f', '#46392f'],
    attention: ['#8e6558', '#69443d', '#452f34'],
  };
  const OUTFITS = [
    ['#315f73', '#447d8f', '#70a6ad'],
    ['#6c3d59', '#925673', '#c18391'],
    ['#4d6844', '#70834f', '#a4a86b'],
    ['#704738', '#986044', '#c8875b'],
    ['#414d76', '#5e6d9b', '#8799bd'],
    ['#6b5634', '#8f7646', '#c0a467'],
  ];
  const SKINS = [
    ['#7c4734', '#b96e4b', '#df9b69', '#f2c18b'],
    ['#70412f', '#a96548', '#d68e65', '#efbb86'],
    ['#8a5539', '#c27a54', '#e4a475', '#f5c795'],
    ['#694a3e', '#9d6d59', '#c89072', '#e6b294'],
  ];
  const HAIRS = [
    ['#211d29', '#3a2b35', '#5a3d3d'],
    ['#35261d', '#5b3d29', '#855932'],
    ['#4a3025', '#744a2d', '#a16b39'],
    ['#332c30', '#51434b', '#75616a'],
  ];
  let mapCache = null;

  function rect(ctx, color, x, y, width, height) {
    if (width <= 0 || height <= 0) return;
    ctx.fillStyle = color;
    ctx.fillRect(Math.round(x), Math.round(y), Math.round(width), Math.round(height));
  }

  function hashText(value) {
    let hash = 2166136261;
    const text = String(value || 'wanderer');
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function noise(x, y, seed) {
    let value = Math.imul(x + seed, 374761393) + Math.imul(y - seed, 668265263);
    value = Math.imul(value ^ (value >>> 13), 1274126177);
    return (value ^ (value >>> 16)) >>> 0;
  }

  function stone(ctx, x, y, width, height, base, light, dark, seed) {
    rect(ctx, base, x, y, width, height);
    for (let yy = y; yy < y + height; yy += 8) {
      const stagger = ((yy - y) / 8) % 2 ? 7 : 0;
      rect(ctx, dark, x, yy, width, 1);
      for (let xx = x + stagger; xx < x + width; xx += 14) rect(ctx, dark, xx, yy, 1, 8);
    }
    for (let yy = y + 3; yy < y + height; yy += 11) {
      for (let xx = x + 4; xx < x + width; xx += 17) {
        if ((noise(xx, yy, seed) & 3) === 0) rect(ctx, light, xx, yy, 3, 1);
      }
    }
  }

  function planks(ctx, x, y, width, height, base, light, dark, seed) {
    rect(ctx, base, x, y, width, height);
    for (let yy = y; yy < y + height; yy += 12) {
      rect(ctx, dark, x, yy, width, 2);
      for (let xx = x + 8 + ((yy / 12) % 2) * 18; xx < x + width; xx += 36) {
        rect(ctx, dark, xx, yy, 1, 12);
      }
      for (let xx = x + 4; xx < x + width; xx += 23) {
        if ((noise(xx, yy, seed) & 1) === 0) rect(ctx, light, xx, yy + 4, 8, 1);
      }
    }
  }

  function tree(ctx, x, y, seed) {
    const greens = seed % 2
      ? ['#173e38', '#286048', '#3f8154', '#70a45f']
      : ['#1c463d', '#2f6a49', '#4c8c50', '#7bac5b'];
    rect(ctx, '#3b302a', x - 3, y + 13, 7, 13);
    rect(ctx, '#63452f', x - 1, y + 11, 4, 15);
    rect(ctx, greens[0], x - 13, y - 3, 25, 20);
    rect(ctx, greens[1], x - 17, y + 3, 33, 13);
    rect(ctx, greens[2], x - 10, y - 9, 20, 21);
    rect(ctx, greens[3], x - 7, y - 6, 9, 5);
    rect(ctx, greens[0], x + 8, y + 7, 6, 5);
  }

  function shrub(ctx, x, y, color) {
    rect(ctx, '#173b35', x - 7, y + 4, 16, 6);
    rect(ctx, color, x - 9, y + 1, 7, 7);
    rect(ctx, color, x - 2, y - 2, 8, 10);
    rect(ctx, color, x + 5, y + 2, 7, 7);
    rect(ctx, '#91b85e', x, y, 3, 2);
  }

  function lantern(ctx, x, y) {
    rect(ctx, '#342d31', x - 2, y - 12, 4, 17);
    rect(ctx, '#614b37', x - 5, y - 12, 10, 3);
    rect(ctx, '#c06d38', x - 5, y - 9, 10, 10);
    rect(ctx, '#ffc85a', x - 3, y - 8, 6, 7);
    rect(ctx, '#fff0a1', x - 1, y - 7, 2, 4);
    rect(ctx, '#342d31', x - 6, y + 1, 12, 3);
  }

  function rug(ctx, x, y, width, height, color, trim) {
    rect(ctx, '#342b2d', x - 2, y - 2, width + 4, height + 4);
    rect(ctx, color, x, y, width, height);
    rect(ctx, trim, x + 3, y + 3, width - 6, 2);
    rect(ctx, trim, x + 3, y + height - 5, width - 6, 2);
    for (let xx = x + 8; xx < x + width - 5; xx += 12) rect(ctx, trim, xx, y + 8, 4, height - 16);
  }

  function table(ctx, x, y, width, color) {
    rect(ctx, '#392a28', x - 2, y + 3, width + 4, 9);
    rect(ctx, color, x, y, width, 8);
    rect(ctx, '#c28b58', x + 3, y + 1, width - 6, 2);
    rect(ctx, '#3c2c29', x + 3, y + 8, 4, 8);
    rect(ctx, '#3c2c29', x + width - 7, y + 8, 4, 8);
  }

  function shelf(ctx, x, y, width, height, seed) {
    rect(ctx, '#352827', x - 2, y - 2, width + 4, height + 5);
    rect(ctx, '#765039', x, y, width, height);
    rect(ctx, '#a87849', x + 2, y + 2, width - 4, 3);
    for (let yy = y + 10; yy < y + height - 3; yy += 11) {
      rect(ctx, '#30272a', x + 3, yy, width - 6, 2);
      let xx = x + 4;
      while (xx < x + width - 5) {
        const n = noise(xx, yy, seed);
        const colors = ['#a94f3d', '#d09845', '#496f68', '#67557c', '#c3aa6b'];
        const bookWidth = 2 + (n % 3);
        rect(ctx, colors[n % colors.length], xx, yy - 7 - (n % 3), bookWidth, 7 + (n % 3));
        xx += bookWidth + 1;
      }
    }
  }

  function crate(ctx, x, y, size) {
    rect(ctx, '#3a2d29', x, y, size, size);
    rect(ctx, '#8e623d', x + 2, y + 2, size - 4, size - 4);
    rect(ctx, '#bd8650', x + 4, y + 4, size - 8, 2);
    rect(ctx, '#5e422f', x + 3, y + 3, 2, size - 6);
    rect(ctx, '#5e422f', x + size - 5, y + 3, 2, size - 6);
    rect(ctx, '#5e422f', x + 5, y + size - 7, size - 10, 2);
  }

  function bench(ctx, x, y, width) {
    rect(ctx, '#332b2a', x, y + 5, width, 8);
    rect(ctx, '#885d3f', x + 2, y, width - 4, 8);
    rect(ctx, '#bc8250', x + 4, y + 1, width - 8, 2);
    rect(ctx, '#4c352d', x + 4, y + 12, 4, 6);
    rect(ctx, '#4c352d', x + width - 8, y + 12, 4, 6);
  }

  function paper(ctx, x, y, angle, sealed) {
    const width = angle ? 10 : 13;
    const height = angle ? 8 : 9;
    rect(ctx, '#ddc58e', x, y, width, height);
    rect(ctx, '#fff0bd', x + 1, y + 1, width - 2, 2);
    rect(ctx, '#896d54', x + 2, y + 5, width - 5, 1);
    if (sealed) {
      rect(ctx, '#842f35', x + width - 5, y + height - 4, 4, 4);
      rect(ctx, '#d26348', x + width - 4, y + height - 3, 2, 2);
    }
  }

  function roomShell(ctx, room, index, world) {
    const palette = ROOM_COLORS[room.id];
    const floorX = room.x + 10;
    const floorY = room.y + 18;
    const floorW = room.width - 20;
    const floorH = room.height - 27;
    if (index % 3 === 1) stone(ctx, floorX, floorY, floorW, floorH, palette[0], '#acc4ad', palette[1], index + 9);
    else planks(ctx, floorX, floorY, floorW, floorH, palette[0], '#e1bd7a', palette[1], index + 15);

    rect(ctx, '#242d31', room.x - 5, room.y - 8, room.width + 10, 12);
    stone(ctx, room.x, room.y - 5, room.width, 14, palette[1], palette[0], palette[2], index + 2);
    rect(ctx, '#181f25', room.x - 5, room.y + 3, 10, room.height - 1);
    rect(ctx, palette[2], room.x, room.y + 8, 10, room.height - 15);
    rect(ctx, '#181f25', room.x + room.width - 5, room.y + 3, 10, room.height - 1);
    rect(ctx, palette[2], room.x + room.width - 10, room.y + 8, 10, room.height - 15);

    const top = room.door.y < world.hallY;
    const edgeY = top ? room.y + room.height - 11 : room.y;
    const doorLeft = room.door.x - 22;
    const doorRight = room.door.x + 22;
    rect(ctx, '#20262b', room.x - 4, edgeY, doorLeft - room.x + 4, 13);
    rect(ctx, '#20262b', doorRight, edgeY, room.x + room.width - doorRight + 4, 13);
    rect(ctx, palette[2], room.x, edgeY + (top ? 0 : 4), doorLeft - room.x, 8);
    rect(ctx, palette[2], doorRight, edgeY + (top ? 0 : 4), room.x + room.width - doorRight, 8);

    const openingY = top ? edgeY : room.y - 8;
    const openingHeight = top ? 13 : 21;
    rect(ctx, palette[0], room.door.x - 18, openingY, 36, openingHeight);
    rect(ctx, palette[1], room.door.x - 18, openingY, 36, 2);
    rect(ctx, '#171d22', doorLeft, edgeY, 4, 13);
    rect(ctx, '#171d22', doorRight - 4, edgeY, 4, 13);
    rect(ctx, '#9d7449', room.door.x - 18, edgeY + (top ? 5 : 0), 36, 4);
  }

  function drawWaiting(ctx, room) {
    rug(ctx, room.x + 77, room.y + 73, 80, 35, '#7b3f43', '#d09b5b');
    bench(ctx, room.x + 24, room.y + 82, 42);
    bench(ctx, room.x + 166, room.y + 82, 42);
    table(ctx, room.x + 93, room.y + 91, 46, '#895f40');
    rect(ctx, '#4b6d58', room.x + 29, room.y + 37, 23, 29);
    rect(ctx, '#86a86b', room.x + 32, room.y + 40, 17, 20);
    rect(ctx, '#d8c27d', room.x + 37, room.y + 45, 7, 5);
    lantern(ctx, room.x + 198, room.y + 60);
  }

  function drawAnalysis(ctx, room) {
    shelf(ctx, room.x + 18, room.y + 28, 45, 65, 41);
    shelf(ctx, room.x + 169, room.y + 28, 45, 65, 53);
    table(ctx, room.x + 82, room.y + 82, 68, '#76513b');
    rect(ctx, '#c8b46e', room.x + 89, room.y + 84, 53, 5);
    rect(ctx, '#3d695e', room.x + 103, room.y + 85, 15, 4);
    rect(ctx, '#a85b3e', room.x + 125, room.y + 84, 10, 5);
    rect(ctx, '#795f42', room.x + 92, room.y + 35, 49, 32);
    rect(ctx, '#cfb96f', room.x + 96, room.y + 39, 41, 24);
    rect(ctx, '#7d8c62', room.x + 99, room.y + 42, 13, 8);
    rect(ctx, '#587175', room.x + 117, room.y + 48, 16, 10);
    rect(ctx, '#b46d46', room.x + 108, room.y + 55, 9, 5);
    rect(ctx, '#735032', room.x + 113, room.y + 39, 2, 24);
  }

  function drawCoding(ctx, room) {
    shelf(ctx, room.x + 18, room.y + 31, 34, 54, 71);
    table(ctx, room.x + 65, room.y + 79, 102, '#86543a');
    paper(ctx, room.x + 75, room.y + 79, 0, false);
    paper(ctx, room.x + 137, room.y + 79, 1, false);
    rect(ctx, '#322f38', room.x + 110, room.y + 72, 8, 13);
    rect(ctx, '#8fa67c', room.x + 112, room.y + 68, 4, 7);
    rect(ctx, '#d4a342', room.x + 181, room.y + 40, 21, 28);
    rect(ctx, '#623c31', room.x + 184, room.y + 44, 15, 21);
    rect(ctx, '#e0b866', room.x + 187, room.y + 47, 9, 3);
    rect(ctx, '#91a66e', room.x + 189, room.y + 54, 5, 8);
    table(ctx, room.x + 174, room.y + 82, 35, '#765039');
  }

  function drawReview(ctx, room) {
    table(ctx, room.x + 51, room.y + 72, 130, '#76513e');
    paper(ctx, room.x + 62, room.y + 73, 0, true);
    paper(ctx, room.x + 106, room.y + 72, 1, true);
    paper(ctx, room.x + 151, room.y + 73, 0, true);
    bench(ctx, room.x + 68, room.y + 37, 96);
    rect(ctx, '#392c30', room.x + 19, room.y + 37, 24, 38);
    rect(ctx, '#8c563e', room.x + 22, room.y + 40, 18, 32);
    for (let y = room.y + 45; y < room.y + 69; y += 8) {
      rect(ctx, '#d3aa59', room.x + 27, y, 8, 5);
      rect(ctx, '#81343c', room.x + 29, y + 1, 4, 3);
    }
    lantern(ctx, room.x + 207, room.y + 61);
  }

  function drawExecution(ctx, room) {
    rect(ctx, '#27333a', room.x + 19, room.y + 28, 195, 48);
    rect(ctx, '#4b6168', room.x + 22, room.y + 31, 189, 42);
    for (let x = room.x + 28; x < room.x + 204; x += 29) {
      rect(ctx, '#17282d', x, room.y + 36, 21, 19);
      rect(ctx, '#5ea88d', x + 3, room.y + 39, 11, 3);
      rect(ctx, '#c69b49', x + 3, room.y + 46, 15, 2);
      rect(ctx, '#bd543f', x + 16, room.y + 39, 2, 2);
      rect(ctx, '#2b3035', x + 3, room.y + 58, 3, 8);
      rect(ctx, '#d0a84e', x + 8, room.y + 58, 3, 8);
      rect(ctx, '#9b4a3e', x + 13, room.y + 58, 3, 8);
    }
    rect(ctx, '#26343b', room.x + 82, room.y + 82, 68, 12);
    rect(ctx, '#8c6542', room.x + 86, room.y + 85, 60, 5);
    rect(ctx, '#c98542', room.x + 105, room.y + 84, 7, 3);
    rect(ctx, '#539187', room.x + 125, room.y + 84, 7, 3);
  }

  function drawTesting(ctx, room) {
    table(ctx, room.x + 48, room.y + 77, 136, '#6c5743');
    rect(ctx, '#26373c', room.x + 58, room.y + 54, 28, 25);
    rect(ctx, '#8eb4a0', room.x + 62, room.y + 58, 20, 13);
    rect(ctx, '#466c69', room.x + 65, room.y + 61, 14, 2);
    rect(ctx, '#d9b654', room.x + 69, room.y + 66, 6, 3);
    rect(ctx, '#523c32', room.x + 107, room.y + 46, 6, 33);
    rect(ctx, '#d8c47c', room.x + 98, room.y + 47, 24, 4);
    rect(ctx, '#c15e48', room.x + 101, room.y + 51, 6, 21);
    rect(ctx, '#5f9f9a', room.x + 114, room.y + 51, 6, 16);
    rect(ctx, '#efe39f', room.x + 102, room.y + 60, 4, 9);
    rect(ctx, '#86d1c0', room.x + 115, room.y + 57, 4, 8);
    rect(ctx, '#33393d', room.x + 142, room.y + 53, 31, 26);
    rect(ctx, '#b9d2a7', room.x + 146, room.y + 57, 23, 15);
    rect(ctx, '#bf704b', room.x + 149, room.y + 66, 17, 2);
    lantern(ctx, room.x + 204, room.y + 63);
  }

  function drawDone(ctx, room) {
    shelf(ctx, room.x + 18, room.y + 28, 43, 62, 101);
    crate(ctx, room.x + 75, room.y + 57, 31);
    crate(ctx, room.x + 111, room.y + 48, 39);
    crate(ctx, room.x + 157, room.y + 61, 28);
    rect(ctx, '#3b302d', room.x + 192, room.y + 34, 18, 56);
    rect(ctx, '#92633d', room.x + 195, room.y + 37, 12, 50);
    for (let y = room.y + 43; y < room.y + 82; y += 11) rect(ctx, '#d0a95f', room.x + 198, y, 6, 5);
  }

  function drawAttention(ctx, room) {
    table(ctx, room.x + 43, room.y + 74, 146, '#713d36');
    rect(ctx, '#352d31', room.x + 52, room.y + 54, 39, 22);
    rect(ctx, '#a74c41', room.x + 56, room.y + 58, 31, 13);
    rect(ctx, '#e3a64d', room.x + 60, room.y + 62, 23, 3);
    rect(ctx, '#2e363b', room.x + 112, room.y + 45, 8, 31);
    rect(ctx, '#c5a257', room.x + 104, room.y + 47, 24, 5);
    rect(ctx, '#b63e3c', room.x + 157, room.y + 55, 19, 21);
    rect(ctx, '#f0a64e', room.x + 160, room.y + 58, 13, 4);
    rect(ctx, '#e6d27c', room.x + 162, room.y + 65, 9, 7);
    rect(ctx, '#8c3435', room.x + 24, room.y + 29, 18, 30);
    rect(ctx, '#e1a34b', room.x + 27, room.y + 33, 12, 19);
    rect(ctx, '#432e31', room.x + 31, room.y + 37, 4, 10);
    lantern(ctx, room.x + 207, room.y + 64);
  }

  const ROOM_DECORATORS = {
    waiting: drawWaiting,
    analysis: drawAnalysis,
    coding: drawCoding,
    review: drawReview,
    execution: drawExecution,
    testing: drawTesting,
    done: drawDone,
    attention: drawAttention,
  };

  function drawLandscape(ctx, world) {
    rect(ctx, '#102f31', 0, 0, world.width, world.height);
    for (let y = 0; y < world.height; y += 16) {
      for (let x = 0; x < world.width; x += 16) {
        const n = noise(x, y, 117);
        rect(ctx, n % 3 ? '#1c4940' : '#24543f', x, y, 16, 16);
        if ((n & 7) === 0) rect(ctx, '#557a46', x + 3, y + 5, 3, 2);
      }
    }
    stone(ctx, 5, world.hallY - 26, world.width - 10, 53, '#786f62', '#9b917a', '#4a4945', 301);
    rect(ctx, '#b49b6b', 5, world.hallY - 4, world.width - 10, 8);
    rect(ctx, '#d1b67b', 5, world.hallY - 3, world.width - 10, 3);
    tree(ctx, 17, 25, 1);
    tree(ctx, world.width - 22, 26, 2);
    tree(ctx, 15, world.height - 34, 3);
    tree(ctx, world.width - 22, world.height - 33, 4);
    for (let x = 81; x < world.width - 54; x += 122) {
      shrub(ctx, x, 25, x % 244 ? '#3f7b48' : '#5a8750');
      shrub(ctx, x + 48, world.height - 27, x % 244 ? '#4f8648' : '#39774d');
    }
    lantern(ctx, 14, world.hallY + 9);
    lantern(ctx, world.width - 14, world.hallY + 9);
  }

  function renderMap(ctx, world, rooms) {
    drawLandscape(ctx, world);
    rooms.forEach((room, index) => roomShell(ctx, room, index, world));
    rooms.forEach((room, index) => {
      const decorRoom = room.door.y < world.hallY
        ? room
        : Object.assign({}, room, {y: room.y + 44});
      ROOM_DECORATORS[room.id](ctx, decorRoom);
    });

    // Door approaches and the center hall remain clear for character routes.
    rooms.forEach(room => {
      const center = room.door.x;
      const thresholdY = room.door.y < world.hallY ? room.door.y - 7 : room.door.y - 4;
      rect(ctx, '#4d443c', center - 17, thresholdY, 34, 7);
      rect(ctx, '#8e7959', center - 13, thresholdY + 1, 26, 3);
      rect(ctx, '#c4aa70', center - 5, thresholdY + 2, 10, 2);
    });
    for (let x = 56; x < world.width - 14; x += 122) lantern(ctx, x, world.hallY + 13);
  }

  function makeCache(canvas, world, rooms) {
    const owner = canvas && canvas.ownerDocument;
    const documentObject = owner || (root && root.document);
    if (!documentObject || typeof documentObject.createElement !== 'function') return null;
    const buffer = documentObject.createElement('canvas');
    buffer.width = world.width;
    buffer.height = world.height;
    const context = buffer.getContext('2d');
    if (!context) return null;
    context.imageSmoothingEnabled = false;
    renderMap(context, world, rooms);
    return buffer;
  }

  function drawMap(canvas) {
    if (!canvas || typeof canvas.getContext !== 'function') return;
    const motion = root && root.WorkDashboardMotion;
    if (!motion || !motion.WORLD || !motion.ROOMS) return;
    const {WORLD: world, ROOMS: rooms} = motion;
    if (canvas.width !== world.width) canvas.width = world.width;
    if (canvas.height !== world.height) canvas.height = world.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, world.width, world.height);
    if (!mapCache) mapCache = makeCache(canvas, world, rooms);
    if (mapCache && mapCache !== canvas && typeof ctx.drawImage === 'function') ctx.drawImage(mapCache, 0, 0);
    else renderMap(ctx, world, rooms);
  }

  function pixel(ctx, color, x, y, width = 1, height = 1) {
    rect(ctx, color, x, y, width, height);
  }

  function drawShadow(ctx) {
    pixel(ctx, '#10182066', 4, 25, 12, 2);
    pixel(ctx, '#10182044', 6, 27, 8, 1);
  }

  const BAG_PALETTES = {
    mcp: ['#164f56', '#2c9293', '#67c5b8'],
    call: ['#164f56', '#2c9293', '#67c5b8'],
    executor: ['#75471f', '#c17a2e', '#f0b64c'],
    job: ['#75471f', '#c17a2e', '#f0b64c'],
    agent: ['#4c3e68', '#8067a5', '#c3a7e0'],
  };

  function drawBag(ctx, pose, side, step) {
    const [edge, fill, shine] = BAG_PALETTES[pose.kind] || BAG_PALETTES.agent;
    const x = side === 'left' ? 2 : 15;
    const y = 16 + (pose.walking ? step : 0);
    pixel(ctx, edge, x, y, 4, 7);
    pixel(ctx, fill, x + 1, y + 1, 3, 5);
    pixel(ctx, shine, x + 2, y + 2, 1, 2);
    pixel(ctx, edge, side === 'left' ? x + 2 : x, y - 3, 1, 4);
  }

  const WORK_ACTIONS = new Set([
    'read', 'search', 'code', 'build', 'test', 'review', 'execute', 'commit', 'push',
    'reason',
  ]);

  function drawWorkProp(ctx, pose, frame) {
    const mirror = pose.direction === 'w';
    const block = (color, x, y, width = 1, height = 1) => {
      pixel(ctx, color, mirror ? 20 - x - width : x, y, width, height);
    };
    const lift = frame ? -1 : 0;

    switch (pose.action) {
      case 'read':
        block('#5e4638', 2, 18 + lift, 16, 6);
        block('#f3df9b', 3, 19 + lift, 6, 4);
        block('#f3df9b', 11, 19 + lift, 6, 4);
        block('#b98655', 9, 18 + lift, 2, 6);
        block('#87684d', 5, 20 + lift, 3, 1);
        block('#87684d', 12, 20 + lift, 3, 1);
        break;
      case 'search': {
        const y = 14 + lift;
        block('#315c61', 10, y, 7, 2);
        block('#7dd6d0', 9, y + 2, 2, 4);
        block('#7dd6d0', 16, y + 2, 2, 4);
        block('#7dd6d0', 11, y + 6, 5, 2);
        block('#315c61', 7, y + 7, 3, 2);
        block('#315c61', 5, y + 9, 3, 4);
        break;
      }
      case 'code':
        block('#26383b', 3, 17, 14, 7);
        block('#406168', 4, 18, 12, 3);
        block('#79d98b', 5 + frame * 5, 19, 4, 1);
        block('#1d272c', 4, 22, 12, 3);
        block('#79d98b', 5 + frame, 23, 2, 1);
        block('#79d98b', 9 + frame, 23, 2, 1);
        break;
      case 'build':
        block('#5c4030', 4, 17 - frame, 2, 8);
        block('#f0a84b', 2, 15 - frame, 7, 3);
        block('#8b623b', 12, 21, 5, 3);
        block('#f0a84b', 13 + frame, 19, 2, 2);
        block('#f0a84b', 15 + frame, 23, 2, 2);
        break;
      case 'test':
        block('#3c586b', 4, 16, 12, 9);
        block('#e5e0c8', 5, 17, 10, 7);
        block('#85c7ef', 6, 18 + frame, 2, 2);
        block('#85c7ef', 6, 22 - frame, 2, 2);
        block('#65747a', 9, 18, 5, 1);
        block('#65747a', 9, 22, 5, 1);
        break;
      case 'review':
        block('#5c4964', 2, 18 + lift, 8, 7);
        block('#d8a2e8', 3, 19 + lift, 6, 5);
        block('#6d5675', 10, 16 - lift, 8, 8);
        block('#eadcf0', 11, 17 - lift, 6, 6);
        block('#9b5d69', 5, 20 + lift, 3, 1);
        block('#6a7792', 12, 19 - lift, 3, 1);
        break;
      case 'execute':
        block('#233439', 2, 16, 16, 8);
        block('#3d5f61', 3, 17, 14, 5);
        block('#65d6a6', 5 + frame * 5, 18, 5, 1);
        block('#65d6a6', 6 + frame * 4, 20, 3, 1);
        block('#233439', 8, 24, 4, 2);
        break;
      case 'commit':
        block('#694148', 7, 14 + frame, 6, 4);
        block('#e87878', 8, 15 + frame, 4, 2);
        block('#694148', 9, 18 + frame, 2, 4);
        block('#d9c9a0', 4, 22, 12, 3);
        block('#e87878', 6 + frame, 23, 8, 2);
        break;
      case 'push':
        block('#7b542e', 4, 18 + lift, 12, 7);
        block('#e6b85c', 5, 19 + lift, 10, 5);
        block('#a66f35', 9, 19 + lift, 2, 5);
        block('#e6b85c', 9, 14 + lift, 2, 4);
        block('#e6b85c', 7, 14 + lift, 6, 2);
        break;
      case 'reason':
        block('#8c7435', 14 + frame, 1 + lift, 4, 4);
        block('#ffd966', 15 + frame, 1 + lift, 2, 3);
        block('#fff2a6', 15 + frame, 1 + lift, 1, 1);
        block('#ffd966', 13 + frame, 6 + lift, 2, 2);
        block('#8c7435', 11 + frame, 9 + lift, 2, 2);
        break;
      default:
        break;
    }
  }

  function drawFront(ctx, colors, pose, step, blink, workFrame, breathe) {
    const [skinDark, skin, skinLight, skinBright] = colors.skin;
    const [hairDark, hair, hairLight] = colors.hair;
    const [clothDark, cloth, clothLight] = colors.outfit;
    const y = breathe;
    pixel(ctx, hairDark, 6, 2 + y, 8, 2);
    pixel(ctx, hair, 5, 4 + y, 10, 5);
    pixel(ctx, hairLight, 7, 3 + y, 5, 2);
    pixel(ctx, skinDark, 5, 8 + y, 10, 3);
    pixel(ctx, skin, 6, 6 + y, 8, 7);
    pixel(ctx, skinLight, 7, 7 + y, 6, 5);
    pixel(ctx, skinBright, 8, 7 + y, 3, 1);
    pixel(ctx, hairDark, 5, 5 + y, 2, 4);
    pixel(ctx, hairDark, 13, 5 + y, 2, 4);
    if (!blink) {
      pixel(ctx, '#273039', 7, 9 + y, 2, 1);
      pixel(ctx, '#273039', 12, 9 + y, 2, 1);
    } else {
      pixel(ctx, skinDark, 7, 9 + y, 2, 1);
      pixel(ctx, skinDark, 12, 9 + y, 2, 1);
    }
    pixel(ctx, skinDark, 9, 12 + y, 3, 1);
    pixel(ctx, clothDark, 5, 13 + y, 10, 10);
    pixel(ctx, cloth, 6, 13 + y, 8, 9);
    pixel(ctx, clothLight, 8, 14 + y, 4, 7);
    pixel(ctx, '#d6b45f', 9, 14 + y, 2, 2);
    pixel(ctx, clothDark, 5, 22 + y, 10, 2);

    if (pose.working) {
      pixel(ctx, clothDark, 3, 14 + y, 3, 7);
      pixel(ctx, skin, 2 + workFrame, 19 + y, 4, 3);
      pixel(ctx, clothDark, 14, 14 + y, 3, 6);
      pixel(ctx, skin, 14 - workFrame, 18 + y, 4, 3);
      pixel(ctx, '#d7c47e', 4, 20 + y, 10, 2);
      pixel(ctx, '#54443b', 7 + workFrame, 18 + y, 1, 5);
    } else {
      pixel(ctx, clothDark, 3, 14 + y + step, 3, 7);
      pixel(ctx, skin, 3, 20 + y + step, 3, 3);
      pixel(ctx, clothDark, 14, 14 + y - step, 3, 7);
      pixel(ctx, skin, 14, 20 + y - step, 3, 3);
    }
    pixel(ctx, clothDark, 6 + step, 23 + y, 4, 3);
    pixel(ctx, clothDark, 11 - step, 23 + y, 4, 3);
    pixel(ctx, '#332f35', 5 + step, 26 + y, 5, 2);
    pixel(ctx, '#332f35', 11 - step, 26 + y, 5, 2);
    drawBag(ctx, pose, 'left', step);
  }

  function drawBack(ctx, colors, pose, step, workFrame, breathe) {
    const [skinDark] = colors.skin;
    const [hairDark, hair, hairLight] = colors.hair;
    const [clothDark, cloth, clothLight] = colors.outfit;
    const y = breathe;
    pixel(ctx, hairDark, 6, 2 + y, 8, 2);
    pixel(ctx, hair, 5, 4 + y, 10, 8);
    pixel(ctx, hairLight, 7, 3 + y, 5, 2);
    pixel(ctx, hairDark, 5, 8 + y, 3, 5);
    pixel(ctx, hairDark, 12, 8 + y, 3, 5);
    pixel(ctx, skinDark, 8, 11 + y, 4, 2);
    pixel(ctx, clothDark, 5, 13 + y, 10, 11);
    pixel(ctx, cloth, 6, 14 + y, 8, 8);
    pixel(ctx, clothLight, 8, 14 + y, 4, 2);
    pixel(ctx, clothDark, 7, 17 + y, 6, 1);
    pixel(ctx, '#d6b45f', 9, 17 + y, 2, 2);
    pixel(ctx, clothDark, 3, 14 + y + (pose.working ? workFrame : step), 3, 8);
    pixel(ctx, clothDark, 14, 14 + y - (pose.working ? workFrame : step), 3, 8);
    pixel(ctx, colors.skin[1], 3, 21 + y, 3, 2);
    pixel(ctx, colors.skin[1], 14, 21 + y, 3, 2);
    pixel(ctx, clothDark, 6 + step, 23 + y, 4, 3);
    pixel(ctx, clothDark, 11 - step, 23 + y, 4, 3);
    pixel(ctx, '#302d34', 5 + step, 26 + y, 5, 2);
    pixel(ctx, '#302d34', 11 - step, 26 + y, 5, 2);
    drawBag(ctx, pose, 'right', step);
  }

  function drawSide(ctx, colors, pose, facingEast, step, blink, workFrame, breathe) {
    const [skinDark, skin, skinLight, skinBright] = colors.skin;
    const [hairDark, hair, hairLight] = colors.hair;
    const [clothDark, cloth, clothLight] = colors.outfit;
    const block = (color, x, y, width, height) => {
      const drawX = facingEast ? x : 20 - x - width;
      pixel(ctx, color, drawX, y, width, height);
    };
    const y = breathe;
    block(hairDark, 6, 2 + y, 8, 2);
    block(hair, 5, 4 + y, 9, 7);
    block(hairLight, 8, 3 + y, 5, 2);
    block(skinDark, 7, 8 + y, 9, 4);
    block(skin, 8, 6 + y, 7, 7);
    block(skinLight, 11, 7 + y, 4, 4);
    block(skinBright, 15, 9 + y, 2, 2);
    if (!blink) block('#273039', 13, 8 + y, 2, 1);
    else block(skinDark, 13, 8 + y, 2, 1);
    block(hairDark, 6, 5 + y, 3, 7);
    block(clothDark, 6, 13 + y, 9, 11);
    block(cloth, 8, 13 + y, 7, 9);
    block(clothLight, 11, 14 + y, 3, 7);
    block('#d6b45f', 8, 15 + y, 2, 2);
    if (pose.working) {
      block(clothDark, 13, 14 + y, 3, 6);
      block(skin, 14 + workFrame, 19 + y, 4, 3);
      block('#55443b', 18, 17 + y + workFrame, 1, 6);
    } else {
      block(clothDark, 13, 14 + y + step, 3, 7);
      block(skin, 14, 20 + y + step, 3, 3);
    }
    block(clothDark, 6 - step, 23 + y, 5, 3);
    block(clothDark, 11 + step, 23 + y, 4, 3);
    block('#302d34', 5 - step, 26 + y, 6, 2);
    block('#302d34', 11 + step, 26 + y, 5, 2);
    drawBag(ctx, pose, facingEast ? 'left' : 'right', step);
  }

  function drawPerson(canvas, pose) {
    if (!canvas || typeof canvas.getContext !== 'function') return;
    if (canvas.width !== 20) canvas.width = 20;
    if (canvas.height !== 28) canvas.height = 28;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, 20, 28);

    const actor = pose || {};
    const activeWork = Boolean(actor.working && !actor.walking && WORK_ACTIONS.has(actor.action));
    const renderedActor = actor.working && !activeWork ? {...actor, working: false} : actor;
    const hash = hashText(actor.id);
    const colors = {
      outfit: OUTFITS[hash % OUTFITS.length],
      skin: SKINS[(hash >>> 4) % SKINS.length],
      hair: HAIRS[(hash >>> 7) % HAIRS.length],
    };
    const clock = Number.isFinite(actor.clock) ? actor.clock : 0;
    const walkFrame = actor.walking ? (Math.floor(clock * 7) % 2) : 0;
    const step = actor.walking ? (walkFrame ? 1 : -1) : 0;
    const workFrame = activeWork ? Math.floor(clock * 5) % 2 : 0;
    const breathe = !actor.walking && !activeWork && Math.floor(clock * 1.5) % 4 === 3 ? -1 : 0;
    const blink = !actor.walking && !activeWork && Math.floor((clock + (hash % 13) / 10) * 4) % 17 === 0;
    drawShadow(ctx);
    switch (actor.direction) {
      case 'n': drawBack(ctx, colors, renderedActor, step, workFrame, breathe); break;
      case 'e': drawSide(ctx, colors, renderedActor, true, step, blink, workFrame, breathe); break;
      case 'w': drawSide(ctx, colors, renderedActor, false, step, blink, workFrame, breathe); break;
      default: drawFront(ctx, colors, renderedActor, step, blink, workFrame, breathe); break;
    }
    if (activeWork) drawWorkProp(ctx, actor, workFrame);
  }

  return Object.freeze({drawMap, drawPerson});
}));
