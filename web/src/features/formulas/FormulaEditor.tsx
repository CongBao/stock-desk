import Editor from '@monaco-editor/react';
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import type * as MonacoNamespace from 'monaco-editor';
import type { editor } from 'monaco-editor';

import './monacoSetup';

import type { FormulaDiagnostic } from './formulaApi';
import {
  registerTdxLanguage,
  setTdxDiagnostics,
  TDX_LANGUAGE_ID,
  type TdxDocumentationEntry,
} from './tdxLanguage';

export type FormulaEditorHandle = {
  readonly insertSnippet: (snippet: string) => void;
  readonly focus: () => void;
};

type MonacoInstance = typeof MonacoNamespace;

type FormulaEditorProps = {
  readonly diagnostics: readonly FormulaDiagnostic[];
  readonly documentation: readonly TdxDocumentationEntry[];
  readonly onChange: (source: string) => void;
  readonly onValidate: () => void;
  readonly source: string;
};

export const FormulaEditor = forwardRef<
  FormulaEditorHandle,
  FormulaEditorProps
>(function FormulaEditor(
  { diagnostics, documentation, onChange, onValidate, source },
  forwardedRef,
) {
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<MonacoInstance | null>(null);
  const onValidateRef = useRef(onValidate);
  onValidateRef.current = onValidate;

  const beforeMount = (monaco: MonacoInstance) => {
    monacoRef.current = monaco;
    registerTdxLanguage(monaco, documentation);
  };
  const onMount = (
    instance: editor.IStandaloneCodeEditor,
    monaco: MonacoInstance,
  ) => {
    editorRef.current = instance;
    monacoRef.current = monaco;
    setTdxDiagnostics(monaco, instance.getModel(), diagnostics);
    instance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () =>
      onValidateRef.current(),
    );
  };

  useEffect(() => {
    if (monacoRef.current !== null) {
      registerTdxLanguage(monacoRef.current, documentation);
    }
  }, [documentation]);

  useEffect(() => {
    if (monacoRef.current !== null && editorRef.current !== null) {
      setTdxDiagnostics(
        monacoRef.current,
        editorRef.current.getModel(),
        diagnostics,
      );
    }
  }, [diagnostics]);

  useImperativeHandle(
    forwardedRef,
    () => ({
      insertSnippet(snippet) {
        const instance = editorRef.current;
        const position = instance?.getPosition();
        if (
          instance === null ||
          instance === undefined ||
          position === null ||
          position === undefined
        ) {
          onChange(
            `${source}${source.length > 0 && !source.endsWith('\n') ? ' ' : ''}${snippet}`,
          );
          return;
        }
        instance.executeEdits('formula-library', [
          {
            range: {
              startLineNumber: position.lineNumber,
              startColumn: position.column,
              endLineNumber: position.lineNumber,
              endColumn: position.column,
            },
            text: snippet,
            forceMoveMarkers: true,
          },
        ]);
        const openingParenthesis = snippet.indexOf('(');
        const firstSeparator = snippet.indexOf(',', openingParenthesis + 1);
        if (openingParenthesis >= 0) {
          instance.setSelection({
            startLineNumber: position.lineNumber,
            startColumn: position.column + openingParenthesis + 1,
            endLineNumber: position.lineNumber,
            endColumn:
              position.column +
              (firstSeparator > openingParenthesis
                ? firstSeparator
                : snippet.length - 1),
          });
        }
        instance.focus();
      },
      focus() {
        editorRef.current?.focus();
      },
    }),
    [onChange, source],
  );

  return (
    <div className="formula-monaco-shell">
      <Editor
        height="100%"
        language={TDX_LANGUAGE_ID}
        theme="vs-dark"
        value={source}
        beforeMount={beforeMount}
        onMount={onMount}
        onChange={(value) => onChange(value ?? '')}
        options={{
          ariaLabel: '通达信公式代码',
          automaticLayout: true,
          bracketPairColorization: { enabled: true },
          cursorBlinking: 'smooth',
          fontFamily:
            'JetBrains Mono, SFMono-Regular, Menlo, Consolas, monospace',
          fontLigatures: true,
          fontSize: 13,
          formatOnPaste: false,
          glyphMargin: true,
          lineHeight: 22,
          minimap: { enabled: false },
          padding: { top: 12, bottom: 12 },
          quickSuggestions: { other: true, comments: false, strings: false },
          renderLineHighlight: 'all',
          scrollBeyondLastLine: false,
          suggest: { showWords: false },
          tabSize: 2,
          wordWrap: 'on',
        }}
      />
    </div>
  );
});
