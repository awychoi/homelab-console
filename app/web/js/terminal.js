import * as ui from './ui.js';
import { openTerminal } from './native.js';

async function copyFallback(command, reason) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(command);
      ui.toast('Terminal did not open', 'warn', reason ? `${reason} — command copied.` : 'Command copied.');
      return;
    } catch {
      // fall through
    }
  }
  ui.toast('Terminal did not open', 'warn', command);
}

window.hlcTerminalFailed = async (payload = {}) => {
  const command = window.hlcTerminalLastCommand || '';
  if (command) await copyFallback(command, payload.reason || 'Terminal could not open');
};


/** Ask the backend for a safe ssh command, then open Terminal (or copy it). */
export async function openHostTerminal(ctx, host, path) {
  try {
    const result = await ctx.api.get(
      `/api/hosts/${encodeURIComponent(host)}/terminal?path=${encodeURIComponent(path)}`,
    );
    window.hlcTerminalLastCommand = result.command;
    if (openTerminal(result.command)) {
      ui.toast('Opening Terminal', 'ok', `${host}:${path}`);
      return;
    }
    await copyFallback(result.command, 'No native Terminal bridge');
  } catch (error) {
    ui.toast('Could not prepare Terminal', 'err', ui.errorText(error));
  }
}
