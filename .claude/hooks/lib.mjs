// Shared helpers for the Claude Code hooks in this folder (registered in .claude/settings.json).
// A hook gets one JSON object on stdin and answers with JSON on stdout (exit code 0).
// Any internal error lets the action through: a broken guard must never block the work.

/** Reads the hook input, runs `check(input)` and prints its answer (if any). */
export async function hook(check) {
  try {
    let raw = '';
    for await (const chunk of process.stdin) raw += chunk;
    const answer = await check(JSON.parse(raw || '{}'));
    if (answer) process.stdout.write(JSON.stringify(answer));
  } catch (error) {
    // Fail open, but leave a trace in the hook log
    process.stderr.write(`hook failed, action let through: ${error}\n`);
  }
  process.exit(0);
}

/** Answer for PreToolUse: 'deny' stops the tool, 'ask' asks the person; the reason is shown to Claude/the person. */
export const decide = (decision, reason) => ({
  hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: decision, permissionDecisionReason: reason },
});

/** Extra text for Claude alongside the person's message (UserPromptSubmit). */
export const context = (text) => ({ hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: text } });
