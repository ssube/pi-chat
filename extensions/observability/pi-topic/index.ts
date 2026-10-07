import { execFile } from 'node:child_process';
import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { Text } from '@earendil-works/pi-tui';
import { loadSecrets } from '../common/redact.mjs';
import { parseTopic, recentActivity, topicPrompt } from './topic.mjs';

const PERIOD = 5 * 60 * 1000;

export default function (pi: ExtensionAPI) {
  let timer: ReturnType<typeof setInterval> | undefined;
  let controller: AbortController | undefined;
  let generation = 0;
  let lastAttempt = 0;
  let lastActivity = '';

  // Custom entries render in the transcript but never enter agent/model context.
  pi.registerEntryRenderer<{ message: string }>('pi-topic-status', (entry, _options, theme) =>
    new Text(theme.fg('dim', `* ${entry.data?.message ?? 'Topic update'}`), 0, 0));

  function report(message: string) {
    pi.appendEntry('pi-topic-status', { message });
  }

  function stop() {
    generation++;
    if (timer) clearInterval(timer);
    timer = undefined;
    controller?.abort();
    controller = undefined;
  }

  async function update(ctx: ExtensionContext, force = false, timerTick = false) {
    if (ctx.mode !== 'tui' || (timerTick && !ctx.isIdle())) return;
    if (controller) { if (force) report('Topic refresh already running'); return; }
    let secrets: string[];
    try { secrets = loadSecrets(); } catch {
      if (force) report('Topic refresh unavailable: redaction configuration');
      return;
    } // Explicit redaction config must fail closed.
    const activity = recentActivity(ctx.sessionManager.getBranch(), secrets);
    if (!activity) { if (force) report('No session activity to name yet'); return; }
    if (!force && (activity === lastActivity || Date.now() - lastAttempt < PERIOD)) return;
    const provider = process.env.PI_TOPIC_PROVIDER;
    const modelId = process.env.PI_TOPIC_MODEL;
    const model = provider && modelId ? ctx.modelRegistry.find(provider, modelId) : undefined;
    if (!model || !ctx.modelRegistry.hasConfiguredAuth(model)) {
      if (force) report('Topic refresh unavailable: naming model');
      return;
    }
    lastAttempt = Date.now();
    const current = ++generation;
    const abort = new AbortController();
    controller = abort;
    ctx.ui.setStatus('pi-topic', '… Refreshing topic');
    const timeout = setTimeout(() => abort.abort(), 20_000);
    try {
      const reply = await ctx.modelRegistry.streamSimple(model, {
        messages: [{ role: 'user', content: [{ type: 'text', text: topicPrompt(activity) }], timestamp: Date.now() }],
      }, { reasoning: 'off', maxTokens: 180, signal: abort.signal }).result();
      if (abort.signal.aborted || current !== generation) return;
      if (reply.stopReason !== 'stop') { report('Topic refresh failed; previous title kept'); return; }
      const topic = parseTopic(reply.content.filter(c => c.type === 'text').map(c => c.text).join(''), secrets);
      if (!topic) { report('Topic refresh returned no usable label; previous title kept'); return; }
      lastActivity = activity;
      if (pi.getSessionName() !== topic.name) pi.setSessionName(topic.name);
      ctx.ui.setTitle(`π - ${topic.topic}`);
      // Pane ID is inherited from this Pi process; never target an entire window or session.
      const inTmux = Boolean(process.env.TMUX && /^%\d+$/.test(process.env.TMUX_PANE ?? ''));
      const paneUpdated = !inTmux || await new Promise<boolean>(resolve => {
        execFile('tmux', ['select-pane', '-t', process.env.TMUX_PANE!, '-T', topic.label],
          { timeout: 2000 }, error => resolve(!error));
      });
      if (current === generation) report(paneUpdated
        ? `Topic: ${topic.name}${inTmux ? ` · pane: ${topic.label}` : ''}`
        : `Topic: ${topic.name} · pane update failed`);
    } catch {
      if (current === generation) report('Topic refresh failed; previous title kept');
    } finally {
      clearTimeout(timeout);
      if (controller === abort) {
        controller = undefined;
        ctx.ui.setStatus('pi-topic', undefined);
      }
    }
  }

  pi.on('session_start', (_event, ctx) => {
    stop();
    lastActivity = '';
    lastAttempt = 0;
    if (ctx.mode !== 'tui') return;
    timer = setInterval(() => void update(ctx, false, true), PERIOD);
    // A resumed session already has activity; wait until it is idle before naming it.
    void update(ctx);
  });
  pi.on('agent_settled', (_event, ctx) => { void update(ctx); });
  pi.on('session_shutdown', (_event, ctx) => {
    stop();
    if (ctx.mode === 'tui') ctx.ui.setStatus('pi-topic', undefined);
  });

  pi.registerCommand('topic-refresh', {
    description: 'Refresh the current session name, terminal topic and tmux pane label now',
    handler: async (_args, ctx) => { await update(ctx, true); },
  });
}
