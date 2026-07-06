import type * as MonacoNamespace from 'monaco-editor';
import type { editor, IDisposable, languages, Position } from 'monaco-editor';

import type { FormulaDiagnostic } from './formulaApi';

export const TDX_LANGUAGE_ID = 'stock-desk-tdx';
type Monaco = typeof MonacoNamespace;

export type TdxDocumentationEntry = {
  readonly name: string;
  readonly signature: string;
  readonly summary: string;
  readonly details: string;
  readonly kind: 'field' | 'function';
};

export type TdxLanguageDefinition = {
  readonly configuration: languages.LanguageConfiguration;
  readonly monarchTokensProvider: languages.IMonarchLanguage & {
    readonly keywords: readonly string[];
  };
};

export type TdxCompletionItem = {
  readonly label: string;
  readonly insertText: string;
  readonly documentation: string;
  readonly detail: string;
  readonly kind: TdxDocumentationEntry['kind'];
};

function snippetFromSignature(entry: TdxDocumentationEntry): string {
  if (entry.kind === 'field') return entry.name;
  const match = /^([A-Z][A-Z0-9_]*)\((.*)\)$/u.exec(entry.signature.trim());
  if (match === null) return `${entry.name}($0)`;
  const parameters = (match[2] ?? '')
    .split(',')
    .map((parameter) => parameter.trim())
    .filter(Boolean);
  return `${match[1] ?? entry.name}(${parameters
    .map((parameter, index) => `\${${String(index + 1)}:${parameter}}`)
    .join(', ')})`;
}

export function completionItems(
  entries: readonly TdxDocumentationEntry[],
): readonly TdxCompletionItem[] {
  return entries.map((entry) => ({
    label: entry.name,
    insertText: snippetFromSignature(entry),
    documentation: `${entry.summary}\n\n${entry.details}`,
    detail: entry.signature,
    kind: entry.kind,
  }));
}

export function createTdxLanguageDefinition(
  entries: readonly TdxDocumentationEntry[],
): TdxLanguageDefinition {
  const keywords = entries.map((entry) => entry.name);
  return {
    configuration: {
      comments: { lineComment: '//' },
      brackets: [
        ['(', ')'],
        ['[', ']'],
      ],
      autoClosingPairs: [
        { open: '(', close: ')' },
        { open: '[', close: ']' },
        { open: "'", close: "'" },
      ],
    },
    monarchTokensProvider: {
      keywords,
      ignoreCase: true,
      tokenizer: {
        root: [
          [/\/\/.*$/u, 'comment'],
          [
            /[A-Z_][A-Z0-9_]*/u,
            { cases: { '@keywords': 'keyword', '@default': 'identifier' } },
          ],
          [/-?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?/u, 'number'],
          [/:=|:|;|,/u, 'delimiter'],
          [/[+\-*/<>=]/u, 'operator'],
          [/[()[\]]/u, '@brackets'],
          [/\s+/u, 'white'],
        ],
      },
    },
  };
}

export function diagnosticMarkers(
  diagnostics: readonly FormulaDiagnostic[],
): readonly editor.IMarkerData[] {
  return diagnostics.map((diagnostic) => ({
    severity: 8,
    message: diagnostic.explanation,
    code: diagnostic.code,
    startLineNumber: diagnostic.span.line,
    startColumn: diagnostic.span.column,
    endLineNumber: diagnostic.span.endLine,
    endColumn: diagnostic.span.endColumn,
  }));
}

type TdxRegistration = {
  readonly entries: readonly TdxDocumentationEntry[];
  readonly disposables: readonly IDisposable[];
};

const registeredLanguages = new WeakSet<object>();
const registrations = new WeakMap<object, TdxRegistration>();

