import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { recentConversation, suggestionPrompt } from './context.mjs';

// Explicit opt-in: never choose a provider or model for the owner.
const PROVIDER = process.env.PI_SUGGEST_PROVIDER;
const MODEL = process.env.PI_SUGGEST_MODEL;

export default function (pi: ExtensionAPI) {
  let pending: AbortController | undefined;
  let epoch = 0;
  const leaveSession = () => { epoch++; pending?.abort('session'); };
  pi.on('session_before_switch', leaveSession);
  pi.on('session_before_fork', leaveSession);
  pi.on('session_before_tree', leaveSession);
  pi.on('session_shutdown', leaveSession);
  pi.registerCommand('suggest-next', {
    description: 'Draft a next user prompt; /suggest-next cancel cancels drafting without submitting',
    handler: async (args, ctx) => {
      if (ctx.mode !== 'tui') return;
      if (args.trim() === 'cancel') {
        if (pending) pending.abort('user');
        else ctx.ui.notify('No prompt suggestion is being drafted.', 'info');
        return;
      }
      if (args.trim()) { ctx.ui.notify('Use /suggest-next or /suggest-next cancel.', 'warning'); return; }
      if (pending) { ctx.ui.notify('Already drafting. Use /suggest-next cancel to cancel.', 'info'); return; }
      if (ctx.ui.getEditorText().trim()) {
        ctx.ui.notify('Editor is not empty; clear or submit your draft first.', 'warning');
        return;
      }
      const conversation = recentConversation(ctx.sessionManager.getBranch());
      if (!conversation) {
        ctx.ui.notify('No conversation text to suggest from yet.', 'warning');
        return;
      }
      if (!PROVIDER || !MODEL) {
        ctx.ui.notify('Set PI_SUGGEST_PROVIDER and PI_SUGGEST_MODEL before loading this extension.', 'warning');
        return;
      }
      const model = ctx.modelRegistry.find(PROVIDER, MODEL);
      if (!model || !ctx.modelRegistry.hasConfiguredAuth(model)) {
        ctx.ui.notify(`${PROVIDER}/${MODEL} is not available.`, 'error');
        return;
      }
      const controller = new AbortController();
      pending = controller;
      const token = epoch;
      const session = ctx.sessionManager.getSessionId();
      const current = () => token === epoch && session === ctx.sessionManager.getSessionId();
      const timeout = setTimeout(() => controller.abort('timeout'), 30_000);
      let onAbort: (() => void) | undefined;
      ctx.ui.setStatus('suggest-next', 'Drafting next prompt · /suggest-next cancel');
      try {
        const request = ctx.modelRegistry.streamSimple(model, {
          messages: [{
            role: 'user',
            content: [{ type: 'text', text: suggestionPrompt(pi.getSessionName(), conversation) }],
            timestamp: Date.now(),
          }],
        }, { maxTokens: 300, signal: controller.signal }).result();
        const response = await Promise.race([request, new Promise<never>((_resolve, reject) => {
          onAbort = () => reject(new Error('Draft cancelled.'));
          controller.signal.addEventListener('abort', onAbort, { once: true });
          if (controller.signal.aborted) onAbort();
        })]);
        if (!current() || controller.signal.aborted) return;
        if (response.stopReason !== 'stop') throw new Error('model did not finish');
        const suggestion = response.content.filter(part => part.type === 'text')
          .map(part => part.text).join('\n').trim();
        if (!suggestion) throw new Error('model returned no prompt');
        if (ctx.ui.getEditorText().trim()) {
          ctx.ui.notify('Editor changed while drafting; suggestion was not inserted.', 'warning');
          return;
        }
        ctx.ui.setEditorText(suggestion.slice(0, 800));
        ctx.ui.notify('Suggested prompt loaded. Edit it before sending.', 'info');
      } catch {
        if (current()) {
          if (controller.signal.reason === 'user') ctx.ui.notify('Prompt suggestion cancelled; editor left unchanged.', 'info');
          else if (controller.signal.reason === 'timeout') ctx.ui.notify('Prompt suggestion timed out; editor left unchanged.', 'warning');
          else ctx.ui.notify('Could not draft a prompt; editor left unchanged.', 'error');
        }
      } finally {
        clearTimeout(timeout);
        if (onAbort) controller.signal.removeEventListener('abort', onAbort);
        if (pending === controller) {
          pending = undefined;
          ctx.ui.setStatus('suggest-next', undefined);
        }
      }
    },
  });
}
