import { createRequire, registerHooks } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Resolve the business plugin's peer packages from the installed DSH runtime.
// This runs inside Electron's Node process, which can read app.asar modules.
const runtimeRoot = process.env.VISTA_DSH_MODULE_ROOT ||
  '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh';
const runtimeRequire = createRequire(`${runtimeRoot}/package.json`);

export function packagedModule(name) {
  return pathToFileURL(runtimeRequire.resolve(name)).href;
}

let registered = false;
export function installBusinessResolution() {
  if (registered) return;
  registerHooks({
    resolve(specifier, context, nextResolve) {
      try { return nextResolve(specifier, context); }
      catch (error) {
        if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
        if (specifier.startsWith('@deepseek-ai/')) return { url: packagedModule(specifier), shortCircuit: true };
        const businessExports = {
          'dsh-shader/tools': 'tools.js',
          'dsh-shader/session-role': 'session-role.js',
          'dsh-shader/budget': 'budget-plugin.js',
          'dsh-shader/diagnosis-shell': 'diagnosis-shell.js',
        };
        if (!businessExports[specifier] || !process.env.VISTA_BUSINESS_ROOT) throw error;
        return { url: pathToFileURL(resolve(process.env.VISTA_BUSINESS_ROOT, 'src', businessExports[specifier])).href, shortCircuit: true };
      }
    },
  });
  registered = true;
}
