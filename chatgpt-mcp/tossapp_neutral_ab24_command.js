#!/home/jhw/.nvm/versions/node/v24.12.0/bin/node --experimental-strip-types
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { pathToFileURL } = require('url');

const MODEL = 'gemini-3.1-flash-lite';
const PROMPT_VERSION = 'tarot-grounded.v4';
const CONTRACT_VERSION = 'reading-result.v1';
const REQUESTS_REL = 'content/evaluation/reading-section-safety/runs/neutral-ab24-prepared-20261021/requests.json';
const MANIFEST_REL = 'content/evaluation/reading-section-safety/runs/neutral-ab24-prepared-20261021/manifest.json';
const RUNNER_REL = 'scripts/reading-neutral-ab24-v2/runner.mjs';
const OUTPUT_REL = 'content/evaluation/reading-section-safety/runs/neutral-ab24-live-20261023';
const REQUESTS_SHA256 = '0e9f370127d37fd235bf1672706e558504fedcc8607bbfbba9aaaf35807aa31c';
const MANIFEST_SHA256 = 'ab361d7aaa48bb12dd3c059fd03151985d745deea95621327b191ee6e11cb250';
const SYSTEM_SHA256 = 'e31de3e0d2c9b620fee7c4e6098b439fe4ee5a5f51fd3946d85f2cae480ca6cd';
const CALL_LIMIT = 24;
const RETRIES = 0;
const TIMEOUT_MS = 60000;
const MAX_OUTPUT_TOKENS = 4096;
const SPACING_MS = 4200;
const DEADLINE_MS = 26 * 60 * 1000;
const KEY_REL = '.config/tarororo-test/gemini-api-key';
const ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/' + MODEL + ':generateContent';

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}
function assertCondition(value, code) {
  if (!value) {
    const error = new Error(code);
    error.code = 'EXPERIMENT_ERROR';
    throw error;
  }
}
function readExact(root, relative, expected) {
  const file = path.join(root, relative);
  const bytes = fs.readFileSync(file);
  assertCondition(sha256(bytes) === expected, 'PIN_MISMATCH');
  return bytes;
}
function exactRequestBody(systemInstruction, userJson, readingResultSchema) {
  return JSON.stringify({
    systemInstruction: { parts: [{ text: systemInstruction }] },
    contents: [{ role: 'user', parts: [{ text: userJson }] }],
    generationConfig: {
      responseMimeType: 'application/json',
      responseJsonSchema: readingResultSchema,
      maxOutputTokens: MAX_OUTPUT_TOKENS,
    },
  });
}
function secureApiKey() {
  const file = path.join(os.homedir(), KEY_REL);
  const flags = fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | (fs.constants.O_CLOEXEC || 0);
  let fd = -1;
  try {
    fd = fs.openSync(file, flags);
    const info = fs.fstatSync(fd);
    assertCondition(info.isFile(), 'KEY_UNSAFE');
    assertCondition(info.uid === process.getuid(), 'KEY_UNSAFE');
    assertCondition((info.mode & 0o777) === 0o600, 'KEY_UNSAFE');
    assertCondition(info.size > 0 && info.size <= 8192, 'KEY_UNSAFE');
    const key = fs.readFileSync(fd, 'utf8').trim();
    assertCondition(key.length > 0 && key.length <= 8192, 'KEY_UNSAFE');
    return key;
  } finally {
    if (fd >= 0) fs.closeSync(fd);
  }
}
function writePrivateJson(file, value) {
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
}
async function loadPrepared(root) {
  const requestBytes = readExact(root, REQUESTS_REL, REQUESTS_SHA256);
  const manifestBytes = readExact(root, MANIFEST_REL, MANIFEST_SHA256);
  const requestDoc = JSON.parse(requestBytes.toString('utf8'));
  const manifest = JSON.parse(manifestBytes.toString('utf8'));
  assertCondition(Object.keys(requestDoc).length === 1 && Array.isArray(requestDoc.schedule), 'REQUESTS_INVALID');
  assertCondition(requestDoc.schedule.length === CALL_LIMIT, 'REQUESTS_INVALID');
  assertCondition(manifest.callLimit === CALL_LIMIT && manifest.retries === RETRIES, 'MANIFEST_INVALID');
  assertCondition(manifest.model === MODEL && manifest.promptVersion === PROMPT_VERSION, 'MANIFEST_INVALID');
  assertCondition(manifest.contractVersion === CONTRACT_VERSION, 'MANIFEST_INVALID');
  assertCondition(manifest.timeoutMs === TIMEOUT_MS && manifest.maxOutputTokens === MAX_OUTPUT_TOKENS, 'MANIFEST_INVALID');
  assertCondition(manifest.systemSha256 === SYSTEM_SHA256, 'MANIFEST_INVALID');
  assertCondition(manifest.actualCalls === 0, 'PREPARED_ALREADY_EXECUTED');
  assertCondition(manifest.status === 'PREPARED_NOT_EXECUTED', 'MANIFEST_INVALID');
  assertCondition(manifest.materialReleaseApproval === 'NOT_GRANTED', 'MANIFEST_INVALID');
  for (const relative of Object.keys(manifest.pinnedSources || {})) {
    const expected = manifest.pinnedSources[relative];
    const actual = sha256(fs.readFileSync(path.join(root, relative)));
    assertCondition(actual === expected, 'SOURCE_PIN_MISMATCH');
  }
  const runnerUrl = pathToFileURL(path.join(root, RUNNER_REL)).href;
  const promptUrl = pathToFileURL(path.join(root, 'server/src/ai/prompt.ts')).href;
  const schemaUrl = pathToFileURL(path.join(root, 'contracts/reading.schemas.ts')).href;
  const runner = await import(runnerUrl);
  const promptModule = await import(promptUrl);
  const schemaModule = await import(schemaUrl);
  runner.validateSchedule(requestDoc.schedule);
  for (const sample of requestDoc.schedule) {
    const prompt = promptModule.buildPrompt(sample.context, PROMPT_VERSION);
    assertCondition(sha256(prompt.systemInstruction) === SYSTEM_SHA256, 'SYSTEM_PROMPT_MISMATCH');
    assertCondition(sha256(prompt.userJson) === sample.contextSha256, 'CONTEXT_HASH_MISMATCH');
    const body = exactRequestBody(prompt.systemInstruction, prompt.userJson, schemaModule.readingResultSchema);
    assertCondition(sha256(body) === sample.requestBodySha256, 'REQUEST_BODY_MISMATCH');
  }
  return { manifest, schedule: requestDoc.schedule, runner };
}
async function verify(root) {
  const loaded = await loadPrepared(root);
  return {
    status: 'VERIFIED_NOT_EXECUTED',
    samples: loaded.schedule.length,
    callLimit: CALL_LIMIT,
    retries: RETRIES,
    model: MODEL,
    promptVersion: PROMPT_VERSION,
    requestsSha256: REQUESTS_SHA256,
    manifestSha256: MANIFEST_SHA256,
    outputRelative: OUTPUT_REL,
    externalCalls: 0,
  };
}
async function run(root) {
  const loaded = await loadPrepared(root);
  const output = path.join(root, OUTPUT_REL);
  assertCondition(!fs.existsSync(output), 'OUTPUT_ALREADY_EXISTS');
  const apiKey = secureApiKey();
  const geminiUrl = pathToFileURL(path.join(root, 'server/src/ai/gemini.ts')).href;
  const gemini = await import(geminiUrl);
  let lastStart = 0;
  let transportCalls = 0;
  const transport = [];
  const invoke = async function(sample, reserve) {
    const wait = Math.max(0, SPACING_MS - (Date.now() - lastStart));
    if (wait) await new Promise(function(resolve) { setTimeout(resolve, wait); });
    lastStart = Date.now();
    let sampleCalls = 0;
    const provider = new gemini.GeminiProvider({
      apiKey: apiKey,
      model: MODEL,
      timeoutMs: TIMEOUT_MS,
      fetch: async function(url, init) {
        sampleCalls += 1;
        assertCondition(sampleCalls === 1, 'MULTIPLE_FETCHES');
        assertCondition(transportCalls < CALL_LIMIT, 'CALL_LIMIT');
        assertCondition(String(url) === ENDPOINT, 'ENDPOINT_MISMATCH');
        assertCondition(init && init.method === 'POST' && init.redirect === 'error', 'REQUEST_OPTIONS_MISMATCH');
        const body = String(init.body);
        const bodyHash = sha256(body);
        assertCondition(bodyHash === sample.requestBodySha256, 'REQUEST_BODY_MISMATCH');
        reserve(bodyHash);
        transportCalls += 1;
        try {
          const response = await fetch(url, init);
          transport.push({ sampleId: sample.sampleId, httpStatus: response.status });
          return response;
        } catch (error) {
          transport.push({ sampleId: sample.sampleId, httpStatus: null });
          throw error;
        }
      },
    });
    const generated = await provider.generate({
      context: sample.context,
      promptVersion: PROMPT_VERSION,
      contractVersion: CONTRACT_VERSION,
    });
    return { result: generated.result, usage: generated.usage };
  };
  const deadline = Date.now() + DEADLINE_MS;
  const summary = await loaded.runner.executeOnce({
    dir: output,
    schedule: loaded.schedule,
    deadline: deadline,
    invoke: invoke,
    metadata: {
      experiment: 'neutral-ab24',
      model: MODEL,
      promptVersion: PROMPT_VERSION,
      contractVersion: CONTRACT_VERSION,
      maxOutputTokens: MAX_OUTPUT_TOKENS,
      timeoutMs: TIMEOUT_MS,
      preparedManifestSha256: MANIFEST_SHA256,
      preparedRequestsSha256: REQUESTS_SHA256,
      externalCallApproval: 'HOST_COMMAND_ONE_USE_REQUIRED',
    },
  });
  writePrivateJson(path.join(output, 'transport-observation.json'), {
    observedBy: 'tossapp-neutral-ab24-command.v1',
    transportCalls: transportCalls,
    callLimit: CALL_LIMIT,
    retries: RETRIES,
    observations: transport,
  });
  return {
    status: 'COMPLETE',
    attempted: summary.attempted,
    successful: summary.successful,
    failed: summary.failed,
    notAttempted: summary.notAttempted,
    reservedCalls: summary.reservedCalls,
    observedTransportCalls: transportCalls,
    retries: summary.retries,
    stopReason: summary.stopReason,
    knownUsage: summary.knownUsage,
    outputRelative: OUTPUT_REL,
  };
}
function selfTest() {
  assertCondition(sha256('abc') === 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 'SELF_TEST_HASH');
  assertCondition(CALL_LIMIT === 24 && RETRIES === 0, 'SELF_TEST_LIMIT');
  return { status: 'SELF_TEST_PASS', callLimit: CALL_LIMIT, retries: RETRIES, model: MODEL, promptVersion: PROMPT_VERSION };
}
async function main() {
  const mode = process.argv[2];
  assertCondition(process.argv.length === 3, 'ARGUMENTS_INVALID');
  if (mode === 'self-test') return selfTest();
  const root = fs.realpathSync(process.cwd());
  if (mode === 'verify') return await verify(root);
  if (mode === 'run') return await run(root);
  throw Object.assign(new Error('ARGUMENTS_INVALID'), { code: 'EXPERIMENT_ERROR' });
}
if (require.main === module) {
  main().then(function(value) {
    process.stdout.write(JSON.stringify(value) + '\n');
  }).catch(function(error) {
    const code = error && Object.prototype.hasOwnProperty.call(error, 'code') && typeof error.code === 'string'
      ? error.code : 'EXPERIMENT_ERROR';
    process.stderr.write('NEUTRAL_AB24_FAILED ' + code + '\n');
    process.exitCode = 2;
  });
}
module.exports = { selfTest, verify, run, exactRequestBody, sha256 };