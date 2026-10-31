import assert from "assert";
import fs from "fs";

const html = fs.readFileSync(new URL("../host-command-approval.html", import.meta.url), "utf8");
assert.match(html, /"external_call"/);
assert.match(html, /외부 API 호출/);
assert.match(html, /비용/);
console.log("external_call UI assertions passed");
