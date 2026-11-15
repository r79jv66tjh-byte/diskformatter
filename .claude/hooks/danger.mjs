// Guard against dangerous commands (PreToolUse on Bash). Central template from AI-Coding-Tools.
// - always stopped: pushing to main (changes only via pull request) and force pushes
// - only with a question to the person: deleting folders, resetting/cleaning the working copy,
//   rewriting or deleting history (branches, refs)
import { execFileSync } from 'node:child_process';
import { decide, hook } from './lib.mjs';

const MAIN = /^(?:\+?(?:[^:\s]*:)?(?:refs\/heads\/)?(?:main|master))$/;
// Folders that are rebuilt anyway and may be deleted without asking
const SCRATCH = /^(?:\.\/)?(?:dist|build|shots|release|node_modules|\.astro|\.wrangler|coverage|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache)(?:\/.*)?$|^(?:\/tmp|\$\{?TMPDIR\}?)\//;

/**
 * Splits a shell line into simple commands and those into words (good enough for a guard). Quoted
 * text stays one word and heredoc bodies are skipped, so messages that only mention a command
 * (commit messages, PR texts) do not trigger; redirections like `2>/dev/null` are dropped.
 */
function commands(line) {
  const text = line.replace(/<<-?\s*(['"]?)(\w+)\1([^\n]*)\n[\s\S]*?\n[ \t]*\2[ \t]*(?=\n|$)/g, ' $3');
  const list = [[]];
  let word = null;
  let quote = '';
  const end = () => {
    if (word !== null) list.at(-1).push(word);
    word = null;
  };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (c === quote) quote = '';
      else if (c === '\\' && quote === '"' && i + 1 < text.length) word += text[++i];
      else word += c;
    } else if (c === '"' || c === "'") {
      quote = c;
      word ??= '';
    } else if (c === '\\' && i + 1 < text.length) {
      word = (word ?? '') + text[++i];
    } else if (/[;|&\n()]/.test(c) && !(c === '&' && (text[i - 1] === '>' || text[i + 1] === '>'))) {
      end();
      list.push([]);
    } else if (/\s/.test(c)) end();
    else word = (word ?? '') + c;
  }
  end();
  return list
    .map((words) => {
      const out = [];
      for (let k = 0; k < words.length; k++) {
        if (/^(?:\d*|&)>>?&?$|^\d*<$/.test(words[k])) k++; // operator alone: skip its target too
        else if (!/^(?:\d*|&)>|^\d*</.test(words[k])) out.push(words[k]);
      }
      while (/^\w+=/.test(out[0] ?? '')) out.shift();
      // Skip wrappers like `sudo`, `env`, `command`, `time`
      while (/^(?:sudo|env|command|time|nohup|exec)$/.test(out[0] ?? '')) out.shift();
      return out;
    })
    .filter((w) => w.length);
}

const branch = (cwd) => {
  try {
    return execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return '';
  }
};
const flags = (words) => words.filter((w) => w.startsWith('-'));
const has = (words, short, long) =>
  flags(words).some((f) => f === long || (short && !f.startsWith('--') && f.slice(1).includes(short)));

function check(words, cwd) {
  if (words[0] === '{') words = words.slice(1);
  const [cmd, ...rest] = words;
  // sh -c "…", bash -c "…": check the inner command line, too
  if (/^(?:ba|z|da)?sh$/.test(cmd ?? '') && rest[0] === '-c' && rest[1]) return worst(commands(rest[1]).map((w) => check(w, cwd)));
  if (cmd === 'git') {
    // git -C dir … → drop global options before the sub-command
    while (rest[0]?.startsWith('-')) rest.splice(0, /^(?:-C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)$/.test(rest[0]) ? 2 : 1);
    const [sub, ...a] = rest;
    // Words that are not options (option values like `-o x` or `--repo x` do not count)
    const plain = a.filter((w, k) => !w.startsWith('-') && !/^(?:-o|--push-option|--repo|--receive-pack|--exec)$/.test(a[k - 1] ?? ''));
    // HEAD / @ stand for the current branch
    const target = (r) => r.replace(/^(\+?)(?:HEAD|@)(?=$|:)/, (_, plus) => plus + branch(cwd));
    if (sub === 'push') {
      if (has(a, 'f', '--force') || a.includes('--mirror') || plain.slice(1).some((r) => r.startsWith('+')))
        return ['deny', 'Force pushes are not allowed (they overwrite history on GitHub).'];
      if (plain.slice(1).some((r) => MAIN.test(target(r))) || a.includes('--all') || (plain.length <= 1 && MAIN.test(branch(cwd))))
        return ['deny', 'Pushing to main is blocked: every change goes through its own branch and a pull request.'];
      if (a.some((w) => w.startsWith('--force-with-lease')))
        return ['ask', 'Force push with lease: overwrites the branch on GitHub.'];
      if (a.includes('--delete') || a.includes('-d') || plain.slice(1).some((r) => r.startsWith(':')))
        return ['ask', 'Deletes a branch on GitHub.'];
    }
    if (sub === 'reset' && a.includes('--hard')) return ['ask', 'git reset --hard throws away changes that are not committed.'];
    if (sub === 'clean' && has(a, 'f', '--force')) return ['ask', 'git clean deletes files that are not in git.'];
    if ((sub === 'checkout' || sub === 'restore') && (a.includes('.') || (sub === 'checkout' && has(a, 'f', '--force'))))
      return ['ask', `git ${sub} discards uncommitted changes.`];
    if (sub === 'branch' && (a.includes('-D') || (has(a, 'd', '--delete') && has(a, 'f', '--force'))))
      return ['ask', 'Deletes a local branch even if it is not merged.'];
    if (sub === 'filter-branch' || sub === 'filter-repo' || (sub === 'update-ref' && a.includes('-d')) || (sub === 'reflog' && a[0] === 'expire'))
      return ['ask', 'Rewrites or deletes history.'];
    if (sub === 'stash' && (a[0] === 'clear' || a[0] === 'drop')) return ['ask', 'Deletes saved changes (stash).'];
  }
  if (cmd === 'rm' && (has(rest, 'r', '--recursive') || has(rest, 'R'))) {
    const targets = rest.filter((w) => !w.startsWith('-'));
    if (targets.some((t) => !SCRATCH.test(t) || t.split('/').includes('..'))) return ['ask', `Deletes folders: ${targets.join(' ')}`];
  }
  if (cmd === 'gh' && rest[0] === 'pr' && rest[1] === 'merge') return ['ask', 'Merging only after the owner said yes.'];
  if (cmd === 'find' && rest.includes('-delete')) return ['ask', 'find -delete removes files.'];
  return null;
}

/** The strongest of several results: deny before ask before nothing. */
function worst(results) {
  const found = results.filter(Boolean);
  return found.find(([d]) => d === 'deny') ?? found[0] ?? null;
}

hook(({ tool_input: input = {}, cwd }) => {
  const hit = worst(commands(String(input.command || '')).map((w) => check(w, cwd || process.cwd())));
  if (hit) return decide(hit[0], `Command guard: ${hit[1]}`);
});
