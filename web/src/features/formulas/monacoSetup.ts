import { loader } from '@monaco-editor/react';
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js';
import 'monaco-editor/esm/vs/editor/contrib/suggest/browser/suggestController.js';
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';

loader.config({ monaco });

(
  globalThis as typeof globalThis & {
    MonacoEnvironment?: { readonly getWorker: () => Worker };
  }
).MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};
