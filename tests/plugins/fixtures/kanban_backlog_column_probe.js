// Exercises the shipped dashboard fallback helpers without a separate build source.
// Run via: node kanban_backlog_column_probe.js <path-to-bundle>
const fs = require("fs");

const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("const FALLBACK_COLUMN_LABEL");
const end = src.indexOf("function getDestructiveConfirm", start);
if (start === -1 || end === -1) {
  console.error("column fallback helpers not found in dashboard bundle");
  process.exit(1);
}

function tx(_translation, _key, fallback) {
  return fallback;
}

eval(src.slice(start, end));

if (getColumnLabel(null, "backlog") !== "Backlog") {
  console.error("FAIL: backlog has no distinct dashboard label");
  process.exit(1);
}
if (getColumnHelp(null, "backlog") !== "Human-controlled parking — move to todo when ready") {
  console.error("FAIL: backlog has no human-controlled parking help");
  process.exit(1);
}
console.log("PASS");
