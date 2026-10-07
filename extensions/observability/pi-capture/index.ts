import { homedir } from 'node:os';
import { join } from 'node:path';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { recordOccurrence } from './storage.mjs';
import { loadSecrets, redactText } from './redact.mjs';
import { loadKnownFixes, matchingFixes, knownFixNudge, notifiableFixes } from './known-fixes.mjs';
import { isIntentionalFailureProbe } from './probe.mjs';
import { structuredMatch } from './structured-match.mjs';
import { expandedSkillName, recordSkillEvent, skillNameFromRead } from './skill-events.mjs';

function textsFromMessage(message: any): string | null {
  if (message?.role !== 'user' && message?.role !== 'assistant') return null;
  const content = message?.content;
  if (typeof content === 'string') return content.slice(0, 2000);
  if (!Array.isArray(content)) return null;
  return content.filter((part) => part?.type === 'text').map((part) => part.text).join('\n').slice(0, 2000);
}

function contentText(content: any[]): string {
  return content.filter((part) => part?.type === 'text').map((part) => part.text).join('\n').slice(0, 12000);
}


export default function (pi: ExtensionAPI) {
  const agent = process.env.PI_CAPTURE_AGENT;
  // Installation must set the identity explicitly; silently guessing can misattribute incidents.
  const origin = process.env.PI_CAPTURE_ORIGIN;
  if (!agent || !origin) {
    process.stderr.write('pi-capture: agent or origin not configured; capture disabled\n');
    return;
  }
  const root = process.env.PI_CAPTURE_ROOT || join(homedir(), '.local', 'state', 'pi-capture');
  const fixesPath = process.env.PI_CAPTURE_FIXES || join(root, 'known-fixes.json');
  const nudgeMode = process.env.PI_CAPTURE_NUDGE_MODE || 'inject';
  const enabledSkills = new Set((process.env.PI_CAPTURE_ENABLED_SKILLS || '').split(',').map((s) => s.trim()).filter(Boolean));
  let secrets: string[];
  try { secrets = loadSecrets(); } catch {
    process.stderr.write('pi-capture: redaction configuration unavailable; capture disabled\n');
    return;
  }
  const messages: string[] = [];
  const starts = new Map<string, number>();
  const cooldowns = new Map<string, number>();
  const structuredCooldowns = new Map<string, number>();
  let fixes = loadKnownFixes(fixesPath);
  const pendingSkillCommands: string[] = [];
  const recordSkill = (skillName: string, eventKind: 'loaded' | 'invoked', source: 'read' | 'skill_command') => {
    try { recordSkillEvent({ root, agent, origin, skillName, eventKind, source }); }
    catch (error) {
      // Never let telemetry change a prompt or tool result, or echo private paths.
      process.stderr.write(`pi-capture: failed to record skill event: ${error instanceof Error ? error.name : 'unknown'}\n`);
    }
  };
  const recordExpandedSkill = (text: string) => {
    const name = expandedSkillName(text);
    if (!name) return;
    const index = pendingSkillCommands.indexOf(name);
    if (index < 0) return;
    pendingSkillCommands.splice(index, 1);
    recordSkill(name, 'invoked', 'skill_command');
  };

  pi.on('session_start', () => {
    messages.length = 0; starts.clear(); pendingSkillCommands.length = 0;
    structuredCooldowns.clear();
    fixes = loadKnownFixes(fixesPath);
  });
  pi.on('input', (event) => {
    const match = /^\/skill:([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s|$)/.exec(event.text);
    if (match && pendingSkillCommands.length < 16) pendingSkillCommands.push(match[1]);
  });
  pi.on('before_agent_start', (event) => { recordExpandedSkill(event.prompt); });
  pi.on('message_end', (event) => {
    const text = textsFromMessage(event.message);
    if (event.message?.role === 'user' && text) recordExpandedSkill(text);
    if (text) { messages.push(text); if (messages.length > 5) messages.shift(); }
  });
  pi.on('tool_execution_start', (event) => { starts.set(event.toolCallId, Date.now()); });
  pi.on('tool_result', async (event, ctx) => {
    if (event.toolName === 'read' && !event.isError) {
      const name = skillNameFromRead((event.input as any)?.path);
      if (name) recordSkill(name, 'loaded', 'read');
    }
    try {
      const output = contentText(event.content);
      const tail = event.content.filter((part) => part?.type === 'text')
        .map((part) => part.text).join('\n').slice(-200);
      const exitMatch = /(?:^|\n)Command exited with code (\d+)\s*$/.exec(tail);
      const exitCode = exitMatch ? Number(exitMatch[1]) : null;
      const duration = starts.has(event.toolCallId) ? Date.now() - starts.get(event.toolCallId)! : undefined;
      const matchText = `${output}\n${event.toolName === 'bash' ? String((event.input as any)?.command ?? '') : ''}`;
      const isProbe = event.toolName === 'bash' && isIntentionalFailureProbe((event.input as any)?.command);
      const matches = event.isError && !isProbe
        ? matchingFixes(fixes, { agent, enabledSkills, text: matchText }) : [];
      const structured = nudgeMode === 'dry-run' && event.isError && event.toolName === 'bash' && !isProbe
        ? await structuredMatch({ agent,
            command: redactText(String((event.input as any)?.command ?? ''), secrets).slice(0, 2048),
            output: redactText(output, secrets).slice(0, 4096), exit_code: exitCode }) : null;
      const candidates = Array.isArray(structured?.candidates) ? structured.candidates.filter((item: any) =>
        typeof item?.fix_id === 'string' && /^[a-z0-9][a-z0-9_-]{2,79}$/.test(item.fix_id) &&
        Number.isInteger(item?.score) && item.score >= 0 && item.score <= 100 &&
        item.matcher_version === 1 && item.disposition === 'conditional_hint') : [];
      const dueCandidates = candidates.filter((item: any) =>
        !structuredCooldowns.has(item.fix_id) || Date.now() - structuredCooldowns.get(item.fix_id)! >= 300_000);
      for (const item of dueCandidates) structuredCooldowns.set(item.fix_id, Date.now());
      starts.delete(event.toolCallId);
      const row = recordOccurrence({
        root, agent, origin, kind: 'tool', outcome: event.isError ? 'error' : 'success',
        toolName: event.toolName, input: event.input,
        output: event.isError ? { content: output, details: event.details } : undefined,
        messages: event.isError ? messages : [],
        sessionId: ctx.sessionManager.getSessionId(), toolCallId: event.toolCallId,
        durationMs: duration, matchedFixIds: matches.map((fix) => fix.id),
        candidateMatches: candidates.map((item: any) => ({
          fix_id: item.fix_id, score: item.score, matcher_version: item.matcher_version,
          disposition: item.disposition,
          shown_to_user: Boolean(ctx.hasUI && dueCandidates.some((due: any) => due.fix_id === item.fix_id)),
        })),
        exitCode, extraSecrets: secrets,
      });
      if (!event.isError) return;
      const dueMatches = notifiableFixes(matches, cooldowns);
      if (nudgeMode === 'dry-run') {
        if (!ctx.hasUI) return;
        const hints = [];
        const structuredIds = new Set(dueCandidates.map((item: any) => item.fix_id));
        const regexOnly = dueMatches.filter((fix) => !structuredIds.has(fix.id));
        if (regexOnly.length) hints.push(knownFixNudge(regexOnly, row.id));
        if (dueCandidates.length) hints.push(`Structured candidates (ranking score, not confidence): ${dueCandidates.map((item: any) =>
          `${item.fix_id} (${item.score}/100)`).join(', ')}. Check context before using any fix.`);
        if (structured?.stderr_check?.check_id === 'stderr-suppressed-on-failure')
          hints.push(structured.stderr_check.message);
        if (hints.length) ctx.ui.notify(`Dry run for ${row.id} (user only): ${hints.join(' ')}`, 'info');
        return;
      }
      // Persist eligible matches even during cooldown. Cooldown controls only
      // the nudge, so on-demand lookup still works for a repeated error.
      if (dueMatches.length === 0) return;
      if (nudgeMode !== 'inject') return;
      pi.sendMessage({
        customType: 'known-fix',
        content: knownFixNudge(matches, row.id),
        display: true,
        details: { occurrence_id: row.id },
      }, { triggerTurn: false });
    } catch (error) {
      // Capture must never change tool execution or leak tool content through diagnostics.
      process.stderr.write(`pi-capture: failed to record occurrence: ${error instanceof Error ? error.name : 'unknown'}\n`);
    }
  });
}
