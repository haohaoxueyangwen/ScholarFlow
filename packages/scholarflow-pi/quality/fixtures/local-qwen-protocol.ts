import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function protocolFixture(pi: ExtensionAPI) {
  pi.registerTool({
    name: "sf_local_test_echo", label: "Synthetic echo", description: "Return a hidden synthetic protocol token. No file or domain writes.",
    parameters: Type.Object({value: Type.String()}, {additionalProperties:false}),
    async execute(_id, params) {
      const data = {value:params.value,token:process.env.SCHOLARFLOW_LOCAL_TEST_TOKEN};
      return {content:[{type:"text",text:JSON.stringify(data)}],details:data};
    },
  });
  pi.registerTool({
    name: "sf_local_test_nested", label: "Synthetic nested arguments", description: "Validate and echo Unicode and nested synthetic arguments. No writes.",
    parameters: Type.Object({label:Type.String(),payload:Type.Object({enabled:Type.Boolean(),values:Type.Array(Type.Number()),meta:Type.Object({unit:Type.String()},{additionalProperties:false})},{additionalProperties:false})},{additionalProperties:false}),
    async execute(_id, params) {
      const data = {...params,token:process.env.SCHOLARFLOW_LOCAL_TEST_TOKEN};
      return {content:[{type:"text",text:JSON.stringify(data)}],details:data};
    },
  });
  pi.registerTool({
    name: "sf_local_test_failure", label: "Expected synthetic failure", description: "Always fails with SYNTHETIC_TOOL_FAILURE. Do not retry this tool. No side effects.",
    parameters: Type.Object({reason:Type.String()},{additionalProperties:false}),
    async execute() { throw new Error("SYNTHETIC_TOOL_FAILURE"); },
  });
}