export function registerTdxLanguage(
  monaco: Monaco,
  entries: readonly TdxDocumentationEntry[],
): void {
  const previous = registrations.get(monaco);
  if (previous?.entries === entries) return;
  previous?.disposables.forEach((registration) => registration.dispose());

  const definition = createTdxLanguageDefinition(entries);
  if (!registeredLanguages.has(monaco)) {
    monaco.languages.register({ id: TDX_LANGUAGE_ID });
    registeredLanguages.add(monaco);
  }
  const completions = completionItems(entries);
  const disposables: IDisposable[] = [
    monaco.languages.setLanguageConfiguration(
      TDX_LANGUAGE_ID,
      definition.configuration,
    ),
    monaco.languages.setMonarchTokensProvider(
      TDX_LANGUAGE_ID,
      definition.monarchTokensProvider,
    ),
  ];
  disposables.push(
    monaco.languages.registerCompletionItemProvider(TDX_LANGUAGE_ID, {
      triggerCharacters: ['(', ','],
      provideCompletionItems(model: editor.ITextModel, position: Position) {
        const word = model.getWordUntilPosition(position);
        const range = {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: word.startColumn,
          endColumn: word.endColumn,
        };
        return {
          suggestions: completions.map((item) => ({
            label: item.label,
            kind:
              item.kind === 'function'
                ? monaco.languages.CompletionItemKind.Function
                : monaco.languages.CompletionItemKind.Field,
            insertText: item.insertText,
            insertTextRules:
              monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
            documentation: { value: item.documentation },
            detail: item.detail,
            range,
          })),
        };
      },
    }),
  );
  disposables.push(
    monaco.languages.registerHoverProvider(TDX_LANGUAGE_ID, {
      provideHover(model: editor.ITextModel, position: Position) {
        const word = model.getWordAtPosition(position);
        const entry = entries.find(
          (candidate) => candidate.name === word?.word.toUpperCase(),
        );
        if (entry === undefined) return null;
        return {
          range:
            word === null || word === undefined
              ? undefined
              : new monaco.Range(
                  position.lineNumber,
                  word.startColumn,
                  position.lineNumber,
                  word.endColumn,
                ),
          contents: [
            { value: `\`\`\`${entry.signature}\`\`\`` },
            { value: entry.summary },
            { value: entry.details },
          ],
        };
      },
    }),
  );
  disposables.push(
    monaco.languages.registerSignatureHelpProvider(TDX_LANGUAGE_ID, {
      signatureHelpTriggerCharacters: ['(', ','],
      provideSignatureHelp(model: editor.ITextModel, position: Position) {
        const prefix = model.getValueInRange({
          startLineNumber: position.lineNumber,
          startColumn: 1,
          endLineNumber: position.lineNumber,
          endColumn: position.column,
        });
        const match = /([A-Z][A-Z0-9_]*)\([^()]*$/iu.exec(prefix);
        const entry = entries.find(
          (candidate) =>
            candidate.kind === 'function' &&
            candidate.name === match?.[1]?.toUpperCase(),
        );
        if (entry === undefined) return null;
        const parameterText = /^.+\((.*)\)$/u.exec(entry.signature)?.[1] ?? '';
        const parameters = parameterText
          .split(',')
          .map((label) => ({ label: label.trim() }));
        const activeParameter = Math.min(
          parameters.length - 1,
          Math.max(0, (match?.[0].match(/,/gu) ?? []).length),
        );
        return {
          value: {
            activeParameter,
            activeSignature: 0,
            signatures: [
              {
                label: entry.signature,
                documentation: entry.summary,
                parameters,
              },
            ],
          },
          dispose() {},
        };
      },
    }),
  );
  registrations.set(monaco, { entries, disposables });
}

export function setTdxDiagnostics(
  monaco: Monaco,
  model: editor.ITextModel | null,
  diagnostics: readonly FormulaDiagnostic[],
): void {
  if (model === null) return;
  monaco.editor.setModelMarkers(model, TDX_LANGUAGE_ID, [
    ...diagnosticMarkers(diagnostics),
  ]);
}
