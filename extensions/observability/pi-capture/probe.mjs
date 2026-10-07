// These standalone commands deliberately produce a failed bash result. Keep
// their occurrences for trend and review, but do not suggest a repair for them.
export function isIntentionalFailureProbe(command) {
  if (typeof command !== 'string') return false;
  const value = command.trim();
  const probe = '(?:exit\\s+0*[1-9][0-9]*|false)';
  if (new RegExp(`^${probe}\\s*;?$`).test(value)) return true;
  return new RegExp(`^(?:bash|sh)\\s+-(?:c|lc)\\s+(['"])${probe}\\1\\s*;?$`).test(value);
}
