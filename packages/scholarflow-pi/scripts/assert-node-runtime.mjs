export function assertNodeRuntimeAtLeast(actual, minimum, label = "evaluation") {
  const parse = (value) => /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value).trim())?.slice(1).map(Number);
  const observed = parse(actual);
  const required = parse(minimum);
  if (!observed || !required) throw new Error(`Cannot validate the ${label} Node.js version against runtime policy`);
  for (let index = 0; index < 3; index += 1) {
    if (observed[index] > required[index]) return;
    if (observed[index] < required[index]) {
      throw new Error(`ScholarFlow ${label} requires Node.js >= ${minimum}; found ${String(actual).trim()}`);
    }
  }
}
