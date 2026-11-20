const deadline=setTimeout(()=>{console.error("Import diagnostic deadline");process.exit(124);},20000);
console.log("local-config:start");
await import("../providers/local-qwen/config.mjs");
console.log("local-config:ok");
await import("../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js");
console.log("native-api:ok");
await import("../node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js");
console.log("native-loader:ok");
clearTimeout(deadline);
