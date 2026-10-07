import { readFileSync } from 'node:fs';

const SECRET_KEY = /(?:secret|token|password|passwd|api[_-]?key|authorization|credential|cookie|private[_-]?key|session[_-]?key|dsn|database[_-]?url|connection[_-]?string)/i;
const SAFE_KEYS = new Set(['command', 'cmd', 'message', 'text', 'output', 'stderr', 'stdout', 'path', 'url', 'name']);
const MASK = '[REDACTED]';

export function redactText(value, extraSecrets = []) {
  let result = String(value);
  for (const secret of extraSecrets.filter(Boolean)) result = result.split(String(secret)).join(MASK);
  result = result.replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*/gi, `$1 ${MASK}`);
  result = result.replace(/((?:(?:https?|postgres(?:ql)?|redis(?:s)?|amqps?|mysql):\/\/)[^\s/@]+:)[^\s/@]+@/gi, `$1${MASK}@`);
  result = result.replace(/([?&](?:token|key|secret|password|api_key|access_token|refresh_token|code)=)[^\s&#]+/gi, `$1${MASK}`);
  result = result.replace(/\b([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(?:(['"])[^'"\n]*\3|[^\s;|&]+)/g, (whole, key, sep) => SECRET_KEY.test(key) ? `${key}${sep}${MASK}` : whole);
  result = result.replace(/\b(Authorization|X-Api-Key|Cookie|Set-Cookie)\s*:\s*[^\r\n]+/gi, `$1: ${MASK}`);
  result = result.replace(/(--(?:token|secret|password|api-key|access-token|refresh-token)(?:=|\s+))(?:(['\"])[^'\"\n]*\2|[^\s;|&]+)/gi, `$1${MASK}`);
  result = result.replace(/((?:^|\s)(?:--user|-u)(?:=|\s+))(?:(['\"])[^'\"\n]*\2|[^\s;|&]+)/gm, `$1${MASK}`);
  result = result.replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{16})\b/g, MASK);
  result = result.replace(/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g, MASK);
  return result;
}

export function redactValue(value, extraSecrets = [], depth = 0) {
  if (depth > 8) return '[OMITTED]';
  if (typeof value === 'string') return redactText(value, extraSecrets);
  if (Array.isArray(value)) return value.slice(0, 30).map((item) => redactValue(item, extraSecrets, depth + 1));
  if (value && typeof value === 'object') {
    const result = {};
    for (const [key, item] of Object.entries(value).slice(0, 50)) {
      result[key] = SECRET_KEY.test(key) && !SAFE_KEYS.has(key.toLowerCase()) ? MASK : redactValue(item, extraSecrets, depth + 1);
    }
    return result;
  }
  return value;
}

export function stableSignature(tool, command, output) {
  const normalized = `${tool}\n${command}\n${output}`
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi, '<uuid>')
    .replace(/\b\d{4}-\d\d-\d\d[T ][\d:.+Z-]+\b/g, '<time>')
    .replace(/(?:\/[^\s:'"?]+){2,}/g, '<path>')
    .replace(/\b\d{4,}\b/g, '<n>')
    .replace(/\s+/g, ' ').trim().slice(0, 2048);
  return normalized;
}

export function loadSecrets(env = process.env) {
  const secrets = [];
  const file = env.PI_CAPTURE_REDACTION_FILE;
  if (file) {
    const data = JSON.parse(readFileSync(file, 'utf8'));
    if (!Array.isArray(data.secrets) || data.secrets.some((s) => typeof s !== 'string')) {
      throw new Error('invalid redaction configuration');
    }
    secrets.push(...data.secrets.filter((s) => s.length >= 4));
  }
  for (const [key, value] of Object.entries(env)) {
    if (SECRET_KEY.test(key) && value && value.length >= 4) {
      secrets.push(value);
    }
  }
  return [...new Set(secrets)];
}
