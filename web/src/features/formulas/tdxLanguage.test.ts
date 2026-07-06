import {
  completionItems,
  createTdxLanguageDefinition,
  diagnosticMarkers,
  type TdxDocumentationEntry,
} from './tdxLanguage';

const entries: readonly TdxDocumentationEntry[] = [
  {
    name: 'EMA',
    signature: 'EMA(系列, 周期)',
    summary: '指数移动平均',
    details: '只使用当前和历史数据。',
    kind: 'function',
  },
  {
    name: 'CLOSE',
    signature: 'CLOSE',
    summary: '收盘价',
    details: '标准收盘价序列。',
    kind: 'field',
  },
];

it('defines TDX tokens and completion snippets from server metadata', () => {
  const definition = createTdxLanguageDefinition(entries);
  const items = completionItems(entries);

  expect(definition.monarchTokensProvider.keywords).toContain('EMA');
  const ema = items.find((item) => item.label === 'EMA');
  expect(ema?.insertText).toBe('EMA(${1:系列}, ${2:周期})');
  expect(ema?.documentation).toContain('指数移动平均');
});

it('classifies commas as delimiters instead of unconfigured brackets', () => {
  const definition = createTdxLanguageDefinition(entries);
  const rootRules = definition.monarchTokensProvider.tokenizer
    .root as readonly unknown[];
  const commaRule = rootRules.find(
    (rule: unknown) =>
      Array.isArray(rule) && rule[0] instanceof RegExp && rule[0].test(','),
  );

  expect(Array.isArray(commaRule) ? commaRule[1] : undefined).toBe('delimiter');
});

it('maps one-based backend spans to Monaco markers without widening them', () => {
  expect(
    diagnosticMarkers([
      {
        code: 'unsupported_function',
        functionName: 'UNKNOWN',
        explanation: '不支持函数 UNKNOWN',
        span: { line: 2, column: 4, endLine: 2, endColumn: 11 },
        blocksPreview: true,
        blocksSave: true,
        blocksBacktest: true,
      },
    ]),
  ).toEqual([
    expect.objectContaining({
      startLineNumber: 2,
      startColumn: 4,
      endLineNumber: 2,
      endColumn: 11,
      message: '不支持函数 UNKNOWN',
    }),
  ]);
});
