export function parseLibraryRequest(args) {
  function requiredValue(option) {
    const first = args.indexOf(option);
    if (first < 0 || first !== args.lastIndexOf(option)) return "";
    const value = args[first + 1];
    return typeof value === "string" && !value.startsWith("--") ? value : "";
  }
  const stage = requiredValue("--stage");
  const question = requiredValue("--question");
  if (!/^stage-[A-Za-z0-9_-]{1,160}$/.test(stage) || !question.trim() || question.length > 2000) {
    throw new Error("Bounded project/question required");
  }
  return { qualification: args.includes("--qualification"), stage, question };
}
