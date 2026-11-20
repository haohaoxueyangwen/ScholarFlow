import { TOOL_CALLING_QUALIFIED, toolsEnabled } from "./config.mjs";
console.log(JSON.stringify({qualified: TOOL_CALLING_QUALIFIED, enabled: toolsEnabled()}));
