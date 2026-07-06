import { act, render } from '@testing-library/react';
import type { ReactNode } from 'react';

import { FormulaEditor } from './FormulaEditor';
import type { TdxDocumentationEntry } from './tdxLanguage';

const monacoHarness = vi.hoisted((): { editor: unknown; monaco: unknown } => ({
  editor: null,
  monaco: null,
}));

vi.mock('./monacoSetup', () => ({}));

vi.mock('@monaco-editor/react', async () => {
  const { useEffect, useRef } = await import('react');
  return {
    loader: { config: vi.fn() },
    default: ({
      beforeMount,
      onMount,
    }: {
      readonly beforeMount?: (monaco: unknown) => void;
      readonly onMount?: (editor: unknown, monaco: unknown) => void;
      readonly children?: ReactNode;
    }) => {
      const mounted = useRef(false);
      useEffect(() => {
        if (mounted.current) return;
        mounted.current = true;
        beforeMount?.(monacoHarness.monaco);
        onMount?.(monacoHarness.editor, monacoHarness.monaco);
      }, [beforeMount, onMount]);
      return <div aria-label="Monaco 测试编辑器" />;
    },
  };
});

type Provider = {
  readonly provideCompletionItems: (
    model: {
      readonly getWordUntilPosition: () => {
        readonly startColumn: number;
        readonly endColumn: number;
      };
    },
    position: { readonly lineNumber: number },
  ) => { readonly suggestions: readonly { readonly label: string }[] };
};

function createHarness() {
  const providers: Provider[] = [];
  const hoverProviders: {
    readonly provideHover: (
      model: { readonly getWordAtPosition: () => Word },
      position: Position,
    ) => { readonly contents: readonly { readonly value: string }[] } | null;
  }[] = [];
  const signatureProviders: {
    readonly provideSignatureHelp: (
      model: { readonly getValueInRange: () => string },
      position: Position,
    ) => {
      readonly value: {
        readonly signatures: readonly { readonly label: string }[];
      };
    } | null;
  }[] = [];
  const oldGenerationDisposals: ReturnType<typeof vi.fn>[] = [];
  const providerDisposals: ReturnType<typeof vi.fn>[] = [];
  let command: (() => void) | undefined;
  const disposable = () => {
    const dispose = vi.fn();
    oldGenerationDisposals.push(dispose);
    return { dispose };
  };
  const monaco = {
    KeyMod: { CtrlCmd: 1 },
    KeyCode: { Enter: 2 },
    Range: class {},
    editor: { setModelMarkers: vi.fn() },
    languages: {
      CompletionItemKind: { Function: 1, Field: 2 },
      CompletionItemInsertTextRule: { InsertAsSnippet: 4 },
      register: vi.fn(() => ({ dispose: vi.fn() })),
      setLanguageConfiguration: vi.fn(disposable),
      setMonarchTokensProvider: vi.fn(disposable),
      registerCompletionItemProvider: vi.fn(
        (_language: string, provider: Provider) => {
          providers.push(provider);
          const dispose = vi.fn();
          providerDisposals.push(dispose);
          oldGenerationDisposals.push(dispose);
          return { dispose };
        },
      ),
      registerHoverProvider: vi.fn((_language: string, provider: never) => {
        hoverProviders.push(provider);
        return disposable();
      }),
      registerSignatureHelpProvider: vi.fn(
        (_language: string, provider: never) => {
          signatureProviders.push(provider);
          return disposable();
        },
      ),
    },
  };
  const editor = {
    addCommand: vi.fn((_shortcut: number, callback: () => void) => {
      command = callback;
      return 'command-id';
    }),
    getModel: vi.fn(() => null),
  };
  return {
    editor,
    hoverProviders,
    monaco,
    oldGenerationDisposals,
    providerDisposals,
    providers,
    runCommand: () => command?.(),
    signatureProviders,
  };
}

type Position = { readonly lineNumber: number; readonly column: number };
type Word = {
  readonly word: string;
  readonly startColumn: number;
  readonly endColumn: number;
};

const ema: TdxDocumentationEntry = {
  name: 'EMA',
  signature: 'EMA(系列, 周期)',
  summary: '指数移动平均',
  details: '只使用当前和历史数据。',
  kind: 'function',
};

it('refreshes Monaco providers when async documentation arrives without leaking old providers', () => {
  const harness = createHarness();
  monacoHarness.monaco = harness.monaco;
  monacoHarness.editor = harness.editor;
  const firstValidate = vi.fn();
  const latestValidate = vi.fn();
  const { rerender } = render(
    <FormulaEditor
      diagnostics={[]}
      documentation={[]}
      onChange={vi.fn()}
      onValidate={firstValidate}
      source=""
    />,
  );

  expect(harness.providers).toHaveLength(1);
  expect(
    harness.providers[0]?.provideCompletionItems(
      { getWordUntilPosition: () => ({ startColumn: 1, endColumn: 1 }) },
      { lineNumber: 1 },
    ).suggestions,
  ).toHaveLength(0);

  rerender(
    <FormulaEditor
      diagnostics={[]}
      documentation={[ema]}
      onChange={vi.fn()}
      onValidate={latestValidate}
      source="EMA(CLOSE, 12);"
    />,
  );

  expect(harness.providers).toHaveLength(2);
  expect(harness.providerDisposals[0]).toHaveBeenCalledTimes(1);
  expect(harness.oldGenerationDisposals.slice(0, 5)).toSatisfy(
    (disposals: ReturnType<typeof vi.fn>[]) =>
      disposals.every((dispose) => dispose.mock.calls.length === 1),
  );
  expect(
    harness.providers[1]
      ?.provideCompletionItems(
        { getWordUntilPosition: () => ({ startColumn: 1, endColumn: 4 }) },
        { lineNumber: 1 },
      )
      .suggestions.map((item) => item.label),
  ).toEqual(['EMA']);
  expect(
    harness.hoverProviders[1]?.provideHover(
      {
        getWordAtPosition: () => ({
          word: 'EMA',
          startColumn: 1,
          endColumn: 4,
        }),
      },
      { lineNumber: 1, column: 2 },
    )?.contents[0]?.value,
  ).toContain('EMA(系列, 周期)');
  expect(
    harness.signatureProviders[1]?.provideSignatureHelp(
      { getValueInRange: () => 'EMA(' },
      { lineNumber: 1, column: 5 },
    )?.value.signatures[0]?.label,
  ).toBe('EMA(系列, 周期)');

  act(() => harness.runCommand());
  expect(firstValidate).not.toHaveBeenCalled();
  expect(latestValidate).toHaveBeenCalledTimes(1);
});
