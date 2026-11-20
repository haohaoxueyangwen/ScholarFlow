import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function syntheticEcho(pi: ExtensionAPI) {
  pi.registerTool({
    name: "sf_local_test_echo", label: "Synthetic protocol echo", description: "Read an isolated protocol test token. No file, domain or external writes.",
    parameters: Type.Object({value: Type.String()}, {additionalProperties: false}),
    async execute(_id, params) {
      const data={value:params.value,token:process.env.SCHOLARFLOW_LOCAL_TEST_TOKEN};
      return {content:[{type:"text",text:JSON.stringify(data)}],details:data};
    },
  });
}
