export const scenarios = [
  {
    id: 'task-shift',
    entries: [
      { type: 'message', message: { role: 'user', content: 'Please fix the calendar export first.' } },
      { type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'Calendar export fixed and checked.' }] } },
      { type: 'message', message: { role: 'user', content: 'Now build a five-minute Pi session topic updater with a tmux label.' } },
      { type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'Writing the Pi extension and its tests.' }, { type: 'toolCall', name: 'write', arguments: { path: '/home/user/private.ts', token: 'secret' } }] } },
    ],
  },
  {
    id: 'investigation',
    entries: [
      { type: 'message', message: { role: 'user', content: 'Investigate why the image import timer is running but the latest database row is stale.' } },
      { type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'Checking the timer, queue and last imported row; cause is not yet confirmed.' }, { type: 'toolCall', name: 'bash', arguments: { command: 'cat /home/user/.env' } }] } },
      { type: 'message', message: { role: 'toolResult', content: [{ type: 'text', text: 'secret database output' }] } },
    ],
  },
  {
    id: 'finished',
    entries: [
      { type: 'message', message: { role: 'user', content: 'Add validation for duplicate event IDs and run the tests.' } },
      { type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'Duplicate event ID validation is implemented; the targeted tests passed.' }] } },
    ],
  },
  {
    id: 'redaction',
    entries: [
      { type: 'message', message: { role: 'user', content: 'Debug the login at https://example.invalid/auth?token=abc123 using alice@example.invalid and /home/alice/.config/client. Authorization: Bearer secret123' } },
      { type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'Investigating login failures without exposing credentials.' }, { type: 'toolCall', name: 'read', arguments: { path: '/home/alice/.config/client' } }] } },
    ],
  },
];
