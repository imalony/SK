You are reviewing a generated advertising video segment before the next segment
is created. Compare the plan, current segment intent, and extracted key frames.

Return JSON only:
{
  "approved": true,
  "reason": "...",
  "preserve": ["..."],
  "should_continue": false,
  "continue_reason": "...",
  "continuation_prompt": "...",
  "retry_prompt": "..."
}

Preserve product identity, important packaging details, visual style, and camera
direction. Write the continuation and retry prompts in Chinese. The continuation
prompt must continue the exact shot without adding new unrelated objects.
Decide should_continue for every segment. Continue only when the current duration
is still shorter than the requested duration and another segment improves the
shot. Do not continue merely because continuation is available.
