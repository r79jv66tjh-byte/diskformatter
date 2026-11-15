// Cost brake (PreToolUse on Read): reading a large file in full costs many tokens. Without a line
// range the first read of a large file is turned away with a hint; reading with offset/limit
// (or searching with Grep) always works, so nothing is ever impossible.
import { statSync, readFileSync } from 'node:fs';
import { decide, hook } from './lib.mjs';

const MAX_LINES = 800;
const MAX_BYTES = 60_000;
// Pictures, PDFs and notebooks are read differently
const SKIP = /\.(?:png|jpe?g|gif|webp|svg|ico|pdf|ipynb)$/i;

hook(({ tool_input: input = {} }) => {
  const path = input.file_path || '';
  if (!path || SKIP.test(path) || input.offset != null || input.limit != null) return;
  const size = statSync(path).size;
  if (size <= 8_000) return;
  const lines = readFileSync(path, 'utf8').split('\n').length;
  if (lines <= MAX_LINES && size <= MAX_BYTES) return;
  return decide(
    'deny',
    `Cost brake: ${path} has ${lines} lines (${Math.round(size / 1000)} kB). Search it with Grep first, ` +
      'then read only the needed part with offset/limit. If the whole file is really needed, read it with an explicit limit.',
  );
});
