// Cost brake (UserPromptSubmit): every message re-sends the whole conversation, so long sessions get
// expensive. When the conversation file grows past a threshold, Claude gets a reminder (once per
// step of STEP_MB) to suggest a new session after the current step.
import { statSync, existsSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { context, hook } from './lib.mjs';

const START_MB = 6;
const STEP_MB = 3;

hook(({ transcript_path: file, session_id: id = 'x' }) => {
  if (!file || !existsSync(file)) return;
  const mb = statSync(file).size / 1e6;
  if (mb < START_MB) return;
  const level = Math.floor((mb - START_MB) / STEP_MB);
  const mark = join(tmpdir(), `claude-long-session-${String(id).replace(/\W/g, '')}-${level}`);
  if (existsSync(mark)) return;
  writeFileSync(mark, '');
  return context(
    `Cost brake: this session is long (conversation file about ${Math.round(mb)} MB), so every message costs more. ` +
      'Finish the current step, then briefly suggest to the owner to continue in a new session with a short handover ' +
      '(state, open wishes, next step) – in German, without jargon. Do not interrupt work in progress for this.',
  );
});
