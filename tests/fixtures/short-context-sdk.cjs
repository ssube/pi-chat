// Offline SDK boundary; never opens real agent configuration or credentials.
exports.state = { dir: '', runtime: null, calls: [] };
exports.getAgentDir = () => exports.state.dir;
exports.ModelRuntime = { create: async options => {
  exports.state.calls.push(options);
  return exports.state.runtime;
} };
