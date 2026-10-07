let executor = async () => ({ exitCode: 0 });
exports.setExecutor = (fn) => { executor = fn; };
exports.createLocalBashOperations = () => ({ exec: (...args) => executor(...args) });
