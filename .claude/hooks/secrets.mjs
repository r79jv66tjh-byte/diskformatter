// Secret guard (PreToolUse on Write/Edit/MultiEdit/NotebookEdit): stops writing passwords, keys and
// tokens into files. Secrets belong into Cloudflare secrets or the git-ignored .dev.vars.
// The same kind of check runs on GitHub for every pull request (Gitleaks, .github/workflows/pruefungen.yml).
import { decide, hook } from './lib.mjs';

// Well-known token formats (prefix + random part)
const KNOWN = [
  ['private key', /-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----/],
  ['AWS key', /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ['GitHub token', /\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b/],
  ['Anthropic/OpenAI key', /\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}/],
  ['Stripe key', /\b[sr]k_live_[A-Za-z0-9]{20,}\b/],
  ['Google API key', /\bAIza[0-9A-Za-z_-]{35}\b/],
  ['Slack token', /\bxox[abposr]-[A-Za-z0-9-]{10,}/],
  ['JSON Web Token', /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
  ['URL with password', /\b[a-z][a-z0-9+.-]*:\/\/[^\s:/@'"]+:[^\s@'"]{6,}@[^\s'"]+/i],
];

// name = "value" or name = value (YAML, shell, INI) with a name that sounds secret and a value that looks random
const ASSIGN =
  /\b([A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret|auth)[A-Za-z0-9_-]*)["']?\s*[:=]\s*(?:["'`]([^"'`\s]{8,})["'`]|([A-Za-z0-9_+/=.:-]{8,})(?=[\s,;]|$))/gim;
// Passwords are often short and chosen by people ('supersecret123'), tokens are long and generated.
const PASSWORD_NAME = /pass(?:word|wd)/i;
// Names that describe a password field instead of holding one ('PASSWORD_FIELD', 'password_hint')
const DESCRIBES = /(?:field|input|label|name|hint|prompt|message|msg|text|placeholder|selector|column|param|type|key|id|url|path|route)$/i;
// People pick passphrases like 'correct-horse-battery-staple': readable, so random() lets them pass.
const passphrase = (name, v) =>
  PASSWORD_NAME.test(name) && !DESCRIBES.test(name) && !/pass/i.test(v) && !PLACEHOLDER.test(v) &&
  /^[a-z]+(?:[-_.][a-z]+){2,}\d*$/i.test(v);
const PLACEHOLDER = /^(?:x+|\*+|\.+|<.*>|\$\{.*\}|your[-_].*|example.*|changeme|dummy.*|test.*|fake.*|placeholder.*)$/i;

const random = (v, password) => {
  if (v.length < (password ? 8 : 16)) return false;
  if (PLACEHOLDER.test(v) || /^(?:\/|\.\/|https?:)/.test(v)) return false;
  // Readable names like 'app-push-token-v2' (words with separators, at most a short number per word)
  if (/^[a-z]+\d{0,3}(?:[._:-][a-z]*\d{0,3})+$/i.test(v) || /^[a-z]+$/i.test(v)) return false;
  const kinds = [/[a-z]/, /[A-Z]/, /\d/].filter((r) => r.test(v)).length;
  const distinct = new Set(v).size;
  return kinds >= 2 && distinct >= (password ? 6 : 10);
};

/** Returns the kind of secret found in `text`, or ''. */
function findSecret(text) {
  for (const [kind, re] of KNOWN) if (re.test(text)) return kind;
  for (const m of text.matchAll(ASSIGN)) {
    const value = m[2] ?? m[3];
    if (/^public/i.test(m[1])) continue;
    if (random(value, PASSWORD_NAME.test(m[1])) || passphrase(m[1], value)) return `value of "${m[1]}"`;
  }
  return '';
}

// Files where secrets may live on purpose (all git-ignored)
const ALLOWED = /(?:^|[\\/])(?:\.dev\.vars[^\\/]*|\.env(?:\.[^\\/]*)?|\.claude-bridge\.(?:env|token))$/;

hook(({ tool_name: tool, tool_input: input = {} }) => {
  const path = input.file_path || input.notebook_path || '';
  if (ALLOWED.test(path)) return;
  const text =
    tool === 'Write' ? input.content :
    tool === 'Edit' ? input.new_string :
    tool === 'MultiEdit' ? (input.edits || []).map((e) => e.new_string).join('\n') :
    tool === 'NotebookEdit' ? input.new_source : '';
  const kind = findSecret(String(text || ''));
  if (!kind) return;
  return decide(
    'deny',
    `Secret guard: this looks like a secret (${kind}) in ${path}. Never put secrets into the repository – ` +
      'use a Cloudflare secret (npx wrangler secret put NAME) or the git-ignored .dev.vars for local runs. ' +
      'If it is only an example, use an obvious placeholder such as "your-token-here".',
  );
});
